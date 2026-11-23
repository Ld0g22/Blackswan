"""New-campaign exit stages must use receipt-confirmed original quantities."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from campaign_v3 import defaultCampaign, validateCampaign
from live_executor import ExecutionStore, LiveExecutor
from test_sacrificial_integration import Fx, Rpc, Routes, Journal, TOKEN, WALLET


class ConfiguredExitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        cfg=defaultCampaign(WALLET)
        cfg["exits"]=[
            {"multiple":"1.5","percent":"50"},
            {"multiple":"3","percent":"25"},
            {"multiple":"50","percent":"25"},
        ]
        self.store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=validateCampaign(cfg))
        selector=root/"selector.sqlite3"
        with sqlite3.connect(selector) as db:
            db.execute("CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)")
            db.execute("CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)")
        detector=root/"detector.sqlite3"
        with sqlite3.connect(detector) as db:
            db.execute("CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)")
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        self.tx=Journal(self.store)
        self.routes=Routes()
        self.engine=LiveExecutor(self.store,selector,detector,Rpc(),Fx(),self.tx)
        self.engine.swaps=self.routes
        self.selection="configured-1"
        self.store.db.execute("""INSERT INTO selections(selectionId,token,decisionAt,event,status)
                                  VALUES(?,?,0,'{}','BOUGHT')""",(self.selection,TOKEN))
        self.store.db.execute("""INSERT INTO positions(
             selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
             VALUES(?,?,'100','100',1,1,1)""",(self.selection,TOKEN))
        self.store.seedExitStages(self.selection)
        self.store.db.execute("""INSERT INTO lamb_probes(
             selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
             VALUES(?,?,?,1,'10','1','PROBE_PASSED')""",
             (self.selection,TOKEN,'0x'+'8'*64))
        self.wallet=[110]
        async def balance(token):return self.wallet[0]
        self.engine.balance=balance

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.tmp.cleanup()

    def position(self):
        return self.store.db.execute("SELECT * FROM positions WHERE selectionId=?",(self.selection,)).fetchone()

    def stages(self):
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM exit_slices WHERE selectionId=? ORDER BY stage",(self.selection,))]

    async def test_custom_multiplier_crossings_latch_in_db(self):
        self.assertEqual([stage for stage,_ in self.engine.stages],[1,2,3])
        self.routes.valueGbp=Decimal("0.149")
        await self.engine.checkPosition(self.position())
        self.assertEqual([x["triggered"] for x in self.stages()],[0,0,0])
        self.routes.valueGbp=Decimal("0.150")
        await self.engine.checkPosition(self.position())
        self.assertEqual([x["triggered"] for x in self.stages()],[1,0,0])
        self.routes.valueGbp=Decimal("5.00")
        await self.engine.checkPosition(self.position())
        self.assertEqual([x["triggered"] for x in self.stages()],[1,1,1])
        self.routes.valueGbp=Decimal("0.01")
        await self.engine.checkPosition(self.position())
        self.assertEqual([x["triggered"] for x in self.stages()],[1,1,1])

    async def test_custom_slice_sizes_exclude_sacrifice_tokens(self):
        self.store.db.execute(
            "UPDATE exit_slices SET triggered=1 WHERE selectionId=?",(self.selection,))
        for stage,remaining,debited in ((1,60,50),(2,35,25),(3,10,25)):
            await self.engine.prepareSell(self.position())
            self.assertEqual(self.tx.orders[-1],("SELL_"+str(stage),debited))
            self.store.db.execute("UPDATE exit_slices SET sold=1,soldQty=? WHERE selectionId=? AND stage=?",
                                  (str(debited),self.selection,stage))
            self.wallet[0]=remaining
            self.store.db.execute(
                "UPDATE transactions SET state='CONFIRMED' WHERE kind=?",("SELL_"+str(stage),))
        self.assertEqual(self.wallet[0],10)
        self.assertEqual(self.stages()[-1]["sold"],1)


if __name__ == "__main__":
    unittest.main()
