#!/usr/bin/env python3
"""Read-only/mainnet ABI and installed-library probe. Never signs or broadcasts."""
import argparse
import asyncio
from importlib.metadata import version
from pathlib import Path

import aiohttp
import peak_tracker as peak
from execution_peak_routes import PeakExecutionRoutes
from wallet_transactions import eip1559FeeFields
from swap_adapters import (SwapAdapters, buildV3, buildV4, abiTools, calldata,
                           V3_ROUTER, V4_ROUTER, PONS_FACTORY, PERMIT2,
                           NATIVE, WETH, USDG)


def offline():
    from eth_account import Account
    encode,decode,keccak=abiTools()
    dummy=Account.from_key(bytes.fromhex('11'*32))
    sample=dict(to='0x'+'2'*40,value=1,gas=21000,gasPrice=1,nonce=0,chainId=4663)
    signed=dummy.sign_transaction(sample)
    if not isinstance(signed.raw_transaction,bytes):
        raise RuntimeError('Unexpected eth-account signed API')
    if len(encode(['uint256'],[99]))!=32 or decode(['uint256'],encode(['uint256'],[99]))[0]!=99:
        raise RuntimeError('eth-abi encode/decode mismatch')
    if len(keccak(text='execute(bytes,bytes[],uint256)'))!=32:
        raise RuntimeError('eth-utils keccak mismatch')
    print('DEPENDENCIES', 'eth-account',version('eth-account'), 'eth-abi',version('eth-abi'),
          'eth-utils',version('eth-utils'),flush=True)
    print('PASS offline ABI and LocalAccount synchronous signing',flush=True)


async def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env',type=Path,default=Path.home()/'blackswanRH_guarded/.env')
    p.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    p.add_argument('--deployment-only',action='store_true',help='Verify deployment and wallet read access, without fixed-value swap simulations')
    p.add_argument('--exits-only',action='store_true',help='Do not require entry-only factory verification')
    args=p.parse_args()
    if args.exits_only and not args.deployment_only:p.error('--exits-only requires --deployment-only')
    offline()
    url,wallet=peak.loadSettings(args.env,None,args.wallet_file)
    async with aiohttp.ClientSession() as session:
        rpc=peak.Rpc(session,url,wallet)
        if int(await rpc.call('eth_chainId',[]),16)!=4663:
            raise RuntimeError('Wrong RPC chain')
        block=await rpc.call('eth_getBlockByNumber',['latest',False])
        suggested=await rpc.call('eth_maxPriorityFeePerGas',[])
        fees=eip1559FeeFields(block,suggested)
        from eth_account import Account
        dummy=Account.from_key(bytes.fromhex('11'*32))
        signed=dummy.sign_transaction(dict(
            chainId=4663,nonce=0,to='0x0000000000000000000000000000000000000001',
            value=1,data='0x',gas=21000,**fees))
        if not signed.raw_transaction.startswith(bytes([2])):
            raise RuntimeError('Installed SDK failed to encode type-2 fee envelope')
        print('PASS live EIP-1559 baseFee',int(block['baseFeePerGas'],16),
              'priority',fees['maxPriorityFeePerGas'],
              'maxFee',fees['maxFeePerGas'],
              'type-2 signing (offline only)',flush=True)
        for label,addr in ([('v3 SwapRouter02',V3_ROUTER),('v4 Universal Router 2.1.2',V4_ROUTER),
                           ('Permit2',PERMIT2),('WETH',WETH),('USDG',USDG),
                           ('v3 quoter',peak.V3_QUOTER),('v4 quoter',peak.V4_QUOTER)]+([] if args.exits_only else [('Pons factory',PONS_FACTORY)])):
            code=await rpc.call('eth_getCode',[addr,'latest'])
            if len(code)<10:raise RuntimeError('Missing live contract code: '+label)
            print('PASS code',label,addr,flush=True)
        # Verify the factory origin lookup against a known non-Pons token.
        # The execution engine refuses buys if this query/decode cannot work.
        if not args.exits_only:
            types=['address','address','address','address','address','uint256','uint24',
                   'int24','uint16','bool','uint8','uint256','uint256','uint256','bool']
            _,decode,_=abiTools()
            launch=await rpc.ethCall(PONS_FACTORY,calldata('getLaunchedToken(address)',
                                             ['address'],[WETH]),'latest')
            record=decode(types,bytes.fromhex(launch[2:]))
            if len(record)!=15 or bool(record[14]):
                raise RuntimeError('Pons V2 origin check failed for WETH')
            print('PASS Pons V2 factory origin check on known non-Pons WETH',flush=True)
        if args.deployment_only:
            native=int(await rpc.call('eth_getBalance',[wallet,'latest']),16)
            wrapped=int(await rpc.ethCall(WETH,calldata('balanceOf(address)',['address'],[wallet]),'latest'),16)
            print('WALLET BALANCES wei ETH',native,'WETH',wrapped,flush=True)
            print('PASS deployment and wallet reads; route simulation required per transaction',flush=True)
            return
        amount=10**13  # 0.00001 ETH, simulation only
        q=peak.Quotes(rpc)
        block=await rpc.block()
        passed=set()
        for venue in peak.USDG_BRIDGES:
            # Selection remains v4; v3 and v4 can both execute swaps.
            # Require real quotes and exact calldata simulation for both.
            try:
                if venue['source']=='uniswapV3':
                    quote=await q.quote(venue,WETH,amount,block)
                    tx=buildV3(venue,NATIVE,amount,max(1,quote['out']*8//10),wallet)
                else:
                    quote=await q.quote(venue,NATIVE,amount,block)
                    tx=buildV4(venue,NATIVE,amount,max(1,quote['out']*8//10),wallet)
                await rpc.call('eth_call',[{'from':wallet,'to':tx['to'],
                                           'data':tx['data'],'value':hex(tx['value'])},'latest'])
                print('PASS quote + exact-calldata simulation',venue['source'],flush=True)
                passed.add(venue['source'])
            except (RuntimeError,ValueError) as exc:
                # A route without liquidity is not proof that a router ABI is wrong.
                print('NOT VERIFIED',venue['source'],str(exc)[:200],flush=True)
        if not {'uniswapV3','uniswapV4'}.issubset(passed):
            raise RuntimeError('V3_V4_ROUTER_PREFLIGHT_NOT_VALIDATED')
        # Exercise the exact NEW bridge end-to-end with original
        # Tracker.best, same-block reverse quote, existing transaction
        # builder, and RPC eth_call. USDG is only a known public test pool,
        # not an eligible campaign token and never bought here.
        class KnownPoolStore:
            def venues(self,token):
                return list(peak.USDG_BRIDGES) if token==USDG else []

        parityRoutes=PeakExecutionRoutes(rpc,None,KnownPoolStore())
        candidate=(await parityRoutes.verifiedBuyCandidates(USDG,amount))[0]
        built=parityRoutes.build(candidate,amount,wallet)
        await rpc.call('eth_call',[{'from':wallet,'to':built['to'],
                                  'data':built['data'],'value':hex(built['value'])},
                                  candidate['block']])
        print('PASS original Tracker.best -> peak route translation -> '
              'same-block reverse check -> exact-calldata eth_call',
              candidate['peakRoute'],flush=True)
        print('Probe complete. No transactions were signed or sent.',flush=True)


if __name__=='__main__':asyncio.run(main())