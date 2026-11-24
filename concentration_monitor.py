#!/usr/bin/env python3
"""BlackSwan full-screen read-only concentration / buying-block control room.

No keys. No RPC. No wallet. No signing. No subprocess. No DB writes.
Reads the same 3 journals as the original buying-block live_dashboard.py.

Usage:
  .venv/bin/python concentration_monitor.py --run-dir data/configured_v3_...
  .venv/bin/python concentration_monitor.py --latest
  .venv/bin/python concentration_monitor.py --run-dir ... --once
Keys: 1–5 select page; n/p move between blocks; q or Ctrl+C quits.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import select
import sys
import termios
import time
import tty

from rich.console import Console
from rich.live import Live

from monitor_snapshot import readSnapshot
from monitor_view import PAGES,renderReport,renderScreen


def chooseRunDirectory(runDir,latestBase):
    if runDir is not None:
        directory=Path(runDir).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("RUN_DIRECTORY_MISSING: "+str(directory))
        return directory
    if latestBase is None:
        raise ValueError("RUN_DIRECTORY_REQUIRED: use --run-dir or --latest")
    base=Path(latestBase).expanduser().resolve()
    if not base.is_dir():
        raise ValueError("RUN_DIRECTORY_NOT_FOUND")
    choices=[p for pattern in ("configured_v3_*","concentration_shadow_*")
             for p in base.glob(pattern) if p.is_dir()]
    if not choices:
        raise ValueError("RUN_DIRECTORY_NOT_FOUND: no configured/shadow campaign")
    return max(choices,key=lambda p:p.stat().st_mtime)


def nextNewRun(dataDir,seen):
    """Detect the first NEW configured campaign without touching existing runs."""
    base=Path(dataDir).expanduser().resolve()
    if not base.is_dir():
        return None
    matches=[p.resolve() for p in base.glob("configured_v3_*")
             if p.is_dir() and p.resolve() not in seen]
    return min(matches,key=lambda p:(p.stat().st_mtime,p.name)) if matches else None


@contextmanager
def keyboard():
    """Enable one-key page navigation without changing exit behavior."""
    if not sys.stdin.isatty():
        yield False
        return
    fd=sys.stdin.fileno()
    old=termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd,termios.TCSADRAIN,old)


def nextKey():
    if not sys.stdin.isatty():
        return None
    ready,_,_=select.select([sys.stdin],[],[],0)
    if ready:
        return sys.stdin.read(1)
    return None


def parseArgs(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    location=parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--run-dir",type=Path,help="Exact data/configured_v3_* or concentration_shadow_* run directory")
    location.add_argument("--latest",action="store_true",help="Pick newest configured or concentration shadow run; displays full chosen path")
    location.add_argument("--follow-new",action="store_true",help="Start monitor BEFORE arming; attach to the next NEW configured_v3_* campaign")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    parser.add_argument("--view",choices=PAGES,default="overview")
    parser.add_argument("--interval",type=float,default=.5,help="Refresh period seconds (0.25–30, default 0.5)")
    parser.add_argument("--rows",type=int,default=120,help="Maximum journal rows loaded (1–500)")
    parser.add_argument("--stale-after",type=int,default=180,help="Quote staleness threshold in seconds")
    parser.add_argument("--once",action="store_true",help="Print a COMPLETE static report and exit; no TTY required")
    args=parser.parse_args(argv)
    if args.once and args.follow_new:
        parser.error("--follow-new needs an interactive screen, not --once")
    if not .25<=args.interval<=30:
        parser.error("--interval must be between 0.25 and 30")
    if not 1<=args.rows<=500:
        parser.error("--rows must be between 1 and 500")
    if not 5<=args.stale_after<=86400:
        parser.error("--stale-after must be between 5 and 86400 seconds")
    return args


def run(argv=None):
    args=parseArgs(argv)
    waiting=args.follow_new
    followBase=args.data_dir.expanduser().resolve()
    seen={p.resolve() for p in followBase.glob("configured_v3_*") if p.is_dir()}
    try:
        directory=(followBase if waiting else
                   chooseRunDirectory(args.run_dir,args.data_dir if args.latest else None))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    console=Console(highlight=False)
    kwargs=dict(limit=args.rows,staleAfterSeconds=args.stale_after)
    if args.once:
        view=readSnapshot(directory,**kwargs)
        console.print(renderReport(view,width=console.width))
        return 0
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        raise SystemExit("Full-screen mode needs an interactive terminal; use --once for reports")
    screen=args.view
    offset=0
    lastGood=None
    with keyboard():
        try:
            with Live(console=console,screen=True,refresh_per_second=2,
                      auto_refresh=False,redirect_stdout=False,
                      redirect_stderr=False,vertical_overflow="ellipsis") as live:
                while True:
                    t0=time.monotonic()
                    try:
                        if waiting:
                            newRun=nextNewRun(followBase,seen)
                            if newRun is not None:
                                directory=newRun
                                waiting=False
                                lastGood=None
                                offset=0
                        view=readSnapshot(directory,**kwargs)
                        if waiting:
                            view["mode"]="WAITING_FOR_NEW_CAMPAIGN"
                            view["warnings"].append("MONITOR ARMED FOR OBSERVATION ONLY: awaiting a fresh configured_v3_* folder")
                        # A partially read journal must never look like a clean
                        # empty campaign. Keep prior data and visibly mark stale.
                        trouble=[w for w in view["warnings"]
                            if any(x in w.lower() for x in
                               ("unreadable","read error","database is locked","databaseerror"))]
                        if trouble and lastGood:
                            stale=dict(lastGood)
                            stale["warnings"]=list(lastGood["warnings"])+[
                                "STALE MONITOR SNAPSHOT: "+", ".join(trouble)]
                            stale["now"]=time.time()
                            view=stale
                        else:
                            lastGood=view
                    except (OSError,ValueError,RuntimeError) as exc:
                        if lastGood is None:
                            raise
                        view=dict(lastGood)
                        view["warnings"]=list(lastGood["warnings"])+[
                            "MONITOR READ ERROR: "+type(exc).__name__]
                        view["now"]=time.time()
                    live.update(renderScreen(view,screen=screen,
                        width=console.width,height=console.height,offset=offset),
                        refresh=True)
                    end=t0+args.interval
                    while time.monotonic()<end:
                        key=nextKey()
                        if key=="q":
                            return 0
                        if key in ("1","2","3","4","5"):
                            screen=PAGES[int(key)-1]
                            offset=0
                            break
                        if key=="g":
                            offset=0
                            break
                        if key=="n":
                            offset+=max(1,console.height-15)
                            break
                        if key=="p":
                            offset=max(0,offset-max(1,console.height-15))
                            break
                        if key=="j":
                            offset+=3
                            break
                        if key=="k":
                            offset=max(0,offset-3)
                            break
                        time.sleep(min(.06,max(0,end-time.monotonic())))
        except KeyboardInterrupt:
            return 0


if __name__=="__main__":
    raise SystemExit(run())
