#!/usr/bin/env python3
"""BlackSwan compact campaign overview. Observer only; no RPC or wallet access.

Reuses the existing receipt/quote snapshot and detail pages. Aggregates are
independent of the recent-event display limit and are read from full journals.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import sys
import time

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
from rich.text import Text

from concentration_monitor import keyboard, nextKey, nextNewRun
from monitor_snapshot import readonly, readSnapshot
from monitor_view import ScrollableContent, renderScreen

DETAILS=("overview","decisions","positions","ledger","alerts")
ACTIVE_PROBES={"LAMB_QUEUED","PROBE_PENDING","PROBE_CONFIRMED","PROBE_UNVERIFIED","QUEUED","BUY_PENDING","BUY_UNVERIFIED"}


def readTotals(directory):
    """All campaign counts, never inferred from a limited recent-history list."""
    directory=Path(directory)
    result=dict(detector=None, decisions=None, pending=None, selectorReasons=None,
                executionReasons=None, probes=None, errors=[])
    for name in ("detector","selector","execution"):
        db=None
        try:
            db=readonly(directory/(name+".sqlite3"))
            if db is None:
                result["errors"].append(name+" journal not present")
                continue
            db.execute("BEGIN")
            if name=="detector":
                result["detector"]=dict(db.execute(
                    "SELECT source,COUNT(*) FROM event_log GROUP BY source"))
            elif name=="selector":
                reasonExpr="CASE WHEN json_valid(data) THEN COALESCE(json_extract(data,'$.reason'),'NOT_RECORDED') ELSE 'INVALID_DECISION_DATA' END"
                rows=[tuple(r) for r in db.execute(
                    "SELECT status,"+reasonExpr+" AS reason,COUNT(*) FROM decisions GROUP BY status,reason ORDER BY COUNT(*) DESC,status,reason")]
                counts=Counter()
                for status,reason,count in rows:
                    counts[status]+=count
                result["decisions"]=dict(counts)
                result["selectorReasons"]=rows
                result["pending"]=db.execute("SELECT COUNT(*) FROM launches WHERE state='PENDING'").fetchone()[0]
            else:
                rows=[tuple(r) for r in db.execute(
                    "SELECT status,COALESCE(reason,'NOT_RECORDED'),COUNT(*) FROM selections GROUP BY status,COALESCE(reason,'NOT_RECORDED')")]
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "excluded_passes" in tables:
                    rows.extend(tuple(r) for r in db.execute(
                        "SELECT 'EXCLUDED',reason,COUNT(*) FROM excluded_passes GROUP BY reason"))
                result["executionReasons"]=sorted(rows,key=lambda r:(-r[2],r[0],r[1]))
                if "lamb_probes" in tables:
                    result["probes"]=dict(db.execute("SELECT COALESCE(result,'NOT_RECORDED'),COUNT(*) FROM lamb_probes GROUP BY COALESCE(result,'NOT_RECORDED')"))
        except (sqlite3.Error,OSError) as exc:
            result["errors"].append(name+" totals unavailable: "+type(exc).__name__)
        finally:
            if db is not None:db.close()
    return result


def val(x):
    return "—" if x is None else str(x)


def money(x):
    try:
        d=Decimal(str(x))
        return f"£{d:.4f}" if d.is_finite() else "—"
    except (InvalidOperation,TypeError,ValueError):
        return "—"


def count(mapping,key):
    return val(mapping.get(key,0)) if mapping is not None else "—"


def stateText(value):
    s=val(value)
    color="green" if s in ("PASS","SOLD","BOUGHT","CONFIRMED","LIVE","LIVE_HEARTBEAT","READY","OPEN") else (
        "red" if any(x in s for x in ("FAIL","ERROR","REVERT","STALE","MISSED","UNVERIFIED","UNCERTAIN","STUCK")) else
        "yellow" if any(x in s for x in ("PENDING","QUEUED","WAIT","UNKNOWN","SKIPPED","EXCLUDED")) else "white")
    return Text(s,style=color)


def cell(text):
    return Text(val(text),overflow="fold")


def title(text):
    return Text(text,style="bold cyan")


def table(headers, ratios=None):
    t=Table(box=box.SIMPLE,expand=True,padding=(0,1),header_style="bold cyan")
    for i,h in enumerate(headers):
        t.add_column(h,ratio=ratios[i] if ratios else None,overflow="fold")
    return t


def reasons(rows):
    t=table(("RESULT","REASON","COUNT"),(2,5,1))
    if rows is None:t.add_row("—","Journal unavailable","—")
    elif not rows:t.add_row("—","None recorded","0")
    else:
        for status,reason,n in rows:
            t.add_row(stateText(status),cell(reason),str(n))
    return t


def holdingsTable(view):
    t=table(("TOKEN (FULL ADDRESS)","STATE","LATEST £ / ×","PEAK £ / ×","QUOTE AGE","EXITS","SOLD %","RECEIVED £","ERROR"),(6,2,2,2,1,3,1,2,3))
    stake=(view.get("policy",{}).get("main") or {}).get("stakeGbp")
    for p in view.get("holdings",[]):
        latest=p.get("latestFullSizeGbp")
        latestMultiple=p.get("latestFullSizeMultiple")
        peak=p.get("peakFullSizeGbp")
        peakMultiple=None
        try:
            if peak is not None and Decimal(str(stake))>0:
                peakMultiple=f"{Decimal(str(peak))/Decimal(str(stake)):.2f}"
        except (InvalidOperation,ValueError,TypeError):pass
        original=p.get("originalQty") or 0
        remain=p.get("remainingMainQty")
        sold="—"
        if original and remain is not None:
            sold=f"{Decimal(original-remain)*100/Decimal(original):.0f}%"
        stages=p.get("exitSlices",[])
        exitText=" | ".join(f"{val(s.get('multiple'))}× {val(s.get('state'))}" for s in stages) or "—"
        # Only qualified receipt GBP; absent qualifiers are not zero proceeds.
        receiptValues=[s.get("realisedGbp") for s in stages if s.get("state")=="SOLD"]
        received="—"
        if receiptValues and all(v is not None for v in receiptValues):
            try:received=money(sum(Decimal(str(v)) for v in receiptValues))
            except InvalidOperation:pass
        age=p.get("quoteAgeSeconds")
        ageText="—" if age is None else f"{int(age)}s"+(" STALE" if p.get("quoteStale") else "")
        t.add_row(cell(p.get("token")),stateText("OPEN" if p.get("isOpen") else "CLOSED"),
            cell(money(latest)+" / "+(val(latestMultiple)+"×" if latestMultiple is not None else "—")),
            cell(money(peak)+" / "+(peakMultiple+"×" if peakMultiple else "—")),
            stateText(ageText),cell(exitText),sold,received,
            stateText(p.get("quoteError") or p.get("lastError") or "—"))
    if not view.get("holdings"):t.add_row("No confirmed main purchases",*["—"]*8)
    return t


def probeTable(view):
    t=table(("TOKEN (FULL ADDRESS)","SACRIFICE / ENTRY","BLOCKS LEFT","TX STATE","REASON"),(5,3,1,2,4))
    rows=[p for p in view.get("sacrificesDetailed",[]) if p.get("status") in ACTIVE_PROBES]
    for p in rows:
        t.add_row(cell(p.get("token")),stateText(p.get("status")),val(p.get("blocksRemaining")),
                  stateText(p.get("txState")),cell(p.get("reason")))
    if not rows:t.add_row("No active sacrifices / queued entries","—","—","—","—")
    return t


def overview(view,width=160):
    h=view.get("health",{}); c=view.get("counts",{}); p=view.get("policy",{})
    f=view.get("finance",{}); totals=view.get("totals",{})
    d=totals.get("detector"); s=totals.get("decisions")
    probes=totals.get("probes"); limits=p.get("limits",{}); main=p.get("main",{})
    total=val(sum(d.values())) if d is not None else "—"
    stamp=datetime.fromtimestamp(view["now"],timezone.utc).strftime("%H:%M:%S UTC")
    banner=Text("BLACKSWAN  /  CAMPAIGN CONTROL   "+stamp+"   READ ONLY",style="bold cyan")
    run=Text(Path(view["directory"]).name+"  |  "+val(view.get("mode")),style="dim")
    detector=Text(f"DETECTOR  {total} events   •   V4 {count(d,'uniswapV4')}   V3 {count(d,'uniswapV3')}   Pons {count(d,'ponsV2')}")
    selector=Text("SELECTOR  ")
    selector.append(f"PASS {count(s,'PASS')}","bold green")
    selector.append(f"   FAIL {count(s,'FAIL')}","bold red")
    selector.append(f"   UNKNOWN {count(s,'UNKNOWN')}   PENDING {val(totals.get('pending'))}","yellow")
    selector.append(f"   revoked {val(h.get('revoked'))}")
    health=Text(f"SERVICES  Detector {val(h.get('detectorStatus'))}   |   Selector {val(h.get('selectorStatus'))}   |   Executor {val(h.get('executorProcessLiveness'))}   |   Intake {val(h.get('intakeState'))}")
    quality=Text(f"Head {val(h.get('detectorHead'))}   |   Gaps {val(h.get('detectorGaps'))}   |   Decode errors {val(h.get('detectorDecodeErrors'))}   |   Backlog {val(h.get('detectorBacklog'))}   |   Watcher {val(h.get('watcherState'))}",style="dim")
    progress=Text(f"PURCHASES {val(c.get('positions'))}/{val(main.get('maxPurchases'))}   |   OPEN {val(c.get('open'))}   |   SACRIFICES SIGNED {val(c.get('sacrificesCommitted'))}/{val(limits.get('maxSacrifices'))}   |   SALES {val(c.get('confirmedSales'))}   |   UNSETTLED {val(c.get('unsettledTx'))}",style="bold white")
    probeStates=Text("SACRIFICE RESULTS  "+("   |   ".join(f"{k}: {v}" for k,v in sorted(probes.items())) if probes else ("None recorded" if probes is not None else "Unavailable")))
    finance=Text(f"MONEY  Committed nominal {money(c.get('nominalCommittedGbp'))} / {money(limits.get('maxNominalSpendGbp'))} (excl. gas)   |   Actual expense {money(f.get('expenseGbp'))}   |   Realised GBP {money(f.get('actualRealisedGbp'))}   |   Cash P/L {money(f.get('cashPnlGbp'))}")
    warnings=list(totals.get("errors",[]))+list(view.get("warnings",[]))
    alertRows=[a for a in view.get("alerts",[]) if a.get("level") in ("CRITICAL","WARNING")]
    alert=Text(f"ALERTS  {len(alertRows)} recorded   |   DATA WARNINGS {len(warnings)}",style="bold red" if warnings or alertRows else "green")
    if warnings:alert.append("   "+str(warnings[0]))
    if alertRows:alert.append("   "+val(alertRows[0].get("message")))
    left=Group(title("SELECTOR REASONS — ALL DECISIONS"),reasons(totals.get("selectorReasons")))
    right=Group(title("EXECUTION OUTCOMES — ALL RECORDED SELECTIONS"),reasons(totals.get("executionReasons")))
    if width>=140:
        grid=Table.grid(expand=True,padding=(0,1))
        grid.add_column(ratio=1);grid.add_column(ratio=1)
        grid.add_row(left,right)
        reasonSection=grid
    else:reasonSection=Group(left,right)
    recent=table(("TOKEN (FULL ADDRESS)","DECISION","LARGEST BUY %","REASON","EXECUTION"),(5,1,1,4,3))
    for row in view.get("opportunities",[])[:6]:
        recent.add_row(cell(row.get("token")),stateText(row.get("status")),
            val(row.get("largestSharePct")),cell(row.get("selectorReason")),cell(row.get("whyNotBought")))
    if not view.get("opportunities"):recent.add_row("No recorded opportunities","—","—","—","—")
    return Group(banner,run,detector,selector,health,quality,progress,probeStates,finance,alert,
                 title("POSITIONS — latest and peak full-entry-size quotes, not remaining liquidation value"),
                 holdingsTable(view),title("ACTIVE SACRIFICES / ENTRY QUEUE"),probeTable(view),
                 reasonSection,title("RECENT SELECTOR ACTIVITY (6; totals above cover whole campaign)"),recent)


def newestRun(base):
    choices=[p.resolve() for p in Path(base).glob("configured_v3_*") if p.is_dir()]
    if not choices:raise SystemExit("No configured campaign found; specify --run-dir")
    return max(choices,key=lambda p:(p.stat().st_mtime,p.name))


def readView(directory):
    view=readSnapshot(directory,limit=120)
    view["totals"]=readTotals(directory)
    return view


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    pick=parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--run-dir",type=Path)
    pick.add_argument("--latest",action="store_true")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    parser.add_argument("--interval",type=float,default=1)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if not .5<=args.interval<=30:parser.error("interval must be .5–30 seconds")
    directory=args.run_dir.expanduser().resolve() if args.run_dir else newestRun(args.data_dir)
    if not directory.is_dir():parser.error("run directory missing")
    console=Console(highlight=False)
    if args.once:
        console.print(overview(readView(directory),console.width))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive terminal required; use --once")
    page="overview";offset=0
    try:
        with keyboard(),Live(console=console,screen=True,auto_refresh=False,
                             redirect_stdout=False,redirect_stderr=False) as live:
            while True:
                view=readView(directory)
                if page=="overview":
                    layout=Layout()
                    layout.split_column(Layout(ScrollableContent(overview(view,console.width),offset),ratio=1),
                        Layout(Text("1 Overview  2 Decisions  3 Positions  4 Ledger  5 Alerts  |  j/k scroll  n/p page  g top  q quit monitor",style="bold cyan"),size=1))
                    display=layout
                else:display=renderScreen(view,page,console.width,console.height,offset)
                live.update(display,refresh=True)
                end=time.monotonic()+args.interval
                while time.monotonic()<end:
                    key=nextKey()
                    if key=="q":return 0
                    if key in ("1","2","3","4","5"):
                        page=DETAILS[int(key)-1];offset=0;break
                    if key in ("j","k","n","p","g"):
                        step=max(1,console.height-5)
                        offset=0 if key=="g" else max(0,offset+{"j":3,"k":-3,"n":step,"p":-step}[key])
                        break
                    time.sleep(.05)
    except KeyboardInterrupt:return 0

if __name__=="__main__":
    raise SystemExit(main())
