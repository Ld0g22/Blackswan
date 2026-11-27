"""Verified direct-path Robinhood swaps. No fabricated or partial-chain routes.

This first implementation supports native/WETH Pons V2, Uniswap v3 and v4 pools.
USDG paths are deliberately fail-closed until an atomic conversion path is tested.
"""
from decimal import Decimal
import time

import peak_tracker as peak

CHAIN_ID=4663
NATIVE=peak.NATIVE
WETH=peak.WETH
USDG=peak.USDG
PONS_FACTORY='0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e'
PONS_HOOK='0xe5e702641ea86f4ae6cc3cdaed2b886f976be044'
V3_ROUTER='0xcaf681a66d020601342297493863e78c959e5cb2'
V4_ROUTER='0x204faca1764b154221e35c0d20abb3c525710498'  # official Universal Router 2.1.2
PERMIT2='0x000000000022d473030f116ddee9f6b43ac78ba3'
ROUTER_RECIPIENT='0x0000000000000000000000000000000000000002'
SLIPPAGE_BPS=2000  # Entry bound, unchanged.
SELL_SLIPPAGE_BPS=3400  # User accepts 3x -> 2x price retreat after a sale trigger.


def abiTools():
    try:
        from eth_abi import encode, decode
        from eth_utils import keccak
    except ImportError as exc:
        raise RuntimeError('Install requirements_live.txt in project .venv') from exc
    return encode,decode,keccak


def calldata(signature, types, values):
    encode,_,keccak=abiTools()
    return '0x'+(keccak(text=signature)[:4]+encode(types,values)).hex()


def minimumOut(quoted):
    if int(quoted)<=0:
        raise ValueError('No usable positive quote')
    return max(1,int(quoted)*(10000-SLIPPAGE_BPS)//10000)


def counterpart(token,venue):
    t0=venue['token0']; t1=venue['token1']
    if token==t0:return t1
    if token==t1:return t0
    raise ValueError('Token absent from route')


def buildV4(venue, tokenIn, amountIn, minOut, wallet, path=None):
    """Uniswap Universal Router 2.1.2 ABI. WETH entry wraps native ETH.

    The 'SETTLE' (not 'SETTLE_ALL') action with payerIsUser=false consumes WETH
    previously wrapped into the router during the same transaction.
    """
    encode,_,_=abiTools()
    currency0,currency1=venue['token0'],venue['token1']
    if currency0.lower()>=currency1.lower():
        raise ValueError('Invalid v4 PoolKey currency order')
    if not 0<amountIn<2**128 or not 0<minOut<2**128:
        raise ValueError('v4 uint128 size exceeded')
    output=path[-1][0] if path else counterpart(tokenIn,venue)
    key=(currency0,currency1,int(venue['fee']),int(venue['spacing']),venue['hooks'])
    # Universal Router 2.1.2 uses V4 periphery 2.1.1's minHopPriceX36 field.
    # Encode the entire *struct as one tuple* (dynamic bytes requires outer offset).
    if path:
        single=encode(['(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)'],
                      [(tokenIn,path,[],amountIn,minOut)])
        swapAction=0x07  # SWAP_EXACT_IN (multi-hop)
        output=path[-1][0]
    else:
        single=encode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                      [(key,tokenIn==currency0,amountIn,minOut,0,b'')])
        swapAction=0x06  # SWAP_EXACT_IN_SINGLE
    if tokenIn==WETH:
        actions=bytes([swapAction,0x0b,0x0f])
        params=[single,encode(['address','uint256','bool'],[WETH,amountIn,False]),
                encode(['address','uint256'],[output,minOut])]
        commands=bytes([0x0b,0x10])
        inputs=[encode(['address','uint256'],[ROUTER_RECIPIENT,amountIn]),
                encode(['bytes','bytes[]'],[actions,params])]
        value=amountIn
    else:
        actions=bytes([swapAction,0x0c,0x0f])
        params=[single,encode(['address','uint256'],[tokenIn,amountIn]),
                encode(['address','uint256'],[output,minOut])]
        commands=bytes([0x10])
        inputs=[encode(['bytes','bytes[]'],[actions,params])]
        value=amountIn if tokenIn==NATIVE else 0
    data=calldata('execute(bytes,bytes[],uint256)',
                  ['bytes','bytes[]','uint256'],[commands,inputs,int(time.time())+120])
    return {'to':V4_ROUTER,'data':data,'value':value}


def buildV3(venue, tokenIn, amountIn, minOut, wallet, path=None):
    """SwapRouter02 single-hop or atomic WETH/USDG/token exact-input routing.

    ETH-funded WETH input uses router msg.value; ERC20 token sales return WETH.
    The canonical V3 encoded path is token(20) | fee(3) | token(20) ...
    """
    inputCurrency=WETH if tokenIn==NATIVE else tokenIn
    if path:
        if len(path)<3 or path[0][0]!=inputCurrency or not 0<amountIn:
            raise ValueError('INVALID_V3_ATOMIC_PATH')
        raw=bytes.fromhex(path[0][0][2:])
        for (_,fee),(nextToken,_) in zip(path,path[1:]):
            if not 0<=int(fee)<2**24:
                raise ValueError('INVALID_V3_PATH_FEE')
            raw+=int(fee).to_bytes(3,'big')+bytes.fromhex(nextToken[2:])
        data=calldata('exactInput((bytes,address,uint256,uint256))',
                      ['(bytes,address,uint256,uint256)'],
                      [(raw,wallet,amountIn,minOut)])
    else:
        outputCurrency=counterpart(inputCurrency,venue)
        data=calldata('exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))',
                      ['(address,address,uint24,address,uint256,uint256,uint160)'],
                      [(inputCurrency,outputCurrency,int(venue['fee']),wallet,amountIn,minOut,0)])
    return {'to':V3_ROUTER,'data':data,'value':amountIn if tokenIn==NATIVE else 0}


def boundEntryTransaction(transaction,deadlineAt):
    """Preserve all swap bytes/amounts; give only new entries a chain expiry.
    SwapRouter02's deadline multicall delegates to the original exact input.
    Unknown router or selector fails closed; sales use the original builder.
    """
    encode,decode,keccak=abiTools()
    deadline=int(deadlineAt)
    tx=dict(transaction)
    data=bytes.fromhex(tx['data'][2:])
    if tx['to'].lower()==V4_ROUTER:
        signature='execute(bytes,bytes[],uint256)'
        if data[:4]!=keccak(text=signature)[:4]:
            raise ValueError('INVALID_V4_ENTRY_SELECTOR')
        commands,inputs,_=decode(['bytes','bytes[]','uint256'],data[4:])
        tx['data']=calldata(signature,['bytes','bytes[]','uint256'],
                            [commands,inputs,deadline])
    elif tx['to'].lower()==V3_ROUTER:
        selectors=[keccak(text=s)[:4] for s in
            ('exactInput((bytes,address,uint256,uint256))',
             'exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))')]
        if data[:4] not in selectors:raise ValueError('INVALID_V3_ENTRY_SELECTOR')
        tx['data']=calldata('multicall(uint256,bytes[])',['uint256','bytes[]'],[deadline,[data]])
    else:
        raise ValueError('UNSUPPORTED_DEADLINE_ENTRY_ROUTER')
    return tx


def buildPons(venue, tokenIn, amountIn, minOut, wallet):
    if counterpart(venue['token0'],venue)!=NATIVE:
        raise ValueError('PONS_NON_NATIVE_QUOTE_REQUIRES_CONVERSION')
    curve=venue.get('emitter',venue['id'])
    if tokenIn==NATIVE:
        data=calldata('buy(uint256,uint256,address)',
                      ['uint256','uint256','address'],[amountIn,minOut,wallet])
        return {'to':curve,'data':data,'value':amountIn}
    data=calldata('sell(uint256,uint256,address)',
                  ['uint256','uint256','address'],[amountIn,minOut,wallet])
    return {'to':curve,'data':data,'value':0}


class SwapAdapters:
    def __init__(self,rpc,fx,store):
        self.rpc,self.fx,self.store=rpc,fx,store
        self.quoting=peak.Quotes(rpc)
        self.lastPonsRefresh={}

    async def directQuotes(self,token,amount,buy,block=None):
        """Quote both verified v3 and v4 routes, but never Pons curve execution.

        Passing a block keeps buy/return-path proof at one exact chain state.
        """
        block=block or await self.rpc.block()
        quotes=[]; errors=[]
        for venue in self.store.venues(token):
            source=venue['source']
            allowed=getattr(self,'allowedExecutionSources',None)
            if allowed is not None and source not in allowed:
                continue
            if getattr(self,'executionOnlyV4',False) and source!='uniswapV4':
                continue
            # Exclude a known Pons graduation route as well as Pons V2 curves.
            if source=='uniswapV4' and venue.get('hooks','').lower()==PONS_HOOK:
                continue
            base=counterpart(token,venue)
            if base==USDG and source in ('uniswapV3','uniswapV4'):
                bridge=next(v for v in peak.USDG_BRIDGES if v['source']==source)
                try:
                    if buy:
                        bridgeIn=WETH if source=='uniswapV3' else NATIVE
                        first=await self.quoting.quote(bridge,bridgeIn,amount,block)
                        second=await self.quoting.quote(venue,USDG,int(first['out']),block)
                        if source=='uniswapV3':
                            path=[(WETH,bridge['fee']),(USDG,venue['fee']),(token,None)]
                            basis='V3_TWO_HOP'
                        else:
                            path=[(USDG,bridge['fee'],bridge['spacing'],bridge['hooks'],b''),
                                  (token,venue['fee'],venue['spacing'],venue['hooks'],b'')]
                            basis='V4_TWO_HOP'
                        quotes.append(dict(venue=venue,output=int(second['out']),spent=amount,
                                           tokenIn=NATIVE,block=block,basis=basis,path=path))
                    else:
                        first=await self.quoting.quote(venue,token,amount,block)
                        second=await self.quoting.quote(bridge,USDG,int(first['out']),block)
                        if source=='uniswapV3':
                            path=[(token,venue['fee']),(USDG,bridge['fee']),(WETH,None)]
                            basis='V3_TWO_HOP'
                        else:
                            path=[(USDG,venue['fee'],venue['spacing'],venue['hooks'],b''),
                                  (NATIVE,bridge['fee'],bridge['spacing'],bridge['hooks'],b'')]
                            basis='V4_TWO_HOP'
                        quotes.append(dict(venue=venue,output=int(second['out']),spent=amount,
                                           tokenIn=token,block=block,basis=basis,path=path))
                except (RuntimeError,ValueError) as exc:
                    errors.append(source+'_TWO_HOP:'+str(exc)[:60])
                continue
            if base not in (NATIVE,WETH):
                errors.append('NO_VERIFIED_DIRECT_ETH_ROUTE:'+base)
                continue
            try:
                # QUOTER input is WETH for a v3 WETH pool. ETH is sent as
                # msg.value only when building the actual Router02 purchase.
                quoteTokenIn=base if buy else token
                executionTokenIn=(NATIVE if buy and base==WETH and source=='uniswapV3'
                                  else quoteTokenIn)
                result=await self.quoting.quote(venue,quoteTokenIn,amount,block)
                quotes.append(dict(venue=venue,output=int(result['out']),
                                   spent=int(result['spent']),tokenIn=executionTokenIn,block=block,
                                   basis=result['basis']))
            except (RuntimeError,ValueError) as exc:
                errors.append(source+':'+str(exc)[:90])
        if not quotes:
            raise RuntimeError('NO_ROUTE:'+';'.join(errors[:3]))
        return sorted(quotes,key=lambda r:r['output'],reverse=True)

    async def verifiedBuyCandidates(self,token,amount):
        """Require a full-quantity reverse route, possibly via another pool.

        Both buy and reverse quotes refer to the exact SAME block. A v4
        selection is eligible even if the best buy or eventual exit is v3.
        """
        candidates=await self.directQuotes(token,amount,True)
        viable=[];failed=[]
        for quote in candidates:
            projected=int(quote['output'])
            try:
                if projected<=0:
                    raise ValueError('Projected purchase has zero tokens')
                reverse=await self.directQuotes(token,projected,False,block=quote['block'])
                bestReverse=reverse[0]
                output=int(bestReverse['output'])
                if output<=0:
                    raise ValueError('Reverse sell returned zero')
                quote['preBuySellOutput']=output
                quote['preBuySellBlock']=quote['block']
                quote['preBuySellVenue']=bestReverse['venue']['id']
                quote['preBuySellSource']=bestReverse['venue']['source']
                viable.append(quote)
            except (RuntimeError,ValueError) as exc:
                failed.append(str(quote['venue'].get('source','unknown'))+':'+str(exc)[:110])
        if not viable:
            raise RuntimeError('NO_TWO_WAY_EXIT_QUOTE:'+(';'.join(failed[:3]) or 'no usable route'))
        return viable

    def build(self,quote,amountIn,wallet,minimumOut=None):
        venue=quote['venue']; out=quote['output']
        # Preserve the automatic entry/exit bounds. Only explicitly requested
        # manual exits supply a stricter on-chain minimum output.
        slippage=SLIPPAGE_BPS if quote['tokenIn'] in (NATIVE,WETH,USDG) else SELL_SLIPPAGE_BPS
        minimum=max(1,int(out)*(10000-slippage)//10000)
        if minimumOut is not None:
            if type(minimumOut) is not int or minimumOut<=0 or minimumOut>int(out):
                raise ValueError('INVALID_MANUAL_MINIMUM_OUTPUT')
            if quote['tokenIn'] in (NATIVE,WETH,USDG):
                raise ValueError('MANUAL_MINIMUM_REQUIRES_SELL')
            minimum=minimumOut
        if venue['source']=='ponsV2':
            return buildPons(venue,quote['tokenIn'],amountIn,minimum,wallet)
        if venue['source']=='uniswapV3':
            return buildV3(venue,quote['tokenIn'],amountIn,minimum,wallet,quote.get('path'))
        if venue['source']=='uniswapV4':
            return buildV4(venue,quote['tokenIn'],amountIn,minimum,wallet,quote.get('path'))
        raise ValueError('Unsupported venue')

    def saleSpend(self,quote,token):
        source=quote['venue']['source']
        if source=='uniswapV4':
            return [(PERMIT2,token),(V4_ROUTER,token)]
        return [(quote['venue'].get('emitter',quote['venue']['id']) if source=='ponsV2' else V3_ROUTER,token)]

    async def allowance(self,token,owner,spender):
        result=await self.rpc.ethCall(token,calldata('allowance(address,address)',
                     ['address','address'],[owner,spender]),'latest')
        return int(result,16)

    async def permitAllowance(self,token,owner,spender):
        _,decode,_=abiTools()
        result=await self.rpc.ethCall(PERMIT2,calldata('allowance(address,address,address)',
                     ['address','address','address'],[owner,token,spender]),'latest')
        return decode(['uint160','uint48','uint48'],bytes.fromhex(result[2:]))

    async def approvalNeeded(self,quote,token,wallet,amount):
        source=quote['venue']['source']
        if source!='uniswapV4':
            spender=quote['venue'].get('emitter',quote['venue']['id']) if source=='ponsV2' else V3_ROUTER
            if await self.allowance(token,wallet,spender)>=amount:return None
            return ('ERC20',token,spender,amount)
        if await self.allowance(token,wallet,PERMIT2)<amount:
            return ('ERC20',token,PERMIT2,amount)
        permitted,expiration,_=await self.permitAllowance(token,wallet,V4_ROUTER)
        if permitted>=amount and expiration>time.time()+300:return None
        return ('PERMIT2',token,V4_ROUTER,min(amount,2**160-1))

    def buildApproval(self,approval):
        kind,token,spender,amount=approval
        if kind=='ERC20':
            return {'to':token,'value':0,'data':calldata('approve(address,uint256)',
                    ['address','uint256'],[spender,amount])}
        return {'to':PERMIT2,'value':0,'data':calldata('approve(address,address,uint160,uint48)',
                    ['address','address','uint160','uint48'],
                    [token,spender,amount,int(time.time())+7*86400])}

    async def refreshPons(self,token,force=False):
        if not force and time.time()-self.lastPonsRefresh.get(token,0)<60:
            return
        self.lastPonsRefresh[token]=time.time()
        if not any(v['source']=='ponsV2' for v in self.store.venues(token,includeInactive=True)):
            return
        _,decode,keccak=abiTools()
        output=await self.rpc.ethCall(PONS_FACTORY,calldata('getLaunchedToken(address)',
                         ['address'],[token]),'latest')
        types=['address','address','address','address','address','uint256','uint24','int24',
               'uint16','bool','uint8','uint256','uint256','uint256','bool']
        fields=decode(types,bytes.fromhex(output[2:]))
        phase=int(fields[10]); pair=str(fields[4]).lower()
        if phase not in (1,2,3):
            return
        for venue in self.store.venues(token,includeInactive=True):
            if venue['source']=='ponsV2':
                self.store.setVenue(token,venue,False)
        if phase!=2:
            return  # Swept: neither curve nor v4 safe until pool is confirmed.
        t0,t1=sorted((token,pair))
        poolFee=int(fields[6]);spacing=int(fields[7])
        encode,_,keccak=abiTools()
        poolId='0x'+keccak(encode(['address','address','uint24','int24','address'],
                                   [t0,t1,poolFee,spacing,PONS_HOOK])).hex()
        venue=dict(id=poolId,source='uniswapV4',token0=t0,token1=t1,
                   fee=poolFee,spacing=spacing,hooks=PONS_HOOK)
        self.store.setVenue(token,venue,True)