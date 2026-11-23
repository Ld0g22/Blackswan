"""Build One regression: event_state and event_log must commit atomically.

The source detector uses a single SQLite writer. A failure after its state
update must not mark the event as delivered when no outbox row exists.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import detector
from test_detector import logTemplate


class JournalAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = detector.EventJournal(Path(self.directory.name) / "events.sqlite3")

    def tearDown(self):
        self.journal.close()
        self.directory.cleanup()

    def failNextOutboxInsert(self):
        self.journal.db.execute(
            "CREATE TRIGGER reject_event_log BEFORE INSERT ON event_log "
            "BEGIN SELECT RAISE(ABORT, 'injected disk-write failure'); END"
        )

    def allowOutboxInsert(self):
        self.journal.db.execute("DROP TRIGGER reject_event_log")

    def test_new_event_is_not_lost_after_outbox_failure(self):
        event = detector.decodeLog(logTemplate("ponsV2", txNumber=100))
        self.failNextOutboxInsert()
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.append(event)
        self.assertIsNone(
            self.journal.db.execute(
                "SELECT removed FROM event_state WHERE event_key=?", (event["eventKey"],)
            ).fetchone()
        )
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM event_log").fetchone()[0], 0)
        self.allowOutboxInsert()
        self.assertEqual(self.journal.append(event), 1)

    def test_failed_reorg_update_does_not_change_persisted_state(self):
        event = detector.decodeLog(logTemplate("uniswapV4", txNumber=101))
        self.assertEqual(self.journal.append(event), 1)
        removal = {**event, "removed": True}
        self.failNextOutboxInsert()
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.append(removal)
        self.assertEqual(
            self.journal.db.execute(
                "SELECT removed FROM event_state WHERE event_key=?", (event["eventKey"],)
            ).fetchone()[0], 0
        )
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM event_log").fetchone()[0], 1)
        self.allowOutboxInsert()
        self.assertEqual(self.journal.append(removal), 2)
        self.assertEqual(self.journal.append(removal), None)


if __name__ == "__main__":
    unittest.main()
