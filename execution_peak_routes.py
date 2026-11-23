"""Execution bridge around the UNCHANGED prospective peak tracker.

The original peak_tracker.Tracker.best() chooses all entry/sell quotes.
This module ONLY turns its selected route into an existing real swap
transaction and checks a full-size reverse quote before buying.

Unsupported mixed v3/v4 multihop routes are NEVER guessed or silently
replaced with a worse route. Pons curve and graduation-hook execution
remain excluded by the already-locked entry policy.
"""
from types import SimpleNamespace

import peak_tracker as peak
from swap_adapters import SwapAdapters, NATIVE, WETH, USDG, PONS_HOOK, counterpart


class _BlockRpc:
    """Use precisely the same chain block for forward and return quotes."""
    def __init__(self, block):
        self.pinnedBlock=block

    async def block(self):
        return self.pinnedBlock


class _EligibleVenues:
    def __init__(self,store,allowed):
        self.store=store
        self.allowed=allowed

    def venues(self,token):
        return [v for v in self.store.venues(token)
                if v['source'] in self.allowed
                and not (v['source']=='uniswapV4'
                         and str(v.get('hooks','')).lower()==PONS_HOOK)]


class PeakExecutionRoutes:
    """Use original prospective quote selection; delegate calldata only.

    Public methods match the existing executor's SwapAdapters surface.
    """
    def __init__(self,rpc,fx,store):
        self.rpc=rpc
        self.store=store
        self.execution=SwapAdapters(rpc,fx,store)
        self.originalQuoter=peak.Quotes(rpc)
        self.allowedExecutionSources=frozenset(('uniswapV3','uniswapV4'))

    async def _best(self,token,amount,buy,block=None):
        if amount<=0:
            raise ValueError('ZERO_QUOTE_AMOUNT')
        if block is None:
            block=await self.rpc.block()
        # No alternate price calculations. Invoke the original method unchanged.
        original=SimpleNamespace(
            rpc=_BlockRpc(block),
            store=_EligibleVenues(self.store,self.allowedExecutionSources),
            quotes=self.originalQuoter,exporter=None)
        result=await peak.Tracker.best(original,token,amount,buy)
        return result

    def _asExecutable(self,token,raw,buy):
        route=raw['venue']
        routeParts=route.split('>')
        known={venue['id']:venue for venue in
               _EligibleVenues(self.store,self.allowedExecutionSources).venues(token)}
        bridges={b['id']:b for b in peak.USDG_BRIDGES}
        path=None
        if len(routeParts)==1:
            if routeParts[0] not in known:
                raise RuntimeError('UNKNOWN_PEAK_POOL:'+routeParts[0][:65])
            venue=known[routeParts[0]]
            base=counterpart(token,venue)
            if base not in (NATIVE,WETH):
                raise RuntimeError('PEAK_ROUTE_REQUIRES_ATOMIC_BRIDGE')
            tokenIn=(NATIVE if buy and base==WETH and venue['source']=='uniswapV3'
                     else base if buy else token)
        elif len(routeParts)==2:
            if buy:
                bridge=bridges.get(routeParts[0])
                venue=known.get(routeParts[1])
            else:
                venue=known.get(routeParts[0])
                bridge=bridges.get(routeParts[1])
            if venue is None or bridge is None or counterpart(token,venue)!=USDG:
                raise RuntimeError('UNKNOWN_PEAK_BRIDGE_ROUTE:'+route[:100])
            if venue['source']!=bridge['source']:
                # Original peak tracker can price this by two independent
                # eth_calls; no verified atomic v3+v4 router path exists.
                raise RuntimeError('PEAK_ROUTE_NOT_ATOMIC_MIXED_PROTOCOL:'+route[:100])
            if venue['source']=='uniswapV3':
                if buy:
                    path=[(WETH,bridge['fee']),(USDG,venue['fee']),(token,None)]
                    tokenIn=NATIVE
                else:
                    path=[(token,venue['fee']),(USDG,bridge['fee']),(WETH,None)]
                    tokenIn=token
            elif venue['source']=='uniswapV4':
                if buy:
                    path=[(USDG,bridge['fee'],bridge['spacing'],bridge['hooks'],b''),
                          (token,venue['fee'],venue['spacing'],venue['hooks'],b'')]
                    tokenIn=NATIVE
                else:
                    path=[(USDG,venue['fee'],venue['spacing'],venue['hooks'],b''),
                          (NATIVE,bridge['fee'],bridge['spacing'],bridge['hooks'],b'')]
                    tokenIn=token
            else:
                raise RuntimeError('UNSUPPORTED_PEAK_SOURCE')
        else:
            raise RuntimeError('PEAK_ROUTE_TOO_MANY_HOPS:'+route[:100])
        return dict(venue=venue,output=int(raw['out']),
                    spent=int(raw['spent']),tokenIn=tokenIn,block=raw['block'],
                    path=path,basis=raw['basis'],peakRoute=route,
                    routesChecked=int(raw['routesChecked']))

    async def directQuotes(self,token,amount,buy,block=None):
        result=await self._best(token,amount,buy,block)
        # Returning one quote is intentional: it is the ORIGINAL tracker best,
        # not a separately implemented or separately ranked route collection.
        return [self._asExecutable(token,result,buy)]

    async def verifiedBuyCandidates(self,token,amount):
        buy=(await self.directQuotes(token,amount,True))[0]
        quantity=buy['output']
        if quantity<=0:
            raise ValueError('ZERO_PROJECTED_BUY')
        # Same pinned block, same original peak-tracker best() method.
        try:
            sell=(await self.directQuotes(token,quantity,False,buy['block']))[0]
        except (ValueError,RuntimeError) as exc:
            raise RuntimeError('NO_TWO_WAY_PEAK_ROUTE:'+str(exc)[:150]) from None
        if sell['output']<=0:
            raise RuntimeError('ZERO_PROJECTED_SELL')
        # Both calldata paths must be representable before real signing.
        self.build(buy,amount,self.rpc.wallet)
        self.build(sell,quantity,self.rpc.wallet)
        buy['preBuySellOutput']=sell['output']
        buy['preBuySellBlock']=buy['block']
        buy['preBuySellVenue']=sell['peakRoute']
        buy['preBuySellSource']=sell['venue']['source']
        return [buy]

    def build(self,quote,amountIn,wallet,minimumOut=None):
        if minimumOut is None:
            return self.execution.build(quote,amountIn,wallet)
        return self.execution.build(quote,amountIn,wallet,minimumOut=minimumOut)

    async def approvalNeeded(self,quote,token,wallet,amount):
        return await self.execution.approvalNeeded(quote,token,wallet,amount)

    def buildApproval(self,approval):
        return self.execution.buildApproval(approval)

    async def refreshPons(self,token,force=False):
        return await self.execution.refreshPons(token,force=force)
