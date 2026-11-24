"""Concentration runtime throughput and readiness regressions (read-only)."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from concentration_selector import ConcentrationSelector,MAX_ACTIVE_CANDIDATES
from run_configured_campaign import selectorReady,resumeNeedsSources
from test_concentration_selector import ConcentrationHandoffTests
from five_second_selector import Candidate


class ConcentrationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ConcentrationHandoffTests.asyncSetUp
    asyncTearDown=ConcentrationHandoffTests.asyncTearDown
    fake=ConcentrationHandoffTests.fake
    async def test_pending_scheduler_skips_existing_active_candidates(self):
        keys=[self.key]
        for number in range(2,7):
            event=dict(self.event,eventKey=self.event["eventKey"]+":"+str(number))
            key=self.sel.store.create(event,self.venue,float(number))
            keys.append(key)
        # The first two pending candidates are already active; a LIMIT of 2
        # on the SQL query would never start the next two.
        self.sel.candidates[keys[0]]=object()
        self.sel.candidates[keys[1]]=object()
        pending=self.sel.pendingToStart(2)
        self.assertEqual([row["eventKey"] for row in pending],keys[2:4])

    async def test_shared_head_cache_suppresses_repeated_rpc_calls(self):
        fake=self.fake([25,25,25,25])
        heads=await asyncio.gather(*[self.sel.head() for _ in range(10)])
        self.assertEqual(heads,[1003]*10)
        self.assertEqual(sum(call[0]=="eth_blockNumber" for call in fake.calls),1)

    async def test_selector_not_ready_until_chain_is_verified(self):
        self.assertFalse(selectorReady(self.selectorPath))
        self.sel.markReady(4663)
        self.assertTrue(selectorReady(self.selectorPath))

    async def test_idle_pools_yield_slots_to_new_launches_without_losing_progress(self):
        # Reproduce the production problem: twelve no-trade pools fill the
        # scheduler; a fresh pool at the end cannot be observed for minutes.
        oldKeys=[self.key]
        for n in range(2,MAX_ACTIVE_CANDIDATES+1):
            ev=dict(self.event,eventKey=self.event["eventKey"]+":"+str(n))
            oldKeys.append(self.sel.store.create(ev,self.venue,float(n)))
        for index,key in enumerate(oldKeys):
            row=self.sel.store.db.execute(
                "SELECT data FROM launches WHERE eventKey=?",(key,)).fetchone()
            obj=__import__("json").loads(row["data"])
            candidate=Candidate(key,obj["event"],obj["venue"])
            candidate.startedAt=100+index/1000
            self.sel.candidates[key]=candidate
        recent=dict(self.event,eventKey=self.event["eventKey"]+":new")
        fresh=self.sel.store.create(recent,self.venue,100)
        victim=self.sel.chooseSilentPreemption(110)
        self.assertEqual(victim,oldKeys[0])
        self.sel.deferUntil[victim]=125
        self.sel.candidates.pop(victim)
        self.sel.rotationClock=lambda:110
        self.assertEqual(self.sel.pendingToStart(1)[0]["eventKey"],fresh)
        # The displaced pool retains its durable SQLite observation cursor.
        self.assertEqual(self.sel.store.db.execute(
            "SELECT state FROM launches WHERE eventKey=?",(victim,)).fetchone()[0],
            "PENDING")
        self.assertNotIn(victim,[x["eventKey"] for x in self.sel.pendingToStart(2)])

    async def test_active_trade_window_cannot_be_preempted_as_silent(self):
        key=self.key
        candidate=Candidate(key,self.event,self.venue)
        candidate.startedAt=100
        self.sel.candidates[key]=candidate
        self.sel.store.db.execute(
            "INSERT INTO concentration_swaps(eventKey,blockNumber,blockHash,transactionHash,logIndex,blockTimestamp,buyBaseRaw,raw)"
            " VALUES(?,1000,?,?,0,100000,'1','{}')",
            (key,"0x"+"1"*64,"0x"+"2"*64))
        self.sel.rotationClock=lambda:110
        self.assertIsNone(self.sel.chooseSilentPreemption(110))

    def test_resume_closed_intake_never_restarts_sources_even_if_state_active(self):
        self.assertFalse(resumeNeedsSources({
            "intakeState":"CLOSED","workerState":"ACTIVE"}))
        self.assertFalse(resumeNeedsSources({
            "intakeState":"CLOSED","workerState":"RETIRED"}))
        self.assertTrue(resumeNeedsSources({"intakeState":"OPEN"}))

    async def test_restart_clears_stale_selector_ready_flag(self):
        self.sel.markReady(4663)
        self.assertTrue(selectorReady(self.selectorPath))
        self.sel.detector.close();self.sel.store.db.close()
        self.sel=ConcentrationSelector(self.detectorPath,self.selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.assertFalse(selectorReady(self.selectorPath))


if __name__=="__main__":
    unittest.main()
