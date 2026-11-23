"""Early 1.5x/3x 80/20 isolated real-executor offline tests; no on-chain broadcasts."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from campaign_config import (
    EARLY_KIND, EARLY_POLICY, EARLY_EXIT, EARLY_TRANCHES, EARLY_PREFIX,
    PILOT_KIND, PILOT_POLICY, earlyManifest, verifyEarlyLedger, pilotManifest
)
from live_executor import ExecutionStore, LiveExecutor
from test_flex_campaign import TOKEN, WALLET, Fx, FakeRpc, ManualRoutes, PendingJournal, sellReceipt


def earlyConfig():
    return dict(chainId=4663,mode='REAL_MONEY',campaignKind=EARLY_KIND,
        walletPublicAddress=WALLET,targetPurchases=2,gbpEach='0.20',
        configVersion=4,entryPolicy='uniswapV4_nonPons_only',
        quoteEngine='original_peak_tracker_Tracker_best',
        exitPolicy=EARLY_EXIT,sellTranches=EARLY_TRANCHES.copy(),
        probeGbp='0.01',survivalBlocks=500,postProbeBuyingBlockGate='OFF',
        selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)


class EarlyPolicyTests(unittest.TestCase):
    def test_campaign_identity_and_old_mode_rejection(self):
        cfg=earlyConfig()
        self.assertEqual(earlyManifest(cfg,WALLET),('0.20',2,WALLET))
        self.assertEqual(cfg['sellTranches'],{'1.5x':80,'3x':20})
        self.assertEqual(EARLY_PREFIX,'v4_lamb_flex1p5x3x_')
        self.assertNotEqual(EARLY_KIND,PILOT_KIND)
        self.assertNotEqual(EARLY_POLICY,PILOT_POLICY)
        with self.assertRaises(ValueError):
            pilotManifest(cfg,WALLET)

    def test_fail_closed_for_old_or_tampered_manifest(self):
        for key,value in [
            ('configVersion',3),('campaignKind',PILOT_KIND),
            ('exitPolicy','latched_3x_5x_full'),
            ('postProbeBuyingBlockGate','ON'),
            ('survivalBlocks',499),('selectorDecisionSeconds',5),
            ('selectorMaxBuyingBlocks',11),
            ('sellTranches',{'1.5x':79,'3x':21}),
            ('probeMaxBuyingBlocks',7),('survivalSeconds',10),
        ]:
            cfg=earlyConfig()
            cfg[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                earlyManifest(cfg,WALLET)

    def test_early_store_version_and_cross_mode_resume_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'execution.sqlite3'
            store=ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                flexEarly=True,flexBuyGbp='0.20',
                flexMaxPurchases=2,flexWalletAddress=WALLET)
            self.assertEqual(store.campaignKind,EARLY_KIND)
            self.assertEqual(store.get('exitPolicy'),EARLY_POLICY)
            self.assertEqual(store.get('flexConfigVersion'),'3')
            self.assertIsNone(store.probeMaxBuyingBlocks)
            self.assertTrue(verifyEarlyLedger(db,'0.20',2,WALLET))
            self.assertIn('manual_requests',[
                r[0] for r in store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")])
            store.db.close()
            before=db.read_bytes()
            with self.assertRaises(RuntimeError):
                ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                    flexPilot=True,flexBuyGbp='0.20',
                    flexMaxPurchases=2,flexWalletAddress=WALLET)
            self.assertEqual(db.read_bytes(),before)
            with self.assertRaises(RuntimeError):
                verifyEarlyLedger(db,'0.30',2,WALLET)
            self.assertEqual(db.read_bytes(),before)

    def test_experimental_cli_flag_exists(self):
        result=subprocess.run([sys.executable,'run_live_campaign.py','--help'],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--flex-1p5x3x',result.stdout)


class EarlyLadderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=ExecutionStore(self.root/'execution.sqlite3',
            sacrifice=True,probeWaitBlocks=500,flexEarly=True,
            flexBuyGbp='0.20',flexMaxPurchases=2,flexWalletAddress=WALLET)
        (self.root/'campaign.json').write_text(json.dumps(earlyConfig()))
        selector=self.root/'selector.sqlite3'
        with sqlite3.connect(selector) as db:
            db.execute('CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)')
            db.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        detector=self.root/'detector.sqlite3'
        with sqlite3.connect(detector) as db:
            db.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        self.tx=PendingJournal(self.store)
        self.engine=LiveExecutor(self.store,selector,detector,FakeRpc(),Fx(),self.tx)
        self.routes=ManualRoutes()
        self.engine.swaps=self.routes
        self.balanceVal=110
        async def balance(token):
            self.assertEqual(token,TOKEN)
            return self.balanceVal
        self.engine.balance=balance
        self.sid='early-exit-test'
        self.store.db.execute(
            "INSERT INTO selections(selectionId,token,decisionAt,event,status) "
            "VALUES(?,?,0,'{}','BOUGHT')",(self.sid,TOKEN))
        self.store.db.execute(
            """INSERT INTO positions
            (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,'100','200000000000000',1,1,1)""",(self.sid,TOKEN))
        self.store.db.execute(
            """INSERT INTO lamb_probes
            (selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
            VALUES(?,?,?,1,'10','100','PROBE_PASSED')""",
            (self.sid,TOKEN,'0x'+'b'*64))

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def pos(self):
        return self.store.db.execute(
            'SELECT * FROM positions WHERE selectionId=?',(self.sid,)).fetchone()

    async def confirmed(self,stage,qty):
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt=? WHERE kind=?",
            (json.dumps(sellReceipt(qty)),'SELL_'+str(stage)))
        await self.engine.reconcileReceipts()
        self.assertEqual(self.pos()['sold'+str(stage)],1)
        self.assertEqual(int(self.pos()['soldQty'+str(stage)]),qty)
        self.balanceVal-=qty

    async def test_exact_threshold_boundaries_and_confirmed_80_20_main_only(self):
        self.assertEqual(self.engine.stages,((3,80),(5,20)))
        self.assertEqual(self.engine.exitThresholds,
                         {3:Decimal('1.5'),5:Decimal('3')})
        self.assertIsNone(self.engine.lamb.activity)
        self.routes.estimatePerUnit=2_980_000_000_000  # 1.49x of £0.20
        await self.engine.checkPosition(self.pos())
        self.assertEqual((self.pos()['trigger3'],self.pos()['trigger5']),(0,0))
        self.routes.estimatePerUnit=3_000_000_000_000  # exactly 1.50x
        await self.engine.checkPosition(self.pos())
        self.assertEqual((self.pos()['trigger3'],self.pos()['trigger5']),(1,0))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_3',80))
        await self.confirmed(3,80)

        self.routes.estimatePerUnit=5_980_000_000_000  # 2.99x, no 2nd trigger
        await self.engine.checkPosition(self.pos())
        self.assertEqual((self.pos()['trigger3'],self.pos()['trigger5']),(1,0))
        self.routes.estimatePerUnit=6_000_000_000_000  # exactly 3x
        await self.engine.checkPosition(self.pos())
        self.assertEqual((self.pos()['trigger3'],self.pos()['trigger5']),(1,1))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_5',20))
        await self.confirmed(5,20)

        self.assertEqual(self.balanceVal,10)  # 1p probe tokens retained
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),0)
        self.assertEqual(self.store.status()['openPositions'],0)
        self.assertEqual([(r[0],r[1]) for r in self.store.db.execute(
            'SELECT stage,tokenDebited FROM sales ORDER BY stage')],
            [(3,'80'),(5,'20')])
        before=list(self.tx.orders)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders,before)

    def test_status_uses_true_trigger_names_for_early_campaign(self):
        status=self.store.status()
        self.assertEqual(status['positions'][0]['triggered1p5x'],False)
        self.assertEqual(status['positions'][0]['triggered3x'],False)
        self.assertEqual(status['positions'][0]['sold1p5x'],False)
        self.assertEqual(status['positions'][0]['sold3x'],False)
        self.assertNotIn('triggered5x',status['positions'][0])

    def test_manual_cli_and_dashboard_early_labels(self):
        from manual_sale import connect, remainingMain
        from live_dashboard import snapshot, card_positions, card_proceeds
        db,meta=connect(self.root)
        with db:
            position=db.execute('SELECT * FROM positions LIMIT 1').fetchone()
            self.assertEqual(remainingMain(db,position),100)
        db.close()
        view=snapshot(self.root)
        self.assertTrue(view['summary']['earlyMode'])
        self.assertTrue(view['summary']['pilotMode'])
        panel=card_positions(view)
        labels=[str(c.header) for c in panel.renderable.columns]
        self.assertIn('1.5× 80%',labels)
        self.assertIn('3× 20%',labels)
        self.assertNotIn('5× 20%',labels)


if __name__=='__main__':
    unittest.main()
