"""Manual exit console and engine controls, offline only."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from campaign_v3 import defaultCampaign, validateCampaign
from live_executor import ExecutionStore
from test_manual_sale import ManualExitTests
import manual_sale
import manual_exit_engine

WALLET="0x"+"1"*40
TOKEN="0x"+"a"*40

class ManualConsoleTests(unittest.TestCase):
    def test_exact_reserve_and_readonly_discovery(self):
        import manual_console as console
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            run=root/"configured_v3_test"
            run.mkdir()
            cfg=defaultCampaign(WALLET)
            cfg["exits"]=[{"multiple":"3","percent":"80"},{"multiple":"5","percent":"10"}]
            cfg=validateCampaign(cfg)
            (run/"campaign.json").write_text(json.dumps(cfg))
            store=ExecutionStore(run/"execution.sqlite3",configuredCampaign=cfg)
            store.db.execute("INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES('s',?,1,'{}','BOUGHT')",(TOKEN,))
            store.db.execute("INSERT INTO positions(selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue) VALUES('s',?,'103','1',1,1,1)",(TOKEN,))
            store.seedExitStages("s")
            now=time.time()
            store.set("manualExitHeartbeat",now)
            store.set("manualControlHeartbeat",now)
            store.db.execute("UPDATE exit_slices SET sold=1,soldQty='82' WHERE selectionId='s' AND stage=1")
            store.db.execute("UPDATE exit_slices SET sold=1,soldQty='10' WHERE selectionId='s' AND stage=2")
            self.assertEqual(console.discover(root),run)
            p=console.positions(store.db)[0]
            self.assertEqual(p["remaining"],11)
            self.assertEqual(p["reserve"],11)
            self.assertEqual(p["token"],TOKEN)
            store.db.close()

    def test_cancel_is_atomic_and_no_schema_change_from_console(self):
        import manual_console as console
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=validateCampaign(defaultCampaign(WALLET))
            (root/"campaign.json").write_text(json.dumps(cfg))
            store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=cfg)
            now=time.time()
            store.set("manualExitHeartbeat",now)
            store.set("manualControlHeartbeat",now)
            store.db.execute("INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES('s',?,1,'{}','BOUGHT')",(TOKEN,))
            store.db.execute("INSERT INTO positions(selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue) VALUES('s',?,'100','1',1,1,1)",(TOKEN,))
            store.seedExitStages("s")
            request=console.enqueue(store.db,"s","SELL",100,"0",1000,False,60)
            self.assertTrue(console.cancel(store.db,request))
            self.assertEqual(store.db.execute("SELECT status FROM manual_requests WHERE requestId=?",(request,)).fetchone()[0],"CANCELLED")
            request=console.enqueue(store.db,"s","SELL",100,"0",1000,False,60)
            store.db.execute("UPDATE manual_requests SET status='PREPARING' WHERE requestId=?",(request,))
            self.assertFalse(console.cancel(store.db,request))
            store.db.close()


    def test_legacy_protocol_no_control_rows_or_schema_changes(self):
        import manual_console as console
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=validateCampaign(defaultCampaign(WALLET))
            (root/"campaign.json").write_text(json.dumps(cfg))
            store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=cfg)
            store.db.execute("DROP TABLE manual_request_controls")
            store.db.execute("DELETE FROM meta WHERE key LIKE 'manualControl%'")
            store.set("manualExitHeartbeat",time.time())
            store.db.execute("INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES('s',?,1,'{}','BOUGHT')",(TOKEN,))
            store.db.execute("INSERT INTO positions(selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue) VALUES('s',?,'100','1',1,1,1)",(TOKEN,))
            store.seedExitStages("s")
            db,_=manual_sale.connect(root,readOnly=True)
            try:
                self.assertEqual(console.positions(db)[0]["remaining"],100)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("DELETE FROM positions")
            finally:db.close()
            with self.assertRaisesRegex(RuntimeError,"LEGACY_EXECUTOR"):
                console.enqueue(store.db,"s","SELL",100,"0",1000,False,60)
            with self.assertRaisesRegex(RuntimeError,"EXACT_RESERVE_REQUIRES|NO_KNOWN_REMAINING_RESERVE"):
                console.enqueue(store.db,"s","SELL",100,"0",1000,True,60,True)
            request=console.enqueue(store.db,"s","SELL",100,"0",1000,False,60,True)
            self.assertEqual(store.db.execute("SELECT status FROM manual_requests WHERE requestId=?",(request,)).fetchone()[0],"QUEUED")
            self.assertIsNone(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_request_controls'").fetchone())
            store.db.close()

class ManualControlTests(ManualExitTests):
    async def test_expired_request_never_signs(self):
        req=self.request()
        self.store.db.execute("INSERT INTO manual_request_controls VALUES(?,?,?)",(req,time.time()-1,0))
        await self.manual.sell(req)
        self.assertEqual(self.manual.row(req)["status"],"EXPIRED")
        self.assertEqual(self.tx.orders,[])

    async def test_no_sacrifice_quote(self):
        self.engine.lamb=None
        self.balanceVal=100
        req=self.request(kind="PREVIEW")
        await self.manual.preview(req)
        self.assertEqual(self.manual.row(req)["status"],"PREVIEW_READY")
        self.assertEqual(self.manual.row(req)["quantity"],"100")


    async def test_expiry_after_quote_before_signing(self):
        req=self.request()
        self.store.db.execute("INSERT INTO manual_request_controls VALUES(?,?,?)",(req,time.time()+60,0))
        old=self.routes.approvalNeeded
        async def late(*args):
            result=await old(*args)
            self.store.db.execute("UPDATE manual_request_controls SET expiresAt=? WHERE requestId=?",(time.time()-1,req))
            return result
        self.routes.approvalNeeded=late
        await self.manual.sell(req)
        self.assertEqual(self.manual.row(req)["status"],"EXPIRED")
        self.assertEqual(self.tx.orders,[])

    async def test_signed_sale_survives_expiry(self):
        req=self.request()
        self.store.db.execute("INSERT INTO manual_request_controls VALUES(?,?,?)",(req,time.time()+60,0))
        await self.manual.sell(req)
        self.store.db.execute("UPDATE manual_request_controls SET expiresAt=? WHERE requestId=?",(time.time()-1,req))
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)["status"],"WAITING_SELL")
        self.assertFalse(self.manual.expireUnsigned(req))
        self.assertEqual(len(self.tx.orders),1)
        self.confirm(req,100)
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)["status"],"CONFIRMED")

    async def test_signed_sale_not_marked_failed_on_broadcast_exception(self):
        req=self.request()
        async def fail(intent):raise RuntimeError("simulated provider disconnect")
        self.tx.broadcast=fail
        await self.manual.sell(req)
        self.assertEqual(self.manual.row(req)["status"],"WAITING_SELL")
        self.assertTrue(self.manual.active(self.sid))
        self.assertEqual(len(self.tx.orders),1)
        self.confirm(req,100)
        self.manual.reconcile()
        self.assertEqual(self.manual.row(req)["status"],"CONFIRMED")

class ConfiguredManualControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_reserve_then_full_early_exit(self):
        from test_manual_sale import ManualRoutes, PendingJournal, FakeRpc, sellReceipt
        from test_sacrificial_integration import Fx, TOKEN, WALLET
        from live_executor import LiveExecutor
        import manual_console as console
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=defaultCampaign(WALLET)
            cfg["exits"]=[{"multiple":"3","percent":"80"},{"multiple":"5","percent":"10"}]
            cfg=validateCampaign(cfg)
            (root/"campaign.json").write_text(json.dumps(cfg))
            store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=cfg)
            selector=root/"selector.sqlite3"
            detector=root/"detector.sqlite3"
            with sqlite3.connect(selector) as db:
                db.execute("CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)")
                db.execute("CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)")
            with sqlite3.connect(detector) as db:
                db.execute("CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)")
                db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO meta VALUES('lastConnectedHead','100')")
            tx=PendingJournal(store)
            engine=LiveExecutor(store,selector,detector,FakeRpc(),Fx(),tx)
            engine.swaps=ManualRoutes()
            sid="config-manual"
            store.db.execute("INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES(?,?,1,'{}','BOUGHT')",(sid,TOKEN))
            store.db.execute("INSERT INTO positions(selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue) VALUES(?,?,'103','1',1,1,1)",(sid,TOKEN))
            store.seedExitStages(sid)
            store.db.execute("INSERT INTO lamb_probes(selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result) VALUES(?,?,?,1,'10','1','PROBE_PASSED')",(sid,TOKEN,'0x'+'b'*64))
            wallet=[113]
            async def balance(token):return wallet[0]
            engine.balance=balance
            now=time.time()
            store.set("manualExitHeartbeat",now)
            store.set("manualControlHeartbeat",now)
            try:
                req=console.enqueue(store.db,sid,"SELL",100,"0",1000,True,60)
                await engine.manual.sell(req)
                self.assertEqual(tx.orders,[("MANUAL_SELL",11)])
                store.db.execute("UPDATE transactions SET state='CONFIRMED',receipt=? WHERE intentId=?",(json.dumps(sellReceipt(11)),'manual:sell:'+req))
                wallet[0]-=11
                engine.manual.reconcile()
                self.assertEqual(console.positions(store.db)[0]["remaining"],92)
                self.assertEqual(console.positions(store.db)[0]["reserve"],0)
                req=console.enqueue(store.db,sid,"SELL",100,"0",1000,False,60)
                await engine.manual.sell(req)
                self.assertEqual(tx.orders[-1],("MANUAL_SELL",92))
                store.db.execute("UPDATE transactions SET state='CONFIRMED',receipt=? WHERE intentId=?",(json.dumps(sellReceipt(92)),'manual:sell:'+req))
                wallet[0]-=92
                engine.manual.reconcile()
                self.assertEqual(console.positions(store.db),[])
                await engine.prepareSell(store.db.execute("SELECT * FROM positions").fetchone())
                self.assertEqual(len(tx.orders),2)
                self.assertEqual(wallet[0],10)
            finally:
                await engine.close()
                store.db.close()

class ActualSignerDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_during_rpc_simulation_blocks_real_signing(self):
        from eth_account import Account
        from test_live_execution import FakeRpc
        from wallet_transactions import TransactionJournal
        with tempfile.TemporaryDirectory() as tmp:
            store=ExecutionStore(Path(tmp)/"execution.sqlite3",configuredCampaign=validateCampaign(defaultCampaign(WALLET)))
            key=bytes.fromhex("11"*32)
            rpc=FakeRpc()
            old=rpc.call
            req="deadline"
            store.db.execute("INSERT INTO manual_request_controls VALUES(?,?,?)",(req,time.time()+60,0))
            async def call(method,params):
                result=await old(method,params)
                if method=="eth_getBalance":
                    store.db.execute("UPDATE manual_request_controls SET expiresAt=? WHERE requestId=?",(time.time()-1,req))
                return result
            rpc.call=call
            journal=TransactionJournal(store,rpc,Account.from_key(key),key)
            try:
                with self.assertRaisesRegex(RuntimeError,"MANUAL_REQUEST_EXPIRED"):
                    await journal.create("manual:sell:"+req,"s","MANUAL_SELL",{"to":"0x"+"f"*40,"value":0,"data":"0x"},1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],0)
                self.assertEqual(len(rpc.mempool),0)
            finally:store.db.close()

if __name__=="__main__": unittest.main()
