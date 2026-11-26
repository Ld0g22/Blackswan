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
            store.set("manualExitHeartbeat",time.time())
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
            store.set("manualExitHeartbeat",time.time())
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

if __name__=="__main__": unittest.main()
