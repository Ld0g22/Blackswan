#!/usr/bin/env python3
"""Black Swan 3 — independent widescreen SQLite read-only monitoring.

Does not import wallet/signing modules, open wallet.json, read .env, use RPC,
or modify any campaign database. Ctrl+C exits without stopping execution.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from campaign_config import (FLEX_KIND, FLEX_POLICY, PILOT_KIND, PILOT_POLICY,
    EARLY_KIND, EARLY_POLICY, InvalidOperation)
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def readonly(path):
    if not path.is_file():
        return None
    uri='file:'+quote(str(path.resolve()),safe='/')+'?mode=ro'
    db=sqlite3.connect(uri,uri=True,timeout=.5)
    db.row_factory=sqlite3.Row
    return db


def rows(db,sql,params=()):
    return [dict(r) for r in db.execute(sql,params)]


def value(db,sql,params=(),default=0):
    row=db.execute(sql,params).fetchone()
    return row[0] if row and row[0] is not None else default


def number(raw,default=0):
    try:return int(raw)
    except (TypeError,ValueError):return default


def money(raw):
    try:return Decimal(str(raw))
    except (TypeError,InvalidOperation):return None


def short(address):
    if not address:return '—'
    return address[:8]+'…'+address[-5:] if len(address)>20 else address


def age(ts,now):
    if ts is None or not ts:return '—'
    n=max(0,int(now-float(ts)))
    if n<60:return f'{n}s'
    if n<3600:return f'{n//60}m {n%60:02d}s'
    if n<86400:return f'{n//3600}h {n%3600//60}m'
    return f'{n//86400}d'


def event_age(iso,now):
    if not iso:return 'none'
    try:
        dt=datetime.fromisoformat(iso.replace('Z','+00:00'))
        if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
        return age(dt.timestamp(),now)+' ago'
    except (ValueError,TypeError):return 'unknown'


def snapshot(directory,now=None):
    """Reads committed SQLite records only; missing DBs cause no file creation."""
    now=time.time() if now is None else now
    out=dict(now=now,directory=str(directory),warnings=[],summary={},probes=[],positions=[],
             transactions=[],sales=[],manualFills=[],manualRequests=[],selections=[],selectionCounts={},detector={},selector={})
    opened={}
    try:
        for kind in ('execution','detector','selector'):
            try:
                opened[kind]=readonly(directory/(kind+'.sqlite3'))
                if opened[kind] is None:out['warnings'].append(kind+' database not created yet')
            except (OSError,sqlite3.Error) as exc:
                opened[kind]=None
                out['warnings'].append(kind+' unavailable: '+type(exc).__name__)
        ex=opened['execution']
        if ex:
            try:
                meta={r['key']:r['value'] for r in rows(ex,'SELECT key,value FROM meta')}
                buy=money(meta.get('buyGbp')) or Decimal('.10')
                cap=number(meta.get('maxPurchases'),20)
                newLadder=(meta.get('campaignKind') in ('TWO_50P_2X_LAMB','ONE_20P_2X_LAMB')
                           or (meta.get('campaignKind')==FLEX_KIND
                               and meta.get('exitPolicy')==FLEX_POLICY))
                early=meta.get('campaignKind')==EARLY_KIND and meta.get('exitPolicy')==EARLY_POLICY
                pilot=(meta.get('campaignKind')==PILOT_KIND and meta.get('exitPolicy')==PILOT_POLICY) or early
                sacrificial=meta.get('campaignKind') in ('FIVE_LAMB','ONE_50P_LAMB','TWO_50P_LAMB','TWO_50P_2X_LAMB','ONE_20P_2X_LAMB',FLEX_KIND,PILOT_KIND,EARLY_KIND)
                lastStage=('trigger1000,sold3,sold5,sold1000,soldQty3,soldQty5,soldQty1000'
                           if sacrificial else 'trigger10,sold3,sold5,sold10,soldQty3,soldQty5,soldQty10')
                if sacrificial:
                    out['probes']=rows(ex,'''SELECT p.token,p.confirmedAt,p.probeQty,p.passedAt,
                       p.lastCheck,p.result,p.reason,s.status,t.receipt AS probeReceipt
                       FROM lamb_probes p JOIN selections s ON s.selectionId=p.selectionId
                       LEFT JOIN transactions t ON t.selectionId=p.selectionId
                         AND t.kind='PROBE_BUY'
                       ORDER BY p.confirmedAt DESC LIMIT 8''')
                    for probe in out['probes']:
                        try:
                            raw=json.loads(probe.pop('probeReceipt') or '{}')
                            probe['receiptBlock']=int(raw['blockNumber'],16)
                        except (ValueError,TypeError,KeyError):
                            probe['receiptBlock']=None
                stageColumns=('trigger3,trigger5,0 AS trigger10,sold3,sold5,0 AS sold10,'
                               'soldQty3,soldQty5,0 AS soldQty10' if pilot else
                               'trigger2 AS trigger3,trigger50 AS trigger5,trigger100 AS trigger10,'
                              'sold2 AS sold3,sold50 AS sold5,sold100 AS sold10,'
                              'soldQty2 AS soldQty3,soldQty50 AS soldQty5,soldQty100 AS soldQty10'
                              if newLadder else
                              'trigger3,trigger5,trigger1000 AS trigger10,'
                              'sold3,sold5,sold1000 AS sold10,'
                              'soldQty3,soldQty5,soldQty1000 AS soldQty10'
                              if sacrificial else
                              'trigger3,trigger5,trigger10,sold3,sold5,sold10,'
                              'soldQty3,soldQty5,soldQty10')
                out['positions']=rows(ex,'''SELECT selectionId,token,originalQty,boughtAt,
                  bestGbp,lastCheck,'''+stageColumns+
                  ''',error FROM positions ORDER BY boughtAt DESC''')
                out['selectionCounts']={r['status']:r['count'] for r in rows(ex,
                    'SELECT status,COUNT(*) count FROM selections GROUP BY status')}
                out['selectionCounts']['SKIPPED_V3']=value(ex,
                    "SELECT COUNT(*) FROM excluded_passes WHERE reason='V3_LAUNCH_EXCLUDED'")
                out['selections']=rows(ex,'''SELECT token,status,reason,decisionAt
                    FROM selections ORDER BY decisionAt DESC LIMIT 8''')
                out['transactions']=rows(ex,'''SELECT t.kind,t.state,t.txHash,t.reason,
                    t.createdAt,s.token FROM transactions t
                    LEFT JOIN selections s ON s.selectionId=t.selectionId
                    ORDER BY t.createdAt DESC LIMIT 10''')
                # Confirmed real sale ledger. WETH receipts are verified
                # transfer amounts; native ETH outputs are NOT derivable from
                # the existing SQLite receipts, and must remain unmeasured.
                out['sales']=rows(ex,'''SELECT s.stage,s.txHash,s.tokenDebited,
                     s.wethReceivedWei,s.confirmedAt,p.token
                     FROM sales s LEFT JOIN positions p ON p.selectionId=s.selectionId
                     ORDER BY s.confirmedAt DESC''')
                if meta.get('manualExitVersion')=='1':
                    out['manualFills']=rows(ex,'''SELECT f.requestId,f.selectionId,
                         f.txHash,f.tokenDebited,f.wethReceivedWei,f.gasWei,
                         f.confirmedAt,p.token
                         FROM manual_fills f JOIN positions p ON p.selectionId=f.selectionId
                         ORDER BY f.confirmedAt DESC''')
                    out['manualRequests']=rows(ex,'''SELECT requestId,operation,status,reason,
                         createdAt,txHash FROM manual_requests
                         ORDER BY createdAt DESC LIMIT 5''')
                    bySelection={}
                    for fill in out['manualFills']:
                        bySelection[fill['selectionId']]=(
                            bySelection.get(fill['selectionId'],0)+number(fill['tokenDebited']))
                        out['sales'].append(dict(stage='MANUAL',txHash=fill['txHash'],
                            tokenDebited=fill['tokenDebited'],
                            wethReceivedWei=fill['wethReceivedWei'],
                            confirmedAt=fill['confirmedAt'],token=fill['token']))
                    for position in out['positions']:
                        position['manualSold']=bySelection.get(position['selectionId'],0)
                    out['sales'].sort(key=lambda sale:sale['confirmedAt'],reverse=True)
                    for req in out['manualRequests']:
                        if req['status'] in ('BLOCKED','UNVERIFIED','FAILED'):
                            out['warnings'].append('manual '+req['status']+': '+
                                                   str(req['reason'] or '')[:100])
                verifiedWei=sum(number(r['wethReceivedWei']) for r in out['sales']
                                if r['wethReceivedWei'] is not None)
                unmeasured=sum(r['wethReceivedWei'] is None for r in out['sales'])
                proceeds=Decimal(verifiedWei)/Decimal(10**18)
                byTx={r['state']:r['count'] for r in rows(ex,
                    'SELECT state,COUNT(*) count FROM transactions GROUP BY state')}
                reserved=sum(out['selectionCounts'].get(x,0)
                             for x in ('QUEUED','BUY_PENDING','BUY_UNVERIFIED'))
                bought=len(out['positions'])
                out['summary']=dict(cap=cap,bought=bought,buyGbp=buy,
                    campaignKind=meta.get('campaignKind'),
                    sacrifice=sacrificial,newLadder=newLadder,pilotMode=pilot,earlyMode=early,
                    probeWaitBlocks=number(meta.get('waitBlocks'),0),
                    probesConfirmed=value(ex,"SELECT COUNT(*) FROM lamb_probes WHERE confirmedAt IS NOT NULL")
                        if sacrificial else 0,
                    probesFailed=out['selectionCounts'].get('PROBE_FAILED',0) if sacrificial else 0,
                    probesUnknown=(out['selectionCounts'].get('PROBE_UNKNOWN',0)+out['selectionCounts'].get('PROBE_UNVERIFIED',0)) if sacrificial else 0,
                    probesPassed=value(ex,"SELECT COUNT(*) FROM lamb_probes WHERE passedAt IS NOT NULL")
                        if sacrificial else 0,
                    workerState=meta.get('workerState','ACTIVE'),
                    watcherState=meta.get('venueWatchState','NOT_STARTED'),
                    watcherError=meta.get('venueWatchError',''),
                    intakeState=meta.get('intakeState','OPEN'),
                    committed=buy*bought,reserved=reserved,
                    slots=max(0,cap-bought-reserved),
                    open=sum(((newLadder or not p['sold10']) and
                         (number(p['originalQty'])-sum(number(p['soldQty'+str(x)])
                                  for x in (3,5,10))-number(p.get('manualSold'))>0))
                         for p in out['positions']),
                    sales=len(out['sales']),txStates=byTx,
                    proceedsWeth=proceeds,unmeasuredSales=unmeasured)
                if pilot:
                    finance=(rows(ex,"""SELECT f.txHash,f.kind,f.expenseWei,f.gasWei,f.incomeWei,
                        f.incomeAsset,f.incomeBasis,f.fxGbpPerEth,f.fxAt,f.state,f.reason
                        FROM finance_events f JOIN transactions t
                        ON lower(t.txHash)=lower(f.txHash)
                        WHERE t.state IN ('CONFIRMED','REVERTED') AND t.receipt IS NOT NULL
                        ORDER BY f.recordedAt""") if value(ex,
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='finance_events'") else [])
                    out['financeByHash']={r['txHash'].lower():r for r in finance}
                    totalExpense=Decimal(0)
                    totalIncome=Decimal(0)
                    qualified=0
                    for event in finance:
                        if event['state']!='COMPLETE' or event['fxGbpPerEth'] is None:
                            continue
                        rate=money(event['fxGbpPerEth'])
                        if rate is None:continue
                        totalExpense+=(Decimal(number(event['expenseWei']))+
                                       Decimal(number(event['gasWei'])))*rate/Decimal(10**18)
                        if event['incomeWei'] is not None:
                            totalIncome+=Decimal(number(event['incomeWei']))*rate/Decimal(10**18)
                        qualified+=event['incomeBasis']=='QUALIFIED_BLOCK_BALANCE'
                    settled=rows(ex,"""SELECT txHash FROM transactions
                        WHERE state IN ('CONFIRMED','REVERTED') AND receipt IS NOT NULL""")
                    completeHashes={item['txHash'].lower() for item in finance if item['state']=='COMPLETE'}
                    pending=sum(tx['txHash'].lower() not in completeHashes for tx in settled)
                    out['summary']['finance']=dict(expenditureGbp=totalExpense,returnsGbp=totalIncome,
                        cashPnlGbp=totalIncome-totalExpense,pending=pending,
                        complete=not pending,qualifiedNativeSales=qualified)
                    if pending:
                        out['warnings'].append(str(pending)+' transaction receipt(s) pending GBP reconciliation')
                    if qualified:
                        out['warnings'].append(str(qualified)+' native ETH sale(s) based on qualified block balance deltas')
                errors=[p for p in out['positions'] if p['error']]
                if errors:out['warnings'].append(str(len(errors))+' holding(s) with execution errors')
                unresolved=sum(byTx.get(x,0) for x in ('UNCERTAIN','STUCK','REJECTED'))
                if unresolved:out['warnings'].append(str(unresolved)+' unresolved/rejected transaction(s)')
            except sqlite3.Error as exc:
                out['warnings'].append('execution read error: '+str(exc)[:130])
        de=opened['detector']
        if de:
            try:
                meta={r['key']:r['value'] for r in rows(de,'SELECT key,value FROM meta')}
                out['detector']=dict(status=meta.get('status','unknown'),
                    head=meta.get('lastConnectedHead','—'),
                    total=value(de,'SELECT COUNT(*) FROM event_log'),
                    maxSeq=value(de,'SELECT COALESCE(MAX(seq),0) FROM event_log'),
                    last=value(de,'SELECT stored_at_utc FROM event_log ORDER BY seq DESC LIMIT 1',default=None),
                    gaps=value(de,'SELECT COUNT(*) FROM gaps'),
                    decode=value(de,'SELECT COUNT(*) FROM decode_failures'),
                    usage=value(de,'SELECT COALESCE(SUM(estimate_cu),0) FROM usage WHERE at_seconds>?',(now-86400,)),
                    sources={r['source']:r['count'] for r in rows(de,
                        'SELECT source,COUNT(*) count FROM event_log GROUP BY source')})
            except sqlite3.Error as exc:
                out['warnings'].append('detector read error: '+str(exc)[:130])
        se=opened['selector']
        if se:
            try:
                out['selector']=dict(
                    decisions={r['status']:r['count'] for r in rows(se,
                        'SELECT status,COUNT(*) count FROM decisions GROUP BY status')},
                    launches={r['state']:r['count'] for r in rows(se,
                        'SELECT state,COUNT(*) count FROM launches GROUP BY state')},
                    cursor=number(value(se,"SELECT value FROM meta WHERE key='detectorCursor'",default=0)),
                    last=value(se,'SELECT decisionAt FROM decisions ORDER BY seq DESC LIMIT 1',default=None),
                    revoked=value(se,'SELECT COUNT(*) FROM revocations'))
            except sqlite3.Error as exc:
                out['warnings'].append('selector read error: '+str(exc)[:130])
        if out['selector'] and out['detector']:
            out['selector']['backlog']=max(0,out['detector']['maxSeq']-out['selector']['cursor'])
    finally:
        for db in opened.values():
            if db:db.close()
    return out


def panel(label,body,color='bright_blue'):
    return Panel(body,title=label,title_align='left',border_style=color,
                 padding=(0,1),box=box.ROUNDED)


def mark(trigger,sold):
    if sold:return '[bold green]✓ SOLD[/bold green]'
    if trigger:return '[bold yellow]⚡ DUE[/bold yellow]'
    return '[dim]·[/dim]'


def card_summary(view):
    s=view['summary']
    if not s:return panel('CAMPAIGN','[yellow]Waiting for execution database…[/yellow]','yellow')
    grid=Table.grid(expand=True,padding=(0,2))
    for _ in range(6):grid.add_column(ratio=1,justify='center')
    alerts=sum(s['txStates'].get(x,0) for x in ('STUCK','REJECTED','UNCERTAIN'))
    grid.add_row('[cyan]PURCHASES[/cyan]','[cyan]SPENT (BUYS)[/cyan]',
        '[cyan]FREE SLOTS[/cyan]','[cyan]OPEN POSITIONS[/cyan]',
        '[cyan]CONFIRMED SALES[/cyan]','[cyan]TX ALERTS[/cyan]')
    grid.add_row(f"[bold]{s['bought']} / {s['cap']}[/bold]",
        f"[bold]£{s['committed']:.2f}[/bold]",
        f"[bold]{s['slots']}[/bold] [dim](queued {s['reserved']})[/dim]",
        f"[bold]{s['open']}[/bold]",f"[bold]{s['sales']}[/bold]",
        f"[{'red' if alerts else 'green'}]{alerts}[/{'red' if alerts else 'green'}]")
    return panel('CAMPAIGN SCORECARD',grid,'cyan')



def card_lamb(view):
    s=view['summary']
    buyLabel=('£'+str(s['buyGbp']) if s.get('campaignKind') in (FLEX_KIND,PILOT_KIND,EARLY_KIND) else
              '20P' if s.get('buyGbp')==Decimal('0.20') else
              '50P' if s.get('buyGbp')==Decimal('0.50') else '10P')
    grid=Table.grid(expand=True,padding=(0,2))
    for _ in range(5):grid.add_column(ratio=1,justify='center')
    grid.add_row('[cyan]CONFIRMED 1P[/cyan]','[cyan]FAILED[/cyan]',
                 '[cyan]UNKNOWN[/cyan]','[cyan]SURVIVED[/cyan]',
                 '[cyan]'+buyLabel+' FILLS[/cyan]')
    grid.add_row(str(s['probesConfirmed']),str(s['probesFailed']),str(s['probesUnknown']),
                 str(s['probesPassed']),f"{s['bought']}/{s['cap']}")
    grid.add_row('[cyan]PROBE SPENT[/cyan]','[cyan]INTAKE[/cyan]',
                 '[cyan]SOURCES[/cyan]','[cyan]HELD VENUES[/cyan]',
                 '[cyan]'+buyLabel+' SPENT[/cyan]')
    grid.add_row('£'+str(Decimal('.01')*s['probesConfirmed']),s['intakeState'],
                 s['workerState'],s['watcherState'],f"£{s['committed']:.2f}")
    t=Table(expand=True,box=box.SIMPLE,show_edge=False,padding=(0,1))
    for name in ('TOKEN','PROBE','GATE','REASON'):t.add_column(name,no_wrap=True)
    for p in view['probes'][:5]:
        if p['status']=='PROBE_CONFIRMED':
            if s.get('probeWaitBlocks')==500:
                try:
                    chainHead=int(str(view['detector']['head']),0)
                    if chainHead < 0:raise ValueError('invalid head')
                except (KeyError,TypeError,ValueError):
                    try:chainHead=int(str(view['detector']['head']))
                    except (KeyError,TypeError,ValueError):chainHead=None
                start=p.get('receiptBlock')
                if start is None:
                    gate='RECEIPT BLOCK UNKNOWN'
                elif chainHead is None:
                    gate='WAIT 500 BLOCKS • HEAD UNKNOWN'
                else:
                    remaining=max(0,start+500-chainHead)
                    gate=(str(remaining)+' blocks left (detector)'
                          if remaining else 'CHECK DUE (detector head)')
            else:
                seconds=max(0,int(float(p['confirmedAt'])+10-view['now']+0.999))
                gate=(str(seconds)+'s left') if seconds else 'CHECK DUE'
        elif p['passedAt'] is not None or p['status'] in ('QUEUED','BUY_PENDING','BOUGHT'):
            gate='PASSED'
        else:gate=p['status']
        t.add_row(p['token'],age(p['confirmedAt'],view['now']),
                  gate,str(p['reason'] or '—')[:45])
    if not view['probes']:t.add_row('No confirmed probes','—','—','—')
    if s['watcherState']=='DEGRADED':
        t.add_row('Watcher warning','—','DEGRADED',str(s['watcherError'])[:45])
    return panel('SACRIFICIAL LAMB • RECEIPT-CONFIRMED',Group(grid,t),'cyan')


def card_positions(view):
    t=Table(expand=True,box=box.SIMPLE_HEAVY,show_edge=False,
            header_style='bold cyan',padding=(0,1))
    lamb=bool(view['summary'].get('sacrifice'))
    newLadder=bool(view['summary'].get('newLadder'))
    pilot=bool(view['summary'].get('pilotMode'))
    columns=([('1.5× 80%',{}),('3× 20%',{})] if view['summary'].get('earlyMode') else
              [('3× 80%',{}),('5× 20%',{})] if pilot else
              [('2× 80%',{}),('50× 10%',{}),('100× 5%',{})]
             if newLadder else
             [('3× 80%',{}),('5× 10%' if lamb else '5× 15%',{}),
              ('1,000× 10%' if lamb else '10× REST',{})])
    for heading,kwargs in ([('TOKEN',{}),('AGE',{}),('BEST £',dict(justify='right')),
       ('BEST ×',dict(justify='right'))]+columns+
       [('SOLD %',dict(justify='right')),('QUOTE AGE',dict(justify='right'))]):
        t.add_column(heading,no_wrap=True,**kwargs)
    s=view['summary'];buy=s.get('buyGbp',Decimal('.10'))
    for p in view['positions']:
        best=money(p['bestGbp'])
        multiple=best/buy if best is not None and buy>0 else None
        original=number(p['originalQty'])
        sold=sum(number(p['soldQty'+str(x)]) for x in (3,5,10))+number(p.get('manualSold'))
        percent=Decimal(sold)*100/original if original else None
        t.add_row(p['token'],age(p['boughtAt'],view['now']),
            f'£{best:.2f}' if best is not None else '—',
            f'{multiple:.2f}×' if multiple is not None else '—',
            mark(p['trigger3'],p['sold3']),mark(p['trigger5'],p['sold5']),
            *([] if pilot else [mark(p['trigger10'],p['sold10'])]),
            f'{percent:.1f}%' if percent is not None else '—',
            age(p['lastCheck'],view['now']),
            style='red' if p['error'] else None)
    if not view['positions']:t.add_row('No confirmed buys',*(['—']*(7 if pilot else 8)))
    return panel('POSITIONS • BEST = HISTORICAL FULL-SIZE SELL QUOTE, NOT CURRENT WALLET VALUE',t)


def card_proceeds(view):
    """Actual confirmed WETH receipts, never quote-derived sale estimates.

    When a sale pays native ETH, the existing receipt journal verifies token
    debit but does not measure incoming native ETH. Show UNKNOWN, not £0.
    """
    sales=view['sales']
    t=Table(expand=True,box=box.SIMPLE,show_edge=False,
            header_style='bold cyan',padding=(0,1))
    t.add_column('AGE',no_wrap=True)
    t.add_column('TOKEN',no_wrap=True)
    t.add_column('EXIT',no_wrap=True)
    t.add_column('ACTUAL RECEIVED',justify='right',no_wrap=True)
    pilot=view['summary'].get('pilotMode')
    if pilot:t.add_column('GBP EQUIVALENT',justify='right',no_wrap=True)
    for sale in sales[:6]:
        finance=view.get('financeByHash',{}).get(sale['txHash'].lower()) if pilot else None
        raw=sale['wethReceivedWei']
        paid=(Decimal(number(raw))/Decimal(10**18)) if raw is not None else None
        received=f'[green]{paid:.8f} WETH[/green]' if paid is not None else '[yellow]ETH payout unmeasured[/yellow]'
        if finance and finance['state']=='COMPLETE' and finance['incomeWei'] is not None:
            units=Decimal(number(finance['incomeWei']))/Decimal(10**18)
            received=f'[green]{units:.8f} {finance["incomeAsset"]}[/green]'
            gbpValue=units*money(finance['fxGbpPerEth'])
            gbp=f'£{gbpValue:.4f}'+(' *' if finance['incomeBasis']=='QUALIFIED_BLOCK_BALANCE' else '')
        else:
            gbp='[yellow]UNMEASURED[/yellow]'
        t.add_row(age(sale['confirmedAt'],view['now']),sale['token'],
                  ({'3':'1.5×','5':'3×'}.get(str(sale['stage']),str(sale['stage'])+'×')
                    if view['summary'].get('earlyMode') and isinstance(sale['stage'],int)
                    else str(sale['stage'])+'×' if isinstance(sale['stage'],int)
                    else 'MANUAL'),received,*([gbp] if pilot else []))
    if not sales:
        t.add_row('—','No confirmed sales','—','—',*(['—'] if pilot else []))
    summary=view['summary']
    known=summary.get('proceedsWeth',Decimal('0'))
    missing=summary.get('unmeasuredSales',0)
    footer=(f'[bold]* Native ETH payouts are qualified block balance credits, not transaction traces[/bold]'
            if pilot else f'[bold]Verified total: {known:.8f} WETH[/bold]')
    if missing and not pilot:
        footer+=f'    [yellow]{missing} sale(s) with native/unmeasured proceeds — full total unavailable[/yellow]'
    elif not pilot:
        footer+='    [dim]gross received, before gas; not GBP profit[/dim]'
    return panel('CONFIRMED SALE PROCEEDS • ACTUAL RECEIPTS',
                 Group(t,Text.from_markup(footer)),'green')


def card_finances(view):
    f=view.get('summary',{}).get('finance')
    if not f:return panel('CAMPAIGN FINANCES','[yellow]Receipt accounting unavailable[/yellow]','yellow')
    grid=Table.grid(expand=True,padding=(0,2))
    for _ in range(3):grid.add_column(ratio=1,justify='center')
    grid.add_row('[cyan]EXPENDITURE[/cyan]','[cyan]RETURNS[/cyan]','[cyan]CASH P/L[/cyan]')
    suffix=' [yellow](partial)[/yellow]' if not f['complete'] else ''
    pnl=f['cashPnlGbp']
    color='green' if pnl>=0 else 'red'
    grid.add_row(f"[bold]£{f['expenditureGbp']:.4f}[/bold]"+suffix,
                 f"[bold]£{f['returnsGbp']:.4f}[/bold]"+suffix,
                 f"[bold {color}]{pnl:+.4f} GBP[/bold {color}]"+suffix)
    note=('Confirmed buy/probe ETH and all confirmed transaction gas included. '
          'Open tokens excluded from cash P/L. GBP is valued at reconciliation-time ETH/GBP.')
    if f['qualifiedNativeSales']:
        note+=' * '+str(f['qualifiedNativeSales'])+' native sale payout(s) use qualified block balances.'
    if f['pending']:
        note+=' '+str(f['pending'])+' receipt(s) remain unmeasured; totals are incomplete.'
    return panel('CAMPAIGN FINANCES • CONFIRMED CASH FLOWS',Group(grid,Text(note,style='dim')),'cyan')


def card_transactions(view):
    t=Table(expand=True,box=box.SIMPLE,show_edge=False,padding=(0,1),header_style='bold cyan')
    for heading in ('AGE','ACTION','STATE','TOKEN'):t.add_column(heading,no_wrap=True)
    for r in view['transactions'][:9]:
        state=r['state'];color=('red' if state in ('STUCK','REJECTED','UNCERTAIN','REVERTED')
            else 'green' if state=='CONFIRMED' else 'yellow')
        kind=({'SELL_3':'SELL 80% @1.5×','SELL_5':'SELL 20% @3×'}.get(r['kind'],r['kind'])
              if view['summary'].get('earlyMode') else r['kind'])
        t.add_row(age(r['createdAt'],view['now']),kind,
            f'[{color}]{state}[/{color}]',r['token'] or r['txHash'])
    if not view['transactions']:t.add_row('—','No transactions','—','—')
    return panel('TRANSACTION JOURNAL  •  RECENT RECEIPT STATES',t,'magenta')


def card_selections(view):
    t=Table(expand=True,box=box.SIMPLE,show_edge=False,padding=(0,1),header_style='bold cyan')
    for heading in ('AGE','TOKEN','STATE','REASON'):t.add_column(heading,no_wrap=True)
    for r in view['selections']:
        t.add_row(age(r['decisionAt'],view['now']),r['token'],
                  r['status'],str(r['reason'] or '—')[:48])
    if not view['selections']:t.add_row('—','No decisions consumed','—','—')
    return panel('LATEST EXECUTION DECISIONS',t,'bright_blue')


def card_pipeline(view):
    d=view['detector'];sel=view['selector'];counts=view['selectionCounts']
    t=Table.grid(expand=True,padding=(0,2))
    t.add_column(ratio=1);t.add_column(ratio=1)
    t.add_row('[bold cyan]DETECTOR[/bold cyan]','[bold cyan]SELECTOR / EXECUTION[/bold cyan]')
    retired=view['summary'].get('sacrifice') and view['summary'].get('workerState')=='RETIRED'
    t.add_row(f"Events: {d.get('total','—')} | {'RETIRED (frozen)' if retired else 'Reported: '+str(d.get('status','—'))}",
              f"PASS: {sel.get('decisions',{}).get('PASS',0)}  FAIL: {sel.get('decisions',{}).get('FAIL',0)}  UNKNOWN: {sel.get('decisions',{}).get('UNKNOWN',0)}")
    sources=d.get('sources',{})
    t.add_row('  '.join(f'{x}: {sources.get(x,0)}' for x in ('uniswapV4','uniswapV3','ponsV2')),
              f"Queued: {counts.get('QUEUED',0)}  Buy pending: {counts.get('BUY_PENDING',0)}")
    t.add_row(f"Head: {d.get('head','—')} | {'frozen source' if retired else 'last event '+event_age(d.get('last'),view['now'])}",
              f"Skipped Pons: {counts.get('SKIPPED_PONS',0)}  v3: {counts.get('SKIPPED_V3',0)}")
    t.add_row(f"Gaps: {d.get('gaps','—')}  Decode errors: {d.get('decode','—')}",
              f"Detector→selector backlog: {sel.get('backlog','—')}  Revocations: {sel.get('revoked','—')}")
    cu=d.get('usage')
    t.add_row(f"Alchemy estimated CU/24h: {int(cu):,} / 1,000,000" if cu is not None else 'Alchemy CU: —',
              f"Bought selections: {counts.get('BOUGHT',0)}  Skipped: {counts.get('SKIPPED',0)}")
    return panel('PIPELINE HEALTH • DETECTOR & SELECTOR UNCHANGED',t,'bright_green')


def card_alerts(view):
    warnings=list(view['warnings'][:5])
    summary=view.get('summary',{})
    if summary.get('sacrifice') and summary.get('intakeState')=='CLOSED' and summary.get('watcherState')!='READY':
        warnings.append('Held-token watcher '+summary.get('watcherState','UNKNOWN')+
                        ': '+str(summary.get('watcherError',''))[:95])
    for p in view['positions']:
        if p['error']:
            warnings.append(short(p['token'])+': '+str(p['error'])[:110])
    for r in view['transactions']:
        if r['state'] in ('REJECTED','STUCK','UNCERTAIN','REVERTED'):
            warnings.append(r['kind']+' '+short(r['txHash'])+': '+str(r['reason'] or r['state'])[:100])
    if not warnings:warnings=['No recorded problems in these database snapshots.']
    return panel('WARNINGS • CHECK IF ACTION NEEDED',
        '\n'.join('[yellow]•[/yellow] '+x for x in warnings[:9]),
        'red' if len(warnings)>1 or view['warnings'] else 'green')


def render(view,width=160):
    name=Path(view['directory']).name
    timestamp=datetime.fromtimestamp(view['now'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    banner=panel('BLACK SWAN 3   •   EXECUTION CONTROL ROOM',
        '[bold]'+name+'[/bold]    [cyan]'+timestamp+'[/cyan]    '
        '[bold green]READ ONLY[/bold green] • No wallet/RPC access','cyan')
    content=[banner,card_summary(view)]
    if view['summary'].get('pilotMode'):content.append(card_finances(view))
    if view['summary'].get('sacrifice'):content.append(card_lamb(view))
    content.extend([card_positions(view),card_proceeds(view)])
    content.extend([card_transactions(view),card_selections(view)])
    content.extend([card_pipeline(view),card_alerts(view)])
    content.append(Text('Ctrl+C to exit   •   refresh every 2s   •   historical quotes are not realised profits',
                        style='dim'))
    return Group(*content)


def pick_directory(args):
    if args.run_dir:return args.run_dir.expanduser().resolve()
    base=args.data_dir.expanduser().resolve()
    candidates=sorted(list(base.glob('v4_lamb_flex1p5x3x_*'))+
                      list(base.glob('v4_lamb_flex3x5x_*'))+
                       list(base.glob('v4_lamb_flex2x_*'))+
                      list(base.glob('v4_lamb_1x20p_2x_*'))+
                      list(base.glob('v4_lamb_2x50p_*'))+
                      list(base.glob('v4_lamb_1x50p_*'))+
                      list(base.glob('v4_lamb_5x10p_*'))+
                      list(base.glob('v4_peak_20x10p_ladder_*')),
                      key=lambda x:x.stat().st_mtime,reverse=True)
    if not candidates:
        raise SystemExit('No campaign directory found. Supply --run-dir data/<actual run directory>')
    return candidates[0]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    parser.add_argument('--interval',type=float,default=2.0)
    parser.add_argument('--once',action='store_true',help='Print one static snapshot')
    args=parser.parse_args(argv)
    if not .5<=args.interval<=30:parser.error('--interval must be between 0.5 and 30')
    directory=pick_directory(args)
    if not directory.is_dir():parser.error('Campaign directory does not exist')
    console=Console()
    if args.once:
        console.print(render(snapshot(directory),console.width))
        return
    try:
        with Live(console=console,screen=True,refresh_per_second=2) as live:
            while True:
                live.update(render(snapshot(directory),console.width),refresh=True)
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__=='__main__':
    main()
