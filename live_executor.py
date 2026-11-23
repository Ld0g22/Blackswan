#!/usr/bin/env python3
"""Real-money v4-only PASS consumer: 20 x GBP 0.10 buys; latched 80/15/5 sell ladder.

DETECTOR AND SELECTOR ARE READ-ONLY. No paper mode. Each buy and sale is an
independent durable transaction with a confirmed receipt and token-transfer check.
"""
import argparse
import asyncio
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from contextlib import nullcontext
import fcntl
import json
from pathlib import Path
import sqlite3
import time

import aiohttp
import peak_tracker as peak
from swap_adapters import calldata, abiTools, NATIVE, WETH, PONS_FACTORY, PONS_HOOK
from execution_peak_routes import PeakExecutionRoutes
from wallet_transactions import TransactionJournal, decodeKeystore
from sacrificial_lamb import LambGate
from probe_buying_blocks import MAX_BUYING_BLOCKS
from held_venue_watch import HeldVenueWatcher
from manual_exit_engine import ManualExitEngine, installManualSchema
from campaign_config import (FLEX_KIND, FLEX_POLICY, PILOT_KIND, PILOT_POLICY,
    EARLY_KIND, EARLY_POLICY, EARLY_TRIGGER_MULTIPLES, flexSettings,
    globalWalletLock, otherLiveExecutors)
from campaign_finance import CampaignFinance

PURCHASES=20
BUY_GBP=Decimal('0.10')
BUY_LAG_LIMIT=3.0
HOT_SECONDS=3.0
COLD_SECONDS=60.0
COLD_AFTER=1800.0
# Triggers latch on the first executable-value threshold quote.
# Do not re-gate on price after triggering; the user accepts subsequent slippage.
STAGES=((3,80),(5,15),(10,5))


def checkSlice(original, stage, balance, sold3=0, sold5=0, sacrifice=False):
    if stage==3:return original*80//100
    if stage==5:return original*(10 if sacrifice else 15)//100
    if stage==10 and not sacrifice:return min(balance, max(0, original-sold3-sold5))
    if stage==1000 and sacrifice:return min(balance, max(0, original-sold3-sold5))
    raise ValueError('Invalid tranche')


def nextInterval(lastGrowth, now):
    return COLD_SECONDS if now-lastGrowth>=COLD_AFTER else HOT_SECONDS


class ExecutionStore:
    def __init__(self,path,sacrifice=False,probeWaitBlocks=None,oneFifty=False,twoFifty=False,twoFifty2x=False,oneTwenty2x=False,flexLamb=False,flexBuyGbp=None,flexMaxPurchases=None,flexWalletAddress=None,probeMaxBuyingBlocks=None,flexPilot=False,flexEarly=False):
        if flexEarly and (flexPilot or flexLamb):
            raise ValueError('CONFLICTING_FLEX_CAMPAIGN_MODES')
        if (oneFifty and (twoFifty or twoFifty2x)) or (twoFifty and twoFifty2x):
            raise ValueError('CONFLICTING_50P_CAMPAIGN_MODES')
        if oneTwenty2x and (oneFifty or twoFifty or twoFifty2x):
            raise ValueError('CONFLICTING_CAMPAIGN_MODES')
        if (flexLamb or flexPilot or flexEarly) and (oneFifty or twoFifty or twoFifty2x or oneTwenty2x):
            raise ValueError('CONFLICTING_FLEX_CAMPAIGN_MODES')
        if flexLamb or flexPilot or flexEarly:
            if not sacrifice or probeWaitBlocks!=500:
                raise ValueError('FLEX_REQUIRES_500_BLOCK_LAMB')
            flexBuyGbp,flexMaxPurchases=flexSettings(flexBuyGbp,flexMaxPurchases)
            if (not isinstance(flexWalletAddress,str) or len(flexWalletAddress)!=42
                    or not flexWalletAddress.startswith('0x')
                    or any(x not in '0123456789abcdefABCDEF' for x in flexWalletAddress[2:])):
                raise ValueError('FLEX_WALLET_ADDRESS_REQUIRED')
            flexWalletAddress=flexWalletAddress.lower()
        elif any(x is not None for x in (flexBuyGbp,flexMaxPurchases,flexWalletAddress)):
            raise ValueError('FLEX_PARAMETERS_REQUIRE_FLEX_MODE')
        if oneFifty and (not sacrifice or probeWaitBlocks!=500):
            raise ValueError('ONE_50P_REQUIRES_500_BLOCK_LAMB')
        if twoFifty and (not sacrifice or probeWaitBlocks!=500):
            raise ValueError('TWO_50P_REQUIRES_500_BLOCK_LAMB')
        if twoFifty2x and (not sacrifice or probeWaitBlocks!=500):
            raise ValueError('TWO_50P_2X_REQUIRES_500_BLOCK_LAMB')
        if oneTwenty2x and (not sacrifice or probeWaitBlocks!=500):
            raise ValueError('ONE_20P_2X_REQUIRES_500_BLOCK_LAMB')
        if probeWaitBlocks is not None and (
                not sacrifice or type(probeWaitBlocks) is not int or probeWaitBlocks!=500):
            raise ValueError('UNSUPPORTED_PROBE_SURVIVAL_POLICY')
        if probeMaxBuyingBlocks is not None and (
                type(probeMaxBuyingBlocks) is not int or probeMaxBuyingBlocks!=MAX_BUYING_BLOCKS
                or probeWaitBlocks!=500 or not sacrifice):
            raise ValueError('UNSUPPORTED_PROBE_ACTIVITY_POLICY')
        self.sacrifice=bool(sacrifice)
        self.probeWaitBlocks=probeWaitBlocks
        self.probeMaxBuyingBlocks=probeMaxBuyingBlocks
        self.oneFifty=bool(oneFifty)
        self.twoFifty=bool(twoFifty or twoFifty2x)
        self.twoFifty2x=bool(twoFifty2x)
        self.oneTwenty2x=bool(oneTwenty2x)
        self.flexEarly=bool(flexEarly)
        self.flexPilot=bool(flexPilot or flexEarly)
        self.flexLamb=bool(flexLamb or self.flexPilot)
        self.flexPolicy=(EARLY_POLICY if self.flexEarly else PILOT_POLICY if self.flexPilot else FLEX_POLICY)
        self.flexConfigVersion=('3' if self.flexEarly else '2' if self.flexPilot else '1')
        self.flexWalletAddress=flexWalletAddress
        self.newLadder2x=bool(twoFifty2x or oneTwenty2x or (flexLamb and not self.flexPilot))
        self.newLadder3x5x=bool(flexPilot)
        self.buyGbp=(Decimal(flexBuyGbp) if self.flexLamb else
                     Decimal('0.20') if self.oneTwenty2x else
                     Decimal('0.50') if (self.oneFifty or self.twoFifty) else BUY_GBP)
        self.purchaseLimit=(flexMaxPurchases if self.flexLamb else
                            1 if self.oneTwenty2x else 2 if self.twoFifty else
                            1 if self.oneFifty else 5) if sacrifice else PURCHASES
        self.campaignKind=(EARLY_KIND if self.flexEarly else PILOT_KIND if self.flexPilot else FLEX_KIND if self.flexLamb else
                           'ONE_20P_2X_LAMB' if self.oneTwenty2x else
                           'TWO_50P_2X_LAMB' if self.twoFifty2x else
                           'TWO_50P_LAMB' if self.twoFifty else
                           'ONE_50P_LAMB' if self.oneFifty else 'FIVE_LAMB')
        path=Path(path)
        # Validate an existing file in READ-ONLY mode BEFORE any schema writes.
        # Old campaign databases must remain byte-for-byte untouched.
        if self.sacrifice and path.is_file() and path.stat().st_size:
            try:
                with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=2) as previous:
                    recorded=dict(previous.execute('SELECT key,value FROM meta'))
            except sqlite3.Error as exc:
                raise RuntimeError('NOT_A_FIVE_LAMB_DB') from exc
            if (recorded.get('campaignVersion')!='2' or
                    recorded.get('campaignKind')!=self.campaignKind):
                raise RuntimeError('NOT_A_FIVE_LAMB_DB')
            if self.flexLamb:
                required={'buyGbp':str(self.buyGbp),'maxPurchases':str(self.purchaseLimit),
                          'probeGbp':'0.01','waitBlocks':'500','exitPolicy':self.flexPolicy,
                          'walletPublicAddress':self.flexWalletAddress,'flexConfigVersion':self.flexConfigVersion}
                if any(recorded.get(key)!=value for key,value in required.items()):
                    raise RuntimeError('FLEX_IMMUTABLE_DB_SETTINGS_MISMATCH')
            policyKey='waitBlocks' if probeWaitBlocks==500 else 'waitSeconds'
            policyValue='500' if probeWaitBlocks==500 else '10'
            if (recorded.get(policyKey)!=policyValue
                    or ('waitSeconds' in recorded and probeWaitBlocks==500)
                    or ('waitBlocks' in recorded and probeWaitBlocks is None)):
                raise RuntimeError('FIVE_LAMB_PROBE_WAIT_POLICY_MISMATCH')
            expectedActivity=(str(MAX_BUYING_BLOCKS) if probeMaxBuyingBlocks==MAX_BUYING_BLOCKS else None)
            if recorded.get('probeMaxBuyingBlocks')!=expectedActivity:
                raise RuntimeError('PROBE_ACTIVITY_POLICY_MISMATCH: run with matching campaign policy')
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,isolation_level=None,timeout=15)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS selections(
          selectionId TEXT PRIMARY KEY,token TEXT NOT NULL UNIQUE,decisionAt REAL NOT NULL,
          event TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,quoteWei TEXT);
        -- V3 PASSes are recorded separately so the same token's later valid v4
        -- PASS cannot be blocked by selections.token UNIQUE.
        CREATE TABLE IF NOT EXISTS excluded_passes(
          selectionId TEXT PRIMARY KEY,token TEXT NOT NULL,decisionAt REAL NOT NULL,
          source TEXT NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS venues(
          token TEXT NOT NULL,venueId TEXT NOT NULL,data TEXT NOT NULL,active INTEGER NOT NULL,
          PRIMARY KEY(token,venueId));
        CREATE TABLE IF NOT EXISTS positions(
          selectionId TEXT PRIMARY KEY,token TEXT NOT NULL UNIQUE,originalQty TEXT NOT NULL,
          spentWei TEXT NOT NULL,boughtAt REAL NOT NULL,bestGbp TEXT,
          lastGrowth REAL NOT NULL,nextDue REAL NOT NULL,lastCheck REAL,
          trigger3 INTEGER NOT NULL DEFAULT 0,trigger5 INTEGER NOT NULL DEFAULT 0,
          trigger10 INTEGER NOT NULL DEFAULT 0,sold3 INTEGER NOT NULL DEFAULT 0,
          sold5 INTEGER NOT NULL DEFAULT 0,sold10 INTEGER NOT NULL DEFAULT 0,
          soldQty3 TEXT NOT NULL DEFAULT '0',soldQty5 TEXT NOT NULL DEFAULT '0',
          soldQty10 TEXT NOT NULL DEFAULT '0',preApprovalNext REAL NOT NULL DEFAULT 0, error TEXT, FOREIGN KEY(selectionId) REFERENCES selections(selectionId));
        CREATE TABLE IF NOT EXISTS transactions(
          intentId TEXT PRIMARY KEY, selectionId TEXT NOT NULL,kind TEXT NOT NULL,
          state TEXT NOT NULL,txHash TEXT NOT NULL,raw TEXT NOT NULL,nonce INTEGER NOT NULL,
          createdAt REAL NOT NULL,lastBroadcast REAL NOT NULL,inputAmount TEXT NOT NULL,
          receipt TEXT,reason TEXT);
        CREATE TABLE IF NOT EXISTS sales(
          selectionId TEXT NOT NULL,stage INTEGER NOT NULL,txHash TEXT NOT NULL UNIQUE,
          tokenDebited TEXT NOT NULL,wethReceivedWei TEXT,confirmedAt REAL NOT NULL,
          PRIMARY KEY(selectionId,stage));
        -- Same evidence columns as the original peak_tracker.Store observations.
        -- Record each live full-receipt-quantity sell quote and every failure.
        CREATE TABLE IF NOT EXISTS observations(
          id INTEGER PRIMARY KEY,token TEXT NOT NULL,phase TEXT NOT NULL,
          at REAL NOT NULL,block TEXT,venue_id TEXT,status TEXT NOT NULL,
          quantity TEXT,out_wei TEXT,gbp TEXT,multiple TEXT,basis TEXT,reason TEXT);
        CREATE INDEX IF NOT EXISTS execution_obs_token ON observations(token,at);
        CREATE INDEX IF NOT EXISTS tx_intents ON transactions(selectionId,kind,createdAt);
        ''')
        if self.sacrifice:
            # Only the NEW campaign gets the 1p ledger and 1000x tranche.
            for name in ('trigger1000','sold1000'):
                if name not in {r[1] for r in self.db.execute('PRAGMA table_info(positions)')}:
                    self.db.execute(f'ALTER TABLE positions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            if 'soldQty1000' not in {r[1] for r in self.db.execute('PRAGMA table_info(positions)')}:
                self.db.execute("ALTER TABLE positions ADD COLUMN soldQty1000 TEXT NOT NULL DEFAULT '0'")
            self.db.execute('''CREATE TABLE IF NOT EXISTS lamb_probes(
                selectionId TEXT PRIMARY KEY,token TEXT NOT NULL,
                probeTxHash TEXT UNIQUE,confirmedAt REAL,probeQty TEXT,probeInputWei TEXT,
                lastCheck REAL,passedAt REAL,result TEXT,reason TEXT)''')
            if self.newLadder2x:
                # Isolated 2x/50x/100x campaign: never mutate the old ladder schema.
                columns={r[1] for r in self.db.execute('PRAGMA table_info(positions)')}
                for stage in (2,50,100):
                    for name in ('trigger'+str(stage),'sold'+str(stage)):
                        if name not in columns:
                            self.db.execute(f'ALTER TABLE positions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
                    qty='soldQty'+str(stage)
                    if qty not in columns:
                        self.db.execute(f"ALTER TABLE positions ADD COLUMN {qty} TEXT NOT NULL DEFAULT '0'")
            exitPolicy=(self.flexPolicy if self.flexLamb else '80-10-5:2-50-100:manual5' if self.newLadder2x
                        else '80-10-10:3-5-1000')
            waitKey='waitBlocks' if self.probeWaitBlocks==500 else 'waitSeconds'
            waitValue='500' if self.probeWaitBlocks==500 else '10'
            if self.get('campaignVersion','')=='':
                for key,value in (('campaignVersion','2'),('campaignKind',self.campaignKind),
                                  ('maxPurchases',str(self.purchaseLimit)),('buyGbp',str(self.buyGbp)),
                                  ('probeGbp','0.01'),(waitKey,waitValue),
                                  *((('probeMaxBuyingBlocks',str(MAX_BUYING_BLOCKS)),) if self.probeMaxBuyingBlocks==MAX_BUYING_BLOCKS else ()),
                                  ('exitPolicy',exitPolicy),
                                  *((('walletPublicAddress',self.flexWalletAddress),('flexConfigVersion',self.flexConfigVersion)) if self.flexLamb else ()),
                                  ('intakeState','OPEN'),
                                  ('workerState','ACTIVE'),('venueWatchState','NOT_STARTED')):
                    self.set(key,value)
            expected={'campaignVersion':'2','campaignKind':self.campaignKind,
                      'maxPurchases':str(self.purchaseLimit),'buyGbp':str(self.buyGbp),'probeGbp':'0.01',
                      waitKey:waitValue,'exitPolicy':exitPolicy}
            if self.probeMaxBuyingBlocks==MAX_BUYING_BLOCKS:
                expected['probeMaxBuyingBlocks']=str(MAX_BUYING_BLOCKS)
            if self.flexLamb:
                expected.update(walletPublicAddress=self.flexWalletAddress,flexConfigVersion=self.flexConfigVersion)
            if any(self.get(k,'')!=v for k,v in expected.items()):
                raise RuntimeError('Sacrificial campaign settings mismatch; no existing DB migration')
        else:
            if self.get('campaignVersion','')=='':
                self.set('campaignVersion','1')
                self.set('maxPurchases',str(PURCHASES))
                self.set('buyGbp',str(BUY_GBP))
            if self.get('maxPurchases')!=str(PURCHASES) or self.get('buyGbp')!=str(BUY_GBP):
                raise RuntimeError('Campaign settings mismatch')
        installManualSchema(self)

    def observe(self,**values):
        self.db.execute("""INSERT INTO observations
           (token,phase,at,block,venue_id,status,quantity,out_wei,gbp,
            multiple,basis,reason)
           VALUES(:token,:phase,:at,:block,:venue_id,:status,:quantity,
                  :out_wei,:gbp,:multiple,:basis,:reason)""",values)

    def get(self,key,default='0'):
        row=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row['value'] if row else default

    def set(self,key,value):
        self.db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,str(value)))

    def setVenue(self,token,venue,active):
        self.db.execute('INSERT INTO venues VALUES(?,?,?,?) ON CONFLICT(token,venueId) DO UPDATE SET data=excluded.data,active=excluded.active',
                        (token,venue['id'],json.dumps(venue),int(active)))

    def venues(self,token,includeInactive=False):
        sql='SELECT data FROM venues WHERE token=?'+('' if includeInactive else ' AND active=1')
        return [json.loads(r[0]) for r in self.db.execute(sql,(token,))]

    def acquired(self):
        return self.db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]

    def reserved(self):
        return self.db.execute("SELECT COUNT(*) FROM selections WHERE status IN ('QUEUED','BUY_PENDING','BUY_UNVERIFIED')").fetchone()[0]

    def eligible(self):
        return (not self.sacrifice or self.get('intakeState','OPEN')=='OPEN') and (self.acquired()+self.reserved()<self.purchaseLimit)

    def transactions(self,selectionId,kind):
        return self.db.execute('SELECT * FROM transactions WHERE selectionId=? AND kind=? ORDER BY createdAt',
                               (selectionId,kind)).fetchall()

    def status(self):
        byState=dict(self.db.execute('SELECT status,COUNT(*) FROM selections GROUP BY status'))
        v3Count=self.db.execute("SELECT COUNT(*) FROM excluded_passes WHERE reason='V3_LAUNCH_EXCLUDED'").fetchone()[0]
        if v3Count:byState['SKIPPED_V3']=v3Count
        finalStage=('sold5' if self.flexPilot else 'sold100' if self.newLadder2x else
                    'sold1000' if self.sacrifice else 'sold10')
        openPositions=self.db.execute(f'SELECT COUNT(*) FROM positions WHERE {finalStage}=0').fetchone()[0]
        pending=self.db.execute("SELECT COUNT(*) FROM transactions WHERE state IN ('SIGNED','SENT')").fetchone()[0]
        stuck=self.db.execute("SELECT COUNT(*) FROM transactions WHERE state='STUCK'").fetchone()[0]
        rejected=self.db.execute("SELECT COUNT(*) FROM transactions WHERE state='REJECTED'").fetchone()[0]
        uncertain=self.db.execute("SELECT COUNT(*) FROM transactions WHERE state='UNCERTAIN'").fetchone()[0]
        rejectedDetails=[dict(row) for row in self.db.execute("SELECT kind,txHash,reason FROM transactions WHERE state='REJECTED' LIMIT 3")]
        sold=self.db.execute('SELECT COUNT(*) FROM sales').fetchone()[0]
        weth=sum(int(r[0]) for r in self.db.execute('SELECT wethReceivedWei FROM sales WHERE wethReceivedWei IS NOT NULL'))
        if self.oneFifty or self.twoFifty or self.oneTwenty2x or self.flexLamb:
            # A manual sale closes MAIN holdings even without a 1000x ladder fill.
            manuallySold={}
            for fill in self.db.execute('SELECT selectionId,tokenDebited FROM manual_fills'):
                sid=fill['selectionId']
                manuallySold[sid]=manuallySold.get(sid,0)+int(fill['tokenDebited'])
            stages=(3,5) if self.flexPilot else (2,50,100) if self.newLadder2x else (3,5,1000)
            cols=','.join('soldQty'+str(s) for s in stages)
            openPositions=sum(
                int(p['originalQty']) >
                sum(int(p['soldQty'+str(stage)]) for stage in stages) +
                int(manuallySold.get(p['selectionId'],0))
                for p in self.db.execute(
                    'SELECT selectionId,originalQty,'+cols+' FROM positions'))
            sold+=self.db.execute('SELECT COUNT(*) FROM manual_fills').fetchone()[0]
            weth+=sum(int(r[0]) for r in self.db.execute(
                'SELECT wethReceivedWei FROM manual_fills WHERE wethReceivedWei IS NOT NULL'))
        highlights=[{'token':r['token'],'bestGbp':r['bestGbp'],
                     'triggered3x':bool(r['trigger3']),'triggered5x':bool(r['trigger5']),
                     'triggered10x':bool(r['trigger10']),
                     'sold3':bool(r['sold3']),'sold5':bool(r['sold5']),
                     'sold10':bool(r['sold10']),'error':r['error']}
                    for r in self.db.execute('''SELECT token,bestGbp,trigger3,trigger5,trigger10,
                         sold3,sold5,sold10,error FROM positions
                         ORDER BY boughtAt DESC LIMIT 6''')]
        base=dict(purchases=self.acquired(),campaign=self.purchaseLimit,spentGbp=str(self.buyGbp*self.acquired()),
                    openPositions=openPositions,confirmedSales=sold,
                    verifiedWethProceeds=str(Decimal(weth)/Decimal(10**18))+' WETH',
                    nativeSaleProceeds='not yet independently measured',
                    transactionsPending=pending,stuckTransactions=stuck,
                    rejectedTransactions=rejected,uncertainTransactions=uncertain,
                    rejectionDetails=rejectedDetails,
                    selections=byState,positions=highlights)
        if self.flexEarly:
            # Slot identifiers 3/5 are NOT the new quote thresholds.
            base['positions']=[{'token':r['token'],'bestGbp':r['bestGbp'],
                'triggered1p5x':bool(r['trigger3']),'triggered3x':bool(r['trigger5']),
                'sold1p5x':bool(r['sold3']),'sold3x':bool(r['sold5']),'error':r['error']}
                for r in self.db.execute('''SELECT token,bestGbp,trigger3,trigger5,sold3,sold5,error
                    FROM positions ORDER BY boughtAt DESC LIMIT 6''')]
        elif self.flexPilot:
            base['positions']=[{'token':r['token'],'bestGbp':r['bestGbp'],
                'triggered3x':bool(r['trigger3']),'triggered5x':bool(r['trigger5']),
                'sold3':bool(r['sold3']),'sold5':bool(r['sold5']),'error':r['error']}
                for r in self.db.execute('''SELECT token,bestGbp,trigger3,trigger5,sold3,sold5,error
                    FROM positions ORDER BY boughtAt DESC LIMIT 6''')]
        elif self.newLadder2x:
            base['positions']=[{'token':r['token'],'bestGbp':r['bestGbp'],
                'triggered2x':bool(r['trigger2']),'triggered50x':bool(r['trigger50']),
                'triggered100x':bool(r['trigger100']),
                'sold2':bool(r['sold2']),'sold50':bool(r['sold50']),
                'sold100':bool(r['sold100']),'error':r['error']}
                for r in self.db.execute('''SELECT token,bestGbp,trigger2,trigger50,trigger100,
                    sold2,sold50,sold100,error FROM positions ORDER BY boughtAt DESC LIMIT 6''')]
        if self.sacrifice:
            base['probesConfirmed']=self.db.execute('SELECT COUNT(*) FROM lamb_probes WHERE confirmedAt IS NOT NULL').fetchone()[0]
            base['probesFailed']=byState.get('PROBE_FAILED',0)
            base['probesUnknown']=byState.get('PROBE_UNKNOWN',0)+byState.get('PROBE_UNVERIFIED',0)
            base['probesPassed']=self.db.execute("SELECT COUNT(*) FROM lamb_probes WHERE passedAt IS NOT NULL").fetchone()[0]
            base['probeNominalGbp']=str(Decimal('0.01')*base['probesConfirmed'])
            if self.probeWaitBlocks==500 and self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='probe_activity_bins'").fetchone():
                base['activityBinsCompleted']=self.db.execute(
                    'SELECT COUNT(*) FROM probe_activity_bins').fetchone()[0]
                base['activityPasses']=self.db.execute(
                    "SELECT COUNT(*) FROM probe_activity_verdicts WHERE verdict='PASS'").fetchone()[0]
                base['activityRejections']=self.db.execute(
                    "SELECT COUNT(*) FROM probe_activity_verdicts WHERE verdict='FAIL'").fetchone()[0]
                base['maxProbeBuyingBlocks']=MAX_BUYING_BLOCKS
            base['intakeState']=self.get('intakeState','OPEN')
            base['workerState']=self.get('workerState','ACTIVE')
            base['venueWatchState']=self.get('venueWatchState','NOT_STARTED')
            base['venueWatchError']=self.get('venueWatchError','')
            if not self.newLadder2x and not self.flexEarly:
                base['positions']=[{'token':r['token'],'bestGbp':r['bestGbp'],
                    'triggered3x':bool(r['trigger3']),'triggered5x':bool(r['trigger5']),
                    'triggered1000x':bool(r['trigger1000']),
                    'sold3':bool(r['sold3']),'sold5':bool(r['sold5']),
                    'sold1000':bool(r['sold1000']),'error':r['error']}
                    for r in self.db.execute("""SELECT token,bestGbp,trigger3,trigger5,trigger1000,
                        sold3,sold5,sold1000,error FROM positions ORDER BY boughtAt DESC LIMIT 6""")]
        return base


def receiptTransfers(receipt,token,wallet):
    """Net ERC20 Transfer amount credited to wallet in this exact transaction."""
    from eth_utils import keccak
    signature='0x'+keccak(text='Transfer(address,address,uint256)').hex()
    incoming=outgoing=0
    for log in receipt.get('logs',[]):
        topics=log.get('topics',[])
        if log.get('address','').lower()!=token.lower() or len(topics)<3 or topics[0].lower()!=signature:
            continue
        owner='0x'+topics[1][-40:].lower(); target='0x'+topics[2][-40:].lower()
        amount=int(log.get('data','0x0'),16)
        if target==wallet.lower():incoming+=amount
        if owner==wallet.lower():outgoing+=amount
    return incoming-outgoing


class LiveExecutor:
    def __init__(self,store,selectorDb,detectorDb,rpc,fx,tx):
        self.store,self.rpc,self.fx,self.tx=store,rpc,fx,tx
        # Reuse the ORIGINAL prospective peak_tracker.Tracker.best route and
        # quotation logic. Only execution/calldata is new.
        self.swaps=PeakExecutionRoutes(rpc,fx,store)
        self.selector=sqlite3.connect(f'file:{Path(selectorDb).resolve()}?mode=ro',uri=True,timeout=3)
        self.selector.row_factory=sqlite3.Row
        self.detector=sqlite3.connect(f'file:{Path(detectorDb).resolve()}?mode=ro',uri=True,timeout=3)
        self.detector.row_factory=sqlite3.Row
        self.jobs=set()
        self.active=set()
        self.lastStatus=0.0
        self.intakeLock=asyncio.Lock()
        self.exitThresholds=(EARLY_TRIGGER_MULTIPLES.copy() if store.flexEarly else {})
        self.stages=(((3,80),(5,20)) if store.flexPilot else
                     ((2,80),(50,10),(100,5)) if store.newLadder2x else
                     ((3,80),(5,10),(1000,10)) if store.sacrifice else STAGES)
        self.lamb=LambGate(self,receiptTransfers,waitBlocks=store.probeWaitBlocks) if store.sacrifice else None
        self.venueWatcher=HeldVenueWatcher(self) if store.sacrifice else None
        self.lastVenueCheck=0.0
        self.manual=ManualExitEngine(self,receiptTransfers) if (store.oneFifty or store.twoFifty or store.oneTwenty2x or store.flexLamb) else None
        self.finance=CampaignFinance(store,rpc,fx,tx.account.address) if store.flexPilot else None

    def ponsOrigin(self,token):
        """Read the unchanged detector journal; deny even reorg-removed Pons evidence.

        Only a negative within this campaign's detector history is established.
        Known Pons v4 graduation hooks are separately denied.
        """
        return self.detector.execute('''SELECT 1 FROM event_log
            WHERE source='ponsV2'
              AND lower(json_extract(payload,'$.details.token'))=?
            LIMIT 1''',(token.lower(),)).fetchone() is not None

    def entryExclusion(self,token,item):
        """Fail closed on non-v3/v4 PASS events and known Pons-origin tokens."""
        event=item.get('event')
        venue=item.get('venue')
        if not isinstance(event,dict) or not isinstance(venue,dict):
            return 'INVALID_SELECTION_SOURCE'
        source=event.get('source')
        venueSource=venue.get('source')
        if source=='ponsV2' or venueSource=='ponsV2':
            return 'PONS_V2_DIRECT'
        if venueSource!=source:
            return 'UNSUPPORTED_SELECTION_SOURCE'
        if source=='uniswapV4' and str(venue.get('hooks','')).lower()==PONS_HOOK:
            return 'PONS_V2_GRADUATION_HOOK'
        if self.ponsOrigin(token):
            return 'PONS_V2_DETECTOR_ORIGIN'
        if source=='uniswapV3':
            return 'V3_LAUNCH_EXCLUDED'
        if source!='uniswapV4':
            return 'UNSUPPORTED_SELECTION_SOURCE'
        # TEMPORARY flex-lamb exclusion from 2026-11-12 cohort:
        # three fee=1, spacing=1, no-hook purchases became unquotable.
        # Exact launch-time venue only. This executes before the 1p probe,
        # and is checked again before signing the main buy. No RPC required.
        if (self.store.flexLamb
                and int(venue.get('fee',-1))==1
                and int(venue.get('spacing',-1))==1
                and str(venue.get('hooks','')).lower()=='0x'+'0'*40):
            return 'TEMP_BANNED_V4_FEE1_SPACING1_ZERO_HOOK'
        # Known pre-buy pool template: repeated post-buy LP removals of
        # 25725498245903809899643 on Robinhood chain (October/November 2026).
        # Fee and spacing come from the selector's *launch-time* venue,
        # not from later liquidity events or a post-buy observation.
        # Apply to new sacrificial campaigns only; leave legacy mode alone.
        if (self.store.sacrifice
                and int(venue.get('fee',-1))==10000
                and int(venue.get('spacing',-1))==200
                and str(venue.get('hooks','')).lower()=='0x'+'0'*40):
            return 'BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK'
        return None

    async def ponsOnChain(self,token):
        """Definitive V2 factory origin check; RPC/decode uncertainty blocks entry.

        The local detector journal only covers its observation period, so a
        Uniswap pool may contain an older Pons token absent from that journal.
        """
        types=['address','address','address','address','address','uint256','uint24',
               'int24','uint16','bool','uint8','uint256','uint256','uint256','bool']
        try:
            raw=await self.rpc.ethCall(
                PONS_FACTORY,calldata('getLaunchedToken(address)',['address'],[token]),'latest')
            if not isinstance(raw,str) or not raw.startswith('0x'):
                raise ValueError('Invalid factory response')
            _,decode,_=abiTools()
            record=decode(types,bytes.fromhex(raw[2:]))
            if len(record)!=15:
                raise ValueError('Malformed factory launch record')
            if bool(record[14]):
                if str(record[0]).lower()!=token.lower():
                    raise ValueError('Factory returned mismatched token')
                return 'PONS_V2_FACTORY_ORIGIN'
            return None
        except Exception:
            # Cannot safely establish NON-Pons status => do not trade.
            return 'PONS_V2_ORIGIN_UNVERIFIED'

    def readSelections(self):
        cursor=int(self.store.get('selectionCursor'))
        for row in self.selector.execute('SELECT seq,status,data FROM decisions WHERE seq>? ORDER BY seq LIMIT 200',(cursor,)):
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                LiveExecutor._readSelectionRow(self,row)
                self.store.set('selectionCursor',row['seq'])
                self.store.db.execute('COMMIT')
            except BaseException:
                if self.store.db.in_transaction:
                    self.store.db.execute('ROLLBACK')
                raise

    def _readSelectionRow(self,row):
            if row['status']=='PASS':
                item=json.loads(row['data'])
                if item.get('interfaceVersion')!=1:
                    raise RuntimeError('UNSUPPORTED_SELECTOR_INTERFACE')
                token=item['tokenKey'].split(':',1)[1].lower()
                if len(token)!=42 or not token.startswith('0x'):
                    raise ValueError('Bad selector token key')
                if self.store.db.execute('SELECT 1 FROM selections WHERE token=?',(token,)).fetchone() is None:
                    reason=self.entryExclusion(token,item)
                    if reason=='V3_LAUNCH_EXCLUDED':
                        self.store.db.execute('''INSERT OR IGNORE INTO excluded_passes
                            (selectionId,token,decisionAt,source,reason)
                            VALUES(?,?,?,?,?)''',
                            (item['selectionId'],token,item['decisionAt'],'uniswapV3',reason))
                        print('EXECUTION ENTRY EXCLUDED SKIPPED_V3',token,flush=True)
                        return
                    status=('SKIPPED_PONS' if reason and reason.startswith('PONS_V2')
                            else 'SKIPPED_UNSUPPORTED' if reason
                            else ('LAMB_QUEUED' if self.store.sacrifice else 'QUEUED') if self.store.eligible() else 'CAP_FULL')
                    self.store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                        (item['selectionId'],token,item['decisionAt'],json.dumps(item),status,reason))
                    if reason:
                        print('EXECUTION ENTRY EXCLUDED',status,reason,token,flush=True)
                    elif status in ('QUEUED','LAMB_QUEUED'):
                        # Preserve the qualifying launch venue, then recover
                        # any earlier pools for this same token from our
                        # existing detector journal. Do not touch selector.
                        for t,venue in peak.launchVenues(item['event']):
                            if t==token and venue['id']==item['venue']['id']:
                                self.store.setVenue(token,venue,True)
                        LiveExecutor.recoverExistingVenues(self,token)

    def readRevocations(self):
        cursor=int(self.store.get('revocationCursor'))
        sql='''SELECT r.seq,r.eventKey FROM revocations r WHERE r.seq>? ORDER BY r.seq LIMIT 200'''
        for row in self.selector.execute(sql,(cursor,)):
            select=self.store.db.execute('SELECT status,token FROM selections WHERE selectionId=?',(row['eventKey'],)).fetchone()
            if select:
                if select['status'] in ('QUEUED','LAMB_QUEUED','PROBE_CONFIRMED'):
                    self.store.db.execute("UPDATE selections SET status='REVOKED' WHERE selectionId=?",(row['eventKey'],))
                elif select['status'] in ('BUY_PENDING','BOUGHT','PROBE_PENDING'):
                    print('REORG WARNING: position/pending tx retained',select['token'],flush=True)
            self.store.set('revocationCursor',row['seq'])

    def recoverExistingVenues(self,token):
        """Backfill all earlier v3/v4 pools for a selected v4-launch token.

        The ordinary detector cursor may have passed an earlier v3 pool
        long before the five-second v4 PASS. It cannot be replayed otherwise.
        """
        if getattr(self,'detector',None) is None:
            return  # Isolated selection unit-test fixture has no journal.
        sql="""SELECT payload FROM event_log
          WHERE (source='uniswapV3' AND (
                 lower(json_extract(payload,'$.details.token0'))=?
              OR lower(json_extract(payload,'$.details.token1'))=?))
             OR (source='uniswapV4' AND (
                 lower(json_extract(payload,'$.details.currency0'))=?
              OR lower(json_extract(payload,'$.details.currency1'))=?))
          ORDER BY seq"""
        for record in self.detector.execute(sql,(token,token,token,token)):
            event=json.loads(record['payload'])
            for t,venue in peak.launchVenues(event):
                if t==token and venue['source'] in ('uniswapV3','uniswapV4'):
                    if venue['source']=='uniswapV4' and venue.get('hooks','').lower()==PONS_HOOK:
                        continue
                    self.store.setVenue(token,venue,not bool(event.get('removed')))

    def readNewVenues(self):
        cursor=int(self.store.get('detectorCursor'))
        for row in self.detector.execute('SELECT seq,payload FROM event_log WHERE seq>? ORDER BY seq LIMIT 500',(cursor,)):
            event=json.loads(row['payload'])
            for token,venue in peak.launchVenues(event):
                if venue['source'] not in ('uniswapV3','uniswapV4'):
                    continue
                if venue['source']=='uniswapV4' and venue.get('hooks','').lower()==PONS_HOOK:
                    continue
                # A v4 PASS may precede the buy confirmation. Record all
                # subsequently discovered pools while entry is still queued.
                selected=self.store.db.execute(
                    """SELECT 1 FROM selections WHERE token=?
                       AND status IN ('QUEUED','LAMB_QUEUED','PROBE_PENDING','PROBE_CONFIRMED','BUY_PENDING','BUY_UNVERIFIED','BOUGHT')""",
                    (token,)).fetchone()
                if selected or self.store.db.execute(
                        'SELECT 1 FROM positions WHERE token=?',(token,)).fetchone():
                    self.store.setVenue(token,venue,not bool(event.get('removed')))
            self.store.set('detectorCursor',row['seq'])

    async def balance(self,token):
        raw=await self.rpc.ethCall(token,calldata('balanceOf(address)',
                    ['address'],[self.tx.account.address]),'latest')
        return int(raw,16)

    async def buy(self,row):
        selectionId=row['selectionId']; token=row['token']
        # Only the gate may enable a 10p purchase in the five-lamb campaign.
        lamb=getattr(self,'lamb',None)
        # Enforce again at the last step before constructing/signing a swap.
        # This covers Pons origin evidence arriving after a PASS was queued.
        reason=self.entryExclusion(token,json.loads(row['event']))
        if reason:
            # A preexisting signed buy is potentially real; reconciliation owns it.
            if self.store.transactions(selectionId,'BUY'):
                self.store.db.execute("UPDATE selections SET status='BUY_PENDING' WHERE selectionId=?",
                                      (selectionId,))
                return
            status=('SKIPPED_PONS' if reason.startswith('PONS_V2') else
                    'SKIPPED_V3' if reason=='V3_LAUNCH_EXCLUDED' else 'SKIPPED_UNSUPPORTED')
            self.store.db.execute("UPDATE selections SET status=?,reason=? WHERE selectionId=? AND status='QUEUED'",
                                  (status,reason,selectionId))
            print('EXECUTION BUY EXCLUDED',status,reason,token,flush=True)
            return
        deadlineStart=row['decisionAt']
        if lamb is not None:
            deadlineStart=lamb.readyAt(selectionId)
            if (deadlineStart is None or row['status']!='QUEUED' or
                    self.store.get('intakeState','OPEN')!='OPEN'):
                return
        if time.time()-deadlineStart>BUY_LAG_LIMIT:
            self.store.db.execute("UPDATE selections SET status='SKIPPED',reason='MISSED_ENTRY_DEADLINE' WHERE selectionId=?",(selectionId,))
            return
        try:
            rate=await self.fx.get()
            amountWei=int((self.store.buyGbp/rate*10**18).to_integral_value(rounding=ROUND_DOWN))
            # No one-way entry: verify a same-block executable-size reverse
            # quote before signing. This does not guarantee future liquidity.
            candidates=await self.swaps.verifiedBuyCandidates(token,amountWei)
            quote=candidates[0]
            print('ENTRY TWO_WAY_QUOTE',token,
                  'buyTokens',quote['output'],
                  'buySource',quote['venue']['source'],
                  'fullSellWei',quote.get('preBuySellOutput'),
                  'sellSource',quote.get('preBuySellSource'),
                  'peakSelectedRoute',quote.get('peakRoute'),
                  'block',quote.get('preBuySellBlock'),flush=True)
            if time.time()-deadlineStart>BUY_LAG_LIMIT:
                raise RuntimeError('MISSED_ENTRY_DEADLINE')
            # The detector's journal is not a lifetime registry. Verify the
            # token against the actual Pons V2 factory before ANY order is signed.
            # An unavailable or malformed response is not permission to buy.
            originReason=await self.ponsOnChain(token)
            if originReason:
                self.store.db.execute(
                    "UPDATE selections SET status='SKIPPED_PONS',reason=? WHERE selectionId=? AND status='QUEUED'",
                    (originReason,selectionId))
                print('EXECUTION BUY EXCLUDED SKIPPED_PONS',originReason,token,flush=True)
                return
            if time.time()-deadlineStart>BUY_LAG_LIMIT:
                raise RuntimeError('MISSED_ENTRY_DEADLINE')
            transaction=self.swaps.build(quote,amountWei,self.tx.account.address)
            # Intent ID is selection-derived and durable across restart.
            async with (self.intakeLock if lamb is not None else nullcontext()):
                if lamb is not None:
                    fresh=self.store.db.execute('SELECT status FROM selections WHERE selectionId=?',
                                                (selectionId,)).fetchone()
                    if (not fresh or fresh['status']!='QUEUED' or
                            self.store.get('intakeState','OPEN')!='OPEN' or
                            (self.store.flexLamb and self.store.acquired()+self.store.reserved()>self.store.purchaseLimit) or
                            lamb.revoked(selectionId) or
                            time.time()-deadlineStart>BUY_LAG_LIMIT):
                        return
                if lamb is not None and lamb.activity is not None:
                    try:
                        activityValid=await lamb.verifyActivityBeforeBuy(row)
                    except (RuntimeError,ValueError,TypeError,KeyError) as exc:
                        # Transient RPC failure cannot authorise buying. Retry within deadline.
                        print('BUY ACTIVITY PROOF UNAVAILABLE',token,str(exc)[:120],flush=True)
                        return
                    if not activityValid:
                        lamb.state(selectionId,'PROBE_UNVERIFIED',
                                   'ACTIVITY_PASS_PROOF_MISSING_OR_REORGED')
                        print('BUY BLOCKED ACTIVITY PROOF',token,flush=True)
                        return
                signed=await self.tx.create('buy:'+selectionId,selectionId,'BUY',transaction,amountWei)
                self.store.db.execute("UPDATE selections SET status='BUY_PENDING',quoteWei=? WHERE selectionId=?",
                                      (str(quote['output']),selectionId))
                state=signed['state']
                if state in ('SIGNED','SENT','UNCERTAIN'):
                    state=await self.tx.broadcast(signed['intentId'])
            if state=='SENT':
                print('BUY ACCEPTED_BY_RPC',token,signed['txHash'],flush=True)
            else:
                print('BUY NOT CONFIRMED_BY_RPC',token,signed['txHash'],'state='+str(state),flush=True)
        except (RuntimeError,ValueError) as exc:
            reason=str(exc)
            # Busy wallet may recover within the original 3-second deadline.
            if reason=='WALLET_TRANSACTION_IN_FLIGHT' and time.time()-deadlineStart<BUY_LAG_LIMIT:
                return
            self.store.db.execute("UPDATE selections SET status='SKIPPED',reason=? WHERE selectionId=?",
                                  (reason[:160],selectionId))
            print('BUY SKIPPED',token,reason[:120],flush=True)

    async def reconcileReceipts(self):
        # A crash can occur after durable signing but before updating selection
        # status. Any signed/confirmed buy is still a real purchase candidate.
        self.store.db.execute('''UPDATE selections SET status='BUY_PENDING'
          WHERE status IN ('QUEUED','SKIPPED') AND selectionId IN
          (SELECT selectionId FROM transactions WHERE kind='BUY')''')
        lamb=getattr(self,'lamb',None)
        if lamb is not None:
            self.store.db.execute("""UPDATE selections SET status='PROBE_PENDING'
                WHERE status='LAMB_QUEUED' AND selectionId IN
                (SELECT selectionId FROM transactions WHERE kind='PROBE_BUY')""")
        await self.tx.reconcile()
        async with (self.intakeLock if lamb is not None else nullcontext()):
            for row in self.store.db.execute("SELECT * FROM transactions WHERE state IN ('CONFIRMED','REVERTED') AND reason IS NULL OR state='REVERTED' AND reason='receipt status 0'"):
                # Only settle once; a non-null SETTLED marker is durable.
                if lamb is not None and row['kind']=='PROBE_BUY':
                    lamb.settle(row)
                    continue
                if row['kind']=='BUY':
                    selected=self.store.db.execute('SELECT * FROM selections WHERE selectionId=?',(row['selectionId'],)).fetchone()
                    if not selected or selected['status']!='BUY_PENDING':continue
                    if row['state']=='REVERTED':
                        self.store.db.execute("UPDATE selections SET status='SKIPPED',reason='REVERTED_BUY' WHERE selectionId=?",(row['selectionId'],))
                        continue
                    receipt=json.loads(row['receipt'])
                    qty=receiptTransfers(receipt,selected['token'],self.tx.account.address)
                    if qty<=0:
                        self.store.db.execute("UPDATE selections SET status='BUY_UNVERIFIED',reason='CONFIRMED_TX_NO_VERIFIED_TOKEN_TRANSFER' WHERE selectionId=?",(row['selectionId'],))
                        print('CRITICAL: BUY CONFIRMED BUT TOKEN QUANTITY UNVERIFIED',selected['token'],flush=True)
                        continue
                    now=time.time()
                    self.store.db.execute('BEGIN IMMEDIATE')
                    try:
                        self.store.db.execute('''INSERT OR IGNORE INTO positions
                           (selectionId,token,originalQty,spentWei,boughtAt,bestGbp,lastGrowth,nextDue)
                           VALUES(?,?,?,?,?,NULL,?,?)''',
                           (row['selectionId'],selected['token'],str(qty),row['inputAmount'],now,now,now))
                        self.store.db.execute("UPDATE selections SET status='BOUGHT' WHERE selectionId=?",(row['selectionId'],))
                        if getattr(self.store,'sacrifice',False) and self.store.acquired()>=self.store.purchaseLimit:
                            self.store.set('intakeState','CLOSED')
                            self.store.set('intakeClosedAt',now)
                        self.store.db.execute('COMMIT')
                    except BaseException:
                        if self.store.db.in_transaction:
                            self.store.db.execute('ROLLBACK')
                        raise
                    self.store.observe(token=selected['token'],phase='ENTRY',
                        at=now,block=str(receipt.get('blockNumber')),
                        venue_id=None,status='CONFIRMED_FILL',
                        quantity=str(qty),out_wei=str(row['inputAmount']),
                        gbp=str(self.store.buyGbp),multiple=None,basis='RECEIPT_TOKEN_TRANSFER',
                        reason=None)
                    print('BUY CONFIRMED',selected['token'],'qty',qty,flush=True)
                elif row['kind'].startswith('SELL_'):
                    position=self.store.db.execute('SELECT * FROM positions WHERE selectionId=?',(row['selectionId'],)).fetchone()
                    if not position:continue
                    stage=int(row['kind'].split('_')[1])
                    if position['sold'+str(stage)]:continue
                    if row['state']=='REVERTED':
                        # Revert uses a nonce, but it spent zero tokens. Retry from a new quote.
                        continue
                    receipt=json.loads(row['receipt'])
                    debited=-receiptTransfers(receipt,position['token'],self.tx.account.address)
                    if debited<=0:
                        self.store.db.execute('UPDATE positions SET error=? WHERE selectionId=?',
                            ('CONFIRMED_SELL_NO_VERIFIABLE_TOKEN_DEBIT',row['selectionId']))
                        print('CRITICAL: SELL TRANSFER UNVERIFIED; NO RETRY',position['token'],stage,flush=True)
                        continue
                    wethOut=max(0,receiptTransfers(receipt,WETH,self.tx.account.address))
                    self.store.db.execute('BEGIN IMMEDIATE')
                    try:
                        self.store.db.execute(f'UPDATE positions SET sold{stage}=1,soldQty{stage}=?,error=NULL WHERE selectionId=?',(str(debited),row['selectionId']))
                        self.store.db.execute('''INSERT OR IGNORE INTO sales
                             (selectionId,stage,txHash,tokenDebited,wethReceivedWei,confirmedAt)
                             VALUES(?,?,?,?,?,?)''',
                             (row['selectionId'],stage,row['txHash'],str(debited),
                              str(wethOut) if wethOut else None,time.time()))
                        self.store.db.execute('COMMIT')
                    except BaseException:
                        if self.store.db.in_transaction:
                            self.store.db.execute('ROLLBACK')
                        raise
                    print('SELL CONFIRMED',str(stage)+'x',position['token'],'qty',debited,row['txHash'],flush=True)

    def pendingFor(self,selectionId,kind):
        return self.store.db.execute("SELECT 1 FROM transactions WHERE selectionId=? AND kind=? AND state IN ('SIGNED','SENT')",
                  (selectionId,kind)).fetchone() is not None

    async def preApprove(self,position):
        # Approve sell spenders promptly after a confirmed buy, BEFORE any peak.
        # This is not a sale and does not change the 80/15/5 ladder.
        now=time.time(); selectionId=position['selectionId'];token=position['token']
        self.store.db.execute('UPDATE positions SET preApprovalNext=? WHERE selectionId=?',
                              (now+5,selectionId))
        try:
            # Approvals must not depend on a CURRENT successful quote: liquidity
            # may return after a temporary collapse. Only trusted active venues
            # from this position's own detector history are considered.
            venues=[v for v in self.store.venues(token)
                    if v['source'] in ('uniswapV3','uniswapV4')]
            if not venues:
                return
            approval=await self.swaps.approvalNeeded({'venue':venues[0]},token,
                                self.tx.account.address,int(position['originalQty']))
            if approval is None:
                self.store.db.execute('UPDATE positions SET preApprovalNext=? WHERE selectionId=?',
                                      (now+300,selectionId))
                return
            action='APPROVE_'+approval[0]+'_'+approval[2].lower()
            history=self.store.transactions(selectionId,action)
            if any(t['state'] in ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK') for t in history):return
            if len(history)>=3:return
            tx=await self.tx.create('approve:'+selectionId+':'+action+':'+str(len(history)+1),
                          selectionId,action,self.swaps.buildApproval(approval))
            state=await self.tx.broadcast(tx['intentId'])
            print('EARLY SELL APPROVAL',token,approval[0],'state='+str(state),flush=True)
        except (RuntimeError,ValueError) as exc:
            print('EARLY SELL APPROVAL RETRY',token,str(exc)[:160],flush=True)

    async def prepareSell(self,position):
        selectionId=position['selectionId'];token=position['token']
        manual=getattr(self,'manual',None)
        if manual and (manual.active(selectionId) or
                            manual.mainRemaining(position)<=0):
            return
        if position['error']:
            return
        # Persist retries across restarts. An illiquid/reverting market must not
        # cause four quote attempts per second forever.
        retryKey='sellRetryAfter:'+selectionId
        if time.time()<float(self.store.get(retryKey,'0')):
            return
        stage=next((x for x,_ in getattr(self,'stages',STAGES) if position['trigger'+str(x)] and not position['sold'+str(x)]),None)
        if stage is None:return
        kind='SELL_'+str(stage)
        if self.pendingFor(selectionId,kind):return
        attempts=self.store.transactions(selectionId,kind)
        if any(t['state']=='CONFIRMED' for t in attempts):return  # Receipt still unverified => no double sell.
        try:
            original=int(position['originalQty']);balance=await self.balance(token)
            lamb=getattr(self,'lamb',None)
            tradable=max(0,balance-lamb.probeQuantity(selectionId)) if lamb else balance
            amount=(manual.autoSlice(position,stage,tradable)
                    if manual and (self.store.newLadder2x or self.store.flexPilot or manual.hasManualFills(selectionId))
                    else checkSlice(original,stage,tradable,
                                    int(position['soldQty3']),int(position['soldQty5']),
                                    sacrifice=bool(lamb)))
            if amount<=0 or tradable<amount:
                raise RuntimeError('INSUFFICIENT_POSITION_TOKENS')
            quotes=await self.swaps.directQuotes(token,amount,False)
            quote=quotes[0]
            approval=await self.swaps.approvalNeeded(quote,token,self.tx.account.address,amount)
            if approval:
                action='APPROVE_'+approval[0]+'_'+approval[2].lower()
                history=self.store.transactions(selectionId,action)
                if any(t['state'] in ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK') for t in history):return
                if len(history)>=3:raise RuntimeError('TOKEN_APPROVAL_RETRIES_EXHAUSTED')
                approvalTx=self.swaps.buildApproval(approval)
                intent='approve:'+selectionId+':'+action+':'+str(len(history)+1)
                if manual and manual.active(selectionId):return
                tx=await self.tx.create(intent,selectionId,action,approvalTx)
                state=await self.tx.broadcast(tx['intentId'])
                print('APPROVAL',token,approval[0],approval[2],'state='+str(state),flush=True)
                return
            if len(attempts)>=3:
                raise RuntimeError('SALE_RETRIES_EXHAUSTED')
            swapTx=self.swaps.build(quote,amount,self.tx.account.address)
            intent='sell:'+selectionId+':'+str(stage)+':'+str(len(attempts)+1)
            if manual and manual.active(selectionId):return
            tx=await self.tx.create(intent,selectionId,kind,swapTx,amount)
            state=await self.tx.broadcast(tx['intentId'])
            print('SELL SUBMISSION',str(stage)+'x',token,tx['txHash'],
                  'peakSelectedRoute='+str(quote.get('peakRoute')),
                  'state='+str(state),flush=True)
        except (RuntimeError,ValueError) as exc:
            reason=str(exc)
            if reason=='WALLET_TRANSACTION_IN_FLIGHT':
                return
            self.store.set(retryKey,time.time()+10.0)
            print('SELL RETRY IN 10s',str(stage)+'x',token,reason[:200],flush=True)

    async def checkPosition(self,position):
        token=position['token'];now=time.time()
        manual=getattr(self,'manual',None)
        if manual and manual.mainRemaining(position)<=0:
            return
        try:
            await self.swaps.refreshPons(token)
        except (RuntimeError,ValueError):
            pass
        try:
            fullQuotes=await self.swaps.directQuotes(token,int(position['originalQty']),False)
            quote=fullQuotes[0]
            rate=await self.fx.get()
            value=Decimal(quote['output'])*rate/Decimal(10**18)
            multiple=value/self.store.buyGbp
            old=Decimal(position['bestGbp'] or '0')
            growth=value>old
            lastGrowth=now if growth else position['lastGrowth']
            triggers={stage:1 if multiple>=getattr(self,'exitThresholds',{}).get(stage,Decimal(stage)) else position['trigger'+str(stage)] for stage,_ in getattr(self,'stages',STAGES)}
            if self.store.flexPilot:
                self.store.db.execute('''UPDATE positions SET bestGbp=?,lastGrowth=?,lastCheck=?,nextDue=?,
                         trigger3=?,trigger5=? WHERE selectionId=?''',
                    (str(value) if growth else position['bestGbp'],lastGrowth,now,
                     now+nextInterval(lastGrowth,now),triggers[3],triggers[5],position['selectionId']))
            elif self.store.newLadder2x:
                self.store.db.execute('''UPDATE positions SET bestGbp=?,lastGrowth=?,lastCheck=?,nextDue=?,
                         trigger2=?,trigger50=?,trigger100=? WHERE selectionId=?''',
                    (str(value) if growth else position['bestGbp'],lastGrowth,now,
                     now+nextInterval(lastGrowth,now),triggers[2],triggers[50],triggers[100],position['selectionId']))
            elif getattr(self,'lamb',None) is None:
                self.store.db.execute('''UPDATE positions SET bestGbp=?,lastGrowth=?,lastCheck=?,nextDue=?,
                         trigger3=?,trigger5=?,trigger10=? WHERE selectionId=?''',
                    (str(value) if growth else position['bestGbp'],lastGrowth,now,
                     now+nextInterval(lastGrowth,now),triggers[3],triggers[5],triggers[10],position['selectionId']))
            else:
                self.store.db.execute('''UPDATE positions SET bestGbp=?,lastGrowth=?,lastCheck=?,nextDue=?,
                         trigger3=?,trigger5=?,trigger1000=? WHERE selectionId=?''',
                    (str(value) if growth else position['bestGbp'],lastGrowth,now,
                     now+nextInterval(lastGrowth,now),triggers[3],triggers[5],triggers[1000],position['selectionId']))
            self.store.observe(token=token,phase='SELL',at=now,
                block=quote.get('block'),venue_id=quote.get('peakRoute'),
                status='ONCHAIN_QUOTE_ONLY',
                quantity=str(position['originalQty']),
                out_wei=str(quote['output']),gbp=str(value),
                multiple=str(multiple),basis=quote.get('basis'),reason=None)
        except (RuntimeError,ValueError) as exc:
            self.store.db.execute('UPDATE positions SET lastCheck=?,nextDue=? WHERE selectionId=?',
                (now,now+nextInterval(position['lastGrowth'],now),position['selectionId']))
            self.store.observe(token=token,phase='SELL',at=now,block=None,
                venue_id=None,status='UNAVAILABLE',
                quantity=str(position['originalQty']),out_wei=None,gbp=None,
                multiple=None,basis=None,reason=str(exc)[:200])
            # Graduation or temporary missing liquidity is not a valueless position.
            try:await self.swaps.refreshPons(token,force=True)
            except (RuntimeError,ValueError):pass

    async def runJob(self,key,coro):
        try:await coro
        except Exception as exc:
            print('ENGINE JOB ERROR',key,type(exc).__name__,str(exc)[:130],flush=True)
        finally:self.active.discard(key)

    def spawn(self,key,coro):
        if key in self.active:
            coro.close()
            return
        self.active.add(key)
        task=asyncio.create_task(self.runJob(key,coro))
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def run(self):
        assert int(await self.rpc.call('eth_chainId',[]),16)==4663
        await self.fx.get()  # Cache FX before first five-second selector decision.
        title=('BLACK SWAN 3 | FLEX '+str(self.store.purchaseLimit)+' x GBP '+str(self.store.buyGbp)+' | 80% 1.5x / 20% 3x / NO RESERVE'
               if self.store.flexEarly else
               'BLACK SWAN 3 | FLEX '+str(self.store.purchaseLimit)+' x GBP '+str(self.store.buyGbp)+' | 80% 3x / 20% 5x / NO RESERVE'
               if self.store.flexPilot else
               'BLACK SWAN 3 | FLEX '+str(self.store.purchaseLimit)+' x GBP '+str(self.store.buyGbp)+' | 80% 2x / 10% 50x / 5% 100x / 5% MANUAL ONLY'
               if self.store.flexLamb else
               'BLACK SWAN 3 | 1 x GBP 0.20 | 80% 2x / 10% 50x / 5% 100x / 5% MANUAL ONLY'
               if self.store.oneTwenty2x else
               'BLACK SWAN 3 | 2 x GBP 0.50 | 80% 2x / 10% 50x / 5% 100x / 5% MANUAL ONLY'
               if self.store.twoFifty2x else
               'BLACK SWAN 3 | EXISTING EXECUTOR + 1P LAMB | '+str(self.store.purchaseLimit)+' x GBP '+str(self.store.buyGbp)+' | 80% 3x / 10% 5x / 10% 1000x'
               if self.lamb else
               'BLACK SWAN 3 V4-SELECTED CANARIES | original peak quotes | 20 x GBP 0.10 | 80% at 3x / 15% at 5x / balance at 10x | NO FORCED EXITS')
        print(title,flush=True)
        while True:
            self.readSelections()
            self.readRevocations()
            self.readNewVenues()
            await self.reconcileReceipts()
            if self.manual:self.manual.tick()
            now=time.time()
            if self.lamb and self.store.get('intakeState','OPEN')=='CLOSED' and now-self.lastVenueCheck>=0.4:
                self.lastVenueCheck=now
                self.spawn('HELD_VENUES',self.venueWatcher.scanOnce())
            # A latched threshold creates an urgent sale obligation. Launch
            # those exits before any fresh buy can take the wallet nonce.
            positions=self.store.db.execute(
                ("SELECT * FROM positions WHERE sold100=0 ORDER BY boughtAt" if self.store.newLadder2x
                 else "SELECT * FROM positions WHERE sold1000=0 ORDER BY boughtAt" if self.lamb
                 else "SELECT * FROM positions WHERE sold10=0 ORDER BY boughtAt")).fetchall()
            urgentExit=False
            for position in positions:
                if self.manual and self.manual.mainRemaining(position)<=0:
                    continue
                if position['nextDue']<=now:
                    self.spawn('QUOTE:'+position['selectionId'],
                               self.checkPosition(position))
                triggered=any(position['trigger'+str(stage)] and
                              not position['sold'+str(stage)]
                              for stage,_ in self.stages)
                if triggered:
                    urgentExit=True
                    self.spawn('EXIT:'+position['selectionId'],
                               self.prepareSell(position))
            # Once a threshold is latched, existing holdings take priority.
            # Otherwise, prepare exit allowances before opening more positions:
            # new buys must not consume the next wallet nonce first.
            approvalDue=False
            if not urgentExit:
                for position in positions:
                    if self.manual and (self.manual.active(position['selectionId'])
                                        or self.manual.mainRemaining(position)<=0):
                        continue
                    if (not any(position['trigger'+str(stage)] for stage,_ in self.stages)
                            and position['preApprovalNext']<=now):
                        approvalDue=True
                        self.spawn('APPROVE:'+position['selectionId'],
                                   self.preApprove(position))
            approvalActive=any(key.startswith('APPROVE:') for key in self.active)
            if not urgentExit and not approvalDue and not approvalActive:
                for row in self.store.db.execute(
                    "SELECT * FROM selections WHERE status='QUEUED' ORDER BY decisionAt LIMIT 20"):
                    self.spawn('BUY:'+row['selectionId'],self.buy(row))
                if self.lamb and self.store.get('intakeState','OPEN')=='OPEN':
                    for row in self.store.db.execute(
                        "SELECT * FROM selections WHERE status='PROBE_CONFIRMED' ORDER BY decisionAt LIMIT 20"):
                        if self.lamb.activity is not None:
                            self.spawn('ACTIVITY:'+row['selectionId'],self.lamb.collectActivity(row))
                        self.spawn('LAMB_CHECK:'+row['selectionId'],self.lamb.check(row))
                    if self.store.eligible():
                        for row in self.store.db.execute(
                            "SELECT * FROM selections WHERE status='LAMB_QUEUED' ORDER BY decisionAt LIMIT 20"):
                            self.spawn('LAMB_BUY:'+row['selectionId'],self.lamb.start(row))
            if self.finance:
                self.spawn('FINANCE',self.finance.tick())
            if now-self.lastStatus>=5:
                self.lastStatus=now
                if self.lamb:self.store.set('executorHeartbeat',now)
                if self.manual:self.store.set('manualExitHeartbeat',now)
                print('LIVE HEALTH',json.dumps(self.store.status()),flush=True)
            await asyncio.sleep(0.25)

    async def close(self):
        if self.venueWatcher is not None:await self.venueWatcher.close()
        for task in self.jobs:task.cancel()
        if self.jobs:await asyncio.gather(*self.jobs,return_exceptions=True)
        self.selector.close();self.detector.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','status'])
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--selector-db',type=Path)
    parser.add_argument('--detector-db',type=Path)
    parser.add_argument('--env',type=Path,default=Path.home()/'blackswanRH_guarded/.env')
    parser.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    parser.add_argument('--password-fd',type=int)
    parser.add_argument('--arm-live',action='store_true')
    parser.add_argument('--sacrifice-five',action='store_true')
    parser.add_argument('--sacrifice-one-50p',action='store_true')
    parser.add_argument('--sacrifice-two-50p',action='store_true')
    parser.add_argument('--sacrifice-two-50p-2x',action='store_true')
    parser.add_argument('--sacrifice-one-20p-2x',action='store_true')
    parser.add_argument('--flex-lamb',action='store_true')
    parser.add_argument('--flex-3x5x',action='store_true')
    parser.add_argument('--flex-1p5x3x',action='store_true')
    parser.add_argument('--buy-gbp')
    parser.add_argument('--max-purchases')
    parser.add_argument('--campaign-wallet')
    parser.add_argument('--probe-wait-blocks',type=int,choices=(500,),
                        help='500-block survival gate (frozen by campaign manifest)')
    parser.add_argument('--probe-max-buying-blocks',type=int,choices=(7,),
                        help='Versioned post-probe activity gate (new campaigns only)')
    args=parser.parse_args()
    if sum((args.sacrifice_five,args.sacrifice_one_50p,args.sacrifice_two_50p,args.sacrifice_two_50p_2x,args.sacrifice_one_20p_2x,args.flex_lamb,args.flex_3x5x,args.flex_1p5x3x))>1:
        parser.error('Choose only one sacrificial campaign mode')
    lambMode=args.sacrifice_five or args.sacrifice_one_50p or args.sacrifice_two_50p or args.sacrifice_two_50p_2x or args.sacrifice_one_20p_2x or args.flex_lamb or args.flex_3x5x or args.flex_1p5x3x
    if args.flex_lamb or args.flex_3x5x or args.flex_1p5x3x:
        try:flexSettings(args.buy_gbp,args.max_purchases)
        except ValueError as exc:parser.error(str(exc))
        if args.campaign_wallet is None:parser.error('Flex campaign requires --campaign-wallet')
    elif any(v is not None for v in (args.buy_gbp,args.max_purchases,args.campaign_wallet)):
        parser.error('Flex settings only valid with --flex-lamb')
    if args.probe_wait_blocks is not None and not lambMode:
        parser.error('--probe-wait-blocks requires a sacrificial mode')
    if args.probe_max_buying_blocks is not None and (
            args.probe_wait_blocks!=500 or not lambMode):
        parser.error('--probe-max-buying-blocks requires a 500-block sacrificial campaign')
    if args.command=='status':
        if not args.db.exists():parser.error('Campaign database not found')
        store=ExecutionStore(args.db,sacrifice=lambMode,oneFifty=args.sacrifice_one_50p,
                             twoFifty=args.sacrifice_two_50p,
                             twoFifty2x=args.sacrifice_two_50p_2x,
                             oneTwenty2x=args.sacrifice_one_20p_2x,
                             flexLamb=args.flex_lamb,flexPilot=args.flex_3x5x,flexEarly=args.flex_1p5x3x,
                             flexBuyGbp=args.buy_gbp,
                             flexMaxPurchases=args.max_purchases,flexWalletAddress=args.campaign_wallet,
                             probeWaitBlocks=args.probe_wait_blocks,
                             probeMaxBuyingBlocks=args.probe_max_buying_blocks)
        print(json.dumps(store.status(),indent=2))
        store.db.close()
        return
    if not args.arm_live or args.password_fd is None:
        parser.error('Live trading requires --arm-live and inherited --password-fd')
    if not args.selector_db or not args.detector_db:
        parser.error('Selector and detector databases required')
    import os
    with os.fdopen(args.password_fd,'r',encoding='utf-8') as reader:
        password=reader.read().rstrip('\n')
    account,privateKey=decodeKeystore(args.wallet_file,password)
    del password
    if (args.flex_lamb or args.flex_3x5x or args.flex_1p5x3x) and account.address.lower()!=args.campaign_wallet.lower():
        raise SystemExit('FLEX_WALLET_KEYSTORE_MISMATCH')
    store=ExecutionStore(args.db,sacrifice=lambMode,oneFifty=args.sacrifice_one_50p,
                             twoFifty=args.sacrifice_two_50p,
                             twoFifty2x=args.sacrifice_two_50p_2x,
                             oneTwenty2x=args.sacrifice_one_20p_2x,
                             flexLamb=args.flex_lamb,flexPilot=args.flex_3x5x,flexEarly=args.flex_1p5x3x,
                             flexBuyGbp=args.buy_gbp,
                             flexMaxPurchases=args.max_purchases,flexWalletAddress=args.campaign_wallet,
                             probeWaitBlocks=args.probe_wait_blocks,
                             probeMaxBuyingBlocks=args.probe_max_buying_blocks)
    # Lock per wallet across campaign directories. Must not run two live engines.
    walletLock=(globalWalletLock(account.address) if store.flexLamb else
                Path(__file__).resolve().parent/('.live_wallet_'+account.address.lower()+'.lock'))
    walletLock.parent.mkdir(parents=True,exist_ok=True)
    with walletLock.open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another campaign is using this wallet')
        if store.flexLamb:
            overlaps=otherLiveExecutors()
            if overlaps:
                raise SystemExit('OLDER_LIVE_EXECUTOR_RUNNING: stop earlier campaign executors: '+str(overlaps))
        async def runner():
            url,wallet=peak.loadSettings(args.env,None,args.wallet_file)
            if wallet.lower()!=account.address.lower():
                raise RuntimeError('Public wallet / keystore mismatch')
            async with aiohttp.ClientSession() as session:
                rpc=peak.Rpc(session,url,account.address)
                executor=LiveExecutor(store,args.selector_db,args.detector_db,rpc,peak.Fx(session),
                                      TransactionJournal(store,rpc,account,privateKey))
                if executor.venueWatcher is not None:
                    # Reuse the existing detector's exact configured websocket endpoint.
                    from detector import loadEnv
                    executor.venueWatcher.wsUrl=loadEnv(args.env)['ROBINHOOD_ALCHEMY_WSS_URL']
                try:await executor.run()
                finally:await executor.close()
        try:asyncio.run(runner())
        except KeyboardInterrupt:print('LIVE STOPPED - data preserved, resume required for open positions',flush=True)
        finally:store.db.close()


if __name__=='__main__':main()