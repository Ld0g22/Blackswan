#!/usr/bin/env python3
"""Start/resume an isolated, real-money detector -> selector -> execution campaign.

This launcher never writes to existing detector/selector code or experiment data.
"""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import queue
import sqlite3
import time

from run_fresh_peaks import validateWalletFile, detectorReady, startWorker, drain, stopWorkers, newExperimentId
from campaign_config import (FLEX_KIND,FLEX_EXIT,FLEX_TRANCHES,FLEX_PREFIX,PILOT_KIND,PILOT_EXIT,PILOT_TRANCHES,PILOT_PREFIX,
    EARLY_KIND,EARLY_EXIT,EARLY_TRANCHES,EARLY_PREFIX,flexSettings,flexManifest,pilotManifest,
    earlyManifest,verifyFlexLedger,verifyPilotLedger,verifyEarlyLedger,otherLiveExecutors)



def campaignDecisionSeconds(manifest,sacrifice):
    """Persist cutoff per campaign; never silently retime an existing selector."""
    if not sacrifice:
        return 5
    choice=manifest.get('selectorDecisionSeconds',5)
    if type(choice) is not int or choice not in (5,20):
        raise RuntimeError('INVALID_CAMPAIGN_SELECTOR_WINDOW')
    return choice


def campaignMaxBuyingBlocks(manifest,sacrifice):
    """New five-lamb policy is 3–10. Old manifests retain unbounded ≥3."""
    if not sacrifice:
        return None
    value=manifest.get('selectorMaxBuyingBlocks')
    if value is not None and (type(value) is not int or value!=10):
        raise RuntimeError('INVALID_CAMPAIGN_BUY_BLOCK_POLICY')
    return value


def campaignSurvivalBlocks(manifest,sacrifice):
    """500 canonical blocks for new probes, original 10s for existing campaigns.

    Explicitly reject conflicting or malformed policies before starting workers.
    """
    if not sacrifice:return None
    if 'survivalBlocks' in manifest:
        blocks=manifest['survivalBlocks']
        if type(blocks) is not int or blocks!=500 or 'survivalSeconds' in manifest:
            raise RuntimeError('INVALID_CAMPAIGN_SURVIVAL_POLICY')
        if 'probeMaxBuyingBlocks' in manifest:
            if (type(manifest['probeMaxBuyingBlocks']) is not int
                    or manifest['probeMaxBuyingBlocks']!=7
                    or manifest.get('configVersion')==1):
                raise RuntimeError('INVALID_CAMPAIGN_SURVIVAL_POLICY')
        elif manifest.get('configVersion')==2:
            # Version 2 must never downgrade to an unfiltered legacy gate.
            raise RuntimeError('INVALID_CAMPAIGN_SURVIVAL_POLICY')
        return blocks
    if manifest.get('survivalSeconds')!=10:
        raise RuntimeError('INVALID_CAMPAIGN_SURVIVAL_POLICY')
    return None


def readCampaignState(path):
    """Read existing committed executor state, never mistake a corrupt DB for OPEN."""
    if not path.exists():return dict(intake='OPEN',workers='ACTIVE',watch='NOT_STARTED',watchHead=0)
    try:
        with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=2) as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
        return dict(intake=meta.get('intakeState','OPEN'),
            workers=meta.get('workerState','ACTIVE'),
            watch=meta.get('venueWatchState','NOT_STARTED'),
            watchHead=int(meta.get('venueWatchHead','0')))
    except (sqlite3.Error,ValueError) as exc:
        raise RuntimeError('CAMPAIGN_STATE_UNREADABLE') from exc


def safeReadLiveState(path):
    """Read-only status is optional while executor trades; failure delays retirement.

    A missing or partially initialized database is UNKNOWN, never a reason
    to stop the live executor. Strict start/resume identity checks are separate.
    """
    if not Path(path).is_file():
        return None
    try:
        return readCampaignState(path)
    except RuntimeError as exc:
        if str(exc)!='CAMPAIGN_STATE_UNREADABLE':
            raise
        return None


def requireLiveExecutor(executor):
    if executor.poll() is not None:
        raise RuntimeError('LIVE_EXECUTOR_STOPPED: inspect positions and resume')


def requireExistingExecution(path):
    if not Path(path).is_file():
        raise RuntimeError('MISSING_EXECUTION_LEDGER: refuse to resume without past fills')


def shouldStartSources(sacrifice,state):
    return not sacrifice or state['intake']!='CLOSED'


def markWorkerState(path,status):
    with sqlite3.connect(path,timeout=10) as db:
        db.execute("INSERT INTO meta(key,value) VALUES('workerState',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",(status,))


def retireSources(selector,detector,executionDb,stopFn=stopWorkers):
    """Only the ORIGINAL two source children; never touches the live executor."""
    markWorkerState(executionDb,'STOPPING')
    for process in (selector,detector):
        if process is not None:
            stopFn([process])
            if process.poll() is None:
                raise RuntimeError('SOURCE_PROCESS_DID_NOT_RETIRE')
    markWorkerState(executionDb,'RETIRED')



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['start','resume','status'])
    parser.add_argument('--arm-live',action='store_true')
    parser.add_argument('--sacrifice-five',action='store_true',help='Original five real 10p campaign')
    parser.add_argument('--sacrifice-one-50p',action='store_true',help='One real 50p buy after original 1p and 500-block gate')
    parser.add_argument('--sacrifice-two-50p',action='store_true',help='Two real 50p buys after 1p probes and 500-block gates')
    parser.add_argument('--sacrifice-two-50p-2x',action='store_true',help='Two real 50p buys, exits 80%% at 2x, 10%% at 50x, 5%% at 100x; reserve 5%% for manual')
    parser.add_argument('--sacrifice-one-20p-2x',action='store_true',help='One real 20p buy; exits 80%% at 2x, 10%% at 50x, 5%% at 100x; 5%% manual')
    parser.add_argument('--flex-3x5x',action='store_true',help='Flexible 3x/5x policy without post-probe buy-block cap')
    parser.add_argument('--flex-1p5x3x',action='store_true',help='Experimental 80%% at 1.5x, 20%% at 3x; probe and 500-block gate unchanged')
    parser.add_argument('--flex-lamb',action='store_true',help='Reusable live campaign; configure main GBP purchase and purchase limit')
    parser.add_argument('--buy-gbp',help='Main purchase GBP (two decimal places); required only for flex start')
    parser.add_argument('--max-purchases',help='Maximum confirmed main buys; required only for flex start')
    parser.add_argument('--run-dir',type=Path,help='Required for resume/status')
    parser.add_argument('--env',type=Path,default=Path.home()/'blackswanRH_guarded/.env')
    parser.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    args=parser.parse_args()
    if sum((args.sacrifice_five,args.sacrifice_one_50p,args.sacrifice_two_50p,args.sacrifice_two_50p_2x,args.sacrifice_one_20p_2x,args.flex_lamb,args.flex_3x5x,args.flex_1p5x3x))>1:
        parser.error('Choose one sacrificial campaign mode')
    pilotMode=args.flex_3x5x or args.flex_1p5x3x
    flexMode=args.flex_lamb or pilotMode
    lambMode=args.sacrifice_five or args.sacrifice_one_50p or args.sacrifice_two_50p or args.sacrifice_two_50p_2x or args.sacrifice_one_20p_2x or flexMode
    if flexMode and args.command=='start':
        try:args.buy_gbp,args.max_purchases=flexSettings(args.buy_gbp,args.max_purchases)
        except ValueError as exc:parser.error(str(exc))
    elif args.buy_gbp is not None or args.max_purchases is not None:
        parser.error('Stake/count flags are only accepted on a new --flex-lamb start; resume uses frozen settings')
    root=Path(__file__).resolve().parent
    python=root/'.venv/bin/python'
    if not python.is_file():parser.error('Repository .venv/bin/python missing')
    if args.command in ('resume','status') and not args.run_dir:
        parser.error('--run-dir required for resume/status')
    if args.command=='status':
        directory=args.run_dir.expanduser().resolve()
        options=(['--flex-1p5x3x'] if args.flex_1p5x3x else ['--flex-3x5x'] if args.flex_3x5x else ['--flex-lamb'] if args.flex_lamb else
                 ['--sacrifice-one-20p-2x'] if args.sacrifice_one_20p_2x else
                 ['--sacrifice-two-50p-2x'] if args.sacrifice_two_50p_2x else
                 ['--sacrifice-two-50p'] if args.sacrifice_two_50p else
                 ['--sacrifice-one-50p'] if args.sacrifice_one_50p else
                 ['--sacrifice-five'] if args.sacrifice_five else [])
        if lambMode:
            manifest=json.loads((directory/'campaign.json').read_text())
            if flexMode:
                try:stake,count,wallet=(earlyManifest(manifest) if args.flex_1p5x3x else pilotManifest(manifest) if args.flex_3x5x else flexManifest(manifest))
                except ValueError as exc:parser.error(str(exc))
                options.extend(('--buy-gbp',stake,'--max-purchases',str(count),'--campaign-wallet',wallet))
            blocks=campaignSurvivalBlocks(manifest,True)
            if blocks is not None:
                options.extend(('--probe-wait-blocks',str(blocks)))
                if manifest.get('probeMaxBuyingBlocks')==7:
                    options.extend(('--probe-max-buying-blocks','7'))
        os.execv(str(python),[str(python),str(root/'live_executor.py'),'status',
                '--db',str(directory/'execution.sqlite3'),*options])
    if not args.arm_live:
        parser.error('REAL MONEY: explicitly pass --arm-live')
    env=args.env.expanduser().resolve()
    walletFile=args.wallet_file.expanduser().resolve()
    if not env.is_file():parser.error('Existing .env not found')
    try:wallet=validateWalletFile(walletFile)
    except (ValueError,OSError) as exc:parser.error(str(exc))
    if flexMode:
        try:overlaps=otherLiveExecutors()
        except RuntimeError as exc:parser.error(str(exc))
        if overlaps:
            parser.error('OTHER_LIVE_EXECUTOR_RUNNING: stop existing live campaigns first: '+str(overlaps))
    # Required preflight is read-only and runs before processes or wallet decryption.
    import subprocess
    probe=subprocess.run([str(python),str(root/'probe_live_execution.py'),
                          '--env',str(env),'--wallet-file',str(walletFile)],check=False)
    if probe.returncode!=0:
        parser.error('LIVE PREFLIGHT FAILED; no transactions will be signed')
    if lambMode:
        watcherProbe=subprocess.run([str(python),str(root/'lamb_readonly_preflight.py'),
                    '--env',str(env),'--wallet-file',str(walletFile)],check=False)
        if watcherProbe.returncode!=0:
            parser.error('HELD-TOKEN VENUE PREFLIGHT FAILED; no transaction signed')
    if args.command=='start':
        prefix=(EARLY_PREFIX if args.flex_1p5x3x else PILOT_PREFIX if args.flex_3x5x else FLEX_PREFIX if args.flex_lamb else
                'v4_lamb_1x20p_2x_' if args.sacrifice_one_20p_2x else
                'v4_lamb_2x50p_2x_' if args.sacrifice_two_50p_2x else
                'v4_lamb_2x50p_' if args.sacrifice_two_50p else
                'v4_lamb_1x50p_' if args.sacrifice_one_50p else
                'v4_lamb_5x10p_' if args.sacrifice_five else 'v4_peak_20x10p_ladder_')
        directory=args.data_dir.expanduser().resolve()/(prefix+newExperimentId())
        directory.mkdir(parents=True,exist_ok=False)
        if flexMode:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=args.max_purchases,gbpEach=args.buy_gbp,
                      configVersion=(4 if args.flex_1p5x3x else 3 if args.flex_3x5x else 2),entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy=(EARLY_EXIT if args.flex_1p5x3x else PILOT_EXIT if args.flex_3x5x else FLEX_EXIT),
                      sellTranches=(EARLY_TRANCHES if args.flex_1p5x3x else PILOT_TRANCHES if args.flex_3x5x else FLEX_TRANCHES).copy(),
                      campaignKind=(EARLY_KIND if args.flex_1p5x3x else PILOT_KIND if args.flex_3x5x else FLEX_KIND),probeGbp='0.01',survivalBlocks=500,
                      **({'postProbeBuyingBlockGate':'OFF'} if pilotMode else {'probeMaxBuyingBlocks':7}),
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
            (earlyManifest(info,expectedWallet=wallet) if args.flex_1p5x3x else pilotManifest(info,expectedWallet=wallet) if args.flex_3x5x else flexManifest(info,expectedWallet=wallet))
        elif args.sacrifice_one_20p_2x:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=1,gbpEach='0.20',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_2x_50x_100x_manual5',
                      sellTranches={'2x':80,'50x':10,'100x':5,'manual':5},
                      campaignKind='ONE_20P_2X_LAMB',probeGbp='0.01',survivalBlocks=500,probeMaxBuyingBlocks=7,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        elif args.sacrifice_two_50p_2x:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=2,gbpEach='0.50',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_2x_50x_100x_manual5',
                      sellTranches={'2x':80,'50x':10,'100x':5,'manual':5},
                      campaignKind='TWO_50P_2X_LAMB',probeGbp='0.01',survivalBlocks=500,probeMaxBuyingBlocks=7,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        elif args.sacrifice_two_50p:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=2,gbpEach='0.50',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_3x_5x_1000x',
                      sellTranches={'3x':80,'5x':10,'1000x':10},
                      campaignKind='TWO_50P_LAMB',probeGbp='0.01',survivalBlocks=500,probeMaxBuyingBlocks=7,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        elif args.sacrifice_one_50p:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=1,gbpEach='0.50',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_3x_5x_1000x',
                      sellTranches={'3x':80,'5x':10,'1000x':10},
                      campaignKind='ONE_50P_LAMB',probeGbp='0.01',survivalBlocks=500,probeMaxBuyingBlocks=7,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        elif args.sacrifice_five:
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=5,gbpEach='0.10',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_3x_5x_1000x',
                      sellTranches={'3x':80,'5x':10,'1000x':10},
                      campaignKind='FIVE_LAMB',probeGbp='0.01',survivalBlocks=500,probeMaxBuyingBlocks=7,
                      selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)
        else:
            # The EXACT original campaign policy and manifest remain intact.
            info=dict(name=directory.name,createdUtc=datetime.now(timezone.utc).isoformat(),
                      chainId=4663,walletPublicAddress=wallet,mode='REAL_MONEY',
                      targetPurchases=20,gbpEach='0.10',entryPolicy='uniswapV4_nonPons_only',
                      quoteEngine='original_peak_tracker_Tracker_best',
                      exitPolicy='latched_3x_5x_10x',sellTranches={'3x':80,'5x':15,'10x':5})
        (directory/'campaign.json').write_text(json.dumps(info,indent=2)+'\n')
    else:
        directory=args.run_dir.expanduser().resolve()
        info=json.loads((directory/'campaign.json').read_text())
        if flexMode:
            try:(earlyManifest(info,expectedWallet=wallet) if args.flex_1p5x3x else pilotManifest(info,expectedWallet=wallet) if args.flex_3x5x else flexManifest(info,expectedWallet=wallet))
            except ValueError as exc:parser.error(str(exc))
        elif args.sacrifice_one_20p_2x:
            if (info.get('mode')!='REAL_MONEY' or info.get('campaignKind')!='ONE_20P_2X_LAMB'
                 or info.get('targetPurchases')!=1 or info.get('gbpEach')!='0.20'
                 or info.get('probeGbp')!='0.01' or info.get('survivalBlocks')!=500
                 or 'survivalSeconds' in info
                 or info.get('quoteEngine')!='original_peak_tracker_Tracker_best'
                 or info.get('entryPolicy')!='uniswapV4_nonPons_only'
                 or info.get('exitPolicy')!='latched_2x_50x_100x_manual5'
                 or info.get('sellTranches')!={'2x':80,'50x':10,'100x':5,'manual':5}
                 or info.get('selectorDecisionSeconds')!=20
                 or info.get('selectorMaxBuyingBlocks')!=10
                 or info.get('walletPublicAddress','').lower()!=wallet.lower()):
                parser.error('Resume one-20p campaign identity mismatch')
        elif args.sacrifice_two_50p_2x:
            if (info.get('mode')!='REAL_MONEY' or info.get('campaignKind')!='TWO_50P_2X_LAMB'
                 or info.get('targetPurchases')!=2 or info.get('gbpEach')!='0.50'
                 or info.get('probeGbp')!='0.01' or info.get('survivalBlocks')!=500
                 or 'survivalSeconds' in info
                 or info.get('quoteEngine')!='original_peak_tracker_Tracker_best'
                 or info.get('entryPolicy')!='uniswapV4_nonPons_only'
                 or info.get('exitPolicy')!='latched_2x_50x_100x_manual5'
                 or info.get('sellTranches')!={'2x':80,'50x':10,'100x':5,'manual':5}
                 or info.get('selectorDecisionSeconds')!=20
                 or info.get('selectorMaxBuyingBlocks')!=10
                 or info.get('walletPublicAddress','').lower()!=wallet.lower()):
                parser.error('Resume new ladder campaign identity mismatch')
        elif args.sacrifice_two_50p:
            if (info.get('mode')!='REAL_MONEY' or info.get('campaignKind')!='TWO_50P_LAMB'
                 or info.get('targetPurchases')!=2 or info.get('gbpEach')!='0.50'
                 or info.get('probeGbp')!='0.01' or info.get('survivalBlocks')!=500
                 or 'survivalSeconds' in info
                 or info.get('quoteEngine')!='original_peak_tracker_Tracker_best'
                 or info.get('entryPolicy')!='uniswapV4_nonPons_only'
                 or info.get('exitPolicy')!='latched_3x_5x_1000x'
                 or info.get('sellTranches')!={'3x':80,'5x':10,'1000x':10}
                 or info.get('selectorDecisionSeconds')!=20
                 or info.get('selectorMaxBuyingBlocks')!=10
                 or info.get('walletPublicAddress','').lower()!=wallet.lower()):
                parser.error('Resume two-50p campaign identity mismatch')
        elif args.sacrifice_one_50p:
            if (info.get('mode')!='REAL_MONEY' or info.get('campaignKind')!='ONE_50P_LAMB'
                 or info.get('targetPurchases')!=1 or info.get('gbpEach')!='0.50'
                 or info.get('probeGbp')!='0.01' or info.get('survivalBlocks')!=500
                 or 'survivalSeconds' in info
                 or info.get('quoteEngine')!='original_peak_tracker_Tracker_best'
                 or info.get('entryPolicy')!='uniswapV4_nonPons_only'
                 or info.get('exitPolicy')!='latched_3x_5x_1000x'
                 or info.get('sellTranches')!={'3x':80,'5x':10,'1000x':10}
                 or info.get('selectorDecisionSeconds')!=20
                 or info.get('selectorMaxBuyingBlocks')!=10
                 or info.get('walletPublicAddress','').lower()!=wallet.lower()):
                parser.error('Resume one-50p campaign identity mismatch')
        elif args.sacrifice_five:
            if (info.get('mode')!='REAL_MONEY' or info.get('campaignKind')!='FIVE_LAMB'
                 or info.get('targetPurchases')!=5 or info.get('gbpEach')!='0.10'
                 or info.get('probeGbp')!='0.01'
                 or info.get('quoteEngine')!='original_peak_tracker_Tracker_best'
                 or info.get('entryPolicy')!='uniswapV4_nonPons_only'
                 or info.get('exitPolicy')!='latched_3x_5x_1000x'
                 or info.get('sellTranches')!={'3x':80,'5x':10,'1000x':10}
                 or info.get('selectorDecisionSeconds',5) not in (5,20)
                 or (info.get('selectorMaxBuyingBlocks') is not None
                     and (type(info.get('selectorMaxBuyingBlocks')) is not int
                          or info['selectorMaxBuyingBlocks'] != 10))
                 or info.get('walletPublicAddress','').lower()!=wallet.lower()):
                parser.error('Resume sacrificial campaign identity mismatch')
        else:
            # Original twenty-canary resume checks retained verbatim.
            if info.get('mode')!='REAL_MONEY' or info.get('targetPurchases')!=20 or info.get('gbpEach')!='0.10' or info.get('quoteEngine')!='original_peak_tracker_Tracker_best' or info.get('entryPolicy')!='uniswapV4_nonPons_only' or info.get('exitPolicy')!='latched_3x_5x_10x' or info.get('sellTranches')!={'3x':80,'5x':15,'10x':5} or info['walletPublicAddress'].lower()!=wallet.lower():
                parser.error('Resume campaign identity mismatch')
    flexFlags=([('--flex-1p5x3x' if args.flex_1p5x3x else '--flex-3x5x' if args.flex_3x5x else '--flex-lamb'),'--buy-gbp',info['gbpEach'],
                '--max-purchases',str(info['targetPurchases']),
                '--campaign-wallet',info['walletPublicAddress']]
               if flexMode else [])
    # New five-lamb campaigns use +20s; prior manifests (missing this field)
    # permanently keep +5s on resume. Legacy 20-canary policy is unchanged.
    decisionSeconds=campaignDecisionSeconds(info,lambMode)
    maxBuyingBlocks=campaignMaxBuyingBlocks(info,lambMode)
    survivalBlocks=campaignSurvivalBlocks(info,lambMode)
    probeMaxBuyingBlocks=(info.get('probeMaxBuyingBlocks')
                          if survivalBlocks==500 else None)
    detectorDb=directory/'detector.sqlite3'
    selectorDb=directory/'selector.sqlite3'
    executionDb=directory/'execution.sqlite3'
    if lambMode and args.command=='resume':
        requireExistingExecution(executionDb)
    if flexMode and args.command=='resume':
        if args.flex_1p5x3x:
            verifyEarlyLedger(executionDb,info['gbpEach'],info['targetPurchases'],info['walletPublicAddress'])
        elif args.flex_3x5x:
            verifyPilotLedger(executionDb,info['gbpEach'],info['targetPurchases'],info['walletPublicAddress'])
        else:
            verifyFlexLedger(executionDb,info['gbpEach'],info['targetPurchases'],
                             info['walletPublicAddress'],expectedProbeMaxBuyingBlocks=probeMaxBuyingBlocks)
    # No password in argv, environment, git or files. Child reads anonymous pipe.
    password=getpass.getpass('Local wallet keystore password (not uploaded): ')
    readFd,writeFd=os.pipe()
    messages=queue.Queue();workers=[]
    try:
        print('LIVE CAMPAIGN',directory,'SELECTOR_CUTOFF_SECONDS',decisionSeconds,
              'BUYING_BLOCK_RULE',('3..'+str(maxBuyingBlocks) if maxBuyingBlocks else '>=3'),
              'PROBE_SURVIVAL',('500 CONFIRMED-RECEIPT BLOCKS' if survivalBlocks==500
                                else '10 SECONDS'),flush=True)
        state=readCampaignState(executionDb) if lambMode else dict(intake='OPEN',workers='ACTIVE',watch='NOT_STARTED',watchHead=0)
        startBroad=shouldStartSources(lambMode,state)
        detector=selector=None
        if startBroad:
            detector=startWorker('DETECTOR',[str(python),'-u',str(root/'detector.py'),'run',
                                 '--env',str(env),'--db',str(detectorDb)],messages)
            workers.append(detector)
            deadline=time.monotonic()+20
            while not detectorReady(detectorDb):
                drain(messages)
                if detector.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('Detector failed to start')
                time.sleep(.05)
            selector=startWorker('SELECTOR',[str(python),'-u',str(root/'five_second_selector.py'),'run',
                    '--env',str(env),'--detector-db',str(detectorDb),'--db',str(selectorDb),
                    '--decision-seconds',str(decisionSeconds),
                    *(['--max-buying-blocks',str(maxBuyingBlocks)]
                      if maxBuyingBlocks is not None else [])],messages)
            workers.append(selector)
            deadline=time.monotonic()+15
            while not selectorDb.is_file():
                drain(messages)
                if selector.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('Selector failed to start')
                time.sleep(.05)
        else:
            if not detectorDb.exists() or not selectorDb.exists():
                raise RuntimeError('Cannot resume closed campaign without source DB snapshots')
            if state['workers']!='RETIRED':
                print('WARNING: source retirement not yet verified after restart; check old worker terminals',flush=True)
            else:
                print('SOURCES RETIRED: original executor and position watcher continue',flush=True)
        # Unlike generic startWorker, must preserve the secret pipe descriptor.
        from run_fresh_peaks import readLines
        import threading
        cmd=[str(python),'-u',str(root/'live_executor.py'),'run','--arm-live',
             *(flexFlags if flexMode else ['--sacrifice-one-20p-2x'] if args.sacrifice_one_20p_2x else ['--sacrifice-two-50p-2x'] if args.sacrifice_two_50p_2x else ['--sacrifice-two-50p'] if args.sacrifice_two_50p else ['--sacrifice-one-50p'] if args.sacrifice_one_50p else ['--sacrifice-five'] if args.sacrifice_five else []),
             *(['--probe-wait-blocks',str(survivalBlocks)]
               if survivalBlocks is not None else []),
             *(['--probe-max-buying-blocks',str(probeMaxBuyingBlocks)]
               if probeMaxBuyingBlocks is not None else []),
             '--db',str(executionDb),'--selector-db',str(selectorDb),
             '--detector-db',str(detectorDb),'--env',str(env),
             '--wallet-file',str(walletFile),'--password-fd',str(readFd)]
        executor=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                  text=True,bufsize=1,start_new_session=True,pass_fds=(readFd,))
        workers.append(executor)
        threading.Thread(target=readLines,args=('EXECUTION',executor.stdout,messages),daemon=True).start()
        os.close(readFd);readFd=None
        with os.fdopen(writeFd,'w',encoding='utf-8') as writer:
            writer.write(password+'\n')
        writeFd=None;del password
        statusReadFailures=0
        while True:
            drain(messages)
            requireLiveExecutor(executor)
            if lambMode and detector is not None and selector is not None:
                current=safeReadLiveState(executionDb)
                if current is None:
                    statusReadFailures+=1
                    if statusReadFailures==1 or statusReadFailures%100==0:
                        print('LAUNCHER WARNING: read-only campaign status unavailable;',
                              'deferring source retirement; live execution continues.',
                              'database:',executionDb,flush=True)
                else:
                    if statusReadFailures:
                        print('LAUNCHER: campaign status readable again; execution uninterrupted',flush=True)
                    statusReadFailures=0
                if current is not None and current['intake']=='CLOSED' and current['watch']=='READY':
                    # Read the detector's frozen head; watcher must have covered it.
                    with sqlite3.connect(f'file:{detectorDb.resolve()}?mode=ro',
                                         uri=True,timeout=3) as db:
                        row=db.execute("SELECT value FROM meta WHERE key='lastConnectedHead'").fetchone()
                    if row:
                        last=int(row[0],16) if str(row[0]).startswith('0x') else int(row[0])
                        if current['watchHead']>=last:
                            retireSources(selector,detector,executionDb)
                            print(str(info['targetPurchases'])+' CONFIRMED '+info['gbpEach']+' GBP PURCHASES: detector and selector retired',flush=True)
                            selector=detector=None
            if detector is not None and detector.poll() is not None:
                raise RuntimeError('Detector stopped unexpectedly')
            if selector is not None and selector.poll() is not None:
                raise RuntimeError('Selector stopped unexpectedly')
            time.sleep(.10)
    except KeyboardInterrupt:
        print('LAUNCHER: Ctrl+C received. Campaign state retained:',directory,flush=True)
        return 0
    except Exception as exc:
        print('LAUNCHER ERROR:',type(exc).__name__,str(exc),
              '| stopping child processes; this was not necessarily an operator stop',flush=True)
        raise
    finally:
        if readFd is not None:os.close(readFd)
        if writeFd is not None:os.close(writeFd)
        stopWorkers(workers)
        drain(messages)
        print('Resume command:',python,root/'run_live_campaign.py','resume',
              *(flexFlags if flexMode else ['--sacrifice-one-20p-2x'] if args.sacrifice_one_20p_2x else ['--sacrifice-two-50p-2x'] if args.sacrifice_two_50p_2x else ['--sacrifice-two-50p'] if args.sacrifice_two_50p else ['--sacrifice-one-50p'] if args.sacrifice_one_50p else ['--sacrifice-five'] if args.sacrifice_five else []),
              '--run-dir',directory,'--arm-live',flush=True)


if __name__=='__main__':raise SystemExit(main())