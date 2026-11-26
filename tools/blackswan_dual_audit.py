#!/usr/bin/env python3
"""Read-only, bounded launch/entry/liquidity audit for two BlackSwan tokens."""
import argparse
import gzip
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
from eth_utils import keccak

TOKENS = ('0xb2deca30b178d2b8578b9cec24ce80670401bea5',
          '0x1320b7571b6b09027d2daf933d94d1583e483a7e')
RPC = 'https://rpc.mainnet.chain.robinhood.com'
MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
V4_QUOTER = '0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
V3_QUOTER = '0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'
NATIVE = '0x'+'0'*40
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
SIG = {name:'0x'+keccak(text=s).hex() for name,s in {
 'init4':'Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)',
 'liq4':'ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)',
 'swap4':'Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)',
 'init3':'Initialize(uint160,int24)',
 'mint3':'Mint(address,address,int24,int24,uint128,uint256,uint256)',
 'burn3':'Burn(address,int24,int24,uint128,uint256,uint256)',
 'swap3':'Swap(address,address,int256,int256,uint160,uint128,int24)',
 }.items()}
BY_SIG = {v:k for k,v in SIG.items()}

def num(x): return int(x,16) if isinstance(x,str) and x.startswith('0x') else int(x)
def words(s):
    b=bytes.fromhex(s[2:]); assert len(b)%32==0
    return [b[i:i+32] for i in range(0,len(b),32)]
def u(w): return int.from_bytes(w,'big')
def sn(w): return int.from_bytes(w,'big',signed=True)
def addr(w): return '0x'+w[-20:].hex()
def key(l): return tuple(num(l[x]) for x in ('blockNumber','transactionIndex','logIndex'))
def word(n): return f'{n%(1<<256):064x}'

class Rpc:
    def __init__(self): self.calls=0; self.blocks={}
    def call(self,method,params):
        self.calls+=1
        body=json.dumps(dict(jsonrpc='2.0',id=self.calls,method=method,params=params)).encode()
        with urlopen(Request(RPC,body,{'Content-Type':'application/json'}),timeout=15) as r:
            payload=json.load(r)
        if 'error' in payload: raise RuntimeError(str(payload['error'])[:350])
        return payload['result']
    def block(self,b):
        if b not in self.blocks:
            result=self.call('eth_getBlockByNumber',[hex(b),False])
            if result is None: raise RuntimeError('BLOCK_NOT_FOUND')
            self.blocks[b]={k:result[k] for k in ('number','hash','timestamp')}
        return self.blocks[b]
    def stamp(self,b): return num(self.block(b)['timestamp'])
    def logs(self,flt,start,end):
        """Bounded scan; failed ranges explicitly recorded, never silently skipped."""
        logs=[]; missing=[]; calls=0
        def scan(lo,hi):
            nonlocal calls
            if calls>=180:
                missing.append([lo,hi,'QUERY_BUDGET']); return
            calls+=1
            try:
                value=self.call('eth_getLogs',[dict(flt,fromBlock=hex(lo),toBlock=hex(hi))])
                if not isinstance(value,list): raise RuntimeError('NOT_LOG_LIST')
                if len(value)>=10000: raise RuntimeError('POSSIBLE_RESULT_CAP')
                logs.extend(value)
            except Exception as e:
                message=str(e)[:180]
                if hi-lo>31 and any(x in message.lower() for x in ('range','limit','too many','too large','413','result cap','10000')) and '429' not in message:
                    m=(lo+hi)//2; scan(lo,m); scan(m+1,hi)
                else: missing.append([lo,hi,message])
        for lo in range(start,end+1,500): scan(lo,min(end,lo+499))
        dedup={(l['blockHash'],l['transactionHash'],l['logIndex']):l for l in logs if not l.get('removed')}
        return sorted(dedup.values(),key=key),missing

def ro(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    db.row_factory=sqlite3.Row; return db

def rows(db,table,field,value,errors):
    try: return [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE {field}=?',(value,))]
    except sqlite3.Error as e: errors.append(f'{table}: {e}'); return []

def decode(l):
    kind=BY_SIG[l['topics'][0].lower()]; w=words(l['data']); t=l['topics']; b,i,j=key(l)
    d=dict(kind=kind,block=b,index=i,logIndex=j,hash=l['transactionHash'],blockHash=l['blockHash'])
    if kind=='init4': d.update(token0=addr(bytes.fromhex(t[2][2:])),token1=addr(bytes.fromhex(t[3][2:])),fee=u(w[0]),spacing=sn(w[1]),hooks=addr(w[2]),sqrtPriceX96=str(u(w[3])),tick=sn(w[4]))
    elif kind=='init3': d.update(sqrtPriceX96=str(u(w[0])),tick=sn(w[1]))
    elif kind=='liq4': d.update(sender=addr(bytes.fromhex(t[2][2:])),lower=sn(w[0]),upper=sn(w[1]),delta=str(sn(w[2])),salt='0x'+w[3].hex())
    elif kind in ('mint3','burn3'):
        d.update(sender=addr(bytes.fromhex(t[1][2:])),lower=sn(bytes.fromhex(t[2][2:])),upper=sn(bytes.fromhex(t[3][2:])),delta=str(u(w[1]) if kind=='mint3' else -u(w[0])))
    elif kind.startswith('swap'):
        d.update(amount0=str(sn(w[0])),amount1=str(sn(w[1])),sqrtPriceX96=str(u(w[2])),activeAfter=str(u(w[3])),tick=sn(w[4]))
    return d

def enrich(events):
    tick=None; active=None; mismatch=[]
    for e in events:
        kind=e['kind']
        if kind.startswith('init'): tick=e['tick']; active=0
        elif 'delta' in e:
            e['activeBefore']=None if active is None else str(active)
            e['inRange']=None if tick is None else e['lower']<=tick<e['upper']
            if active is not None and e['inRange']: active+=int(e['delta'])
            e['activeAfter']=None if active is None else str(active)
            if active is not None and active<0: mismatch.append(e['hash']); active=None
        elif kind.startswith('swap'):
            tick=e['tick']; active=int(e['activeAfter'])
    return mismatch

def historical_quote(rpc,venue,token,qty,b):
    t0,t1=venue['token0'],venue['token1']; base=t1 if token==t0 else t0
    if token not in (t0,t1): raise ValueError('TOKEN_NOT_IN_POOL')
    if venue['source']=='uniswapV4':
        params=[32,int(t0,16),int(t1,16),int(venue['fee']),int(venue['spacing']),int(venue['hooks'],16),int(token==t0),qty,256,0]
        to=V4_QUOTER; data='0xaa9d21cb'+''.join(map(word,params))
    else:
        to=V3_QUOTER; data='0xc6a5026a'+''.join(map(word,[int(token,16),int(base,16),qty,int(venue['fee']),0]))
    out=rpc.call('eth_call',[{'to':to,'data':data},hex(b)])
    if not out or len(out)<66: raise RuntimeError('INCOMPLETE_QUOTE')
    return dict(base=base,rawOut=str(int(out[2:66],16)),quantity=str(qty),block=b,basis='HISTORICAL_SINGLE_POOL_QUOTER_END_OF_BLOCK')

def campaign(db,path,token,rpc,head,afterBlocks):
    errors=[]; out={'campaign':str(path.parent),'token':token,'errors':errors}
    sel=rows(db,'selections','token',token,errors)[0]; sid=sel['selectionId']
    out['selection']=sel
    decision=json.loads(sel['event']); event=decision['event']; details=event['details']
    out['manifest']=json.loads((path.parent/'campaign.json').read_text()) if (path.parent/'campaign.json').exists() else None
    for t in ('positions','lamb_probes','transactions','sales','exit_slices','manual_fills','manual_requests'):
        out[t]=rows(db,t,'selectionId',sid,errors)
    out['observations']=rows(db,'observations','token',token,errors)
    out['venues']=rows(db,'venues','token',token,errors)
    selectorPath=path.parent/'selector.sqlite3'
    if selectorPath.exists():
        sd=ro(selectorPath)
        try:
            for t in ('launches','decisions','concentration_progress','concentration_chunks','concentration_swaps','concentration_audits'):
                out[t]=rows(sd,t,'eventKey',sid,errors)
        finally: sd.close()
    out['chainTransactions']=[]; purchaseBlocks=[]; actualPools=set()
    for tx in out['transactions']:
        tx.pop('raw',None)
        try:
            rc=rpc.call('eth_getTransactionReceipt',[tx['txHash']])
            if rc is None: continue
            b=num(rc['blockNumber']); rc['timestamp']=rpc.stamp(b)
            txinfo=dict(kind=tx['kind'],receipt=rc)
            if tx['kind'] in ('BUY','PROBE_BUY') and num(rc['status'])==1:
                purchaseBlocks.append(b)
                for l in rc.get('logs',[]):
                    topics=l.get('topics',[])
                    if not topics: continue
                    if l['address'].lower()==MANAGER and topics[0].lower()==SIG['swap4']:
                        actualPools.add(topics[1].lower())
                    elif topics[0].lower()==SIG['swap3']: actualPools.add(l['address'].lower())
                try:
                    chainTx=rpc.call('eth_getTransactionByHash',[tx['txHash']]); txinfo['from']=chainTx.get('from'); txinfo['to']=chainTx.get('to'); txinfo['value']=chainTx.get('value')
                except Exception as e: errors.append('purchase tx: '+str(e)[:180])
            out['chainTransactions'].append(txinfo)
        except Exception as e: errors.append('receipt '+tx['txHash']+': '+str(e)[:180])
    start=num(event['blockNumber']); last=max(purchaseBlocks or [start]); end=min(head,last+afterBlocks)
    if end-start>20000:
        errors.append('SCAN_BOUNDED: only first 20001 blocks from launch; purchase may lie outside scan')
        end=start+20000
    out['scan']={'start':start,'end':end,'headAtStart':head,'actualPurchasePoolIdsIncludingBridges':sorted(actualPools)}
    try: out['launchBlock']=rpc.block(start)
    except Exception as e: errors.append('launch block: '+str(e)[:180])
    venues={}
    for v in out['venues']:
        try:
            p=json.loads(v['data']); venues[p['id'].lower()]=p
        except Exception as e: errors.append('venue: '+str(e)[:100])
    v=decision.get('venue',{})
    if v.get('id'): venues[v['id'].lower()]=v
    for pool in sorted(actualPools-set(venues)):
        if len(pool)!=66: continue
        raw,missing=rpc.logs({'address':MANAGER,'topics':[SIG['init4'],pool]},start,end)
        for l in raw:
            try:
                init=decode(l)
                if token in (init['token0'],init['token1']):
                    venues[pool]=dict(id=pool,source='uniswapV4',**{k:init[k] for k in ('token0','token1','fee','spacing','hooks')})
                    break
            except Exception as e: errors.append('additional venue: '+str(e)[:120])
        if missing: errors.append('additional pool initialization scan incomplete: '+pool)
    out['unmappedPurchasePoolIds']=sorted(actualPools-set(venues))
    out['pools']=[]
    if len(venues)>8: errors.append('VENUE_SCAN_LIMIT: eight venues only')
    for pool,v in sorted(venues.items(),key=lambda x:(x[0] not in actualPools,x[0]))[:8]:
        if v.get('source') not in ('uniswapV4','uniswapV3'): continue
        if token not in (v.get('token0'),v.get('token1')): continue
        v4=v['source']=='uniswapV4'; kinds=['init4','liq4','swap4'] if v4 else ['init3','mint3','burn3','swap3']
        flt=dict(address=MANAGER if v4 else pool,topics=[[SIG[k] for k in kinds]]+([pool] if v4 else []))
        print('  scanning',pool, start,end,flush=True)
        raw,missing=rpc.logs(flt,start,end)
        p={'venue':v,'usedInPurchase':pool in actualPools,'missingRanges':missing,'events':[],'historicalQuotes':[]}
        for l in raw:
            try: p['events'].append(decode(l))
            except Exception as e: errors.append('decode '+l.get('transactionHash','?')+': '+str(e)[:160])
        p['activeLiquidityErrors']=enrich(p['events'])
        p['initializationSeen']=any(e['kind'].startswith('init') for e in p['events'])
        important={start,end,*purchaseBlocks}
        withdrawals=[e for e in p['events'] if int(e.get('delta','0'))<0]
        important.update(e['block'] for e in withdrawals[:40])
        major=sorted(withdrawals,key=lambda e:Decimal(e['activeBefore'] or '0')-Decimal(e['activeAfter'] or '0'),reverse=True)[:10]
        important.update(e['block'] for e in major)
        for tx in out['chainTransactions']:
            rc=tx['receipt']
            if tx['kind'] not in ('BUY','PROBE_BUY') or num(rc['status'])!=1: continue
            after=[e for e in withdrawals if (e['block'],e['index'])>(num(rc['blockNumber']),num(rc['transactionIndex']))]
            if after: important.add(after[0]['block'])
        swaps=[e for e in p['events'] if e['kind'].startswith('swap')]
        if swaps:
            important.update([swaps[0]['block'],swaps[-1]['block']])
            sign=1 if token==v['token0'] else -1
            important.add(max(swaps,key=lambda e:sign*int(e['sqrtPriceX96']))['block'])
        for b in sorted(important):
            try: rpc.block(b)
            except Exception as e: errors.append('block '+str(b)+': '+str(e)[:150])
        if out['positions']:
            qty=int(out['positions'][0]['originalQty'])
            samples={start,end}
            for b in purchaseBlocks: samples.update((max(start,b-1),b,min(end,b+1)))
            for e in withdrawals[:6]+major[:4]: samples.update((max(start,e['block']-1),e['block']))
            if swaps: samples.add(max(swaps,key=lambda e:(1 if token==v['token0'] else -1)*int(e['sqrtPriceX96']))['block'])
            for b in sorted(samples):
                try: p['historicalQuotes'].append(historical_quote(rpc,v,token,qty,b))
                except Exception as e: p['historicalQuotes'].append(dict(block=b,quantity=str(qty),error=str(e)[:230]))
        out['pools'].append(p)
    return out

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    parser.add_argument('--after-blocks',type=int,default=10000)
    parser.add_argument('--output',type=Path,default=Path.home()/'Downloads/blackswan-dual-audit.json.gz')
    a=parser.parse_args()
    if not 1<=a.after_blocks<=20000: parser.error('--after-blocks must be 1..20000')
    rpc=Rpc(); report=dict(tokens=TOKENS,chainId=4663,rpc=RPC,startedAt=time.time(),campaigns=[],errors=[])
    head=None
    try:
        if num(rpc.call('eth_chainId',[]))!=4663: raise RuntimeError('WRONG_CHAIN')
        head=num(rpc.call('eth_blockNumber',[]))
    except Exception as e: report['errors'].append('Public RPC unavailable: '+str(e)[:200])
    matches=set()
    for path in sorted(a.data_dir.rglob('execution.sqlite3')):
        try:
            db=ro(path)
            try:
                for token in TOKENS:
                    if not db.execute('SELECT 1 FROM selections WHERE token=?',(token,)).fetchone(): continue
                    matches.add(token); print('FOUND',token,path.parent,flush=True)
                    if head is None:
                        e=[]; sel=rows(db,'selections','token',token,e)[0]; sid=sel['selectionId']; item=dict(token=token,campaign=str(path.parent),selection=sel,errors=e)
                        for t in ('positions','lamb_probes','transactions','sales','exit_slices','manual_fills'):
                            item[t]=rows(db,t,'selectionId',sid,e)
                        for tx in item['transactions']: tx.pop('raw',None)
                        item['observations']=rows(db,'observations','token',token,e)
                        item['venues']=rows(db,'venues','token',token,e)
                    else: item=campaign(db,path,token,rpc,head,a.after_blocks)
                    report['campaigns'].append(item)
            finally: db.close()
        except Exception as e: report['errors'].append(str(path)+': '+str(e)[:250])
    report['notFound']=sorted(set(TOKENS)-matches); report['blockHeaders']=rpc.blocks; report['rpcCalls']=rpc.calls
    report['finishedAt']=time.time()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(a.output,'wt') as f: json.dump(report,f,separators=(',',':'))
    print('AUDIT CREATED:',a.output,'| matching campaigns:',len(report['campaigns']),'| RPC calls:',rpc.calls,'| errors:',len(report['errors']),flush=True)
    if report['notFound']:print('NOT FOUND:',*report['notFound'])

if __name__=='__main__': main()
