"""Offline configurable GBP main-size and count full-executor regression: 2x/50x/100x and 5% manual reserve.

No RPC broadcast, private key, or live wallet. Old 3x/5x/1000x tests coexist.
"""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from live_executor import ExecutionStore, LiveExecutor
from campaign_config import FLEX_KIND, FLEX_POLICY, FLEX_EXIT, FLEX_TRANCHES, buyAmount, purchaseCount, flexSettings, flexManifest, verifyFlexLedger, globalWalletLock
from test_manual_sale import FakeRpc, ManualRoutes, PendingJournal, sellReceipt
from test_sacrificial_integration import TOKEN, WALLET, Fx


class FlexConfigTests(unittest.TestCase):
    def test_exact_stakes_and_counts(self):
        for value,canonical in [('0.2','0.20'),('0.20','0.20'),
                                ('0.50','0.50'),('1','1.00'),('13.45','13.45')]:
            self.assertEqual(buyAmount(value),canonical)
        self.assertEqual(flexSettings('0.2','3'),('0.20',3))
        self.assertEqual(purchaseCount('0003'),3)

    def test_reject_float_exponents_negative_extra_decimals_and_outsize(self):
        for invalid in ('0','0.00','-1','1.001','1e3','nan','+0.20',
                        ' 0.20','1000.01',False,0.2,None):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                buyAmount(invalid)
        for invalid in (0,-1,True,'0','1.5','x','1001',None):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                purchaseCount(invalid)
        with self.assertRaisesRegex(ValueError,'FLEX_COMMITMENT_LIMIT'):
            flexSettings('1000','11')

    def manifest(self,stake='0.20',count=3):
        return dict(name='v4_lamb_flex2x_test',createdUtc='2026-11-11T00:00:00Z',
                    chainId=4663,walletPublicAddress=WALLET,mode='REAL_MONEY',
                    targetPurchases=count,gbpEach=stake,configVersion=1,
                    entryPolicy='uniswapV4_nonPons_only',
                    quoteEngine='original_peak_tracker_Tracker_best',
                    exitPolicy=FLEX_EXIT,sellTranches=FLEX_TRANCHES.copy(),
                    campaignKind=FLEX_KIND,probeGbp='0.01',survivalBlocks=500,
                    selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)

    def test_immutable_manifest_valid(self):
        self.assertEqual(flexManifest(self.manifest(),WALLET),('0.20',3,WALLET))
        self.assertEqual(globalWalletLock(WALLET),
                         globalWalletLock(WALLET.upper().replace('0X','0x')))

    def test_new_campaign_can_choose_different_amounts_and_counts(self):
        for stake,count in (('0.20',1),('0.20',3),('0.50',5),('1.25',9)):
            with self.subTest(stake=stake,count=count):
                self.assertEqual(flexManifest(self.manifest(stake,count),WALLET),
                                 (stake,count,WALLET))

    def test_reject_changed_manifest_fields(self):
        bad={'chainId':1,
             'probeGbp':'0.02','configVersion':2,
             'selectorDecisionSeconds':5,'selectorMaxBuyingBlocks':11,
             'survivalBlocks':100,'exitPolicy':'different',
             'quoteEngine':'unknown','campaignKind':'OLD',
             'entryPolicy':'any','walletPublicAddress':'0x'+'2'*40}
        for k,v in bad.items():
            manifest=self.manifest()
            manifest[k]=v
            with self.subTest(field=k),self.assertRaises(ValueError):
                flexManifest(manifest,WALLET)
        manifest=self.manifest()
        manifest['survivalSeconds']=10
        with self.assertRaises(ValueError):flexManifest(manifest,WALLET)
        manifest=self.manifest()
        manifest['sellTranches']['2x']=70
        with self.assertRaises(ValueError):flexManifest(manifest,WALLET)

    def test_executor_resume_rejects_mismatched_db_before_write(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'execution.sqlite3'
            store=ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                                 flexLamb=True,flexBuyGbp='0.20',
                                 flexMaxPurchases=3,flexWalletAddress=WALLET)
            store.db.close()
            before=db.read_bytes()
            self.assertTrue(verifyFlexLedger(db,'0.20',3,WALLET))
            for altered in (('0.50',3,WALLET),('0.20',4,WALLET),
                            ('0.20',3,'0x'+'2'*40)):
                with self.subTest(altered=altered),self.assertRaisesRegex(RuntimeError,'FLEX_LEDGER_CONFIG_MISMATCH'):
                    verifyFlexLedger(db,*altered)
                self.assertEqual(db.read_bytes(),before)
            for opts in (dict(flexBuyGbp='0.50',flexMaxPurchases=3,flexWalletAddress=WALLET),
                         dict(flexBuyGbp='0.20',flexMaxPurchases=4,flexWalletAddress=WALLET),
                         dict(flexBuyGbp='0.20',flexMaxPurchases=3,
                              flexWalletAddress='0x'+'2'*40)):
                with self.subTest(options=opts),self.assertRaisesRegex(RuntimeError,'FLEX_IMMUTABLE'):
                    ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                                   flexLamb=True,**opts)
                self.assertEqual(db.read_bytes(),before)
            reopened=ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                                    flexLamb=True,flexBuyGbp='0.20',
                                    flexMaxPurchases=3,flexWalletAddress=WALLET)
            self.assertEqual(reopened.get('flexConfigVersion'),'1')
            reopened.db.close()

class NewLadderIdentityTests(unittest.TestCase):
    def test_new_launcher_argparse_help_initializes_without_wallet(self):
        """Catches invalid %-format help text before a live start can be attempted."""
        script=Path(__file__).with_name('run_live_campaign.py')
        result=subprocess.run([sys.executable,str(script),'--help'],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--flex-lamb',result.stdout)
        self.assertIn('80% at 2x',result.stdout)

    def test_new_policy_identity_and_old_database_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            new=Path(folder)/'new.sqlite3'
            store=ExecutionStore(new,sacrifice=True,probeWaitBlocks=500,flexLamb=True,flexBuyGbp='0.20',flexMaxPurchases=3,flexWalletAddress=WALLET)
            self.assertEqual(store.campaignKind,FLEX_KIND)
            self.assertEqual(store.purchaseLimit,3)
            self.assertEqual(store.buyGbp,Decimal('0.20'))
            self.assertEqual(store.get('exitPolicy'),'80-10-5:2-50-100:manual5')
            self.assertEqual(store.get('manualExitVersion'),'1')
            self.assertEqual(store.get('probeGbp'),'0.01')
            self.assertEqual(store.get('waitBlocks'),'500')
            for stage in (2,50,100):
                self.assertEqual(store.db.execute(
                    'SELECT COUNT(*) FROM pragma_table_info(?) WHERE name=?',
                    ('positions','soldQty'+str(stage))).fetchone()[0],1)
            store.db.close()
            before=new.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'NOT_A_FIVE_LAMB_DB'):
                ExecutionStore(new,sacrifice=True,probeWaitBlocks=500,twoFifty=True)
            self.assertEqual(new.read_bytes(),before)
            old=Path(folder)/'old.sqlite3'
            legacy=ExecutionStore(old,sacrifice=True,probeWaitBlocks=500,twoFifty=True)
            self.assertEqual(legacy.get('exitPolicy'),'80-10-10:3-5-1000')
            legacy.db.close()
            prior=old.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'NOT_A_FIVE_LAMB_DB'):
                ExecutionStore(old,sacrifice=True,probeWaitBlocks=500,flexLamb=True,flexBuyGbp='0.20',flexMaxPurchases=3,flexWalletAddress=WALLET)
            self.assertEqual(old.read_bytes(),prior)

    def test_operator_flag_rejects_wrong_500_block_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'FLEX_REQUIRES'):
                ExecutionStore(Path(folder)/'wrong.sqlite3',sacrifice=True,flexLamb=True,flexBuyGbp='0.20',flexMaxPurchases=3,flexWalletAddress=WALLET)
            with self.assertRaisesRegex(ValueError,'CONFLICTING_FLEX_CAMPAIGN_MODES'):
                ExecutionStore(Path(folder)/'conflict.sqlite3',sacrifice=True,
                               probeWaitBlocks=500,twoFifty=True,flexLamb=True,flexBuyGbp='0.20',flexMaxPurchases=3,flexWalletAddress=WALLET)


class NewLadderFullExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=ExecutionStore(self.root/'execution.sqlite3',sacrifice=True,
                                  probeWaitBlocks=500,flexLamb=True,flexBuyGbp='0.20',flexMaxPurchases=3,flexWalletAddress=WALLET)
        self.selector=self.root/'selector.sqlite3'
        with sqlite3.connect(self.selector) as db:
            db.execute('CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)')
            db.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        detector=self.root/'detector.sqlite3'
        with sqlite3.connect(detector) as db:
            db.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        self.tx=PendingJournal(self.store)
        self.swaps=ManualRoutes()
        self.engine=LiveExecutor(self.store,self.selector,detector,FakeRpc(),Fx(),self.tx)
        self.engine.swaps=self.swaps
        self.balanceVal=110
        async def balance(token):
            self.assertEqual(token,TOKEN)
            return self.balanceVal
        self.engine.balance=balance
        self.sid='flex20p-newladder'
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
        self.tmp.cleanup()

    def pos(self):
        return self.store.db.execute('SELECT * FROM positions WHERE selectionId=?',
                                     (self.sid,)).fetchone()

    async def confirmedAuto(self,stage,qty):
        self.store.db.execute('UPDATE transactions SET state=?,receipt=? WHERE kind=?',
                              ('CONFIRMED',json.dumps(sellReceipt(qty)),'SELL_'+str(stage)))
        await self.engine.reconcileReceipts()
        self.assertEqual(self.pos()['sold'+str(stage)],1)
        self.assertEqual(int(self.pos()['soldQty'+str(stage)]),qty)
        self.balanceVal-=qty

    async def test_configured_20p_purchase_slots_exist(self):
        self.assertEqual(self.store.purchaseLimit,3)
        self.assertEqual(self.store.buyGbp,Decimal('0.20'))
        self.assertEqual(self.store.acquired(),1)
        self.assertEqual(self.store.reserved(),0)
        self.assertTrue(self.store.eligible())
        self.assertEqual(self.store.get('exitPolicy'),'80-10-5:2-50-100:manual5')
        self.assertEqual(self.engine.stages,((2,80),(50,10),(100,5)))

    async def test_three_main_positions_fill_cap_and_fourth_is_blocked(self):
        self.assertEqual(self.store.acquired(),1)
        for i in (2,3):
            token='0x'+format(i,'040x')
            self.store.db.execute(
                "INSERT INTO selections(selectionId,token,decisionAt,event,status) "
                "VALUES(?,?,0,'{}','BOUGHT')",('slot'+str(i),token))
            self.store.db.execute(
                """INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                VALUES(?,?,'100','200000000000000',1,1,1)""",('slot'+str(i),token))
            self.assertEqual(self.store.eligible(),i<3)
        status=self.store.status()
        self.assertEqual(status['purchases'],3)
        self.assertEqual(status['campaign'],3)
        self.assertEqual(status['spentGbp'],'0.60')
        self.assertEqual(self.engine.stages,((2,80),(50,10),(100,5)))

    def test_pending_receipt_reserves_final_purchase_slot(self):
        token2='0x'+format(2,'040x')
        token3='0x'+format(3,'040x')
        self.store.db.execute(
            "INSERT INTO selections(selectionId,token,decisionAt,event,status) "
            "VALUES(?,?,0,'{}','QUEUED')",('slot2',token2))
        self.assertTrue(self.store.eligible())
        self.store.db.execute(
            "INSERT INTO selections(selectionId,token,decisionAt,event,status) "
            "VALUES(?,?,0,'{}','BUY_PENDING')",('slot3',token3))
        self.assertEqual(self.store.acquired()+self.store.reserved(),3)
        self.assertFalse(self.store.eligible())
        self.store.db.execute(
            "UPDATE selections SET status='BUY_UNVERIFIED' WHERE selectionId='slot3'")
        self.assertFalse(self.store.eligible())
        self.store.db.execute("UPDATE selections SET status='SKIPPED' WHERE selectionId='slot3'")
        self.assertTrue(self.store.eligible())

    async def test_three_thresholds_sell_80_10_5_and_leave_five_main_plus_probe(self):
        self.assertEqual(self.engine.stages,((2,80),(50,10),(100,5)))
        for stage,pricePerUnit,qty in ((2,10*10**12,80),
                                      (50,250*10**12,10),
                                      (100,500*10**12,5)):
            self.swaps.estimatePerUnit=pricePerUnit
            await self.engine.checkPosition(self.pos())
            self.assertEqual(self.pos()['trigger'+str(stage)],1)
            await self.engine.prepareSell(self.pos())
            self.assertEqual(self.tx.orders[-1],('SELL_'+str(stage),qty))
            await self.confirmedAuto(stage,qty)
        self.assertEqual(self.balanceVal,15)  # 5 MAIN + 10 sacrificial probe
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),5)
        self.assertEqual(self.engine.manual.autoSlice(self.pos(),100,5),0)
        self.assertEqual(self.store.status()['openPositions'],1)
        before=list(self.tx.orders)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders,before)
        self.assertEqual(
            [(r['stage'],int(r['tokenDebited'])) for r in
             self.store.db.execute('SELECT stage,tokenDebited FROM sales ORDER BY stage')],
            [(2,80),(50,10),(100,5)])
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM transactions WHERE kind=?',('SELL_1000',)
        ).fetchone()[0],0)

    async def test_manual_can_sell_only_the_five_percent_remainder(self):
        for stage,qty in ((2,80),(50,10),(100,5)):
            self.store.db.execute(
                f"UPDATE positions SET trigger{stage}=1,sold{stage}=1,soldQty{stage}=? "
                "WHERE selectionId=?",(str(qty),self.sid))
        self.balanceVal=15
        req='manual-five'
        self.store.db.execute("""INSERT INTO manual_requests
          (requestId,selectionId,operation,percent,minGbp,slippageBps,
           status,createdAt,updatedAt)
          VALUES(?,?, 'SELL',100,'0',1000,'QUEUED',?,?)""",
          (req,self.sid,time.time(),time.time()))
        await self.engine.manual.sell(req)
        self.assertEqual(self.tx.orders,[('MANUAL_SELL',5)])
        self.assertEqual(self.engine.manual.row(req)['status'],'WAITING_SELL')
        self.store.db.execute('UPDATE transactions SET state=?,receipt=? WHERE kind=?',
                              ('CONFIRMED',json.dumps(sellReceipt(5)),'MANUAL_SELL'))
        self.engine.manual.reconcile()
        self.assertEqual(self.engine.manual.row(req)['status'],'CONFIRMED')
        self.balanceVal=10
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),0)
        self.assertEqual(self.store.status()['openPositions'],0)
        self.assertEqual(self.balanceVal,10)

    async def test_manual_partial_sale_does_not_expand_auto_to_reserved_main(self):
        self.store.db.execute("""INSERT INTO manual_fills
          (requestId,selectionId,txHash,tokenDebited,gasWei,confirmedAt)
          VALUES(?,?,?,?,?,?)""",
          ('earlier',self.sid,'0x'+'a'*64,'3','0',time.time()))
        self.balanceVal=107
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),97)
        self.assertEqual(self.engine.manual.autoSlice(self.pos(),2,97),80)
        self.store.db.execute(
            "UPDATE positions SET sold2=1,soldQty2='80' WHERE selectionId=?",(self.sid,))
        self.balanceVal=27
        self.assertEqual(self.engine.manual.autoSlice(self.pos(),50,17),10)
        self.store.db.execute(
            "UPDATE positions SET sold50=1,soldQty50='10' WHERE selectionId=?",(self.sid,))
        self.balanceVal=17
        self.assertEqual(self.engine.manual.autoSlice(self.pos(),100,7),5)
        self.store.db.execute(
            "UPDATE positions SET sold100=1,soldQty100='5' WHERE selectionId=?",(self.sid,))
        self.balanceVal=12
        # Manual reserve now 2 tokens; no automatic stage is allowed further.
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),2)
        self.assertEqual(self.engine.manual.autoSlice(self.pos(),100,2),0)

    def test_rounding_reserves_at_least_five_percent_for_manual(self):
        self.store.db.execute(
            "UPDATE positions SET originalQty='101' WHERE selectionId=?",(self.sid,))
        self.balanceVal=111
        p=self.pos()
        amounts=[self.engine.manual.autoSlice(p,s,101) for s in (2,50,100)]
        self.assertEqual(amounts,[80,10,5])
        self.assertEqual(101-sum(amounts),6)

    def test_dashboard_shows_correct_stages_and_manual_reserve(self):
        from live_dashboard import snapshot, card_positions
        view=snapshot(self.root)
        self.assertTrue(view['summary']['newLadder'])
        self.assertEqual(view['summary']['cap'],3)
        p=view['positions'][0]
        self.assertEqual((p['trigger3'],p['trigger5'],p['trigger10']),(0,0,0))
        panel=card_positions(view)
        headings=[str(column.header) for column in panel.renderable.columns]
        self.assertIn('50× 10%',headings)
        self.assertIn('100× 5%',headings)


if __name__=='__main__':
    unittest.main()
