"""Adversarial concentration tests: canonicality, operational limits and safe recovery."""
import json
from pathlib import Path
import tempfile
import unittest

from concentration_selector import ConcentrationSelector
from five_second_selector import SelectorStore
from test_concentration_selector import ConcentrationHandoffTests, FakeRpc, swap


class ConcentrationEdgeCases(ConcentrationHandoffTests):
    async def test_window_begins_on_first_sell_not_pool_initialize(self):
        logs=[swap(self.venue,1,1001,0,-50),
              swap(self.venue,2,1002,0,25),
              swap(self.venue,3,1002,1,25),
              swap(self.venue,4,1003,0,25),
              swap(self.venue,5,1004,0,25)]
        self.sel.rpc=FakeRpc(logs,{
            1000:100000,1001:100100,1002:100110,1003:100120,
            1004:100129,1005:100130})
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"PASS")
        data=json.loads(row["data"])
        self.assertEqual(data["firstTradeAt"],100100)
        self.assertEqual(data["observedSwapCount"],5)

    async def test_log_at_exactly_window_end_is_excluded(self):
        logs=[swap(self.venue,1,1000,0,25),
              swap(self.venue,2,1001,0,25),
              swap(self.venue,3,1002,0,25),
              swap(self.venue,4,1002,1,25),
              swap(self.venue,5,1003,0,1000000)]
        self.sel.rpc=FakeRpc(logs,{
            1000:100000,1001:100010,1002:100020,1003:100030})
        await self.sel.scanOnce(self.key)
        data=json.loads(self.sel.store.db.execute("SELECT data FROM decisions").fetchone()[0])
        self.assertEqual(data["totalBuyBaseRaw"],"100")
        self.assertEqual(data["observedSwapCount"],4)
        self.assertEqual(data["status"],"PASS")

    async def test_complete_sell_only_window_is_fail(self):
        logs=[swap(self.venue,1,1000,0,-10),swap(self.venue,2,1001,0,-10)]
        self.sel.rpc=FakeRpc(logs,{
            1000:100000,1001:100010,1002:100020,1003:100030})
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"FAIL")
        self.assertEqual(json.loads(row["data"])["reason"],"NO_BUY_VOLUME")

    async def test_no_first_trade_by_budget_marks_unknown(self):
        self.sel.rpc=FakeRpc([],{1000:100000,1001:100301})
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"UNKNOWN")
        self.assertEqual(json.loads(row["data"])["reason"],"NO_FIRST_TRADE_WITHIN_OBSERVATION_BUDGET")

    async def test_first_trade_after_timeout_does_not_pass(self):
        logs=[swap(self.venue,1,1001,0,25),swap(self.venue,2,1002,0,25),
              swap(self.venue,3,1002,1,25),swap(self.venue,4,1003,0,25)]
        self.sel.rpc=FakeRpc(logs,{
            1000:100000,1001:100301,1002:100311,1003:100331})
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status FROM decisions").fetchone()
        self.assertEqual(row[0],"UNKNOWN")

    async def test_noncanonical_log_never_passes(self):
        fake=self.fake([25,25,25,25])
        original=fake.__call__
        async def changed(method,params,timeout=4):
            if method=="eth_getBlockByNumber" and int(params[0],16)==1000:
                return {"number":hex(1000),"hash":"0x"+"f"*64,"timestamp":hex(100000)}
            return await original(method,params,timeout)
        self.sel.rpc=changed
        with self.assertRaisesRegex(RuntimeError,"NONCANONICAL"):
            await self.sel.scanOnce(self.key)
        self.assertFalse(self.sel.store.db.execute("SELECT 1 FROM decisions").fetchone())
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM concentration_chunks").fetchone()[0],0)

    async def test_published_pass_revoked_after_canonical_block_reorg(self):
        fake=self.fake([25,25,25,25])
        await self.sel.scanOnce(self.key)
        original=fake.__call__
        async def changed(method,params,timeout=4):
            if method=="eth_getBlockByNumber" and int(params[0],16)==1003:
                return {"number":hex(1003),"hash":"0x"+"f"*64,"timestamp":hex(100030)}
            return await original(method,params,timeout)
        self.sel.rpc=changed
        await self.sel.auditFinal()
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM revocations").fetchone()[0],1)
        await self.sel.auditFinal()
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM revocations").fetchone()[0],1)

    async def test_canonical_batch_hash_changes_and_rewinds(self):
        fake=self.fake([25,25,25,25],head=1001)
        await self.sel.scanOnce(self.key)
        original=fake.__call__
        async def changed(method,params,timeout=4):
            if method=="eth_getBlockByNumber" and int(params[0],16)==1001:
                return {"number":hex(1001),"hash":"0x"+"f"*64,"timestamp":hex(100010)}
            return await original(method,params,timeout)
        # Force a resume, requiring a checkpoint validation rather than
        # treating the previous chain's coverage as current canonical data.
        self.sel.detector.close();self.sel.store.db.close()
        self.sel=ConcentrationSelector(self.detectorPath,self.selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.sel.rpc=changed
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT nextBlock FROM concentration_progress").fetchone()[0],1000)
        self.assertFalse(self.sel.store.db.execute("SELECT 1 FROM decisions").fetchone())

    async def test_legacy_selector_database_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"selector.sqlite3"
            legacy=SelectorStore(path)
            legacy.db.execute("INSERT INTO meta(key,value) VALUES('decisionSeconds','20')")
            legacy.db.close()
            previous=path.read_bytes()
            with self.assertRaisesRegex(RuntimeError,"MISMATCH"):
                ConcentrationSelector(self.detectorPath,path,
                    "https://not-used.invalid","wss://not-used.invalid")
            self.assertEqual(path.read_bytes(),previous)


if __name__=="__main__":
    unittest.main()
