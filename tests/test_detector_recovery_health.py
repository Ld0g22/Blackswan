"""Build One regression: recovery failures must be visible in machine-readable health."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import detector


class RecoveryHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = detector.EventJournal(Path(self.directory.name) / "events.sqlite3")
        self.instance = detector.Detector({
            "ROBINHOOD_ALCHEMY_HTTP_URL": "https://example.invalid/rpc",
            "ROBINHOOD_ALCHEMY_WSS_URL": "wss://example.invalid/ws",
        }, self.journal, recoveryBlocks=2)

    async def asyncTearDown(self):
        self.journal.close()
        self.directory.cleanup()

    async def test_failed_backfill_is_machine_readable_not_merely_a_warning(self):
        async def failedRpc(session, method, params, cost):
            raise RuntimeError("simulated provider interruption")
        self.instance.rpc = failedRpc
        self.journal.setMeta("status", "LIVE")
        await self.instance.recover(None, 1000)
        health = self.journal.stats()
        self.assertEqual(health["status"], "LIVE", "LIVE means transport state, not coverage")
        self.assertEqual(health["recoveryStatus"], "INCOMPLETE")
        self.assertEqual(health["reportedGaps"], 1)
        self.assertNotEqual(health.get("lastBackfilledHead"), "1000")

    async def test_completed_backfill_publishes_coverage_head(self):
        async def emptySuccess(session, method, params, cost):
            return []
        self.instance.rpc = emptySuccess
        await self.instance.recover(None, 1000)
        health = self.journal.stats()
        self.assertEqual(health["recoveryStatus"], "COMPLETE")
        self.assertEqual(health["lastBackfilledHead"], "1000")
        self.assertEqual(health["reportedGaps"], 0)


if __name__ == "__main__":
    unittest.main()
