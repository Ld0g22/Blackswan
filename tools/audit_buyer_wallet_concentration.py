#!/usr/bin/env python3
"""Read-only buyer identity audit. No log scans, signing, or campaign changes."""
import argparse
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, urlopen

CHAIN=4663
TRANSFER='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO='0x'+'0'*40
WINNERS={'0x1c8004':'CHOP','0xc4c8':'SIGIL','0xfea4d8ee':'Nov25 winner A','0x47a7f5e2':'Nov25 winner B'}
LOSERS={'0xb2deca30b178d2b8578b9cec24ce80670401bea5','0x1320b7571b6b09027d2daf933d94d1583e483a7e'}

def addr(s):
    if not isinstance(s,str) or len(s)!=42 or not s.startswith('0x'):
        raise ValueError('INVALID_ADDRESS')
    int(s[2:],16); return s.lower()

def number(s):
    return int(s,16) if isinstance(s,str) and s.startswith('0x') else int(s)

def ro(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    db.row_factory=sqlite3.Row
    return db

def rows(db,sql,args=()):
    return [dict(x) for x in db.execute(sql,args)]

def optional(db,sql,args=()):
    try: return rows(db,sql,args)
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return []
        raise

class Rpc:
    def __init__(self,url,budget):
        self.url=url; self.budget=budget; self.calls=0; self.cache={};self.halted=None
    def call(self,method,params):
        if self.halted:raise RuntimeError(self.halted)
        if self.calls>=self.budget:raise RuntimeError('REQUEST_BUDGET_EXHAUSTED')
        self.calls+=1;time.sleep(.12)
        body=json.dumps(dict(jsonrpc='2.0',id=self.calls,method=method,params=params)).encode()
        try:
            with urlopen(Request(self.url,body,{'Content-Type':'application/json'}),timeout=12) as response:
                obj=json.load(response)
        except Exception as exc:
            code=getattr(exc,'code',None)
            if code in (401,403,429): self.halted='PROVIDER_HTTP_'+str(code)
            raise RuntimeError('RPC_TRANSPORT_'+str(code or type(exc).__name__)) from None
        if 'error' in obj:raise RuntimeError('RPC_ERROR_'+str(obj['error'].get('code')))
        if obj.get('result') is None:raise RuntimeError('RPC_NULL_RESULT')
        return obj['result']
    def cached(self,method,h):
        key=(method,h)
        if key not in self.cache:
            self.cache[key]=self.call(method,[h])
        return self.cache[key]

def group(decision,swaps):
    if decision.get('policyName')!='concentration' or decision.get('status')!='PASS' or not decision.get('coverageComplete'):
        raise ValueError('NOT_COVERAGE_COMPLETE_CONCENTRATION_PASS')
    start=int(decision['firstTradeAt']);end=int(decision['windowEndAtExclusive'])
    if end-start!=int(decision['windowSeconds']):raise ValueError('WINDOW_MISMATCH')
    if end>float(decision['decisionAt']):raise ValueError('DECISION_BEFORE_WINDOW_END')
    txs=defaultdict(list);seen=set()
    for s in swaps:
        if not start<=int(s['blockTimestamp'])<end:continue
        raw=int(s['buyBaseRaw'])
        if raw<0:raise ValueError('NEGATIVE_BUY_VOLUME')
        k=(s['blockHash'].lower(),s['transactionHash'].lower(),int(s['logIndex']))
        if k in seen:raise ValueError('DUPLICATE_SWAP')
        seen.add(k)
        if raw:txs[k[1]].append(s)
    amounts={h:sum(int(s['buyBaseRaw']) for s in ss) for h,ss in txs.items()}
    if (not amounts or sum(amounts.values())!=int(decision['totalBuyBaseRaw'])
        or len(amounts)!=int(decision['buyTransactionCount'])
        or max(amounts.values())!=int(decision['largestBuyTransactionBaseRaw'])
        or len(seen)!=int(decision['observedSwapCount'])):
        raise ValueError('DECISION_TOTAL_OR_COUNT_MISMATCH')
    return txs,amounts

def recipient(token,logs):
    """Unique net-positive token address, not an inferred economic owner."""
    net=defaultdict(int)
    for log in logs:
        if log['address'].lower()!=token:continue
        topics=log.get('topics',[])
        if not topics or topics[0].lower()!=TRANSFER:continue
        if len(topics)!=3 or len(log['data'])!=66:raise ValueError('NONSTANDARD_TRANSFER')
        a=addr('0x'+topics[1][-40:]);b=addr('0x'+topics[2][-40:]);q=number(log['data'])
        net[a]-=q;net[b]+=q
    positive={a:q for a,q in net.items() if a!=ZERO and q>0}
    return (next(iter(positive)) if len(positive)==1 else None,
            {a:str(q) for a,q in positive.items()})

def resolve(rpc,h,ss,token,amount):
    result={'hash':h,'buyBaseRaw':str(amount),'signer':None,'recipient':None,'errors':[]}
    blocks={s['blockHash'].lower() for s in ss}
    if len(blocks)!=1:result['errors'].append('MULTIPLE_BLOCK_HASHES');return result
    block=next(iter(blocks))
    try:
        tx=rpc.cached('eth_getTransactionByHash',h)
        if tx['hash'].lower()!=h or tx['blockHash'].lower()!=block:
            raise ValueError('TRANSACTION_BLOCK_IDENTITY_MISMATCH')
        result['signer']=addr(tx['from'])
        result['to']=addr(tx['to']) if tx.get('to') else None
        result['nonce']=number(tx['nonce'])
    except Exception as e:result['errors'].append('SIGNER:'+str(e)[:160])
    try:
        rc=rpc.cached('eth_getTransactionReceipt',h)
        if rc['transactionHash'].lower()!=h or rc['blockHash'].lower()!=block or number(rc['status'])!=1:
            raise ValueError('RECEIPT_IDENTITY_MISMATCH')
        # Confirm every recorded buy log exists unchanged in this canonical receipt.
        logs={number(l['logIndex']):l for l in rc['logs']}
        for s in ss:
            actual=logs[int(s['logIndex'])]; saved=json.loads(s['raw'])
            if any(actual[k].lower()!=saved[k].lower() for k in ('address','data','transactionHash','blockHash')):
                raise ValueError('BUY_LOG_MISMATCH')
            if [t.lower() for t in actual['topics']]!=[t.lower() for t in saved['topics']]:
                raise ValueError('BUY_TOPICS_MISMATCH')
        result['receiptVerified']=True
        result['recipient'],result['netPositiveRecipients']=recipient(token,rc['logs'])
        if result['recipient'] is None:result['errors'].append('NO_UNIQUE_NET_RECIPIENT')
    except Exception as e:result['errors'].append('RECEIPT:'+str(e)[:160])
    return result

def concentration(buys,field):
    total=sum(int(t['buyBaseRaw']) for t in buys); by=defaultdict(int);unresolved=0
    for t in buys:
        q=int(t['buyBaseRaw'])
        a=t.get(field)
        if not a:unresolved+=q
        else:by[a]+=q
    largest=max(by.values(),default=0)
    return dict(totalRaw=str(total),resolvedRaw=str(total-unresolved),unresolvedRaw=str(unresolved),
                complete=bool(total and not unresolved),uniqueResolved=len(by),
                largestRaw=str(largest),lowerSharePct=str(Decimal(largest)*100/total) if total else None,
                upperSharePct=str(Decimal(largest+unresolved)*100/total) if total else None,
                walletVolumes={a:str(q) for a,q in sorted(by.items(),key=lambda p:-p[1])})

def label(token):
    for prefix,name in WINNERS.items():
        if token.startswith(prefix):return 'KNOWN_WINNER',name,prefix
    if token in LOSERS:return 'TARGET_LOSER',token,token
    return 'OTHER_PURCHASE',token,token

def outcome(ed,sel):
    sid=sel['selectionId'];token=sel['token']
    pos=optional(ed,'SELECT * FROM positions WHERE selectionId=?',(sid,))
    tx=optional(ed,"SELECT kind,state,txHash,inputAmount FROM transactions WHERE selectionId=?",(sid,))
    if not pos or not any(t['kind']=='BUY' and t['state']=='CONFIRMED' for t in tx):return None
    observations=optional(ed,"SELECT at,status,phase,gbp,multiple FROM observations WHERE token=? ORDER BY at",(token,))
    valid=[Decimal(o['multiple']) for o in observations if o['phase']=='SELL' and o['multiple'] is not None]
    sales=optional(ed,'SELECT * FROM sales WHERE selectionId=?',(sid,))
    manual=optional(ed,'SELECT * FROM manual_fills WHERE selectionId=?',(sid,))
    remaining=max(0,int(pos[0]['originalQty'])-sum(int(s['tokenDebited']) for s in sales+manual))
    return dict(mainPurchaseConfirmed=True,peakObservedMultiple=str(max(valid)) if valid else None,
                recordedBestGbp=pos[0]['bestGbp'],remainingMainRaw=str(remaining),
                lastSellObservation=next((o for o in reversed(observations) if o['phase']=='SELL'),None),
                confirmedSales=len(sales)+len(manual),positionError=pos[0]['error'])

def collect(root):
    cohort=[];errors=[]
    for path in sorted(root.rglob('execution.sqlite3')):
        sdpath=path.parent/'selector.sqlite3'
        if not sdpath.exists():continue
        try:
            with closing(ro(path)) as ed,closing(ro(sdpath)) as sd:
                for sel in rows(ed,'SELECT * FROM selections'):
                    try:
                        d=json.loads(sel['event'])
                        if d.get('policyName')!='concentration':continue
                        o=outcome(ed,sel)
                        if o is None:continue
                        token=addr(sel['token']);kind,name,labelKey=label(token)
                        item=dict(campaign=str(path.parent),token=token,label=kind,name=name,labelKey=labelKey,
                                  selectionId=sel['selectionId'],decision=d,outcome=o)
                        if kind=='OTHER_PURCHASE' and o['peakObservedMultiple'] is not None and Decimal(o['peakObservedMultiple'])>=3:
                            item['label']='ADDITIONAL_3X_OBSERVED'
                        cohort.append(item)
                        try:
                            original=rows(sd,'SELECT * FROM decisions WHERE eventKey=?',(sel['selectionId'],))
                            if len(original)!=1 or json.loads(original[0]['data'])!=d:raise ValueError('EXECUTOR_SELECTOR_DECISION_MISMATCH')
                            ss=rows(sd,'SELECT * FROM concentration_swaps WHERE eventKey=?',(sel['selectionId'],))
                            group(d,ss)
                            item['swaps']=ss
                        except Exception as e:item['evidenceError']=str(e)[:200]
                    except Exception as e:errors.append({'campaign':str(path.parent),'selectionId':sel.get('selectionId'),'error':str(e)[:200]})
        except Exception as e:errors.append({'campaign':str(path.parent),'error':str(e)[:200]})
    return cohort,errors

def sweep(cohort,metric):
    """Maximum share threshold, strict > excludes; UNKNOWN is never a pass."""
    from fractions import Fraction
    w=[x for x in cohort if x['label']=='KNOWN_WINNER']
    l=[x for x in cohort if x['label']=='TARGET_LOSER']
    others=[x for x in cohort if x['label']=='OTHER_PURCHASE']
    additional=[x for x in cohort if x['label']=='ADDITIONAL_3X_OBSERVED']
    expected=set(WINNERS)|LOSERS
    present={x['labelKey'] for x in w+l}
    def val(x):
        m=x.get(metric)
        return Fraction(int(m['largestRaw']),int(m['totalRaw'])) if m and m['complete'] else None
    cuts=sorted({v for x in cohort if (v:=val(x)) is not None})
    results=[]
    for cut in cuts:
        retained=[x['token'] for x in w if val(x) is not None and val(x)<=cut]
        rejected=[x['token'] for x in l if val(x) is not None and val(x)>cut]
        unknown=[x['token'] for x in cohort if val(x) is None]
        results.append(dict(thresholdNumerator=cut.numerator,thresholdDenominator=cut.denominator,
                            thresholdPct=str(Decimal(cut.numerator)*100/Decimal(cut.denominator)),
                            knownWinnersRetained=retained,targetLosersRejected=rejected,
                            additional3xObservedRetained=[x['token'] for x in additional if val(x) is not None and val(x)<=cut],
                            additional3xObservedRejected=[x['token'] for x in additional if val(x) is not None and val(x)>cut],
                            otherPurchasesRejected=[x['token'] for x in others if val(x) is not None and val(x)>cut],
                            unknown=unknown,missingLabels=sorted(expected-present),
                            perfectHistoricalSeparation=(expected<=present and len(retained)==len(w) and len(rejected)==len(l))))
    return results

def selfTest():
    a='0x'+'1'*40;b='0x'+'2'*40;c='0x'+'3'*40
    # Five independent transactions by one signer, passing 20% tx concentration.
    buys=[dict(buyBaseRaw='20',signer=a,recipient=b) for _ in range(5)]
    assert concentration(buys,'signer')['largestRaw']=='100'
    buys[0]['signer']=None
    m=concentration(buys,'signer');assert not m['complete'] and m['lowerSharePct']=='80' and m['upperSharePct']=='100'
    def transfer(x,y,q):
        return dict(address=c,topics=[TRANSFER,'0x'+x[2:].rjust(64,'0'),'0x'+y[2:].rjust(64,'0')],data='0x'+f'{q:064x}')
    assert recipient(c,[transfer(ZERO,a,100),transfer(a,b,100)])[0]==b
    assert recipient(c,[transfer(ZERO,a,100),transfer(a,b,50)])[0] is None
    d=dict(policyName='concentration',status='PASS',coverageComplete=True,firstTradeAt=100,windowEndAtExclusive=130,windowSeconds=30,decisionAt=131,totalBuyBaseRaw='20',buyTransactionCount=1,largestBuyTransactionBaseRaw='20',observedSwapCount=2)
    ss=[dict(transactionHash='0xa',blockHash='0xb',logIndex=i,blockTimestamp=t,buyBaseRaw='10') for i,t in enumerate([100,129,130])]
    assert group(d,ss)[1]=={'0xa':20}
    try:group(dict(d,totalBuyBaseRaw='30'),ss);raise AssertionError('BAD_TOTAL_ACCEPTED')
    except ValueError:pass
    print('SELF TEST PASS: wallet aggregation, unknown bounds, recipient ambiguity, exact window and totals')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env',type=Path)
    p.add_argument('--data-dir',type=Path,default=Path('data'))
    p.add_argument('--output',type=Path,default=Path.home()/'Downloads/blackswan-wallet-concentration.json.gz')
    p.add_argument('--max-requests',type=int,default=1000)
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args()
    selfTest()
    if a.self_test:return
    if not a.env or not 1<=a.max_requests<=2000:p.error('--env required and --max-requests must be 1..2000')
    from detector import loadEnv
    rpc=Rpc(loadEnv(a.env.expanduser())['ROBINHOOD_ALCHEMY_HTTP_URL'],a.max_requests)
    if number(rpc.call('eth_chainId',[]))!=CHAIN:raise RuntimeError('WRONG_CHAIN')
    cohort,errors=collect(a.data_dir)
    # Prioritize the named four winners and two target losers, then all other purchases.
    cohort.sort(key=lambda c:(0 if c['label'] in ('KNOWN_WINNER','TARGET_LOSER') else 1 if c['label']=='ADDITIONAL_3X_OBSERVED' else 2,c['decision']['decisionAt']))
    print('MATCHED MAIN PURCHASES',len(cohort),flush=True)
    for c in cohort:
        if 'swaps' not in c:
            print('MISSING EVIDENCE',c['token'],c.get('evidenceError'),flush=True);continue
        txs,amounts=group(c['decision'],c.pop('swaps'))
        c['buys']=[resolve(rpc,h,ss,c['token'],amounts[h]) for h,ss in txs.items()]
        c['signerConcentration']=concentration(c['buys'],'signer')
        c['recipientConcentration']=concentration(c['buys'],'recipient')
        try:
            event=c['decision']['event']; tx=rpc.cached('eth_getTransactionByHash',event['transactionHash'])
            if tx['hash'].lower()!=event['transactionHash'].lower() or tx['blockHash'].lower()!=event['blockHash'].lower():raise ValueError('LAUNCH_IDENTITY_MISMATCH')
            c['launchTransactionSigner']=addr(tx['from'])
            c['launchSignerBuyRaw']=c['signerConcentration']['walletVolumes'].get(c['launchTransactionSigner'],'0')
        except Exception as e:c['launchSignerError']=str(e)[:160]
        print(c['label'],c['token'],'signer largest %',c['signerConcentration']['lowerSharePct'],
              'complete',c['signerConcentration']['complete'],'recipient largest %',
              c['recipientConcentration']['lowerSharePct'],'complete',c['recipientConcentration']['complete'],flush=True)
    report=dict(chainId=CHAIN,createdAt=time.time(),cohort=cohort,collectionErrors=errors,
                knownWinnerPrefixes=WINNERS,targetLosers=sorted(LOSERS),rpcRequests=rpc.calls,
                signerThresholds=sweep(cohort,'signerConcentration'),recipientThresholds=sweep(cohort,'recipientConcentration'),
                missingKnownLabels=sorted((set(WINNERS)|LOSERS)-{c['labelKey'] for c in cohort}))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(a.output,'wt') as f:json.dump(report,f,separators=(',',':'))
    for metric in ('signerThresholds','recipientThresholds'):
        perfect=[x for x in report[metric] if x['perfectHistoricalSeparation']]
        print(metric,'perfect separation thresholds:',len(perfect),flush=True)
        if perfect:print('  lowest:',perfect[0]['thresholdPct'],'highest:',perfect[-1]['thresholdPct'],flush=True)
    print('AUDIT CREATED:',a.output,'RPC requests:',rpc.calls,'missing known labels:',report['missingKnownLabels'],flush=True)

if __name__=='__main__':main()
