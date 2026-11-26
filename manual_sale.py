#!/usr/bin/env python3
"""Operator manual exits for one-50p Robinhood Chain campaigns.

No private key, keystore password, signing or RPC broadcasting in this CLI.
Requests are stored in the LIVE campaign DB and executed by its sole signer.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from campaign_v3 import validateCampaign, campaignDigest
from campaign_config import (FLEX_KIND, FLEX_POLICY, PILOT_KIND, PILOT_POLICY,
    EARLY_KIND, EARLY_POLICY, flexManifest, pilotManifest, earlyManifest)

ACTIVE=('QUEUED','PREPARING','WAITING_APPROVAL','WAITING_SELL','BLOCKED','UNVERIFIED')


def connect(directory,readOnly=False):
    path=directory/'execution.sqlite3'
    if not path.is_file():
        raise RuntimeError('NO_EXECUTION_DB:'+str(path))
    db=sqlite3.connect(path.resolve().as_uri()+('?mode=ro' if readOnly else '?mode=rw'),uri=True,timeout=5,
                       isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    meta=dict(db.execute('SELECT key,value FROM meta'))
    manifest=json.loads((directory/'campaign.json').read_text())
    kind=manifest.get('campaignKind')
    if meta.get("campaignKind")=="CONFIGURED_V3":
        configured=validateCampaign(manifest)
        if (meta.get("campaignVersion")!="3"
                or meta.get("campaignIdentity")!=campaignDigest(configured)):
            db.close()
            raise RuntimeError("CONFIGURED_CAMPAIGN_IDENTITY_MISMATCH")
        if meta.get("manualExitVersion")!="1":
            db.close()
            raise RuntimeError("MANUAL_EXIT_ENGINE_NOT_INSTALLED")
        return db,meta
    if (manifest.get('chainId')!=4663 or
            kind not in ('ONE_50P_LAMB','TWO_50P_LAMB','TWO_50P_2X_LAMB','ONE_20P_2X_LAMB',FLEX_KIND,PILOT_KIND,EARLY_KIND) or
            meta.get('campaignKind')!=kind):
        db.close()
        raise RuntimeError('NOT_ONE_50P_CAMPAIGN')
    if kind in (FLEX_KIND,PILOT_KIND,EARLY_KIND):
        try:stake,count,wallet=(earlyManifest(manifest) if kind==EARLY_KIND else pilotManifest(manifest) if kind==PILOT_KIND else flexManifest(manifest))
        except ValueError as exc:
            db.close()
            raise RuntimeError('FLEX_MANIFEST_INVALID:'+str(exc)) from exc
        if (meta.get('buyGbp')!=stake or meta.get('maxPurchases')!=str(count)
                or meta.get('walletPublicAddress')!=wallet
                or meta.get('exitPolicy')!=(EARLY_POLICY if kind==EARLY_KIND else PILOT_POLICY if kind==PILOT_KIND else FLEX_POLICY)
                or meta.get('flexConfigVersion')!=('3' if kind==EARLY_KIND else '2' if kind==PILOT_KIND else '1')):
            db.close()
            raise RuntimeError('FLEX_LEDGER_MISMATCH')
    if meta.get('manualExitVersion')!='1':
        db.close()
        raise RuntimeError('MANUAL_EXIT_ENGINE_NOT_INSTALLED: resume with manual branch')
    return db,meta


def selectRun(explicit):
    if explicit:
        return explicit.expanduser().resolve()
    root=Path(__file__).resolve().parent/'data'
    matches=sorted([*root.glob('v4_lamb_flex1p5x3x_*'),*root.glob('v4_lamb_flex3x5x_*'),*root.glob('v4_lamb_flex2x_*'),*root.glob('v4_lamb_1x20p_2x_*'),*root.glob('v4_lamb_1x50p_*'),
                    *root.glob('v4_lamb_2x50p_*')],
                   key=lambda p:p.stat().st_mtime,reverse=True)
    for directory in matches:
        try:
            db,_=connect(directory)
            with db:
                position=db.execute('SELECT 1 FROM positions LIMIT 1').fetchone()
            db.close()
            if position:return directory.resolve()
        except (RuntimeError,OSError,sqlite3.Error,ValueError,KeyError):
            continue
    raise RuntimeError('NO_ONE_50P_POSITION: use --run-dir with an existing campaign')


def display(request,db,directory):
    print('CAMPAIGN',directory)
    print('REQUEST',request['requestId'],'STATUS',request['status'],
          'OPERATION',request['operation'])
    if request['reason']:print('DETAIL',request['reason'])
    pos=db.execute('SELECT token,originalQty FROM positions WHERE selectionId=?',
                   (request['selectionId'],)).fetchone()
    if pos:print('TOKEN',pos['token'])
    print('PERCENT',request['percent'],'MINIMUM_GBP',request['minGbp'],
          'SLIPPAGE_BPS',request['slippageBps'])
    if request['quantity']:print('TOKEN_UNITS',request['quantity'])
    if request['quoteGbp']:
        print('QUOTE_GBP',request['quoteGbp'],
              '(fresh at executor request; not a fill)')
        if request['quoteWei'] and request['minOutWei']:
            low=(Decimal(request['quoteGbp'])*Decimal(request['minOutWei'])/
                 Decimal(request['quoteWei']))
            print('ONCHAIN_MINIMUM_GBP_APPROX',round(low,4),
                  '(GBP equivalent at quote-time FX)')
    if request['quoteBlock']:print('QUOTE_BLOCK',request['quoteBlock'])
    if request['gasEstimate']:print('GAS_ESTIMATE_UNITS',request['gasEstimate'])
    if request['txHash']:print('TRANSACTION',request['txHash'])
    fill=db.execute('SELECT * FROM manual_fills WHERE requestId=?',
                    (request['requestId'],)).fetchone()
    if fill:
        print('VERIFIED_TOKEN_DEBIT',fill['tokenDebited'])
        print('GAS_SPENT_WEI',fill['gasWei'])
        if fill['wethReceivedWei']:
            print('VERIFIED_WETH_RECEIVED_WEI',fill['wethReceivedWei'])
        else:
            print('NATIVE_ETH_PROCEEDS','NOT_MEASURED_BY_ERC20_RECEIPT')
        print('RECEIPT','CONFIRMED (not estimated)')


def remainingMain(db,position):
    kind=db.execute("SELECT value FROM meta WHERE key='campaignKind'").fetchone()[0]
    if kind=="CONFIGURED_V3":
        auto=sum(int(row[0]) for row in db.execute(
            "SELECT soldQty FROM exit_slices WHERE selectionId=?",(position['selectionId'],)))
        manual=sum(int(row[0]) for row in db.execute(
            "SELECT tokenDebited FROM manual_fills WHERE selectionId=?",(position['selectionId'],)))
        return max(0,int(position['originalQty'])-auto-manual)
    stages=(3,5) if kind in (PILOT_KIND,EARLY_KIND) else (2,50,100) if kind in ('TWO_50P_2X_LAMB','ONE_20P_2X_LAMB',FLEX_KIND) else (3,5,1000)
    auto=sum(int(position['soldQty'+str(s)]) for s in stages)
    manual=sum(int(row[0]) for row in db.execute(
        'SELECT tokenDebited FROM manual_fills WHERE selectionId=?',
        (position['selectionId'],)))
    return max(0,int(position['originalQty'])-auto-manual)


def issue(db,directory,args):
    meta=dict(db.execute('SELECT key,value FROM meta'))
    # A schema-only status command must not impersonate a running manual engine.
    heartbeat=float(meta.get('manualExitHeartbeat') or 0)
    if abs(time.time()-heartbeat)>20:
        raise RuntimeError('EXECUTOR_NOT_LIVE: resume the campaign on this branch first')
    positions=db.execute('SELECT * FROM positions ORDER BY boughtAt').fetchall()
    selected=getattr(args,'selection_id',None)
    if selected:
        position=next((p for p in positions if p['selectionId']==selected),None)
        if position is None:raise RuntimeError('SELECTION_ID_NOT_IN_THIS_CAMPAIGN')
        if remainingMain(db,position)<=0:
            raise RuntimeError('NO_REMAINING_MAIN_POSITION')
    else:
        openPositions=[p for p in positions if remainingMain(db,p)>0]
        if not openPositions:raise RuntimeError('NO_REMAINING_MAIN_POSITION')
        if len(openPositions)>1:
            raise RuntimeError('MULTIPLE_OPEN_POSITIONS: use --selection-id from status')
        position=openPositions[0]
    if args.percent<1 or args.percent>100:
        raise ValueError('PERCENT_MUST_BE_1_TO_100')
    if args.slippage_bps<0 or args.slippage_bps>1000:
        raise ValueError('MANUAL_SLIPPAGE_CAP_10_PERCENT')
    try:
        minimum=Decimal(args.min_gbp or '0')
    except InvalidOperation:
        raise ValueError('INVALID_MIN_GBP') from None
    if not minimum.is_finite() or minimum<0:
        raise ValueError('INVALID_MIN_GBP')
    if args.command=='sell' and args.min_gbp is None:
        raise ValueError('SELL_REQUIRES_EXPLICIT_MIN_GBP (use 0 to allow low exits)')
    operation='SELL' if args.command=='sell' else 'PREVIEW'
    requestId=uuid4().hex
    db.execute('BEGIN IMMEDIATE')
    try:
        if operation=='SELL':
            pending=db.execute("""SELECT requestId,status FROM manual_requests
              WHERE operation='SELL' AND selectionId=?
                AND status IN ('QUEUED','PREPARING','WAITING_APPROVAL',
                               'WAITING_SELL','BLOCKED','UNVERIFIED') LIMIT 1""",
              (position['selectionId'],)).fetchone()
            if pending:
                raise RuntimeError('MANUAL_SALE_ALREADY_ACTIVE:'+pending['requestId']+
                                   ':'+pending['status'])
            # Do not accept a new request on a closed position.
            if remainingMain(db,position)<=0:
                raise RuntimeError('NO_REMAINING_MAIN_POSITION')
        now=time.time()
        db.execute("""INSERT INTO manual_requests
          (requestId,selectionId,operation,percent,minGbp,slippageBps,
           status,createdAt,updatedAt)
          VALUES(?,?,?,?,?,?,'QUEUED',?,?)""",
          (requestId,position['selectionId'],operation,args.percent,
           str(minimum),args.slippage_bps,now,now))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:db.execute('ROLLBACK')
        raise
    print('OPERATOR REQUEST ACCEPTED (not a sale)',requestId,
          'TOKEN',position['token'],'CAMPAIGN',directory)
    deadline=time.monotonic()+30
    last=''
    while time.monotonic()<deadline:
        item=db.execute('SELECT * FROM manual_requests WHERE requestId=?',
                        (requestId,)).fetchone()
        if item['status'] in ('PREVIEW_READY','PREVIEW_FAILED','CONFIRMED','FAILED','BLOCKED','UNVERIFIED'):
            display(item,db,directory)
            return 0 if item['status'] in ('PREVIEW_READY','CONFIRMED') else 2
        if item['status']!=last and item['status'] in ('WAITING_APPROVAL','WAITING_SELL'):
            print('CHAIN_TRANSACTION_PENDING',item['status'],item['txHash'])
            last=item['status']
        time.sleep(.3)
    item=db.execute('SELECT * FROM manual_requests WHERE requestId=?',(requestId,)).fetchone()
    display(item,db,directory)
    print('Request remains in executor; check: python manual_sale.py status --run-dir',
          directory)
    return 0


def status(db,directory):
    print('CAMPAIGN',directory)
    positions=db.execute('SELECT selectionId,token,originalQty FROM positions').fetchall()
    for p in positions:
        kind=dict(db.execute('SELECT key,value FROM meta')).get('campaignKind')
        if kind=="CONFIGURED_V3":
            sold=sum(int(row[0]) for row in db.execute(
                "SELECT soldQty FROM exit_slices WHERE selectionId=?",(p['selectionId'],)))
        else:
            stages=(3,5) if kind in (PILOT_KIND,EARLY_KIND) else (2,50,100) if kind in ('TWO_50P_2X_LAMB','ONE_20P_2X_LAMB',FLEX_KIND) else (3,5,1000)
            columns=','.join('soldQty'+str(s) for s in stages)
            auto=db.execute('SELECT '+columns+' FROM positions WHERE selectionId=?',
                            (p['selectionId'],)).fetchone()
            sold=sum(int(v) for v in auto)
        manual=sum(int(v[0]) for v in db.execute(
            'SELECT tokenDebited FROM manual_fills WHERE selectionId=?',
            (p['selectionId'],)))
        print('POSITION',p['token'],'SELECTION_ID',p['selectionId'],
              'MAIN_ORIGINAL',p['originalQty'],
              'AUTO_SOLD',sold,'MANUAL_SOLD',manual,
              'MAIN_REMAINING',max(0,int(p['originalQty'])-sold-manual))
    requests=db.execute('SELECT * FROM manual_requests ORDER BY createdAt DESC LIMIT 5').fetchall()
    if not requests:
        print('NO_MANUAL_REQUESTS')
    for request in requests:
        display(request,db,directory)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('preview','sell','status'))
    p.add_argument('--run-dir',type=Path,help='Specific campaign directory; defaults to latest compatible held position')
    p.add_argument('--selection-id',help='Required when multiple main positions remain open; see status')
    p.add_argument('--percent',type=int,default=100,
                   help='Percent of currently available UNSOLD main tokens; 1..100')
    p.add_argument('--min-gbp',help='Minimum entire slice proceeds in GBP at current FX; mandatory for sell (0 allowed)')
    p.add_argument('--slippage-bps',type=int,default=1000,help='At most 1000 bps (10 percent)')
    a=p.parse_args()
    try:
        directory=selectRun(a.run_dir)
        db,_=connect(directory)
        try:
            if a.command=='status':
                status(db,directory)
                return 0
            return issue(db,directory,a)
        finally:db.close()
    except (RuntimeError,ValueError,sqlite3.Error,OSError,KeyError) as exc:
        p.exit(2,'MANUAL EXIT ERROR: '+str(exc)+'\n')


if __name__=='__main__':
    raise SystemExit(main())
