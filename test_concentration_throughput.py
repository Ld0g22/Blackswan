"""Reproduce the high block-rate, silent-pool and RPC faults seen in the live capture."""
import asyncio
import json
import time
import unittest

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
            pools=f["topics"][1]
            pools={pools} if isinstance(pools,str) else set(pools)
            return [raw for raw in self.logs if start<=int(raw["blockNumber"],16)<=end
                    and raw["topics"][1] in pools]
        return await super().__call__(method,params,timeout)


class ThroughputTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ConcentrationHandoffTests.asyncSetUp
    asyncTearDown=ConcentrationHandoffTests.asyncTearDown

    def fastChain(self,limit=512,head=1300):
        self.sel.scanLimit=512  # Explicit wide-provider tests, not the free default.
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

    async def test_shared_scheduler_keeps_active_window_current_on_ten_block_provider(self):
        fake=self.fastChain(limit=10)
        self.sel.scanLimit=10
        self.sel.wallTime=lambda:100030
        for _ in range(31):
            await self.sel.scanGroup([self.key],head=1300)
        self.assertEqual(self.sel.store.db.execute("SELECT status FROM decisions").fetchone()[0],"PASS")
        self.assertEqual(sum(m=="eth_getLogs" for m,p in fake.calls),31)

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

    async def test_silent_five_minute_coverage_finishes_in_six_ranges(self):
        headers={n:100000+(n-1000)//10 for n in range(1000,4001)}
        fake=FastChainRpc([],headers)
        self.sel.scanLimit=512
        self.sel.rpc=fake
        self.sel.wallTime=lambda:100300
        for _ in range(6):
            await self.sel.scanOnce(self.key)
        row=self.sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"],"UNKNOWN")
        payload=json.loads(row["data"])
        self.assertEqual(payload["reason"],"NO_FIRST_TRADE_WITHIN_OBSERVATION_BUDGET")
        self.assertTrue(payload["coverageComplete"])
        self.assertEqual(sum(m=="eth_getLogs" for m,p in fake.calls),6)

    async def test_fourteen_overlapping_candidates_keep_correct_independent_results(self):
        # The actual capture contained a peak of fourteen unresolved candidates.
        # Two have trades; twelve are quiet. A shared head must not mix pool logs.
        from concentration_selector import MAX_ACTIVE_CANDIDATES
        self.assertGreaterEqual(MAX_ACTIVE_CANDIDATES,14)
        keys=[self.key]
        venues=[self.venue]
        for index in range(1,14):
            venue=dict(self.venue,id="0x"+f"{index+9000:064x}",
                       venueKey=self.venue["venueKey"]+":"+str(index))
            event=dict(self.event,eventKey=self.event["eventKey"]+":"+str(index))
            keys.append(self.sel.store.create(event,venue,time.time()))
            venues.append(venue)
        headers={n:100000+(n-1000)//10 for n in range(1000,1301)}
        logs=[swap(venues[0],i+1,1000+i*30,0,25) for i in range(4)]
        logs.extend(swap(venues[1],i+11,1000+i*30,0,amount)
                    for i,amount in enumerate((80,10,5,5)))
        fake=FastChainRpc(logs,headers)
        self.sel.rpc=fake
        self.sel.scanLimit=512
        await self.sel.scanGroup(keys)
        rows=dict(self.sel.store.db.execute("SELECT eventKey,status FROM decisions"))
        self.assertEqual(rows,{keys[0]:"PASS",keys[1]:"FAIL"})
        self.assertEqual(self.sel.store.db.execute(
            "SELECT COUNT(*) FROM launches WHERE state='PENDING'").fetchone()[0],12)
        self.assertEqual(sum(m=="eth_getLogs" for m,p in fake.calls),1)
        self.assertEqual(sum(m=="eth_blockNumber" for m,p in fake.calls),1)

    async def test_active_hint_prioritizes_fresh_range_over_silent_backlog(self):
        now=time.time()
        self.sel.store.db.execute("UPDATE launches SET detectedAt=?",(now,))
        event=dict(self.event,eventKey=self.event["eventKey"]+":new",blockNumber=1290)
        venue=dict(self.venue,id="0x"+f"{12345:064x}",venueKey=self.venue["venueKey"]+":new")
        key=self.sel.store.create(event,venue,now)
        cand=Candidate(key,event,venue)
        self.sel.candidates[key]=cand
        self.sel.acceptLog(cand,swap(venue,1,1290,0,25),now,"live")
        self.sel.scanLimit=10
        group=self.sel.selectCoverageRange(self.sel.coverageCandidates(),1300)
        self.assertEqual([row["eventKey"] for row in group],[key])
        self.assertEqual(self.sel.store.db.execute(
            "SELECT state FROM launches WHERE eventKey=?",(self.key,)).fetchone()[0],"PENDING")

    async def test_free_tier_shared_scan_covers_fourteen_pools_with_one_range_query(self):
        # Actual chain: about ten blocks/second, 14 unresolved pools, free-tier
        # maximum of ten blocks per eth_getLogs. All cursors overlap.
        keys=[self.key]
        venues=[self.venue]
        for index in range(1,14):
            venue=dict(self.venue,id="0x"+f"{index+9000:064x}",
                       venueKey=self.venue["venueKey"]+":"+str(index))
            event=dict(self.event,eventKey=self.event["eventKey"]+":shared:"+str(index))
            keys.append(self.sel.store.create(event,venue,time.time()))
            venues.append(venue)
        headers={n:100000+(n-1000)//10 for n in range(1000,1301)}
        logs=[swap(venues[0],i+1,1000+i*30,0,25) for i in range(4)]
        logs.extend(swap(venues[1],i+11,1000+i*30,0,amount)
                    for i,amount in enumerate((80,10,5,5)))
        fake=FastChainRpc(logs,headers,limit=10)
        self.sel.rpc=fake
        self.sel.scanLimit=10
        for _ in range(31):
            await self.sel.scanGroup(keys,head=1300)
        rows=dict(self.sel.store.db.execute("SELECT eventKey,status FROM decisions"))
        self.assertEqual(rows,{keys[0]:"PASS",keys[1]:"FAIL"})
        self.assertEqual(self.sel.store.db.execute(
            "SELECT COUNT(*) FROM launches WHERE state='PENDING'").fetchone()[0],12)
        ranges=[p[0] for m,p in fake.calls if m=="eth_getLogs"]
        self.assertEqual(len(ranges),31)
        self.assertTrue(all(int(p["toBlock"],16)-int(p["fromBlock"],16)<10 for p in ranges))
        self.assertTrue(all(isinstance(p["topics"][1],list) for p in ranges))
        self.assertLessEqual(sum(m=="eth_getBlockByNumber" for m,p in fake.calls),110)
        # Shared boundaries, not 14 separate per-pool revalidations.

    async def test_default_provider_range_is_ten_blocks(self):
        self.assertEqual(self.sel.scanLimit,10)

    async def test_provider_cap_catchup_does_not_wait_until_cutoff(self):
        fake=self.fastChain(limit=10)
        self.sel.scanLimit=10
        self.sel.wallTime=lambda:100010
        # Real scheduler, a window still open and canonical head far ahead of cursor.
        self.assertTrue(await self.sel.coverageCycle())
        self.assertTrue(await self.sel.coverageCycle())
        self.assertEqual(self.sel.store.db.execute(
            "SELECT nextBlock FROM concentration_progress WHERE eventKey=?",(self.key,)).fetchone()[0],1020)
        self.assertEqual(sum(m=="eth_getLogs" for m,p in fake.calls),2)
        self.assertEqual(self.sel.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],0)
