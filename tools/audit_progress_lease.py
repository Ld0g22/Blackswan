#!/usr/bin/env python3
"""Read-only offline replay of initial progress lease, across both concentration ledger families.
No RPC, no signer, no changes to campaigns. Quote counterfactuals, never simulated fills.
"""
import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import gzip
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

D=Decimal
KNOWN={'0x1c8004f2003af04582877fde5cf46765338a0ec7':'CHOP',
       '0xc4c8b397546d145b8979e64e91b8d26093fad7fc':'SIGIL',
       '0xfea4d8ee5f82f8039738694311acaa83fe2b8edd':'WINNER_NOV25_A',
       '0x47a7f5e29e260c0c6077edb5af100133f5861698':'WINNER_NOV25_B',
       '0xf9917723146c4621f54c3e54030ec6df1a7bbddf':'WINNER_NOV26_A',
       '0x87918e6b9689866ad802bc1b8f921c73fee652fd':'WINNER_NOV26_B'}
# CHOP also recognized by historical token prefix to flag any identity discrepancy.
TARGETS={'0xb2deca30b178d2b8578b9cec24ce80670401bea5','0x1320b7571b6b09027d2daf933d94d1583e483a7e'}
HORIZONS=(5,10,15,20,30)
FLOORS=('1.05','1.10','1.15','1.20')
DELAYS=(0,3,5,10)
PRIMARY=(10,'1.10')
MAX_SAMPLE_DELAY=12
def query(db,sql,args=()):
    return [dict(x) for x in db.execute(sql,args)]
def one(db,sql,args=()):
    a=query(db,sql,args)
    if len(a)!=1: raise ValueError('EXPECTED_ONE_ROW:'+sql[:75])
    return a[0]
def tableNames(db):
    return {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def num(v):
    if v is None:return None
    try:
        x=D(str(v))
        return x if x.is_finite() and x>=0 else None
    except InvalidOperation:return None
def quote(at,gbp,status,block,stake,reason=None):
    val=num(gbp)
    if status not in ('ONCHAIN_QUOTE_ONLY','OK') or val is None or val<=0 or block is None: val=None
    return dict(at=float(at),gbp=str(val) if val is not None else None,
                multiple=str(val/stake) if val is not None else None,
                status=status,block=block,reason=reason)
def utc(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
def clean(row,qs,stake):
    start=row['entryAt']
    # No full-position counterfactual after an actual sell or before purchase.
    end=row.get('firstSaleAt')
    row['quotes']=sorted([q for q in qs if q['at']>=start and (end is None or q['at']<end)],key=lambda q:q['at'])
    vals=[D(q['multiple']) for q in row['quotes'] if q['multiple'] is not None]
    row['preSalePeakMultiple']=str(max(vals)) if vals else None
    row['stakeGbp']=str(stake)
    row['entryUtc']=utc(start)
    row['knownWinner']=KNOWN.get(row['token'])
    row['targetLoser']=row['token'] in TARGETS
    peak=max(vals,default=D(0))
    if row.get('knownWinner') or row.get('confirmed3xSale') or peak>=3:
        row['outcome']='OBSERVED_3X_OR_KNOWN_WINNER'
    elif row['quotes'] and row.get('firstSaleAt') is not None:
        row['outcome']='SOLD_BEFORE_OBSERVED_3X'
    else:row['outcome']='NO_3X_OBSERVED_NOT_NECESSARILY_FINAL'
    return row
def modern(db,path,tables,issues):
    for p in query(db,'SELECT * FROM positions'):
        try:
            sid=p['selectionId']; token=p['token'].lower()
            s=one(db,'SELECT event FROM selections WHERE selectionId=?',(sid,))
            d=json.loads(s['event'])
            if d.get('policyName')!='concentration':continue
            if d.get('status')!='PASS' or not d.get('coverageComplete'):raise ValueError('UNVERIFIED_CONCENTRATION_PASS')
            tx=one(db,"SELECT txHash,receipt FROM transactions WHERE selectionId=? AND kind='BUY' AND state='CONFIRMED'",(sid,))
            receipt=json.loads(tx['receipt'])
            if int(receipt['status'],16)!=1 or receipt['transactionHash'].lower()!=tx['txHash'].lower():raise ValueError('BUY_RECEIPT_MISMATCH')
            entry=one(db,"SELECT gbp,quantity FROM observations WHERE token=? AND phase='ENTRY' AND status='CONFIRMED_FILL'",(token,))
            stake=num(entry['gbp'])
            if not stake or int(entry['quantity'])!=int(p['originalQty']):raise ValueError('INVALID_ENTRY_BASIS')
            sales=query(db,'SELECT confirmedAt,stage FROM sales WHERE selectionId=?',(sid,))
            manual=query(db,'SELECT confirmedAt FROM manual_fills WHERE selectionId=?',(sid,)) if 'manual_fills' in tables else []
            sellTimes=[float(x['confirmedAt']) for x in sales+manual]
            row=dict(token=token,buyHash=tx['txHash'].lower(),campaign=str(path.parent),family='configured',
                entryAt=float(p['boughtAt']),decisionAt=d['decisionAt'],firstSaleAt=min(sellTimes) if sellTimes else None,
                confirmed3xSale=any(str(x['stage'])=='3' for x in sales),recordedBestGbp=p['bestGbp'])
            qs=[quote(x['at'],x['gbp'],x['status'],x['block'],stake,x['reason']) for x in query(db,"SELECT at,gbp,status,block,reason,quantity FROM observations WHERE token=? AND phase='SELL' ORDER BY at",(token,)) if x['quantity'] is not None and int(x['quantity'])==int(p['originalQty'])]
            # Failure rows may have NULL quantity; retain as failure, never fabricate a quote.
            for x in query(db,"SELECT at,status,block,reason FROM observations WHERE token=? AND phase='SELL' AND quantity IS NULL",(token,)):
                qs.append(quote(x['at'],None,x['status'],x['block'],stake,x['reason']))
            row['preMainProbeEvidence']=query(db,"SELECT at,phase,gbp,quantity,status,block FROM observations WHERE token=? AND phase LIKE 'PROBE%' AND at<=?",(token,p['boughtAt']))
            yield clean(row,qs,stake)
        except Exception as e:issues.append(dict(path=str(path),token=p.get('token'),reason=str(e)[:200]))
def legacy(db,path,issues):
    c=one(db,'SELECT id FROM campaign')
    if 'conc' not in c['id'].lower():return
    for p in query(db,'SELECT * FROM positions'):
        try:
            token=p['token'].lower();stake=num(p['entry_gbp'])
            if not stake:raise ValueError('INVALID_ENTRY_COST')
            tx=one(db,"SELECT tx_hash FROM intents WHERE id=? AND action='BUY' AND status='CONFIRMED'",(p['entry_intent'],))
            if not tx['tx_hash']:raise ValueError('BUY_HASH_MISSING')
            ds=query(db,"SELECT at,numerator,denominator FROM decisions WHERE token=? AND status='PASS' AND at<=? ORDER BY at DESC",(token,p['entry_at']))
            ds=[d for d in ds if int(d['denominator'])>0 and 4*int(d['numerator'])<=int(d['denominator'])]
            if not ds:raise ValueError('CONCENTRATION_PASS_MISSING')
            sales=query(db,"SELECT leg,created FROM intents WHERE token=? AND action='SELL' AND status='CONFIRMED'",(token,))
            # Use the earliest confirmed-sale intent creation as a conservative cutoff;
            # its actual fill could have been later.
            first=min((float(x['created']) for x in sales),default=None)
            row=dict(token=token,buyHash=tx['tx_hash'].lower(),campaign=c['id'],family='legacy',
                entryAt=float(p['entry_at']),decisionAt=ds[0]['at'],firstSaleAt=first,
                confirmed3xSale=any(x['leg']=='3X' for x in sales),realizedGbp=p['realized_gbp'],stage=p['stage'])
            qs=[quote(x['at'],x['value_gbp'],x['status'],x['block'],stake,x['error']) for x in query(db,"SELECT at,value_gbp,status,block,error,quantity FROM quotes WHERE token=? AND stream='REAL' ORDER BY at",(token,)) if x['quantity'] is not None and int(x['quantity'])==int(p['original_qty'])]
            row['preMainProbeEvidence']=query(db,"SELECT at,stream,intent,quantity,value_gbp,block,status FROM quotes WHERE token=? AND stream='SACRIFICE' AND at<=?",(token,p['entry_at']))
            yield clean(row,qs,stake)
        except Exception as e:issues.append(dict(path=str(path),token=p.get('token'),reason=str(e)[:200]))
def atOrAfter(qs,at):
    return next((q for q in qs if q['at']>=at),None)
def replay(row,horizon,floor):
    qs=row['quotes']; start=row['entryAt']; target=start+horizon
    # Never use a later known peak for a decision.
    earlier=[q for q in qs if q['at']<target and q['multiple'] is not None]
    if any(D(q['multiple'])>=3 for q in earlier):
        return dict(decision='ALREADY_3X')
    q=atOrAfter(qs,target)
    if row.get('firstSaleAt') is not None and row['firstSaleAt']<=target and q is None:
        return dict(decision='BASELINE_ALREADY_SOLD_UNKNOWN')
    if q is None or q['at']>target+MAX_SAMPLE_DELAY:
        return dict(decision='UNKNOWN_NO_TIMELY_QUOTE')
    r=dict(decisionAt=q['at'],ageSeconds=q['at']-start,quote=q)
    if q['multiple'] is None:
        r['decision']='EXIT_SIGNAL_UNQUOTABLE';return r
    if D(q['multiple'])>=D(floor):
        r['decision']='RETAIN';return r
    r['decision']='EXIT'
    r['delayScenarios']={}
    for delay in DELAYS:
        wanted=q['at']+delay
        fill=atOrAfter(qs,wanted)
        if fill is None or fill['at']>wanted+MAX_SAMPLE_DELAY:
            r['delayScenarios'][str(delay)]=dict(status='UNKNOWN_NO_TIMELY_QUOTE')
        elif fill['multiple'] is None:
            r['delayScenarios'][str(delay)]=dict(status='UNQUOTABLE',ageSeconds=fill['at']-start)
        else:
            v=D(fill['gbp']);stake=D(row['stakeGbp'])
            r['delayScenarios'][str(delay)]=dict(status='QUOTE_ONLY',ageSeconds=fill['at']-start,
                actualDelaySeconds=fill['at']-q['at'],grossGbp=str(v),grossMultiple=fill['multiple'],
                pnlBeforeCostsGbp=str(v-stake),pnlAfterIllustrative1pCostGbp=str(v-stake-D('.01')))
    return r
def selfTest():
    stake=D('.5')
    a=dict(entryAt=100,stakeGbp='.5',quotes=[quote(110,'.53','OK','0x1',stake),quote(113,'.51','OK','0x2',stake),quote(116,None,'UNAVAILABLE','0x3',stake)])
    x=replay(a,10,'1.10')
    assert x['decision']=='EXIT' and x['delayScenarios']['3']['grossGbp']=='0.51' and x['delayScenarios']['5']['status']=='UNQUOTABLE'
    assert replay(a,10,'1.05')['decision']=='RETAIN'
    assert replay(a,30,'1.10')['decision']=='UNKNOWN_NO_TIMELY_QUOTE'
    assert replay(dict(a,quotes=[quote(110,'1.6','OK','0x1',stake)]),10,'1.10')['decision']=='RETAIN'
    print('SELF TEST PASS: strict chronology, missing coverage, latency stress',flush=True)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('roots',nargs='*',type=Path)
    p.add_argument('--output',type=Path,default=Path.home()/'Downloads/blackswan-progress-lease.json.gz')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args();selfTest()
    if a.self_test:return
    roots=a.roots or [x for x in Path.home().iterdir() if x.is_dir() and 'blackswan' in x.name.lower()]
    issues=[];inventory=[];seen=set();positions={}
    for root in roots:
        for directory,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d not in ('.git','.venv','venv','node_modules','__pycache__')]
            for name in files:
                if name not in ('campaign.sqlite3','execution.sqlite3','campaign.db','execution.db','campaign.sqlite','execution.sqlite'):continue
                path=(Path(directory)/name).resolve()
                if path in seen:continue
                seen.add(path)
                try:
                    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
                        db.row_factory=sqlite3.Row;tables=tableNames(db)
                        if {'positions','selections','transactions','observations'}<=tables:family='configured';records=modern(db,path,tables,issues)
                        elif {'campaign','positions','intents','quotes','decisions'}<=tables:family='legacy';records=legacy(db,path,issues)
                        else:inventory.append(dict(path=str(path),family='UNRECOGNIZED',tables=sorted(tables)));continue
                        count=0
                        for row in records:
                            count+=1;h=row['buyHash']
                            if h in positions:
                                old=positions[h]
                                if any(old[k]!=row[k] for k in ('token','stakeGbp','entryAt')):
                                    issues.append(dict(path=str(path),token=row['token'],reason='DUPLICATE_BUY_CONFLICT'));continue
                                # Keep most complete chronological snapshot, not multiple copies.
                                if (len(row['quotes']),row['firstSaleAt'] is not None)<=(len(old['quotes']),old['firstSaleAt'] is not None):continue
                            positions[h]=row
                        inventory.append(dict(path=str(path),family=family,matched=count))
                except Exception as e:issues.append(dict(path=str(path),reason=str(e)[:180]))
    rows=sorted(positions.values(),key=lambda x:x['entryAt'])
    rules=[]
    for h in HORIZONS:
        for f in FLOORS:
            decisions=[]
            for row in rows:
                x=replay(row,h,f)
                decisions.append(dict(buyHash=row['buyHash'],token=row['token'],outcome=row['outcome'],**x))
            wins=[x for x in decisions if x['outcome']=='OBSERVED_3X_OR_KNOWN_WINNER']
            other=[x for x in decisions if x['outcome']!='OBSERVED_3X_OR_KNOWN_WINNER']
            retained=sum(x['decision'] in ('RETAIN','ALREADY_3X') for x in wins)
            rules.append(dict(horizonSeconds=h,floor=f,primary=(h,f)==PRIMARY,winners=len(wins),winnersRetained=retained,
                winnersExited=sum(x['decision'] in ('EXIT','EXIT_SIGNAL_UNQUOTABLE') for x in wins),
                otherExitSignals=sum(x['decision'] in ('EXIT','EXIT_SIGNAL_UNQUOTABLE') for x in other),decisions=decisions))
    missing=sorted(set(KNOWN)-{x['token'] for x in rows})
    report=dict(primaryRule=dict(seconds=10,minMultiple='1.10'),sampleLagLimitSeconds=MAX_SAMPLE_DELAY,
        positions=rows,rules=rules,inventory=inventory,issues=issues,missingKnownWinners=missing,
        missingTargetLosers=sorted(TARGETS-{x['token'] for x in rows}))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(a.output,'wt') as f:json.dump(report,f,separators=(',',':'))
    primary=next(x for x in rules if x['primary'])
    for x in primary['decisions']:
        print(x['token'],x['outcome'],x['decision'],x.get('quote',{}).get('multiple'),flush=True)
    print('PRIMARY: winners retained',primary['winnersRetained'],'/',primary['winners'],'other exit signals',primary['otherExitSignals'])
    print('AUDIT:',a.output,'unique confirmed buys',len(rows),'missing known winners',missing,'issues',len(issues),flush=True)
if __name__=='__main__':main()
