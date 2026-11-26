#!/usr/bin/env python3
"""Offline deadline incident reconstruction from existing BlackSwan journals.
No RPC, no signer, no database writes, no process control. Historical timings
are distinguished from current state and bounds, not fabricated from snapshots.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sqlite3
import time

MISSED=('MISSED_1P_DEADLINE','MISSED_LAMB_DEADLINE')
OPEN_TX={'SIGNED','SENT','UNCERTAIN','REJECTED','STUCK'}
META={'campaignVersion','campaignIdentity','selectorName','intakeState','workerState',
      'selectionCursor','detectorCursor','executorHeartbeat','manualExitHeartbeat',
      'intakeClosedAt','walletPublicAddress','buyGbp','maxPurchases','probeGbp',
      'probeWaitUnit','probeWaitValue','selectorReady','selectorHeartbeat',
      'selectorPid','selectorVerifiedChainId','lastConnectedHead'}
def safeReason(v):
    if v is None:return None
    s=str(v)
    s=re.sub(r'(?:https?|wss?)://[^\s\'"]+', '[REDACTED_URL]',s)
    s=re.sub(r'(?i)(api[_-]?key|authorization|password|private[_-]?key)\s*[:=]\s*[^\s,;]+',r'\1=[REDACTED]',s)
    return s[:240]
def rows(db,q,args=()):
    return [dict(r) for r in db.execute(q,args)]
def optional(db,q,args=()):
    try:return rows(db,q,args)
    except sqlite3.OperationalError as e:
        if 'no such table' in str(e):return []
        raise
def ro(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db
def utc(t):
    return datetime.fromtimestamp(float(t),timezone.utc).isoformat() if t is not None else None
def delta(t,origin):
    return round(float(t)-float(origin),6) if t is not None else None
def meta(db):
    return {r['key']:r['value'] for r in rows(db,'SELECT key,value FROM meta') if r['key'] in META}
def txSummary(t,settlement):
    # The caller never selects raw signed bytes; receipt is reduced to public identity.
    rc=None
    if t.get('receipt'):
        try:
            obj=json.loads(t['receipt'])
            rc={k:obj.get(k) for k in ('status','blockNumber','blockHash','transactionHash','transactionIndex')}
        except (ValueError,TypeError):rc={'error':'INVALID_RECEIPT_JSON'}
    out={k:t[k] for k in ('intentId','selectionId','kind','state','txHash','nonce','createdAt','lastBroadcast','inputAmount')}
    out['reason']=safeReason(t['reason']);out['receipt']=rc
    out['localSettlementEvidence']=settlement.get(t['txHash'])
    return out
def settlementEvidence(ed,txs):
    evidence={}
    def add(h,at,basis):
        if not h or at is None:return
        item={'at':float(at),'basis':basis}
        if h not in evidence or item['at']<evidence[h]['at']:evidence[h]=item
    for x in optional(ed,'SELECT probeTxHash,confirmedAt FROM lamb_probes'):
        add(x['probeTxHash'],x['confirmedAt'],'PROBE_LOCAL_CONFIRMATION')
    for x in optional(ed,'SELECT txHash,confirmedAt FROM sales'):
        add(x['txHash'],x['confirmedAt'],'SALE_LOCAL_CONFIRMATION')
    for x in optional(ed,'SELECT txHash,confirmedAt FROM manual_fills'):
        add(x['txHash'],x['confirmedAt'],'MANUAL_LOCAL_CONFIRMATION')
    bought={x['selectionId']:x['boughtAt'] for x in rows(ed,'SELECT selectionId,boughtAt FROM positions')}
    for t in txs:
        if t['kind']=='BUY' and t['state']=='CONFIRMED':add(t['txHash'],bought.get(t['selectionId']),'MAIN_LOCAL_CONFIRMATION')
    for x in optional(ed,"SELECT txHash,recordedAt FROM finance_events WHERE state='COMPLETE'"):
        add(x['txHash'],x['recordedAt'],'FINANCE_RECORDED_UPPER_BOUND')
    return evidence
def decisionFacts(sd,sid):
    ds=rows(sd,'SELECT seq,eventKey,status,decisionAt,data FROM decisions WHERE eventKey=?',(sid,))
    ls=rows(sd,'SELECT eventKey,source,state,detectedAt,deadline,createdAt FROM launches WHERE eventKey=?',(sid,))
    if len(ds)!=1:return {'error':'SELECTOR_DECISION_MISSING_OR_DUPLICATE','launch':ls}
    d=ds[0];p=json.loads(d.pop('data'))
    d['facts']={k:p.get(k) for k in ('interfaceVersion','selectionId','tokenKey','status','deadline','decisionAt','reason','buyingBlocks','buyingBlockCount','headerTimestamp','coverageComplete','minBuyingBlocks','maxBuyingBlocks')}
    d['venue']={k:p.get('venue',{}).get(k) for k in ('source','id','fee','spacing','hooks','baseAsset')}
    e=p.get('event',{})
    d['detectorEvent']={k:e.get(k) for k in ('eventKey','observedAtUtc','blockNumber','blockHash','transactionHash','source')}
    d['launch']=ls
    if ls and ls[0]['deadline'] is not None:
        d['decisionMinusWindowSeconds']=delta(d['decisionAt'],ls[0]['deadline'])
        d['detectedToDecisionSeconds']=delta(d['decisionAt'],ls[0]['detectedAt'])
    d['buyEvidence']=optional(sd,'SELECT availableAt,blockHash,valid,origin FROM buyingEvents WHERE eventKey=? ORDER BY availableAt',(sid,))
    d['revocations']=optional(sd,'SELECT seq,notedAt,reason FROM revocations WHERE eventKey=?',(sid,))
    return d
def reconstruct(ed,sd,config,selection,txs):
    start=float(selection['decisionAt']);sid=selection['selectionId'];token=selection['token']
    deadline=float(config['execution']['sacrificeDeadlineSeconds']);end=start+deadline
    retry=rows(ed,'SELECT value FROM meta WHERE key=?',('probeRetryAfter:'+sid,))
    retryAt=float(retry[0]['value'])-.5 if retry else None
    facts=decisionFacts(sd,sid) if sd else {'error':'SELECTOR_DB_MISSING'}
    overlap=[];own=[];nearby=[]
    for tx in txs:
        created=float(tx['createdAt']);last=float(tx['lastBroadcast'] or 0)
        done=tx['localSettlementEvidence'];settled=done['at'] if done else None
        t=dict(tx,createdRelativeSeconds=delta(created,start),lastBroadcastRelativeSeconds=delta(last,start) if last else None,
               localSettlementRelativeSeconds=delta(settled,start))
        if tx['selectionId']==sid:own.append(t)
        elif created<=end and (settled is None or settled>=start):
            # Overlap is possible, not proof of historical lock ownership.
            if created>=start-30 or tx['state'] in OPEN_TX or (settled is not None and settled>=start):
                overlap.append(t)
        if start-10<=created<=end+10:nearby.append(t)
    otherPasses=rows(ed,"SELECT selectionId,token,decisionAt,status,reason FROM selections WHERE decisionAt BETWEEN ? AND ? ORDER BY decisionAt",(start-10,end+10))
    for x in otherPasses:
        x['reason']=safeReason(x['reason']);x['relativeSeconds']=delta(x['decisionAt'],start)
    obs=rows(ed,"SELECT token,phase,at,block,status,reason FROM observations WHERE at BETWEEN ? AND ? ORDER BY at LIMIT 1001",(start-5,end+5))
    for x in obs:x['reason']=safeReason(x['reason']);x['relativeSeconds']=delta(x['at'],start)
    bounds=[]
    if retryAt is not None:
        bounds.append('A retryable pre-sign failure occurred: persisted retryAfter minus 0.5 seconds. Original exception overwritten; exact failing RPC unknown.')
    if overlap:bounds.append('Other journalled transactions could overlap the deadline; local settlement upper bounds do not establish exact historical lock duration.')
    if not own:bounds.append('No durable transaction intent for this selection: the miss happened before journalled signing, or scheduling never reached signing.')
    elif any(t['kind']=='PROBE_BUY' for t in own):bounds.append('A probe intent exists: preserve and reconcile it; do not assume no funds were spent.')
    if facts.get('decisionMinusWindowSeconds') is not None and facts['decisionMinusWindowSeconds']>1:
        bounds.append('Selector published its decision more than one second after window end; this is separate from the post-PASS sacrifice deadline.')
    return dict(token=token,selectionId=sid,status=selection['status'],reason=safeReason(selection['reason']),
        decisionAt=start,decisionUtc=utc(start),deadlineAt=end,deadlineSeconds=deadline,
        selector=facts,selectorExecutorDecisionDelta=delta(start,facts['decisionAt']) if 'decisionAt' in facts else None,
        lastRetryAtEstimated=retryAt,lastRetryRelativeSeconds=delta(retryAt,start),
        ownTransactions=own,possibleWalletOverlap=overlap,nearbyTransactions=nearby,
        nearbyPasses=otherPasses,nearbyObservations=obs[:1000],observationsTruncated=len(obs)>1000,
        evidenceStatements=bounds,unrecorded=['executor first read/queue timestamp','job scheduling and start timestamp',
            'quote and Pons RPC stage timings','intakeLock and wallet lock wait timings',
            'exact historical receipt polling and approval job activity','final failure timestamp and overwritten retry reason'])
def audit(directory):
    config=json.loads((directory/'campaign.json').read_text())
    if config.get('schemaVersion')!=3 or config.get('selector',{}).get('name')!='buyingBlocks':
        return None
    if not config['sacrifice']['enabled']:return None
    with closing(ro(directory/'execution.sqlite3')) as ed:
        selections=rows(ed,'SELECT selectionId,token,decisionAt,status,reason FROM selections ORDER BY decisionAt')
        # Explicit column allowlist; do not read raw signed transaction bytes.
        raw=rows(ed,'SELECT intentId,selectionId,kind,state,txHash,nonce,createdAt,lastBroadcast,inputAmount,receipt,reason FROM transactions ORDER BY createdAt')
        settlement=settlementEvidence(ed,raw)
        txs=[txSummary(t,settlement) for t in raw]
        missed=[s for s in selections if s['reason'] in MISSED]
        if not missed:return dict(campaign=str(directory),manifest=config,selectionCounts=dict(Counter(s['status'] for s in selections)),incidents=[])
        sdpath=directory/'selector.sqlite3'
        with closing(ro(sdpath)) if sdpath.is_file() else closingNull() as sd:
            incidents=[reconstruct(ed,sd,config,s,txs) for s in missed]
            sm=meta(sd) if sd else {}
        # Successful probe timing comparison, not an estimate of failed stage timings.
        bySelection={s['selectionId']:s for s in selections}
        controls=[]
        for t in txs:
            s=bySelection.get(t['selectionId'])
            if t['kind']=='PROBE_BUY' and s:
                controls.append(dict(token=s['token'],selectionId=t['selectionId'],state=t['state'],
                    createdAfterPassSeconds=delta(t['createdAt'],s['decisionAt']),
                    lastBroadcastAfterPassSeconds=delta(t['lastBroadcast'],s['decisionAt']) if t['lastBroadcast'] else None,
                    receipt=t['receipt'],localSettlement=t['localSettlementEvidence']))
        return dict(campaign=str(directory),manifest=config,executionMetaAtSnapshot=meta(ed),selectorMetaAtSnapshot=sm,
            selectionCounts=dict(Counter(s['status'] for s in selections)),incidents=incidents,
            probeControls=controls,unsettledAtSnapshot=[t for t in txs if t['state'] in OPEN_TX])
from contextlib import nullcontext
def closingNull():return nullcontext(None)
def selfTest():
    # Exercise inference boundaries using actual sqlite schemas (no external dependencies).
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        p=Path(tmp)/'test.db'
        with closing(sqlite3.connect(p)) as d:
            d.executescript("CREATE TABLE meta(key TEXT,value TEXT); CREATE TABLE selections(selectionId TEXT,token TEXT,decisionAt REAL,status TEXT,reason TEXT); CREATE TABLE observations(token TEXT,phase TEXT,at REAL,block TEXT,status TEXT,reason TEXT);")
            d.execute("INSERT INTO meta VALUES('probeRetryAfter:s','102.5')")
            d.commit()
        with closing(ro(p)) as ed:
            selection=dict(selectionId='s',token='0x'+'1'*40,decisionAt=100,status='PROBE_FAILED',reason='MISSED_1P_DEADLINE')
            x=reconstruct(ed,None,{'execution':{'sacrificeDeadlineSeconds':3}},selection,[])
            assert x['lastRetryRelativeSeconds']==2 and x['deadlineAt']==103 and not x['ownTransactions']
            assert x['selector']['error']=='SELECTOR_DB_MISSING'
            try:ed.execute("INSERT INTO meta VALUES('x','y')");raise AssertionError('WRITE_ALLOWED')
            except sqlite3.OperationalError:pass
    assert 'secret' not in safeReason('https://example.test/v2/secret')
    print('SELF TEST PASS: read-only enforcement, retry timing, missing evidence, redaction',flush=True)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=Path('data'))
    p.add_argument('--run-dir',type=Path)
    p.add_argument('--output',type=Path,default=Path.home()/'Downloads/blackswan-missed-sacrifice-deadline.json.gz')
    p.add_argument('--self-test',action='store_true')
    a=p.parse_args();selfTest()
    if a.self_test:return
    dirs=[a.run_dir] if a.run_dir else sorted(a.data_dir.glob('configured_v3_*'))
    report=dict(schemaVersion=1,capturedAt=time.time(),campaigns=[],errors=[],
                limitations=['Separate read-only SQLite snapshots, not an atomic cross-process snapshot.',
                 'lastBroadcast is last attempt, not necessarily initial broadcast.',
                 'Historical stage timing was not journalled; no causal diagnosis from current state alone.',
                 'No RPC requests or live worker changes.'])
    for d in dirs:
        try:
            if not (d/'execution.sqlite3').is_file():continue
            x=audit(d)
            if x is not None:
                report['campaigns'].append(x)
                for i in x['incidents']:
                    print('MISSED',i['token'],i['decisionUtc'],'retry relative',i['lastRetryRelativeSeconds'],
                          'possible competing transactions',len(i['possibleWalletOverlap']),
                          'own intents',len(i['ownTransactions']),flush=True)
        except Exception as e:
            report['errors'].append(dict(campaign=str(d),error=type(e).__name__,detail=safeReason(e)))
    total=sum(len(c['incidents']) for c in report['campaigns'])
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(a.output,'wt') as f:json.dump(report,f,separators=(',',':'))
    print('AUDIT:',a.output,'campaigns',len(report['campaigns']),'missed deadlines',total,'errors',len(report['errors']),flush=True)
    if not total:print('NO MATCHING INCIDENT: check --data-dir or --run-dir')
if __name__=='__main__':main()
