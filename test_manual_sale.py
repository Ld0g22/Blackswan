"""Manual-exit safety and real-executor integration regressions (offline only)."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from eth_utils import keccak

from live_executor import ExecutionStore,LiveExecutor
from manual_exit_engine import ManualExitEngine
from test_sacrificial_integration import TOKEN,WALLET,Fx,Journal
import manual_sale


TOPIC='0x'+keccak(text='Transfer(address,address,uint256)').hex()


def sellReceipt(qty):
    return dict(status='0x1',blockNumber='0x215',gasUsed='0x5208',
                effectiveGasPrice='0x3b9aca00',logs=[dict(
                    address=TOKEN,topics=[
                        TOPIC,'0x'+WALLET[2:].rjust(64,'0'),
                        '0x'+('2'*40).rjust(64,'0')],
                    data='0x'+f'{qty:064x}')])


class FakeRpc:
    def __init__(self):
        self.session=SimpleNamespace()
    async def call(self,method,params):
        if method=='eth_estimateGas':return '0x5208'
        raise AssertionError('UNEXPECTED_RPC:'+method)


class ManualRoutes:
    def __init__(self):
        self.outputs=[]
        self.estimatePerUnit=5*10**12
        self.wantApproval=False
    async def directQuotes(self,token,qty,buy,block=None):
        assert token==TOKEN and not buy
        self.outputs.append(('quote',qty))
        return [dict(tokenIn=TOKEN,venue={'source':'uniswapV4'},
                     output=qty*self.estimatePerUnit,block='0x100',peakRoute='v4',
                     basis='ONCHAIN_QUOTE_ONLY')]
    def build(self,quote,amount,wallet,minimumOut=None):
        self.outputs.append(('build',amount,minimumOut))
        return dict(to=WALLET,value=0,data='0xaabb')
    async def approvalNeeded(self,*args):
        if self.wantApproval:return ('ERC20',TOKEN,WALLET,100)
        return None
    def buildApproval(self,approval):
        return dict(to=TOKEN,value=0,data='0xabcd')
    async def refreshPons(self,*a,**kw):pass


class PendingJournal(Journal):
    def unsettled(self):
        return self.store.db.execute(
            "SELECT * FROM transactions WHERE state IN ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK')"
        ).fetchall()


class ManualExitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=ExecutionStore(self.root/'execution.sqlite3',
                                  sacrifice=True,probeWaitBlocks=500,oneFifty=True)
        self.selector=self.root/'selector.sqlite3'
        with sqlite3.connect(self.selector) as db:
            db.execute('CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)')
            db.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        detector=self.root/'detector.sqlite3'
        with sqlite3.connect(detector) as db:
            db.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO meta VALUES('lastConnectedHead','100')")
        self.tx=PendingJournal(self.store)
        self.routes=ManualRoutes()
        self.engine=LiveExecutor(self.store,self.selector,detector,FakeRpc(),Fx(),self.tx)
        self.engine.swaps=self.routes
        self.balanceVal=110
        async def balance(token):
            self.assertEqual(token,TOKEN)
            return self.balanceVal
        self.engine.balance=balance
        self.sid='manual-position'
        self.store.db.execute(
            "INSERT INTO selections(selectionId,token,decisionAt,event,status) "
            "VALUES(?,?,0,'{}','BOUGHT')",(self.sid,TOKEN))
        self.store.db.execute("""INSERT INTO positions
            (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,'100','500000000000000',1,1,1)""",
            (self.sid,TOKEN))
        self.store.db.execute("""INSERT INTO lamb_probes
            (selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
            VALUES(?,?,?,1,'10','100','PROBE_PASSED')""",
            (self.sid,TOKEN,'0x'+'b'*64))
        self.manual=self.engine.manual

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.temp.cleanup()

    def request(self,percent=100,minGbp='0',kind='SELL',slippage=1000):
        req='req-'+str(self.store.db.execute('SELECT count(*) FROM manual_requests').fetchone()[0])
        self.store.db.execute("""INSERT INTO manual_requests
          (requestId,selectionId,operation,percent,minGbp,slippageBps,
           status,createdAt,updatedAt)
          VALUES(?,?,?,?,?,?,'QUEUED',?,?)""",
          (req,self.sid,kind,percent,minGbp,slippage,time.time(),time.time()))
        return req

    def pos(self):
        return self.store.db.execute('SELECT * FROM positions WHERE selectionId=?',
                                     (self.sid,)).fetchone()

    def confirm(self,req,qty,kind='MANUAL_SELL'):
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt=? WHERE kind=?",
            (json.dumps(sellReceipt(qty)),kind))

    async def test_preview_quotes_full_remaining_main_not_probe_and_no_signing(self):
        req=self.request(kind='PREVIEW')
        await self.manual.preview(req)
        row=self.manual.row(req)
        self.assertEqual(row['status'],'PREVIEW_READY')
        self.assertEqual(row['quantity'],'100')
        self.assertEqual(Decimal(row['quoteGbp']),Decimal('0.500'))
        self.assertEqual(row['minOutWei'],str(100*5*10**12*9//10))
        self.assertEqual(row['gasEstimate'],'21000')
        self.assertEqual(self.tx.orders,[])

    async def test_price_floor_fails_without_signing(self):
        req=self.request(minGbp='0.60')
        await self.manual.sell(req)
        self.assertEqual(self.manual.row(req)['status'],'FAILED')
        self.assertIn('BELOW_OPERATOR_MINIMUM',self.manual.row(req)['reason'])
        self.assertFalse(self.tx.orders)

    async def test_manual_full_sell_receipt_closes_main_not_probe_and_is_idempotent(self):
        req=self.request()
        await self.manual.sell(req)
        row=self.manual.row(req)
        self.assertEqual(row['status'],'WAITING_SELL')
        self.assertEqual(self.tx.orders,[('MANUAL_SELL',100)])
        self.assertEqual(self.routes.outputs[-1],('build',100,450000000000000))
        self.assertTrue(self.manual.active(self.sid))
        # Automatic pending stage may not race the manually queued sale.
        self.store.db.execute('UPDATE positions SET trigger3=1 WHERE selectionId=?',(self.sid,))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(len(self.tx.orders),1)
        self.confirm(req,100)
        self.manual.reconcile()
        row=self.manual.row(req)
        self.assertEqual(row['status'],'CONFIRMED')
        self.assertEqual(self.manual.mainRemaining(self.pos()),0)
        self.assertEqual(self.manual.manualSold(self.sid),100)
        self.manual.reconcile()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM manual_fills').fetchone()[0],1)
        self.balanceVal=10  # The sacrificial probe is untouched.
        self.assertFalse(self.manual.active(self.sid))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(len(self.tx.orders),1)
        self.assertEqual(self.store.db.execute('SELECT gasWei FROM manual_fills').fetchone()[0],
                         str(21000*10**9))

    async def test_readonly_dashboard_includes_manual_sale_and_closes_position(self):
        from live_dashboard import snapshot
        req=self.request()
        await self.manual.sell(req)
        self.confirm(req,100)
        self.manual.reconcile()
        view=snapshot(self.root)
        self.assertEqual(len(view['manualFills']),1)
        self.assertEqual(view['summary']['sales'],1)
        self.assertEqual(view['summary']['open'],0)
        self.assertEqual(view['positions'][0]['manualSold'],100)
        self.assertEqual(view['sales'][0]['stage'],'MANUAL')

    async def test_partial_manual_exit_rebalances_three_five_thousand_stages(self):
        req=self.request(percent=50,minGbp='0.20')
        await self.manual.sell(req)
        self.assertEqual(self.tx.orders,[('MANUAL_SELL',50)])
        self.assertEqual(self.routes.outputs[-1],('build',50,225000000000000))
        self.confirm(req,50)
        self.manual.reconcile()
        self.assertEqual(self.manual.mainRemaining(self.pos()),50)
        self.balanceVal=60
        self.store.db.execute("""UPDATE positions SET
          trigger3=1,trigger5=1,trigger1000=1 WHERE selectionId=?""",(self.sid,))
        self.assertEqual(self.manual.autoSlice(self.pos(),3,50),40)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_3',40))
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED' WHERE kind='SELL_3'")
        self.store.db.execute(
            "UPDATE positions SET sold3=1,soldQty3='40' WHERE selectionId=?",(self.sid,))
        self.balanceVal=20
        self.assertEqual(self.manual.autoSlice(self.pos(),5,10),5)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_5',5))
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED' WHERE kind='SELL_5'")
        self.store.db.execute(
            "UPDATE positions SET sold5=1,soldQty5='5' WHERE selectionId=?",(self.sid,))
        self.balanceVal=15
        self.assertEqual(self.manual.autoSlice(self.pos(),1000,5),5)
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders[-1],('SELL_1000',5))
        self.assertEqual(self.balanceVal,15)  # probe plus last 5 tokens

    async def test_pending_auto_sale_waits_for_receipt_then_manual_requotes(self):
        self.store.db.execute('UPDATE positions SET trigger3=1 WHERE selectionId=?',(self.sid,))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders,[('SELL_3',80)])
        req=self.request(minGbp='0.05')
        await self.manual.sell(req)
        self.assertEqual(self.manual.row(req)['status'],'QUEUED')
        self.assertEqual(self.tx.orders,[('SELL_3',80)])
        self.assertTrue(self.manual.active(self.sid))
        self.store.db.execute("UPDATE transactions SET state='CONFIRMED' WHERE kind='SELL_3'")
        self.store.db.execute(
            "UPDATE positions SET sold3=1,soldQty3='80' WHERE selectionId=?",(self.sid,))
        self.balanceVal=30
        await self.manual.sell(req)
        self.assertEqual(self.tx.orders[-1],('MANUAL_SELL',20))
        self.assertEqual(self.manual.row(req)['status'],'WAITING_SELL')

    async def test_missing_sell_receipt_never_double_submits_on_restart(self):
        req=self.request()
        await self.manual.sell(req)
        self.manual.setStatus(req,'QUEUED') # simulate crash after signed journal
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)['status'],'WAITING_SELL')
        await self.manual.sell(req)
        self.assertEqual(self.tx.orders,[('MANUAL_SELL',100)])

    async def test_unverified_receipt_halts_auto_and_future_manual(self):
        req=self.request()
        await self.manual.sell(req)
        self.confirm(req,0)
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)['status'],'UNVERIFIED')
        self.assertTrue(self.manual.active(self.sid))
        await self.engine.prepareSell(self.pos())
        self.assertEqual(self.tx.orders,[('MANUAL_SELL',100)])

    async def test_approval_confirmed_then_new_fresh_sell(self):
        self.routes.wantApproval=True
        req=self.request()
        await self.manual.sell(req)
        self.assertEqual(self.tx.orders[0][0],'MANUAL_APPROVE')
        self.assertEqual(self.manual.row(req)['status'],'WAITING_APPROVAL')
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt='{}' WHERE kind='MANUAL_APPROVE'")
        self.routes.wantApproval=False
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)['status'],'QUEUED')
        await self.manual.sell(req)
        self.assertEqual(self.tx.orders[-1],('MANUAL_SELL',100))

    def test_operator_cli_validates_only_live_new_engine_and_percent(self):
        run=self.root/'v4_lamb_1x50p_test'
        run.mkdir()
        (run/'campaign.json').write_text(json.dumps(dict(
            chainId=4663,campaignKind='ONE_50P_LAMB')))
        # point it at the test execution DB, with live heartbeat
        (run/'execution.sqlite3').symlink_to(self.root/'execution.sqlite3')
        db,meta=manual_sale.connect(run)
        try:
            self.assertEqual(meta['manualExitVersion'],'1')
            # Legacy engine heartbeat alone must NOT authorize a manual order.
            self.store.set('executorHeartbeat',time.time())
            args=SimpleNamespace(command='sell',percent=100,min_gbp='0.1',
                                 slippage_bps=1000)
            with self.assertRaisesRegex(RuntimeError,'EXECUTOR_NOT_LIVE'):
                manual_sale.issue(db,run,args)
            self.store.set('manualExitHeartbeat',time.time())
            args=SimpleNamespace(command='sell',percent=101,min_gbp='0.1',
                                 slippage_bps=1000)
            with self.assertRaisesRegex(ValueError,'PERCENT_MUST'):
                manual_sale.issue(db,run,args)
            args.percent=100;args.slippage_bps=1200
            with self.assertRaisesRegex(ValueError,'MANUAL_SLIPPAGE_CAP'):
                manual_sale.issue(db,run,args)
            args.slippage_bps=1000;args.min_gbp=None
            with self.assertRaisesRegex(ValueError,'SELL_REQUIRES'):
                manual_sale.issue(db,run,args)
        finally:db.close()


class ManualOnchainMinimumTests(unittest.TestCase):
    def test_manual_minimum_is_encoded_in_actual_v4_router_builder(self):
        from swap_adapters import SwapAdapters
        quote=dict(venue=dict(source='uniswapV4'),output=1000,
                   tokenIn=TOKEN,path=None)
        adapter=SwapAdapters(None,None,None)
        with patch('swap_adapters.buildV4',return_value=dict(to=WALLET)) as route:
            adapter.build(quote,100,WALLET,minimumOut=900)
            self.assertEqual(route.call_args.args[3],900)
            adapter.build(quote,100,WALLET)
            self.assertEqual(route.call_args.args[3],660)
            self.assertEqual(route.call_count,2)
        with self.assertRaisesRegex(ValueError,'INVALID_MANUAL_MINIMUM_OUTPUT'):
            adapter.build(quote,100,WALLET,minimumOut=1001)
        with self.assertRaisesRegex(ValueError,'INVALID_MANUAL_MINIMUM_OUTPUT'):
            adapter.build(quote,100,WALLET,minimumOut=0)

    def test_manual_minimum_rejects_buy_routes(self):
        from swap_adapters import SwapAdapters,NATIVE
        adapter=SwapAdapters(None,None,None)
        quote=dict(venue=dict(source='uniswapV4'),output=1000,
                   tokenIn=NATIVE,path=None)
        with self.assertRaisesRegex(ValueError,'MANUAL_MINIMUM_REQUIRES_SELL'):
            adapter.build(quote,100,WALLET,minimumOut=900)


if __name__=='__main__':
    unittest.main()
