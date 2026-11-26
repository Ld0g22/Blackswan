#!/usr/bin/env python3
"""BlackSwan manual exit console. Never holds keys, signs or broadcasts."""
import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
import manual_sale
from manual_exit_engine import reserveRemaining

ACTIVE=manual_sale.ACTIVE
DONE=('PREVIEW_READY','PREVIEW_FAILED','CONFIRMED','FAILED','BLOCKED','UNVERIFIED','CANCELLED','EXPIRED')

def meta(db):
    return dict(db.execute("SELECT key,value FROM meta"))

def live(metadata):
    return 0<=time.time()-float(metadata.get('manualExitHeartbeat') or 0)<=20

def controlled(metadata):
    return (live(metadata) and metadata.get('manualControlVersion')=='1'
            and metadata.get('manualControlHeartbeat')==metadata.get('manualExitHeartbeat'))

def positions(db):
    m=meta(db)
    result=[]
    for p in db.execute("SELECT * FROM positions ORDER BY boughtAt,selectionId").fetchall():
        remaining=manual_sale.remainingMain(db,p)
        if remaining<=0:continue
        reserve=reserveRemaining(db,p,remaining) if m.get('campaignKind')=='CONFIGURED_V3' else None
        original=int(p['originalQty'])
        result.append(dict(selectionId=p['selectionId'],token=p['token'],remaining=remaining,
                           original=original,reserve=reserve,bestGbp=p['bestGbp'],
                           stake=m.get('buyGbp'),error=p['error']))
    return result

def discover(root):
    matches=[]
    for path in sorted(root.glob('configured_v3_*')):
        try:
            db,m=manual_sale.connect(path,readOnly=True)
            try:
                if live(m) and positions(db):matches.append(path)
            finally:db.close()
        except (RuntimeError,ValueError,OSError,sqlite3.Error,KeyError):continue
    if len(matches)!=1:
        raise RuntimeError('NO_UNIQUE_LIVE_CAMPAIGN: use --run-dir (matches '+str(len(matches))+')')
    return matches[0]

def enqueue(db,selectionId,operation,percent,minimum,slippage,reserveOnly,ttl,allowLegacy=False):
    if operation not in ('PREVIEW','SELL'):raise ValueError('INVALID_OPERATION')
    if type(percent) is not int or not 1<=percent<=100:raise ValueError('PERCENT_MUST_BE_1_TO_100')
    if type(slippage) is not int or not 0<=slippage<=1000:raise ValueError('SLIPPAGE_BPS_MUST_BE_0_TO_1000')
    if type(ttl) is not int or not 5<=ttl<=600:raise ValueError('TTL_SECONDS_MUST_BE_5_TO_600')
    try:minimum=Decimal(minimum)
    except (InvalidOperation,TypeError):raise ValueError('INVALID_MIN_GBP') from None
    if not minimum.is_finite() or minimum<0:raise ValueError('INVALID_MIN_GBP')
    requestId=uuid4().hex
    db.execute('BEGIN IMMEDIATE')
    try:
        m=meta(db)
        if not live(m):raise RuntimeError('EXECUTOR_NOT_LIVE')
        new=controlled(m)
        if not new and not allowLegacy:raise RuntimeError('LEGACY_EXECUTOR: use --legacy explicitly; no deadline or cancellation')
        p=next((p for p in positions(db) if p['selectionId']==selectionId),None)
        if not p:raise RuntimeError('NO_REMAINING_MAIN_POSITION')
        if reserveOnly:
            if not p['reserve']:raise RuntimeError('NO_KNOWN_REMAINING_RESERVE')
            if not new and p['remaining']!=p['reserve']:raise RuntimeError('EXACT_RESERVE_REQUIRES_UPGRADED_EXECUTOR')
            percent=100
        if operation=='SELL' and db.execute(
                "SELECT 1 FROM manual_requests WHERE selectionId=? AND operation='SELL' AND status IN ("+
                ','.join('?' for _ in ACTIVE)+")",(selectionId,*ACTIVE)).fetchone():
            raise RuntimeError('MANUAL_SALE_ALREADY_ACTIVE')
        now=time.time()
        db.execute("""INSERT INTO manual_requests(requestId,selectionId,operation,percent,minGbp,slippageBps,
                     status,createdAt,updatedAt) VALUES(?,?,?,?,?,?,'QUEUED',?,?)""",
                   (requestId,selectionId,operation,percent,str(minimum),slippage,now,now))
        if new:db.execute('INSERT INTO manual_request_controls VALUES(?,?,?)',(requestId,now+ttl,int(reserveOnly)))
        db.execute('COMMIT')
    except BaseException:
        if db.in_transaction:db.execute('ROLLBACK')
        raise
    return requestId

def cancel(db,requestId):
    db.execute('BEGIN IMMEDIATE')
    try:
        if not controlled(meta(db)):raise RuntimeError('CANCELLATION_REQUIRES_LIVE_UPGRADED_EXECUTOR')
        row=db.execute("SELECT 1 FROM manual_request_controls WHERE requestId=?",(requestId,)).fetchone()
        if not row:raise RuntimeError('CANNOT_CANCEL_LEGACY_REQUEST')
        prefix='manual:approve:'+requestId+':'
        changed=db.execute("UPDATE manual_requests SET status='CANCELLED',updatedAt=?,reason='OPERATOR_CANCELLED' "
            "WHERE requestId=? AND status='QUEUED' AND NOT EXISTS (SELECT 1 FROM transactions "
            "WHERE intentId=? OR substr(intentId,1,length(?))=?)",
            (time.time(),requestId,'manual:sell:'+requestId,prefix,prefix)).rowcount
        db.execute('COMMIT')
        return changed==1
    except BaseException:
        if db.in_transaction:db.execute('ROLLBACK')
        raise

def show(db,directory):
    m=meta(db)
    print('CAMPAIGN',directory,'| EXECUTOR', 'LIVE' if live(m) else 'OFFLINE',
          '| CONTROLS','EXPIRY/CANCEL' if controlled(m) else 'LEGACY (no expiry/cancel)')
    result=positions(db)
    for i,p in enumerate(result,1):
        print(f"{i}. {p['token']} | stake GBP {p['stake']} | historical full-size peak GBP {p['bestGbp'] or 'unknown'}")
        print(f"   Remaining {Decimal(p['remaining'])*100/p['original']:.2f}% ({p['remaining']} raw units) | reserve {p['reserve']} raw units | selection {p['selectionId']}")
        if p['error']:print('   ERROR',p['error'])
    if not result:print('NO REMAINING MAIN POSITIONS')
    return result

def waitRequest(db,directory,requestId,seconds=30):
    print('REQUEST ACCEPTED (not a fill)',requestId,flush=True)
    end=time.monotonic()+seconds
    while True:
        row=db.execute('SELECT * FROM manual_requests WHERE requestId=?',(requestId,)).fetchone()
        if row['status'] in DONE or time.monotonic()>=end:
            manual_sale.display(row,db,directory)
            if row['status'] not in DONE:print('REQUEST STILL ACTIVE: inspect status; do not duplicate')
            return row['status']
        time.sleep(.25)

def menu(db,directory,args):
    while True:
        result=show(db,directory)
        value=input('Position number, r=refresh, q=quit: ').strip().lower()
        if value=='q':return
        if value=='r':continue
        try:
            index=int(value)
            if not 1<=index<=len(result):raise ValueError()
            p=result[index-1]
        except ValueError:
            print('Invalid position');continue
        action=input('Amount: all, reserve, or 1–100 percent of remaining (q=back): ').strip().lower()
        if action=='q':continue
        try:
            percent=100 if action in ('all','reserve') else int(action)
            sid=p['selectionId']
            print('TOKEN',p['token'])
            request=enqueue(db,sid,'PREVIEW',percent,'0',args.slippage_bps,action=='reserve',args.ttl,args.legacy)
            if waitRequest(db,directory,request)!='PREVIEW_READY':continue
            minimum=input('Minimum GBP proceeds (0 allowed, blank=cancel): ').strip()
            if not minimum:continue
            if input('Submit manual sale? [y/N] ').strip().lower()!='y':continue
            request=enqueue(db,sid,'SELL',percent,minimum,args.slippage_bps,action=='reserve',args.ttl,args.legacy)
            waitRequest(db,directory,request)
        except (ValueError,RuntimeError,sqlite3.Error) as exc:print('REQUEST REFUSED:',exc)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('menu','list','status','preview','sell','cancel'),default='menu',nargs='?')
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent/'data')
    parser.add_argument('--selection-id')
    parser.add_argument('--request-id')
    parser.add_argument('--percent',type=int,default=100)
    parser.add_argument('--reserve',action='store_true')
    parser.add_argument('--min-gbp')
    parser.add_argument('--slippage-bps',type=int,default=1000)
    parser.add_argument('--ttl',type=int,default=60)
    parser.add_argument('--legacy',action='store_true',help='Explicitly permit existing executor protocol; no expiry/cancel')
    args=parser.parse_args()
    try:
        directory=args.run_dir.expanduser().resolve() if args.run_dir else discover(args.data_dir.expanduser().resolve())
        db,m=manual_sale.connect(directory,readOnly=args.command in ('list','status'))
        try:
            if args.command=='list':show(db,directory);return 0
            if args.command=='status':manual_sale.status(db,directory);return 0
            if args.command=='cancel':
                if not args.request_id:raise ValueError('REQUEST_ID_REQUIRED')
                changed=cancel(db,args.request_id)
                print('CANCELLED' if changed else 'NOT CANCELLED: request already preparing, signed or terminal')
                return 0 if changed else 2
            if args.legacy and not controlled(m):
                print('WARNING: legacy requests may remain active indefinitely; do not repeat or stop executor to cancel.')
            if args.command=='menu':menu(db,directory,args);return 0
            if not args.selection_id:raise ValueError('EXPLICIT_SELECTION_ID_REQUIRED')
            if args.command=='sell' and args.min_gbp is None:raise ValueError('EXPLICIT_MIN_GBP_REQUIRED')
            request=enqueue(db,args.selection_id,args.command.upper(),args.percent,args.min_gbp or '0',
                            args.slippage_bps,args.reserve,args.ttl,args.legacy)
            state=waitRequest(db,directory,request)
            return 0 if state in ('PREVIEW_READY','CONFIRMED') else 2
        finally:db.close()
    except (ValueError,RuntimeError,OSError,sqlite3.Error,KeyError) as exc:
        print('MANUAL EXIT ERROR:',exc)
        return 2
    except (KeyboardInterrupt,EOFError):
        print('Console closed; queued/signed requests are not cancelled.')
        return 130

if __name__=='__main__':raise SystemExit(main())
