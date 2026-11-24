"""Reproduce the high block-rate, silent-pool and RPC faults seen in the live capture."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from concentration_selector import ConcentrationSelector
from five_second_selector import Candidate
from test_concentration_selector import ConcentrationHandoffTests, FakeRpc, swap


class FastChainRpc(FakeRpc):
    def __init__(self, logs, headers, limit=512):
        super().__init__(logs, headers)
        self.limit=limit

    async def __call__(self, method, params, timeout=4):
        if method=="eth_getLogs":
            self.calls.append((method,params))
            f=params[0]
            start,end=int(f["fromBlock"],16),int(f["toBlock"],16)
            if end-start+1>self.limit:
                raise RuntimeError("RPC_HTTP_413")
            return [raw for raw in self.logs if start<=int(raw["blockNumber"],16)<=end
                    and raw["topics"][1]==f["topics"][1]]
        return await super().__call__(method,params,timeout)


class ThroughputTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ConcentrationHandoffTests.asyncSetUp
    asyncTearDown=ConcentrationHandoffTests.asyncTearDown

    def fastChain(self,limit=512,head=1300):
        headers={n:100000+(n-1000)//10 for n in range(1000,head+1)}
        logs=[swap(self.venue,i+1,1000+i*30,0,25) for i in range(4)
              if 1000+i*30<=head]
        fake=FastChainRpc(logs,headers,limit)
        self.sel.rpc=fake
        return fake

    async def test_300_block_window_seals_in_one_bounded_scan(self):
        fake=self.fastChain()
        await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"],"PASS")
        self.assertEqual(json.loads(row["data"])["totalBuyBaseRaw"],"100")
        self.assertEqual(sum(m=="eth_getLogs" for m,p in fake.calls),1)

    async def test_provider_range_limit_reduces_batch_without_losing_coverage(self):
        fake=self.fastChain(limit=128)
        for _ in range(5):
            await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"],"PASS")
        self.assertEqual(json.loads(row["data"])["totalBuyBaseRaw"],"100")
        self.assertLessEqual(sum(m=="eth_getLogs" for m,p in fake.calls),6)

    async def test_active_window_waits_until_cutoff_instead_of_rescanning(self):
        self.fastChain(head=1000)
        self.sel.wallTime=lambda:100001
        await self.sel.scanOnce(self.key)
        cand=Candidate(self.key,self.event,self.venue)
        self.assertGreaterEqual(self.sel.nextScanDelay(cand),28)
        self.sel.wallTime=lambda:100030
        self.assertLessEqual(self.sel.nextScanDelay(cand),1)

    async def test_ws_hint_wakes_silent_pool_but_does_not_authorize_pass(self):
        cand=Candidate(self.key,self.event,self.venue)
        self.sel.acceptLog(cand,swap(self.venue,1,1000,0,25),100000,"live")
        self.assertTrue(cand.wake.is_set())
        self.assertTrue(self.sel.hasTradeHint(cand))
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],0)
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM concentration_swaps").fetchone()[0],0)

    async def test_unstarted_expired_candidate_gets_explicit_coverage_timeout(self):
        self.sel.store.db.execute("UPDATE launches SET detectedAt=1")
        self.sel.expireUnstarted(500)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertEqual(row["status"],"UNKNOWN")
        data=json.loads(row["data"])
        self.assertEqual(data["reason"],"OBSERVATION_COVERAGE_TIMEOUT")
        self.assertIn("SCHEDULER_NOT_STARTED",data["evidenceErrors"])

    async def test_rpc_http_429_preserves_code_and_stops_immediate_retry(self):
        class Response:
            status=429
            headers={"Retry-After":"2"}
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
        class Session:
            calls=0
            def post(self,*args,**kwargs):
                self.calls+=1
                return Response()
        self.sel.session=Session()
        with self.assertRaisesRegex(RuntimeError,"RPC_HTTP_429"):
            await self.sel.rpc("eth_getLogs",[])
        with self.assertRaisesRegex(RuntimeError,"RPC_BACKOFF_ACTIVE"):
            await self.sel.rpc("eth_blockNumber",[])
        self.assertEqual(self.sel.session.calls,1)
        self.assertGreater(self.sel.rpcPauseUntil,time.monotonic())

    async def test_changed_end_hash_during_range_read_never_commits(self):
        fake=self.fastChain()
        original=fake.__call__
        seen=0
        async def reorg(method,params,timeout=4):
            nonlocal seen
            result=await original(method,params,timeout)
            if method=="eth_getBlockByNumber" and int(params[0],16)==1300:
                seen+=1
                if seen>=2:
                    result["hash"]="0x"+"f"*64
            return result
        self.sel.rpc=reorg
        with self.assertRaisesRegex(RuntimeError,"RANGE_REORG"):
            await self.sel.scanOnce(self.key)
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM concentration_chunks").fetchone()[0],0)
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],0)
