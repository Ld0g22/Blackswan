#!/usr/bin/env python3
"""Read-only narrow follow-up for the dual audit; no campaign database writes."""
import argparse
import gzip
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from eth_utils import keccak

MANAGER='0x8366a39cc670b4001a1121b8f6a443a643e40951'
QUOTER='0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
TOPIC='0x'+keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()

def number(x):
    return int(x,16) if isinstance(x,str) and x.startswith('0x') else int(x)

def word(x):
    return f'{x%(1<<256):064x}'

class RpcError(RuntimeError):
    pass

class Rpc:
    def __init__(self,url):
        self.url=url; self.calls=0; self.headers={}
    def call(self,method,params):
        if self.calls>=180: raise RpcError('REQUEST_BUDGET_180')
        self.calls+=1
        time.sleep(.12)
        body=json.dumps(dict(jsonrpc='2.0',id=self.calls,method=method,params=params)).encode()
        try:
            with urlopen(Request(self.url,body,{'Content-Type':'application/json'}),timeout=12) as response:
                obj=json.load(response)
        except Exception as e:
            raise RpcError('HTTP_OR_TRANSPORT_'+str(getattr(e,'code',type(e).__name__))) from None
        if 'error' in obj:
            err=obj['error']
            raise RpcError('RPC_'+str(err.get('code'))+' '+str(err.get('message','')).replace(self.url,'[redacted]')[:180])
        return obj['result']
    def header(self,b):
        if b not in self.headers:
            h=self.call('eth_getBlockByNumber',[hex(b),False])
            self.headers[b]={k:h[k] for k in ('number','hash','timestamp')}
        return self.headers[b]

def scan(rpc,pool,start,end):
    events=[]; missing=[]; seen=set()
    def query(lo,hi):
        try:
            logs=rpc.call('eth_getLogs',[dict(address=MANAGER,topics=[TOPIC,pool],fromBlock=hex(lo),toBlock=hex(hi))])
            if not isinstance(logs,list): raise RpcError('NOT_LOG_LIST')
            if len(logs)>=10000: raise RpcError('RESULT_CAP')
            for log in logs:
                if log.get('removed'): raise RpcError('REMOVED_LOG')
                k=(log['blockHash'],log['transactionHash'],log['logIndex'])
                if k not in seen: seen.add(k); events.append(log)
        except RpcError as e:
            if lo<hi and any(s in str(e) for s in ('400','413','-32005','-32602','RESULT_CAP')):
                mid=(lo+hi)//2; query(lo,mid); query(mid+1,hi)
            else: missing.append([lo,hi,str(e)])
    for lo in range(start,end+1,8):
        query(lo,min(lo+7,end))
    return sorted(events,key=lambda l:tuple(number(l[k]) for k in ('blockNumber','transactionIndex','logIndex'))),missing

def decode(log):
    data=bytes.fromhex(log['data'][2:])
    if len(data)!=128: raise ValueError('BAD_MODIFY_LIQUIDITY_DATA')
    w=[data[i:i+32] for i in range(0,128,32)]
    signed=lambda x:int.from_bytes(x,'big',signed=True)
    return dict(block=number(log['blockNumber']),index=number(log['transactionIndex']),
                logIndex=number(log['logIndex']),hash=log['transactionHash'],
                poolId=log['topics'][1].lower(),sender='0x'+log['topics'][2][-40:],
                lower=signed(w[0]),upper=signed(w[1]),delta=str(signed(w[2])),
                salt='0x'+w[3].hex(),raw=log)

def quote(rpc,venue,token,qty,b):
    t0,t1=venue['token0'],venue['token1']
    params=[32,int(t0,16),int(t1,16),int(venue['fee']),int(venue['spacing']),
            int(venue['hooks'],16),int(token==t0),qty,256,0]
    out=rpc.call('eth_call',[dict(to=QUOTER,data='0xaa9d21cb'+''.join(map(word,params))),hex(b)])
    if len(out)<66: raise RpcError('INCOMPLETE_QUOTE')
    return str(int(out[2:66],16))

def inspect(rpc,c):
    obs=sorted([q for q in c['observations'] if q['phase']=='SELL'],key=lambda q:q['at'])
    last=max((q for q in obs if q['gbp'] is not None and q['block']),key=lambda q:q['at'])
    first=next(q for q in obs if q['at']>last['at'] and q['status']=='UNAVAILABLE')
    token=c['token']; start=number(last['block']); end=min(start+150,c['scan']['end'])
    result=dict(token=token,campaign=c['campaign'],lastSuccessful=last,firstFailure=first,scanStart=start,scanEnd=end,pools=[],errors=[])
    launches=json.loads(c['selection']['event'])['event']
    try:
        tx=rpc.call('eth_getTransactionByHash',[launches['transactionHash']])
        result['launchTransaction']={k:tx.get(k) for k in ('hash','from','to','blockNumber','transactionIndex')}
        result['launchReceipt']=rpc.call('eth_getTransactionReceipt',[launches['transactionHash']])
    except Exception as e: result['errors'].append('launch:'+str(e))
    for p in c['pools']:
        venue=p['venue']
        if not p['usedInPurchase'] or venue['source']!='uniswapV4':continue
        pool=venue['id']
        print('SCANNING',token,pool,start,end,flush=True)
        logs,missing=scan(rpc,pool,start,end)
        item=dict(venue=venue,missingRanges=missing,events=[],quotes=[])
        result['pools'].append(item)
        qty=int(c['positions'][0]['originalQty'])
        blocks={start,end}
        for raw in logs:
            try:
                e=decode(raw); item['events'].append(e)
                e['timestamp']=number(rpc.header(e['block'])['timestamp'])
                if int(e['delta'])<0:
                    blocks.update((max(start,e['block']-1),e['block']))
                    tx=rpc.call('eth_getTransactionByHash',[e['hash']])
                    e['transaction']={k:tx.get(k) for k in ('hash','from','to','blockNumber','transactionIndex')}
                    e['receipt']=rpc.call('eth_getTransactionReceipt',[e['hash']])
                    print('WITHDRAWAL',token,e['block'],e['timestamp'],e['delta'],e['hash'],flush=True)
            except Exception as e: result['errors'].append('event:'+str(e)[:200])
        for b in sorted(blocks):
            try:
                header=rpc.header(b)
                item['quotes'].append(dict(block=b,timestamp=number(header['timestamp']),rawOut=quote(rpc,venue,token,qty,b)))
            except Exception as e: item['quotes'].append(dict(block=b,error=str(e)[:200]))
        print('COVERAGE',pool,'events',len(item['events']),'missing ranges',len(missing),flush=True)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--input',type=Path,default=Path.home()/'Downloads/blackswan-dual-audit.json.gz')
    parser.add_argument('--output',type=Path,default=Path.home()/'Downloads/blackswan-liquidity-followup.json.gz')
    a=parser.parse_args()
    from detector import loadEnv
    rpc=Rpc(loadEnv(a.env.expanduser())['ROBINHOOD_ALCHEMY_HTTP_URL'])
    if number(rpc.call('eth_chainId',[]))!=4663: raise RuntimeError('WRONG_CHAIN')
    with gzip.open(a.input,'rt') as f: report=json.load(f)
    if report['chainId']!=4663: raise RuntimeError('WRONG_INPUT_CHAIN')
    follow=dict(chainId=4663,results=[],errors=[])
    for c in report['campaigns']:
        try: follow['results'].append(inspect(rpc,c))
        except Exception as e: follow['errors'].append(dict(token=c['token'],error=str(e)[:200]))
    follow['rpcCalls']=rpc.calls; follow['headers']=rpc.headers
    with gzip.open(a.output,'wt') as f:json.dump(follow,f,separators=(',',':'))
    print('AUDIT CREATED:',a.output,'| RPC calls',rpc.calls,flush=True)

if __name__=='__main__':main()
