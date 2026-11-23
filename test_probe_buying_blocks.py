"""Offline behavioural tests for the REAL 500-block LambGate and v4 event collector.

No private keys, outbound RPC or trading. The oracle is independently built
synthetic ABI-correct eth_getLogs, not the collector's own decoder.
"""
import asyncio
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace
import unittest

from probe_buying_blocks import (
    ProbeBuyingBlocks, MANAGER, SWAP, MAX_BUYING_BLOCKS, venueFor,
)
from sacrificial_lamb import LambGate

TOKEN="0x"+"a"*40
NATIVE="0x"+"0"*40
POOL="0x"+"b"*64
PROBE_TX="0x"+"c"*64
PROBE_HASH="0x"+"d"*64
SELECTION="test-selection"
PROBE_BLOCK=100


def venueEvent():
    return json.dumps({"venue":{
        "source":"uniswapV4","id":POOL,"token0":NATIVE,
        "token1":TOKEN,"baseAsset":NATIVE,
        "fee":100,"spacing":1,"hooks":NATIVE}})


def swapLog(block,buy=True,index=0):
    # A v4 Swap uses int128 amount0, int128 amount1, uint160 sqrtPrice,
    # uint128 liquidity, int24 tick, uint24 fee, each ABI word padded to 32 bytes.
    signed0=("f"*64 if buy else "0"*63+"1")
    signed1=("0"*63+"1" if buy else "f"*64)
    return {"address":MANAGER,"topics":[SWAP,POOL],
            "blockNumber":hex(block),"transactionHash":"0x"+f"{block*10+index:064x}",
            "logIndex":hex(index),"data":"0x"+signed0+signed1+"0"*256}


class FakeRpc:
    def __init__(self,buys=(),sells=()):
        self.buys=list(buys)
        self.sells=list(sells)
        self.logCalls=0
        self.headerCalls=0
        self.revision=0
        self.changeDuringLog=False
        self.failOneRange=False
        self.invalidLog=False
    async def call(self,method,params):
        if method=="eth_blockNumber":
            return hex(1000)
        if method=="eth_getTransactionReceipt":
            return {"transactionHash":PROBE_TX,"blockNumber":hex(PROBE_BLOCK),
                    "blockHash":PROBE_HASH,"status":"0x1"}
        if method=="eth_getBlockByNumber":
            self.headerCalls+=1
            block=int(params[0],16)
            return {"number":hex(block),
                    "hash":"0x"+f"{block+self.revision*10000:064x}"}
        if method!="eth_getLogs":
            raise AssertionError(method)
        self.logCalls+=1
        if self.failOneRange:
            self.failOneRange=False
            raise RuntimeError("RPC_UNAVAILABLE")
        if self.changeDuringLog and self.logCalls==5:
            self.revision+=1
        qry=params[0]
        assert qry["address"]==MANAGER
        assert qry["topics"]==[SWAP,POOL]
        low=int(qry["fromBlock"],16)
        high=int(qry["toBlock"],16)
        assert high-low==9
        events=([swapLog(b,True,i) for i,b in enumerate(self.buys) if low<=b<=high]
               +[swapLog(b,False,i) for i,b in enumerate(self.sells) if low<=b<=high])
        if self.invalidLog and events:
            events[0]["data"]="0x1234"
        return sorted(events,key=lambda x:int(x["blockNumber"],16))


class DummyStore:
    def __init__(self):
        self.db=sqlite3.connect(":memory:",isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
          CREATE TABLE selections(selectionId TEXT PRIMARY KEY,token TEXT,
                 status TEXT,event TEXT,reason TEXT);
          CREATE TABLE lamb_probes(selectionId TEXT PRIMARY KEY,probeTxHash TEXT,
                 confirmedAt REAL,probeQty TEXT,passedAt REAL,result TEXT,reason TEXT,
                 lastCheck REAL);
          CREATE TABLE transactions(selectionId TEXT,kind TEXT,txHash TEXT,
                 state TEXT,receipt TEXT);
        """)
        self.db.execute("INSERT INTO selections VALUES(?,?,?,?,?)",
                        (SELECTION,TOKEN,"PROBE_CONFIRMED",venueEvent(),None))
        receipt={"transactionHash":PROBE_TX,"blockHash":PROBE_HASH,
                 "blockNumber":hex(PROBE_BLOCK),"status":"0x1"}
        self.db.execute("INSERT INTO transactions VALUES(?,?,?,?,?)",
                        (SELECTION,"PROBE_BUY",PROBE_TX,"CONFIRMED",json.dumps(receipt)))
        self.db.execute("INSERT INTO lamb_probes VALUES(?,?,?,?,?,?,?,?)",
                        (SELECTION,PROBE_TX,1,"100",None,"PROBE_CONFIRMED",None,None))
        self.buyGbp=Decimal(".20")
    def get(self,key,default=None):
        return "OPEN" if key=="intakeState" else default
    def eligible(self):
        return True


class DummyFx:
    async def get(self):
        return Decimal(1000)


class DummyRoutes:
    def __init__(self):self.calls=0
    async def verifiedBuyCandidates(self,token,amount):
        self.calls+=1
        return [{"output":100,"preBuySellOutput":20,
                 "block":hex(601),"preBuySellBlock":hex(601)}]
    async def directQuotes(self,token,qty,buy,block):
        return [{"output":20,"block":block}]


class TestProbeBuyingBlocks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=DummyStore()
        self.rpc=FakeRpc()
        self.selector=sqlite3.connect(":memory:")
        self.selector.execute("CREATE TABLE revocations(eventKey TEXT)")
        self.routes=DummyRoutes()
        self.owner=SimpleNamespace(
            store=self.store,rpc=self.rpc,fx=DummyFx(),swaps=self.routes,
            selector=self.selector,intakeLock=asyncio.Lock())
        async def ponsOnChain(token):return None
        self.owner.ponsOnChain=ponsOnChain
        self.gate=LambGate(self.owner,lambda *args:100,waitBlocks=500)
        self.gate.activity.requestPause=0
    async def asyncTearDown(self):
        self.selector.close()
        self.store.db.close()

    def selection(self):
        return self.store.db.execute("SELECT * FROM selections").fetchone()
    def state(self):
        return self.selection()["status"]
    async def collectAll(self):
        for n in range(5):
            await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        self.assertEqual(self.rpc.logCalls,50)
        self.assertEqual(len(self.gate.activity.rows(SELECTION)),5)

    async def test_zero_and_seven_pass_main_gate(self):
        for buys in ([],[101,102,120,201,301,401,600]):
            self.rpc.buys=buys
            self.gate.activity.clear(SELECTION)
            await self.collectAll()
            await self.gate.check(self.selection())
            self.assertEqual(self.state(),"QUEUED")
            self.assertEqual(self.gate.activity.passed(SELECTION)["count"],len(buys))
            self.assertIsNotNone(self.gate.readyAt(SELECTION))
            self.assertTrue(await self.gate.verifyActivityBeforeBuy(self.selection()))
            self.assertEqual(self.routes.calls,1 if not buys else 2)
            # Reset selection for the next subcase; not a production transition.
            self.store.db.execute(
                "UPDATE selections SET status='PROBE_CONFIRMED' WHERE selectionId=?",
                (SELECTION,))
            self.rpc.logCalls=0

    async def test_incomplete_or_unevaluated_history_never_authorises_main_buy(self):
        self.assertFalse(await self.gate.verifyActivityBeforeBuy(self.selection()))
        self.assertIsNone(self.gate.readyAt(SELECTION))
        await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        await self.gate.check(self.selection())
        self.assertEqual(self.state(),"PROBE_CONFIRMED")
        self.assertEqual(self.routes.calls,0)
        self.assertFalse(await self.gate.verifyActivityBeforeBuy(self.selection()))
        for index in range(4):
            await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        # Even complete bins cannot authorise the signer until evaluated.
        self.assertFalse(await self.gate.verifyActivityBeforeBuy(self.selection()))

    async def test_legacy_ten_second_gate_has_no_activity_dependency(self):
        legacy=LambGate(self.owner,lambda *args:100,waitBlocks=None)
        self.assertIsNone(legacy.activity)
        self.store.db.execute("UPDATE lamb_probes SET confirmedAt=1")
        await legacy.check(self.selection())
        self.assertEqual(self.state(),"QUEUED")
        self.assertEqual(self.routes.calls,1)

    async def test_eight_fails_before_buy_quotes(self):
        self.rpc.buys=[101,102,103,104,105,201,301,600]
        await self.collectAll()
        await self.gate.check(self.selection())
        self.assertEqual(self.state(),"PROBE_FAILED")
        self.assertEqual(self.routes.calls,0)
        self.assertIsNone(self.gate.readyAt(SELECTION))
        self.assertIsNone(self.gate.activity.passed(SELECTION))
        self.assertIn("EXCEEDED_8",self.selection()["reason"])

    async def test_two_buys_same_block_count_once(self):
        self.rpc.buys=[101,101,101,101,102,201,301,401,501]
        await self.collectAll()
        verdict,count=await self.gate.activity.evaluate(self.selection(),PROBE_BLOCK)
        self.assertEqual((verdict,count),("PASS",6))

    async def test_missing_rpc_range_cannot_pass(self):
        self.rpc.failOneRange=True
        with self.assertRaisesRegex(RuntimeError,"RPC_UNAVAILABLE"):
            await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        verdict,count=await self.gate.activity.evaluate(self.selection(),PROBE_BLOCK)
        self.assertEqual((verdict,count),("UNKNOWN",None))
        self.assertIsNone(self.gate.activity.passed(SELECTION))
        self.assertEqual(self.rpc.logCalls,1)  # failed request was attempted once
        self.rpc.logCalls=0  # now count the complete retry pass separately
        await self.collectAll()
        self.assertEqual((await self.gate.activity.evaluate(self.selection(),PROBE_BLOCK)),
                         ("PASS",0))

    async def test_reorg_during_scan_discards_bin(self):
        self.rpc.changeDuringLog=True
        with self.assertRaisesRegex(RuntimeError,"REORG_DURING_ACTIVITY_COLLECTION"):
            await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        self.assertEqual(len(self.gate.activity.rows(SELECTION)),0)

    async def test_reorg_after_pass_invalidates_all(self):
        await self.collectAll()
        self.assertEqual((await self.gate.activity.evaluate(self.selection(),PROBE_BLOCK)),
                         ("PASS",0))
        self.rpc.revision+=1
        self.assertFalse(await self.gate.verifyActivityBeforeBuy(self.selection()))
        self.assertEqual((await self.gate.activity.evaluate(self.selection(),PROBE_BLOCK)),
                         ("UNKNOWN",None))
        self.assertEqual(len(self.gate.activity.rows(SELECTION)),0)
        self.assertIsNone(self.gate.activity.passed(SELECTION))

    async def test_restart_resumes_completed_bins(self):
        await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        self.assertEqual(self.rpc.logCalls,20)
        again=ProbeBuyingBlocks(self.store,self.rpc)
        again.requestPause=0
        for n in range(3):
            await again.collect(self.selection(),PROBE_BLOCK,1000)
        self.assertEqual(self.rpc.logCalls,50)
        self.assertEqual((await again.evaluate(self.selection(),PROBE_BLOCK)),
                         ("PASS",0))

    async def test_unmatured_blocks_never_enter_history(self):
        await self.gate.activity.collect(self.selection(),PROBE_BLOCK,199)
        self.assertEqual(self.rpc.logCalls,0)
        await self.gate.activity.collect(self.selection(),PROBE_BLOCK,200)
        self.assertEqual(self.rpc.logCalls,10)

    async def test_bad_swap_decode_never_silently_counts_zero(self):
        self.rpc.buys=[101]
        self.rpc.invalidLog=True
        with self.assertRaisesRegex(ValueError,"INVALID_ACTIVITY_SWAP_ABI"):
            await self.gate.activity.collect(self.selection(),PROBE_BLOCK,1000)
        self.assertEqual(len(self.gate.activity.rows(SELECTION)),0)

    async def test_stale_venue_rejected(self):
        self.store.db.execute(
            "UPDATE selections SET event=?",(json.dumps({"venue":{
                "source":"uniswapV4","id":POOL,"token0":TOKEN,"token1":TOKEN,
                "baseAsset":NATIVE}}),))
        with self.assertRaises(ValueError):
            venueFor(self.selection())

if __name__=="__main__":
    unittest.main()
