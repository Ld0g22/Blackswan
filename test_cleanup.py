"""Clean campaign regression contract; offline, never sends real transactions."""
import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from tempfile import TemporaryDirectory

import campaign_v3
from live_executor import LiveExecutor, ExecutionStore
from test_concentration_selector import ConcentrationHandoffTests
from test_concentration_entry_timing import JournalDeadlineTests, CanonicalTimingTests
from test_sacrificial_lamb import WALLET
from test_concentration_throughput import FastChainRpc
from test_concentration_selector import swap

class CleanCoverageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ConcentrationHandoffTests.asyncSetUp
    asyncTearDown=ConcentrationHandoffTests.asyncTearDown
    fake=ConcentrationHandoffTests.fake

    async def test_late_complete_result_is_not_unknown(self):
        self.fake([25,25,25,25])
        self.sel.liveStartAt=99999
        self.sel.wallTime=lambda:100060
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        data=json.loads(row["data"])
        self.assertEqual(row["status"],"PASS")
        self.assertTrue(data["actionable"])
        self.assertEqual(data["decisionLagSeconds"],30)

    async def test_historical_result_not_actionable_even_if_complete(self):
        self.fake([25,25,25,25])
        self.sel.liveStartAt=100020
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"PASS")
        self.assertFalse(json.loads(row["data"])["actionable"])

    async def test_partial_range_does_not_wait_one_second(self):
        self.fake([25,25,25,25],head=1001)
        self.assertTrue(await self.sel.coverageCycle())

    async def test_parallel_trade_headers_preserve_complete_result(self):
        self.sel.scanLimit=10
        headers={n:100000+(n-1000)*10 for n in range(1000,1004)}
        fake=FastChainRpc([swap(self.venue,i+1,1000+i//2,i,25) for i in range(4)],headers,limit=10)
        inFlight=peak=0
        async def delayed(method,params,timeout=4):
            nonlocal inFlight,peak
            if method!="eth_getBlockByNumber":
                return await fake(method,params,timeout)
            inFlight+=1;peak=max(peak,inFlight)
            try:
                await asyncio.sleep(.02)
                return await fake(method,params,timeout)
            finally:inFlight-=1
        self.sel.rpc=delayed
        await self.sel.scanOnce(self.key)
        self.assertGreaterEqual(peak,2)
        self.assertEqual(self.sel.store.db.execute("SELECT status FROM decisions").fetchone()[0],"PASS")

class CleanManifestTests(unittest.TestCase):
    def test_no_runtime_deadline_fields_in_clean_config(self):
        factory=getattr(campaign_v3,"cleanCampaign",None)
        self.assertIsNotNone(factory)
        cfg=factory(WALLET,"concentration")
        self.assertNotIn("execution",cfg)
        self.assertEqual(cfg["schemaVersion"],4)
        self.assertEqual(campaign_v3.validateCampaign(cfg),cfg)
        with TemporaryDirectory() as root:
            store=ExecutionStore(Path(root)/"execution.sqlite3",configuredCampaign=cfg)
            try:self.assertEqual(store.get("campaignVersion"),"4")
            finally:store.db.close()

class CleanEntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=CanonicalTimingTests.asyncSetUp
    asyncTearDown=CanonicalTimingTests.asyncTearDown
    def clean(self):
        factory=getattr(campaign_v3,"cleanCampaign",None)
        self.assertIsNotNone(factory)
        c=self.case
        c.store.configuredCampaign=factory(WALLET,"concentration")
        c.owner.lamb=c.gate
        c.owner.active=set()
        c.owner.entryWalletConflict=lambda:None
        c.owner.buy=AsyncMock()
        c.owner.cleanEntry=LiveExecutor.cleanEntry.__get__(c.owner)
        c.owner.submitCleanEntry=LiveExecutor.submitCleanEntry.__get__(c.owner)
        return c

    async def test_live_sacrifice_not_rejected_because_decision_is_old(self):
        c=self.clean()
        c.store.db.execute("UPDATE selections SET decisionAt=?",(time.time()-30,))
        await LiveExecutor.processEntry(c.owner,c.selection())
        self.assertEqual(c.status(),"PROBE_PENDING")
        self.assertEqual(len(c.owner.tx.signed),1)

    async def test_survival_exact_block_then_main_without_second_quote(self):
        c=self.clean()
        # Seed actual receipt through existing historical gate before clean entry.
        c.store.configuredCampaign=None
        await c.confirmed()
        c.store.configuredCampaign=campaign_v3.cleanCampaign(WALLET,"concentration")
        previous=c.rpc.call
        async def rpc(method,params):
            if method=='eth_getBlockByNumber':
                return {'number':hex(10),'timestamp':hex(int(time.time())-60),'hash':'0x'+'ab'*32}
            return await previous(method,params)
        c.rpc.call=rpc
        c.rpc.head=509
        await LiveExecutor.processEntry(c.owner,c.selection())
        self.assertEqual(c.status(),"PROBE_CONFIRMED")
        c.gate.headCached=None
        c.rpc.head=510
        await LiveExecutor.processEntry(c.owner,c.selection())
        self.assertEqual(c.status(),"BUY_PENDING")
        self.assertEqual(c.routes.quoteCalls,2)
        self.assertEqual(len(c.owner.tx.signed),2)

class CleanJournalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=JournalDeadlineTests.asyncSetUp
    asyncTearDown=JournalDeadlineTests.asyncTearDown

    async def test_all_intents_persist_possible_send_before_rpc(self):
        await self.journal.create("i","s","BUY",self.tx,1)
        async def send(*args):
            row=self.store.db.execute("SELECT state FROM transactions WHERE intentId='i'").fetchone()
            self.assertEqual(row[0],"UNCERTAIN")
            return "0x"+"00"*32
        with patch("wallet_transactions.sendRaw",side_effect=send):
            self.assertEqual(await self.journal.broadcast("i"),"SENT")

    async def test_all_intents_parallel_preflight(self):
        old=self.journal.rpc.call
        inFlight=peak=0
        async def delayed(method,params):
            nonlocal inFlight,peak
            inFlight+=1;peak=max(peak,inFlight)
            try:
                await asyncio.sleep(.02)
                return await old(method,params)
            finally:inFlight-=1
        self.journal.rpc.call=delayed
        await self.journal.create("i","s","BUY",self.tx,1)
        self.assertGreaterEqual(peak,3)
