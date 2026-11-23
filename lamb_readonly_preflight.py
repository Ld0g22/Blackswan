#!/usr/bin/env python3
"""Read-only preflight of the SAME v3/v4 venue-watcher RPC APIs; never signs."""
import argparse
import asyncio
import json
from pathlib import Path

import aiohttp
import detector
import peak_tracker as peak


async def verify(env,walletFile):
    cfg=detector.loadEnv(env)
    httpUrl,wallet=peak.loadSettings(env,None,walletFile)
    specs=[detector.SOURCES[x] for x in ('uniswapV3','uniswapV4')]
    async with aiohttp.ClientSession() as session:
        rpc=peak.Rpc(session,httpUrl,wallet)
        chain=int(await rpc.call('eth_chainId',[]),16)
        if chain!=4663:raise RuntimeError('WRONG_CHAIN')
        async with session.ws_connect(cfg['ROBINHOOD_ALCHEMY_WSS_URL'],
                                     heartbeat=20,max_msg_size=2_000_000) as ws:
            pending={i+1:spec for i,spec in enumerate(specs)}
            for idx,spec in pending.items():
                await ws.send_json(dict(jsonrpc='2.0',id=idx,method='eth_subscribe',
                    params=['logs',dict(address=spec['address'],topics=[spec['topic']])]))
            while pending:
                msg=await ws.receive(timeout=15)
                if msg.type!=aiohttp.WSMsgType.TEXT:
                    raise RuntimeError('WS_SUBSCRIPTION_NOT_CONFIRMED')
                payload=json.loads(msg.data)
                if payload.get('id') in pending:
                    if not isinstance(payload.get('result'),str):
                        raise RuntimeError('WS_SUBSCRIPTION_REJECTED')
                    pending.pop(payload['id'])
                elif payload.get('method')=='eth_subscription':
                    raw=payload.get('params',{}).get('result')
                    if isinstance(raw,dict):
                        # Only decode logs from known address+topic pairs.
                        if any(raw.get('address','').lower()==spec['address'] and
                               raw.get('topics',[None])[0]==spec['topic']
                               for spec in specs):
                            detector.decodeLog(raw,recovered=True)
            head=int(await rpc.block(),16)
            filt=dict(fromBlock=hex(max(0,head-2)),toBlock=hex(head),
                address=[s['address'] for s in specs],
                topics=[[s['topic'] for s in specs]])
            logs=await rpc.call('eth_getLogs',[filt])
            if not isinstance(logs,list):
                raise RuntimeError('BAD_GETLOGS_RESPONSE')
            for raw in logs:
                if any(raw.get('address','').lower()==spec['address'] and
                       raw.get('topics',[None])[0]==spec['topic']
                       for spec in specs):
                    detector.decodeLog(raw,recovered=True)
        print('READ-ONLY LAMB PREFLIGHT OK | chain 4663 | v3/v4 WS confirmed | getLogs batch accepted |',len(logs),'logs',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env',type=Path,default=Path.home()/'blackswanRH_guarded/.env')
    p.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    args=p.parse_args()
    asyncio.run(verify(args.env,args.wallet_file))


if __name__=='__main__':main()
