"""Pilot 3x/5x and campaign finance offline regression; zero real transactions."""
import json
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from campaign_config import (
    PILOT_KIND,PILOT_POLICY,PILOT_EXIT,PILOT_TRANCHES,
    pilotManifest,verifyPilotLedger,FLEX_KIND
)
from run_live_campaign import campaignSurvivalBlocks,campaignMaxBuyingBlocks
from live_executor import ExecutionStore,LiveExecutor
from campaign_finance import CampaignFinance
from test_flex_campaign import TOKEN,WALLET,Fx,FakeRpc,ManualRoutes,PendingJournal,sellReceipt


def manifest():
    return dict(chainId=4663,mode='REAL_MONEY',campaignKind=PILOT_KIND,
       walletPublicAddress=WALLET,targetPurchases=2,gbpEach='0.20',
       configVersion=3,entryPolicy='uniswapV4_nonPons_only',
       quoteEngine='original_peak_tracker_Tracker_best',
       exitPolicy=PILOT_EXIT,sellTranches=PILOT_TRANCHES.copy(),
       probeGbp='0.01',survivalBlocks=500,postProbeBuyingBlockGate='OFF',
       selectorDecisionSeconds=20,selectorMaxBuyingBlocks=10)


class PilotPolicyTests(unittest.TestCase):
    def test_policy_preserves_initial_selection_and_500_block_survival(self):
        self.assertEqual(pilotManifest(manifest(),WALLET),('0.20',2,WALLET))
        self.assertEqual(campaignSurvivalBlocks(manifest(),True),500)
        self.assertEqual(campaignMaxBuyingBlocks(manifest(),True),10)
        for changed in ('probeMaxBuyingBlocks','survivalSeconds'):
            m=manifest();m[changed]=7
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                pilotManifest(m,WALLET)
        m=manifest();m['sellTranches']={'3x':80,'5x':19,'manual':1}
        with self.assertRaises(ValueError):pilotManifest(m,WALLET)

    def test_campaign_identity_and_no_legacy_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            db=Path(root)/'pilot.sqlite3'
            store=ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                flexPilot=True,flexBuyGbp='0.20',flexMaxPurchases=2,
                flexWalletAddress=WALLET)
            self.assertEqual(store.campaignKind,PILOT_KIND)
            self.assertEqual(store.get('exitPolicy'),PILOT_POLICY)
            self.assertIsNone(store.probeMaxBuyingBlocks)
            self.assertEqual(store.purchaseLimit,2)
            self.assertEqual(store.get('flexConfigVersion'),'2')
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM pragma_table_info('positions') WHERE name='trigger2'").fetchone()[0],0)
            store.db.close()
            self.assertTrue(verifyPilotLedger(db,'0.20',2,WALLET))
            before=db.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'NOT_A_FIVE_LAMB_DB'):
                ExecutionStore(db,sacrifice=True,probeWaitBlocks=500,
                    flexLamb=True,flexBuyGbp='0.20',
                    flexMaxPurchases=2,flexWalletAddress=WALLET)
            self.assertEqual(db.read_bytes(),before)


class PilotLadderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=ExecutionStore(self.root/'execution.sqlite3',sacrifice=True,
            probeWaitBlocks=500,flexPilot=True,flexBuyGbp='0.20',
            flexMaxPurchases=2,flexWalletAddress=WALLET)
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
        self.sid='pilot-single'
        self.store.db.execute("INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES(?,?,0,'{}','BOUGHT')",(self.sid,TOKEN))
        self.store.db.execute("""INSERT INTO positions
            (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,'100','200000000000000',1,1,1)""",(self.sid,TOKEN))
        self.store.db.execute("""INSERT INTO lamb_probes
            (selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
            VALUES(?,?,?,1,'10','100','PROBE_PASSED')""",
            (self.sid,TOKEN,'0x'+'b'*64))
    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.temp.cleanup()
    def pos(self):
        return self.store.db.execute("SELECT * FROM positions WHERE selectionId=?",(self.sid,)).fetchone()
    async def confirm(self,stage,qty):
        self.store.db.execute("UPDATE transactions SET state='CONFIRMED',receipt=? WHERE kind=?",
            (json.dumps(sellReceipt(qty)),'SELL_'+str(stage)))
        await self.engine.reconcileReceipts()
        self.assertEqual(self.pos()['sold'+str(stage)],1)
        self.assertEqual(int(self.pos()['soldQty'+str(stage)]),qty)
        self.balanceVal-=qty

    def test_finance_monitor_uses_real_receipts_not_orphan_or_quote_rows(self):
        from live_dashboard import snapshot,card_finances,card_proceeds
        for i,kind,expense,gas,income in (
                (1,'BUY',2*10**14,10**12,None),
                (2,'SELL_3',0,10**12,8*10**14)):
            h='0x'+format(i,'064x')
            self.store.db.execute("""INSERT INTO transactions
                (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,
                 lastBroadcast,inputAmount,receipt)
                 VALUES(?,?,?,?,?,'0x',0,1,1,'0',?)""",
                 ('test'+str(i),self.sid,kind,'CONFIRMED',h,'{"status":"0x1"}'))
            self.store.db.execute("""INSERT INTO finance_events(
                txHash,kind,expenseWei,gasWei,incomeWei,incomeAsset,incomeBasis,
                fxGbpPerEth,fxAt,state,attempts,nextRetryAt,recordedAt)
                VALUES(?,?,?,?,?,?,?,?,?,'COMPLETE',1,0,1)""",
                (h,kind,str(expense),str(gas),str(income) if income is not None else None,
                 'ETH' if income is not None else None,
                 'QUALIFIED_BLOCK_BALANCE' if income is not None else None,'1000',1))
        ghost='0x'+'f'*64
        self.store.db.execute("""INSERT INTO finance_events(
                txHash,kind,expenseWei,gasWei,incomeWei,incomeAsset,incomeBasis,
                fxGbpPerEth,fxAt,state,attempts,nextRetryAt,recordedAt)
                VALUES(?,'SELL_5','0','0',?,'ETH','QUALIFIED_BLOCK_BALANCE',
                        '1000',1,'COMPLETE',1,0,1)""",(ghost,str(10**18)))
        view=snapshot(self.root)
        self.assertTrue(view['summary']['pilotMode'])
        finance=view['summary']['finance']
        self.assertTrue(finance['complete'])
        self.assertEqual(finance['expenditureGbp'],Decimal('0.202'))
        self.assertEqual(finance['returnsGbp'],Decimal('0.8'))
        self.assertEqual(finance['cashPnlGbp'],Decimal('0.598'))
        self.assertEqual(finance['qualifiedNativeSales'],1)
        self.assertNotIn(ghost,view['financeByHash'])
        self.assertIsNotNone(card_finances(view))
        self.assertIsNotNone(card_proceeds(view))

    async def test_exact_3x_5x_ladder_and_no_reserve(self):
        self.assertEqual(self.engine.stages,((3,80),(5,20)))
        self.assertIsNone(self.engine.lamb.activity)
        self.routes.estimatePerUnit=5_980_000_000_000
        await self.engine.checkPosition(self.pos())
        self.assertEqual(self.pos()['trigger3'],0)
        self.routes.estimatePerUnit=6_000_000_000_000
        await self.engine.checkPosition(self.pos())
        self.assertEqual(self.pos()['trigger3'],1)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_3',80))
        await self.confirm(3,80)
        self.routes.estimatePerUnit=10_000_000_000_000
        await self.engine.checkPosition(self.pos())
        self.assertEqual(self.pos()['trigger5'],1)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_5',20))
        await self.confirm(5,20)
        self.assertEqual(self.balanceVal,10)  # the separate 1p probe
        self.assertEqual(self.engine.manual.mainRemaining(self.pos()),0)
        self.assertEqual(self.store.status()['openPositions'],0)
        self.assertEqual([(r[0],r[1]) for r in self.store.db.execute("SELECT stage,tokenDebited FROM sales ORDER BY stage")],
                         [(3,'80'),(5,'20')])


class FinanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""CREATE TABLE transactions(
            txHash TEXT,kind TEXT,state TEXT,receipt TEXT,createdAt REAL);
            CREATE TABLE sales(txHash TEXT,tokenDebited TEXT,wethReceivedWei TEXT);
            CREATE TABLE manual_fills(txHash TEXT,tokenDebited TEXT,wethReceivedWei TEXT);""")
        self.hash='0x'+'a'*64
        self.fee=21000*10**9
        self.income=2*10**15
        self.receipt={'transactionHash':self.hash,'status':'0x1',
            'blockNumber':'0xa','gasUsed':hex(21000),'effectiveGasPrice':hex(10**9)}
        self.db.execute("INSERT INTO transactions VALUES(?,?,?,?,?)",
            (self.hash,'SELL_3','CONFIRMED',json.dumps(self.receipt),1))
        self.db.execute("INSERT INTO sales VALUES(?,?,NULL)",(self.hash,'80'))
        self.dupe=False
        self.calls=[]
        class MockRpc:
            async def call(other,method,params):
                self.calls.append(method)
                if method=='eth_getTransactionByHash':
                    return {'hash':self.hash,'from':WALLET,'blockNumber':'0xa','value':'0x0'}
                if method=='eth_getBlockByNumber':
                    txs=[{'hash':self.hash,'from':WALLET}]
                    if self.dupe:txs.append({'hash':'0x'+'b'*64,'from':WALLET})
                    return {'transactions':txs}
                if method=='eth_getBalance':
                    return hex(10**18) if params[1]=='0x9' else hex(10**18+self.income-self.fee)
                raise AssertionError(method)
        self.fin=CampaignFinance(SimpleNamespace(db=self.db),MockRpc(),Fx(),WALLET)
    async def asyncTearDown(self):
        self.db.close()

    async def test_native_proceeds_recorded_once(self):
        await self.fin.tick()
        row=self.db.execute('SELECT * FROM finance_events').fetchone()
        self.assertEqual(row['state'],'COMPLETE')
        self.assertEqual(row['incomeWei'],str(self.income))
        self.assertEqual(row['incomeBasis'],'QUALIFIED_BLOCK_BALANCE')
        self.assertEqual(row['gasWei'],str(self.fee))
        self.fin.nextCheck=0
        await self.fin.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM finance_events').fetchone()[0],1)
    async def test_ambiguous_same_wallet_block_is_not_invented_proceeds(self):
        self.dupe=True
        await self.fin.tick()
        row=self.db.execute('SELECT * FROM finance_events').fetchone()
        self.assertEqual(row['state'],'INCOMPLETE')
        self.assertIsNone(row['incomeWei'])


if __name__=='__main__':
    unittest.main()
