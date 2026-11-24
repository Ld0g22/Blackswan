"""Concentration runtime throughput and readiness regressions (read-only)."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest

from concentration_selector import ConcentrationSelector
from run_configured_campaign import selectorReady
from test_concentration_selector import ConcentrationHandoffTests


class ConcentrationRuntimeTests(ConcentrationHandoffTests):
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

    async def test_restart_clears_stale_selector_ready_flag(self):
        self.sel.markReady(4663)
        self.assertTrue(selectorReady(self.selectorPath))
        self.sel.detector.close();self.sel.store.db.close()
        self.sel=ConcentrationSelector(self.detectorPath,self.selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.assertFalse(selectorReady(self.selectorPath))


if __name__=="__main__":
    unittest.main()
