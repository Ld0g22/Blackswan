"""Concentration Build One: exact transaction shares and executable outbox contract.

The fake RPC models canonical chain logs/headers; no wallet or network calls.
These tests intentionally fail before concentration_selector.py exists.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from concentration_selector import ConcentrationSelector, concentrationDecision
from detector import EventJournal, decodeLog
from five_second_selector import eligibleVenues
from research.venue_decoders import V4_MANAGER, V4_SWAP
from test_detector import logTemplate
from live_executor import ExecutionStore, LiveExecutor
from campaign_v3 import defaultCampaign, validateCampaign


def word(v):
    return (v % (1 << 256)).to_bytes(32,"big").hex()


def swap(venue,tx,block,index,base,when=None,removed=False):
    """V4 signed amount: buyBaseAmount positive for negative base delta."""
    isZero=venue["baseAsset"]==venue["token0"]
    a0=-base if isZero else 0
    a1=-base if not isZero else 0
    return {
        "address":V4_MANAGER,
        "topics":[V4_SWAP,venue["id"],"0x"+word(1)],
        "data":"0x"+word(a0)+word(a1)+word(2**96)+word(100)+word(0)+word(3000),
        "blockNumber":hex(block),"blockHash":"0x"+f"{block:064x}",
        "transactionHash":"0x"+f"{tx:064x}","transactionIndex":"0x1",
        "logIndex":hex(index),"removed":removed,
    }


class FakeRpc:
    def __init__(self,logs,headers):
        self.logs=list(logs)
        self.headers=dict(headers)
        self.calls=[]
        self.failOnce=False

    async def __call__(self,method,params,timeout=4):
        self.calls.append((method,params))
        if self.failOnce and method=="eth_getLogs":
            self.failOnce=False
            raise RuntimeError("RPC_HTTP_429")
        if method=="eth_chainId":return hex(4663)
        if method=="eth_blockNumber":return hex(max(self.headers))
        if method=="eth_getBlockByNumber":
            block=int(params[0],16)
            if block not in self.headers:return None
            return {"number":hex(block),"hash":"0x"+f"{block:064x}",
                    "timestamp":hex(self.headers[block])}
        if method=="eth_getLogs":
            f=params[0]
            start,end=int(f["fromBlock"],16),int(f["toBlock"],16)
            if end-start>=10:raise AssertionError("Unbounded getLogs range")
            return [raw for raw in self.logs if start<=int(raw["blockNumber"],16)<=end
                    and raw["topics"][1]==f["topics"][1]]
        raise AssertionError("Unexpected method "+method)


class ConcentrationArithmeticTests(unittest.TestCase):
    def test_exact_25_percent_boundary_and_above(self):
        rows=[{"transactionHash":str(i),"buyBaseRaw":25,"blockTimestamp":100+i}
              for i in range(4)]
        self.assertEqual(concentrationDecision(rows,100)["status"],"PASS")
        rows[0]["buyBaseRaw"]=26
        self.assertEqual(concentrationDecision(rows,100)["status"],"FAIL")

    def test_group_multiple_swaps_same_transaction(self):
        rows=[{"transactionHash":"A","buyBaseRaw":20,"blockTimestamp":100},
              {"transactionHash":"A","buyBaseRaw":20,"blockTimestamp":100},
              *[{"transactionHash":str(i),"buyBaseRaw":20,"blockTimestamp":105}
                for i in range(3)]]
        actual=concentrationDecision(rows,100)
        self.assertEqual(actual["status"],"FAIL")
        self.assertEqual(actual["largestBuyTransactionBaseRaw"],"40")
        self.assertEqual(actual["totalBuyBaseRaw"],"100")
        self.assertEqual(actual["buyTransactionCount"],4)

    def test_first_trade_time_window_exclusive_and_zero_buys(self):
        rows=[{"transactionHash":str(i),"buyBaseRaw":25,"blockTimestamp":101+i}
              for i in range(4)]
        rows.append({"transactionHash":"late","buyBaseRaw":999999,"blockTimestamp":130})
        self.assertEqual(concentrationDecision(rows,100)["status"],"PASS")
        self.assertEqual(concentrationDecision(
            [{"transactionHash":"s","buyBaseRaw":0,"blockTimestamp":100}],100)["reason"],
            "NO_BUY_VOLUME")


class ConcentrationHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.detectorPath=root/"detector.sqlite3"
        self.selectorPath=root/"selector.sqlite3"
        self.detector=EventJournal(self.detectorPath)
        self.event=decodeLog(logTemplate("uniswapV4"))
        self.detector.append(self.event)
        self.venue=eligibleVenues(self.event)[0]
        self.sel=ConcentrationSelector(self.detectorPath,self.selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.sel.wallTime=lambda:100030
        self.key=self.event["eventKey"]+":"+self.venue["venueKey"]
        self.sel.pull()
        self.assertEqual(self.sel.store.db.execute(
            "SELECT state FROM launches WHERE eventKey=?",(self.key,)).fetchone()[0],"PENDING")

    async def asyncTearDown(self):
        self.sel.detector.close()
        self.sel.store.db.close()
        self.detector.close()
        self.tmp.cleanup()

    def fake(self,amounts,head=1003):
        headers={1000:100000,1001:100010,1002:100020,1003:100030}
        logs=[swap(self.venue,i+1,1000+i//2,i+1,value) for i,value in enumerate(amounts)]
        result=FakeRpc(logs,{n:v for n,v in headers.items() if n<=head})
        self.sel.rpc=result
        return result

    async def test_first_trade_rule_to_existing_execution_lamb_queue(self):
        fake=self.fake([25,25,25,25])
        for _ in range(4):
            await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute(
            "SELECT * FROM decisions WHERE eventKey=?",(self.key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"],"PASS")
        payload=json.loads(row["data"])
        self.assertEqual(payload["policyName"],"concentration")
        self.assertEqual(payload["firstTradeAt"],100000)
        self.assertTrue(payload["coverageComplete"])
        self.assertEqual(payload["totalBuyBaseRaw"],"100")
        self.assertTrue(all(m not in ("eth_sendRawTransaction",) for m,p in fake.calls))
        cfg=validateCampaign(defaultCampaign("0x"+"1"*40))
        store=ExecutionStore(Path(self.tmp.name)/"execution.sqlite3",configuredCampaign=cfg)
        consumer=object.__new__(LiveExecutor)
        consumer.store=store
        consumer.selector=sqlite3.connect(f"file:{self.selectorPath.resolve()}?mode=ro",uri=True)
        consumer.selector.row_factory=sqlite3.Row
        consumer.detector=sqlite3.connect(f"file:{self.detectorPath.resolve()}?mode=ro",uri=True)
        consumer.detector.row_factory=sqlite3.Row
        try:
            consumer.readSelections()
            self.assertEqual(store.db.execute("SELECT status FROM selections").fetchone()[0],
                             "LAMB_QUEUED")
            consumer.readSelections()
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM selections").fetchone()[0],1)
        finally:
            consumer.selector.close()
            consumer.detector.close()
            store.db.close()

    async def test_historic_30_second_window_cannot_become_fresh_live_pass(self):
        fake=self.fake([25,25,25,25])
        self.sel.wallTime=lambda:100060
        self.sel.liveStartAt=100040
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"PASS")
        self.assertFalse(json.loads(row["data"])["actionable"])

    async def test_same_transaction_split_swaps_is_fail_not_pass(self):
        logs=[swap(self.venue,1,1000,0,20),swap(self.venue,1,1000,1,20),
              swap(self.venue,2,1001,0,20),swap(self.venue,3,1001,1,20),
              swap(self.venue,4,1002,0,20)]
        self.sel.rpc=FakeRpc(logs,{1000:100000,1001:100010,1002:100020,1003:100030})
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status FROM decisions").fetchone()
        self.assertEqual(row[0],"FAIL")

    async def test_rpc_429_remains_pending_until_recovery(self):
        fake=self.fake([25,25,25,25])
        fake.failOnce=True
        with self.assertRaisesRegex(RuntimeError,"429"):
            await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT state FROM launches").fetchone()[0],"PENDING")
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT status FROM decisions").fetchone()[0],"PASS")

    async def test_restart_recovers_pending_journal_without_duplicate_decision(self):
        fake=self.fake([25,25,25,25],head=1001)
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT state FROM launches").fetchone()[0],"PENDING")
        self.sel.detector.close();self.sel.store.db.close()
        self.sel=ConcentrationSelector(self.detectorPath,self.selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.sel.wallTime=lambda:100030
        fake.headers[1002]=100020;fake.headers[1003]=100030
        self.sel.rpc=fake
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT COUNT(*) FROM decisions").fetchone()[0],1)
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT COUNT(*) FROM decisions").fetchone()[0],1)

    async def test_removed_detector_launch_revokes_pass(self):
        self.fake([25,25,25,25])
        await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute(
            "SELECT status FROM decisions").fetchone()[0],"PASS")
        removed=dict(self.event,removed=True)
        self.detector.append(removed)
        self.sel.pull()
        self.assertEqual(self.sel.store.db.execute(
            "SELECT COUNT(*) FROM revocations").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
