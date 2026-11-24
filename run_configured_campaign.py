#!/usr/bin/env python3
"""Launch a version-3 BlackSwan campaign with unchanged detector and signer.

The historical launcher remains intact for existing campaigns. This file only
orchestrates services and a sealed manifest; it never constructs swap calldata.
"""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time

from campaign_v3 import defaultCampaign, concentrationCampaign, validateCampaign, campaignDigest
from run_fresh_peaks import (validateWalletFile, detectorReady, startWorker, drain,
                             stopWorkers, newExperimentId, readLines)
from campaign_config import otherLiveExecutors


def selectorCommand(config, root, python, env, detectorDb, selectorDb):
    """One policy per campaign, same durable selector.sqlite3 decision outbox."""
    selector=config["selector"]
    if selector["name"]=="integrationOnly":
        raise ValueError("INTEGRATION_SELECTOR_NOT_LIVE")
    if selector["name"]=="concentration":
        return [
            str(python),"-u",str(root/"concentration_selector.py"),"run",
            "--env",str(env),"--detector-db",str(detectorDb),"--db",str(selectorDb),
            "--window-seconds",str(selector["windowSeconds"]),
            "--threshold-bps",str(selector["maxLargestBuyTransactionShareBps"]),
            "--first-trade-timeout-seconds",str(selector["firstTradeTimeoutSeconds"]),
        ]
    if selector["name"]!="buyingBlocks":
        raise ValueError("SELECTOR_UNAVAILABLE")
    return [
        str(python),"-u",str(root/"five_second_selector.py"),"run",
        "--env",str(env),"--detector-db",str(detectorDb),"--db",str(selectorDb),
        "--decision-seconds",str(selector["decisionSeconds"]),
        *([] if selector["maxBuyingBlocks"] is None else [
            "--max-buying-blocks",str(selector["maxBuyingBlocks"])]),
    ]


def requireIdentity(directory,config):
    """Validate resume without modifying the pre-existing campaign DB."""
    manifest=validateCampaign(json.loads((directory/"campaign.json").read_text()))
    if campaignDigest(manifest)!=campaignDigest(config):
        raise RuntimeError("CAMPAIGN_MANIFEST_MISMATCH")
    ledger=directory/"execution.sqlite3"
    if not ledger.is_file():
        raise RuntimeError("EXECUTION_LEDGER_MISSING")
    with sqlite3.connect(f'file:{ledger.resolve()}?mode=ro',uri=True,timeout=3) as db:
        recorded=dict(db.execute("SELECT key,value FROM meta"))
    if recorded.get("campaignVersion")!="3" or recorded.get("campaignIdentity")!=campaignDigest(config):
        raise RuntimeError("EXECUTION_LEDGER_IDENTITY_MISMATCH")


def preflight(python,root,env,walletFile,sacrifice):
    for script in (("probe_live_execution.py",) + (("lamb_readonly_preflight.py",) if sacrifice else ())):
        r=subprocess.run([str(python),str(root/script),"--env",str(env),
                          "--wallet-file",str(walletFile)],check=False)
        if r.returncode:
            raise RuntimeError("READ_ONLY_PREFLIGHT_FAILED: "+script)


def startOrResume(args,root):
    directory=None
    if args.command=="start":
        if args.config is None:
            raise ValueError("START_REQUIRES_CONFIG_TEMPLATE")
        config=validateCampaign(json.loads(args.config.read_text()))
    else:
        if args.run_dir is None:
            raise ValueError("RESUME_REQUIRES_RUN_DIR")
        directory=args.run_dir.expanduser().resolve()
        config=validateCampaign(json.loads((directory/"campaign.json").read_text()))
        requireIdentity(directory,config)
    if not args.arm_live:
        raise ValueError("LIVE_TRADING_REQUIRES_EXPLICIT_--arm-live")

    python=root/".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("REPOSITORY_VENV_NOT_INSTALLED")
    env=args.env.expanduser().resolve()
    walletFile=args.wallet_file.expanduser().resolve()
    wallet=validateWalletFile(walletFile)
    if wallet.lower()!=config["walletPublicAddress"]:
        raise ValueError("CONFIGURED_WALLET_KEYSTORE_MISMATCH")

    # Fail before reading a secret or spawning any worker.
    selectorCommand(config,root,python,env,Path("/detector"),Path("/selector"))
    if otherLiveExecutors():
        raise RuntimeError("OVERLAPPING_LIVE_EXECUTOR_REFUSED")
    preflight(python,root,env,walletFile,config["sacrifice"]["enabled"])

    if directory is None:
        directory=args.data_dir.expanduser().resolve()/("configured_v3_"+newExperimentId())
        directory.mkdir(parents=True,exist_ok=False)
        (directory/"campaign.json").write_text(json.dumps(config,sort_keys=True,indent=2)+"\n")

    password=getpass.getpass("Local encrypted wallet password: ")
    readFd,writeFd=os.pipe()
    workers=[]
    messages=queue.Queue()
    try:
        detDb=directory/"detector.sqlite3"
        selDb=directory/"selector.sqlite3"
        exeDb=directory/"execution.sqlite3"
        if args.command=="resume" and exeDb.is_file():
            with sqlite3.connect(f'file:{exeDb.resolve()}?mode=ro',uri=True,timeout=3) as db:
                current=dict(db.execute("SELECT key,value FROM meta"))
            sourceClosed=current.get("intakeState")=="CLOSED" and current.get("workerState")=="RETIRED"
        else:
            sourceClosed=False
        detector=selector=None
        if not sourceClosed:
            detector=startWorker("DETECTOR",[
                str(python),"-u",str(root/"detector.py"),"run","--env",str(env),
                "--db",str(detDb)],messages)
            workers.append(detector)
            readyDeadline=time.monotonic()+25
            while not detectorReady(detDb):
                drain(messages)
                if detector.poll() is not None or time.monotonic()>readyDeadline:
                    raise RuntimeError("DETECTOR_STARTUP_FAILED")
                time.sleep(.05)
            selector=startWorker("SELECTOR",selectorCommand(
                config,root,python,env,detDb,selDb),messages)
            workers.append(selector)
            readyDeadline=time.monotonic()+15
            while not selDb.is_file():
                drain(messages)
                if selector.poll() is not None or time.monotonic()>readyDeadline:
                    raise RuntimeError("SELECTOR_STARTUP_FAILED")
                time.sleep(.05)
        elif not detDb.is_file() or not selDb.is_file():
            raise RuntimeError("RETIRED_SOURCE_JOURNALS_MISSING")

        command=[str(python),"-u",str(root/"live_executor.py"),"run",
                 "--arm-live","--campaign-file",str(directory/"campaign.json"),
                 "--db",str(exeDb),"--selector-db",str(selDb),
                 "--detector-db",str(detDb),"--env",str(env),
                 "--wallet-file",str(walletFile),"--password-fd",str(readFd)]
        executor=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  text=True,bufsize=1,start_new_session=True,
                                  pass_fds=(readFd,))
        workers.append(executor)
        threading.Thread(target=readLines,args=("EXECUTION",executor.stdout,messages),
                         daemon=True).start()
        os.close(readFd);readFd=None
        with os.fdopen(writeFd,"w",encoding="utf-8") as pipe:
            pipe.write(password+"\n")
        writeFd=None
        del password
        print("CONFIGURED CAMPAIGN",directory,"IDENTITY",campaignDigest(config),
              "SACRIFICES",config["sacrifice"]["enabled"],flush=True)

        while True:
            drain(messages)
            if executor.poll() is not None:
                raise RuntimeError("EXECUTION_STOPPED: positions may still be open; inspect and resume")
            if detector is not None or selector is not None:
                if ((detector is not None and detector.poll() is not None) or
                        (selector is not None and selector.poll() is not None)):
                    # Do not stop existing exits merely because the intake feed
                    # has failed. Freeze new intake, keep executor alive.
                    if exeDb.is_file():
                        with sqlite3.connect(exeDb,timeout=2) as db:
                            db.execute("INSERT INTO meta(key,value) VALUES('intakeState','CLOSED') "
                                       "ON CONFLICT(key) DO UPDATE SET value='CLOSED'")
                    print("CRITICAL: source failed; intake closed; existing positions remain in executor",flush=True)
                    for source in (selector,detector):
                        if source:stopWorkers([source])
                    selector=detector=None
                elif exeDb.is_file():
                    from run_live_campaign import safeReadLiveState,retireSources
                    state=safeReadLiveState(exeDb)
                    if state and state["intake"]=="CLOSED" and state["watch"]=="READY":
                        with sqlite3.connect(f'file:{detDb.resolve()}?mode=ro',uri=True,timeout=2) as db:
                            item=db.execute("SELECT value FROM meta WHERE key='lastConnectedHead'").fetchone()
                        if item and state["watchHead"]>=(int(item[0],16) if str(item[0]).startswith('0x') else int(item[0])):
                            retireSources(selector,detector,exeDb)
                            selector=detector=None
                            print("SOURCE INTAKE RETIRED; EXITS REMAIN ACTIVE",flush=True)
            time.sleep(.5)
    finally:
        if readFd is not None:os.close(readFd)
        if writeFd is not None:os.close(writeFd)
        if "password" in locals():del password
        # Ctrl+C/launcher termination is not a liquidation.
        stopWorkers(workers)
        if directory is not None:
            print("STOPPED WORKERS; open positions require campaign resume: "+str(directory),
                  flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("template","start","resume","status"))
    parser.add_argument("--config",type=Path)
    parser.add_argument("--wallet-address")
    parser.add_argument("--selector",choices=("buyingBlocks","concentration"),default="buyingBlocks",help="New campaign template selector only")
    parser.add_argument("--arm-live",action="store_true")
    parser.add_argument("--run-dir",type=Path)
    parser.add_argument("--env",type=Path,default=Path.home()/"blackswanRH_guarded/.env")
    parser.add_argument("--wallet-file",type=Path,default=Path.home()/"Desktop/wallet.json")
    parser.add_argument("--data-dir",type=Path,default=Path("data"))
    args=parser.parse_args()
    try:
        if args.command=="template":
            if args.config is None or args.wallet_address is None:
                parser.error("template requires --config and --wallet-address")
            config=(concentrationCampaign(args.wallet_address) if args.selector=="concentration"
                    else validateCampaign(defaultCampaign(args.wallet_address)))
            if args.config.exists():
                parser.error("Refusing to overwrite existing campaign config")
            args.config.parent.mkdir(parents=True,exist_ok=True)
            args.config.write_text(json.dumps(config,indent=2)+"\n")
            print("CONFIG TEMPLATE",args.config,"SACRIFICES ON",flush=True)
            return
        if args.command=="status":
            if args.run_dir is None:parser.error("status requires --run-dir")
            directory=args.run_dir.expanduser().resolve()
            cfg=validateCampaign(json.loads((directory/"campaign.json").read_text()))
            execution=directory/"execution.sqlite3"
            if not execution.is_file():
                print("NO_EXECUTION_LEDGER",directory)
                return
            with sqlite3.connect(f'file:{execution.resolve()}?mode=ro',uri=True) as db:
                meta=dict(db.execute("SELECT key,value FROM meta"))
                print(json.dumps({"campaignIdentity":campaignDigest(cfg),
                                  "ledgerIdentity":meta.get("campaignIdentity"),
                                  "intakeState":meta.get("intakeState"),
                                  "workerState":meta.get("workerState"),
                                  "version":meta.get("campaignVersion")},indent=2))
            return
        if args.selector!="buyingBlocks":
            parser.error("--selector is for template generation; campaign start/resume reads the frozen config")
        startOrResume(args,Path(__file__).resolve().parent)
    except (ValueError,RuntimeError,OSError,KeyError,json.JSONDecodeError,sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__=="__main__":
    main()
