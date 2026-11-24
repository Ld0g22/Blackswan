#!/usr/bin/env python3
"""Read-only live concentration shadow audit. No wallet, signer, or trade executor.

Starts the original detector and the interchangeable concentration selector
only. Persists actual provider logs and decisions for later D2/D3 review.
Never sends transactions. Never runs alongside a live BlackSwan executor.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from math import ceil
import json
import queue
import sqlite3
import time

from campaign_config import otherLiveExecutors
from tools.setup_new_alchemy import SOURCE_DEFAULT,requireSameNewCredentials
from run_configured_campaign import selectorReady
from run_fresh_peaks import (
    detectorReady,drain,newExperimentId,startWorker,stopWorkers
)


def buildShadowCommands(root,python,env,directory,firstTradeTimeoutSeconds=300):
    root,python,env,directory=map(Path,(root,python,env,directory))
    if type(firstTradeTimeoutSeconds) is not int or not 30<=firstTradeTimeoutSeconds<=3600:
        raise ValueError("INVALID_FIRST_TRADE_TIMEOUT")
    detectorDb=directory/"detector.sqlite3"
    selectorDb=directory/"selector.sqlite3"
    return {
        "detector":[str(python),"-u",str(root/"detector.py"),"run",
                    "--env",str(env),"--db",str(detectorDb)],
        "selector":[str(python),"-u",str(root/"concentration_selector.py"),"run",
                    "--env",str(env),"--detector-db",str(detectorDb),
                    "--db",str(selectorDb),
                    "--window-seconds","30","--threshold-bps","2500",
                    "--first-trade-timeout-seconds",str(firstTradeTimeoutSeconds)],
    }


def shadowSummary(directory):
    directory=Path(directory)
    initial=dict(
        evidence="NO_CHAIN_DATA",
        chainIdReported=None,
        readOnlyChainValidated=False,
        source="DETECTOR_AND_CONCENTRATION_SELECTOR_ONLY",
        decisions={},swapEvidenceRows=0,
        coverageCompleteDecisions=0,
        observationErrors=[],
        decisionLagSeconds=[],
        decisionLatency={"count":0,"p50":None,"p95":None,"p99":None,"worst":None},
    )
    selectorDb=directory/"selector.sqlite3"
    if not selectorDb.is_file():
        return initial
    try:
        with sqlite3.connect(f"file:{selectorDb.resolve()}?mode=ro",uri=True,timeout=3) as db:
            metadata=dict(db.execute("SELECT key,value FROM meta"))
            initial["chainIdReported"]=metadata.get("selectorVerifiedChainId")
            initial["decisions"]=dict(db.execute(
                "SELECT status,COUNT(*) FROM decisions GROUP BY status"))
            initial["swapEvidenceRows"]=db.execute(
                "SELECT COUNT(*) FROM concentration_swaps").fetchone()[0]
            recorded=db.execute(
                "SELECT data FROM decisions ORDER BY seq DESC LIMIT 1000").fetchall()
            valid=0
            lags=[]
            for (encoded,) in recorded:
                payload=json.loads(encoded)
                if payload.get("coverageComplete") is True:
                    valid+=1
                lag=payload.get("decisionLagSeconds")
                if type(lag) in (float,int) and lag>=0:
                    lags.append(round(float(lag),3))
            initial["coverageCompleteDecisions"]=valid
            initial["decisionLagSeconds"]=lags[:50]
            ordered=sorted(lags)
            def rank(percentile):
                return ordered[max(0,ceil(len(ordered)*percentile)-1)] if ordered else None
            initial["decisionLatency"]={
                "count":len(ordered),
                "p50":rank(.50),
                "p95":rank(.95),
                "p99":rank(.99),
                "worst":ordered[-1] if ordered else None,
            }
            initial["observationErrors"]=[
                {"candidate":row[0],"reason":row[1][:90]}
                for row in db.execute(
                    "SELECT eventKey,lastError FROM concentration_progress "
                    "WHERE lastError IS NOT NULL ORDER BY nextBlock DESC LIMIT 20")]
            initial["readOnlyChainValidated"]=bool(
                metadata.get("selectorVerifiedChainId")=="4663"
                and initial["swapEvidenceRows"]>0
                and valid>0)
            initial["evidence"]=(
                "PROVIDER_SWAP_LOGS_CAPTURED_REVIEW_REQUIRED"
                if initial["readOnlyChainValidated"]
                else "CHAIN_DATA_INCOMPLETE_NOT_READY")
    except (sqlite3.Error,ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
        initial["evidence"]="UNREADABLE_CHAIN_EVIDENCE"
        initial["observationErrors"].append({"reason":type(exc).__name__})
        initial["readOnlyChainValidated"]=False
    return initial


def runShadow(args):
    if args.env is None:
        raise RuntimeError("EXPLICIT_NEW_ALCHEMY_ENV_REQUIRED: supply --env")
    requireSameNewCredentials(args.env,args.credentials_source)
    if otherLiveExecutors():
        raise RuntimeError("LIVE_EXECUTOR_PRESENT_READ_ONLY_AUDIT_REFUSED")
    root=Path(__file__).resolve().parent
    python=root/".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("REPOSITORY_PYTHON_VENV_MISSING")
    env=args.env.expanduser().resolve()
    if not env.is_file():
        raise RuntimeError("PROVIDER_ENV_FILE_MISSING")
    directory=args.data_dir.expanduser().resolve()/(
        "concentration_shadow_"+newExperimentId())
    directory.mkdir(parents=True,exist_ok=False)
    commands=buildShadowCommands(root,python,env,directory,
                                 args.first_trade_timeout_seconds)
    messages=queue.Queue()
    workers=[]
    err=None
    print("READ ONLY: NO WALLET, NO SIGNER, NO ORDERS",flush=True)
    print("CAPTURE FOLDER",directory,flush=True)
    try:
        detector=startWorker("DETECTOR",commands["detector"],messages)
        workers.append(detector)
        start=time.monotonic()
        while not detectorReady(directory/"detector.sqlite3"):
            drain(messages)
            if detector.poll() is not None or time.monotonic()-start>=25:
                raise RuntimeError("DETECTOR_NOT_READY")
            time.sleep(.1)
        selector=startWorker("CONCENTRATION",commands["selector"],messages)
        workers.append(selector)
        start=time.monotonic()
        while not selectorReady(directory/"selector.sqlite3"):
            drain(messages)
            if selector.poll() is not None or time.monotonic()-start>=20:
                raise RuntimeError("SELECTOR_NOT_CHAIN_VALIDATED")
            time.sleep(.1)
        until=time.monotonic()+args.duration_seconds
        while time.monotonic()<until:
            drain(messages)
            if any(worker.poll() is not None for worker in workers):
                raise RuntimeError("READ_ONLY_WORKER_EXITED_EARLY")
            time.sleep(.25)
    except KeyboardInterrupt:
        print("READ ONLY AUDIT INTERRUPTED: preserving partial evidence",flush=True)
    finally:
        stopWorkers(workers)
        summary=shadowSummary(directory)
        (directory/"summary.json").write_text(json.dumps(
            summary,sort_keys=True,indent=2)+"\n")
        print("SHADOW SUMMARY",json.dumps(summary,sort_keys=True),flush=True)
        print("EVIDENCE DIRECTORY",directory,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("run","status"))
    parser.add_argument("--env",type=Path,help="Explicit env created from NEW Alchemy source")
    parser.add_argument("--credentials-source",type=Path,default=SOURCE_DEFAULT,
                        help="Expected NEW Alchemy source (default ~/blackswan-modular/newalchemy.txt)")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    parser.add_argument("--run-dir",type=Path)
    parser.add_argument("--duration-seconds",type=int,default=600)
    parser.add_argument("--first-trade-timeout-seconds",type=int,default=300)
    args=parser.parse_args()
    if args.command=="status":
        if args.run_dir is None:parser.error("--run-dir required for status")
        print(json.dumps(shadowSummary(args.run_dir.expanduser()),indent=2))
        return
    if not 30<=args.duration_seconds<=14400:
        parser.error("AUDIT_DURATION_MUST_BE_30_TO_14400_SECONDS")
    try:runShadow(args)
    except (RuntimeError,ValueError,sqlite3.Error,OSError) as exc:
        parser.error(str(exc))


if __name__=="__main__":
    main()
