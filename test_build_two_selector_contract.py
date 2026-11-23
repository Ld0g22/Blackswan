"""Build One -> interchangeable selector outbox -> restored execution contract.

Uses real decoded source-shaped logs, actual SQLite journals and real historic
selector/executor readers. No network, private key, signer or money movement.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import detector
import peak_tracker as peak
from five_second_selector import SelectorStore, eligibleVenues
from live_executor import ExecutionStore, LiveExecutor
from test_detector import abiAddress, logTemplate


TOKEN = "0x" + "ab" * 20
BASES = (peak.NATIVE, peak.WETH, peak.USDG)


class SelectorSwapContractTests(unittest.TestCase):
    def exerciseSelector(self, base, policy):
        with tempfile.TemporaryDirectory() as temp:
            location = Path(temp)
            detectorPath = location / "detector.sqlite3"
            selectorPath = location / "selector.sqlite3"
            executionPath = location / "execution.sqlite3"
            detectorJournal = detector.EventJournal(detectorPath)
            selectorStore = SelectorStore(selectorPath)
            executionStore = ExecutionStore(executionPath)
            try:
                raw = logTemplate("uniswapV4")
                raw["topics"][2] = "0x" + abiAddress(base)
                raw["topics"][3] = "0x" + abiAddress(TOKEN)
                event = detector.decodeLog(raw)
                self.assertEqual(detectorJournal.append(event), 1)
                venues = eligibleVenues(event)
                self.assertEqual(len(venues), 1)
                selectedVenue = venues[0]
                self.assertEqual(selectedVenue["baseAsset"], base)

                # "buyingBlocks" and "concentration" can make opposite
                # research decisions but must publish the same stable contract.
                selectionId = selectorStore.create(event, selectedVenue, 12345.0)
                self.assertTrue(selectorStore.decide(
                    selectionId, "PASS",
                    {"policyName": policy, "policyVersion": "offline-test"},
                ))
                consumer = object.__new__(LiveExecutor)
                consumer.store = executionStore
                consumer.selector = sqlite3.connect(
                    f"file:{selectorPath.resolve()}?mode=ro", uri=True
                )
                consumer.selector.row_factory = sqlite3.Row
                consumer.detector = sqlite3.connect(
                    f"file:{detectorPath.resolve()}?mode=ro", uri=True
                )
                consumer.detector.row_factory = sqlite3.Row
                try:
                    consumer.readSelections()
                    row = executionStore.db.execute(
                        "SELECT * FROM selections WHERE selectionId=?", (selectionId,)
                    ).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["status"], "QUEUED")
                    self.assertEqual(row["token"], TOKEN)
                    self.assertEqual(json.loads(row["event"])["policyName"], policy)
                    self.assertEqual(executionStore.get("selectionCursor"), "1")
                    self.assertEqual(len(executionStore.venues(TOKEN)), 1)
                    self.assertEqual(executionStore.venues(TOKEN)[0]["source"], "uniswapV4")

                    # Idempotent replay must not duplicate an admitted order.
                    consumer.readSelections()
                    self.assertEqual(executionStore.db.execute(
                        "SELECT COUNT(*) FROM selections"
                    ).fetchone()[0], 1)

                    # Reorg removal must reach venue recovery and selector
                    # revocation without modifying a buy or sale engine.
                    removed = detector.decodeLog(dict(raw, removed=True))
                    self.assertEqual(detectorJournal.append(removed), 2)
                    consumer.readNewVenues()
                    self.assertEqual(executionStore.venues(TOKEN), [])
                    selectorStore.revoke(selectionId, "reorg")
                    consumer.readRevocations()
                    state = executionStore.db.execute(
                        "SELECT status FROM selections WHERE selectionId=?", (selectionId,)
                    ).fetchone()[0]
                    self.assertEqual(state, "REVOKED")
                finally:
                    consumer.selector.close()
                    consumer.detector.close()
            finally:
                executionStore.db.close()
                selectorStore.db.close()
                detectorJournal.close()

    def test_two_policy_outputs_use_same_engine_for_eth_weth_usdg(self):
        for base in BASES:
            for policy in ("buyingBlocks", "concentration"):
                with self.subTest(base=base, policy=policy):
                    self.exerciseSelector(base, policy)


if __name__ == "__main__":
    unittest.main()
