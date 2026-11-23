"""New campaign lifecycle safety, using the ORIGINAL launcher and fake workers."""
from pathlib import Path
import tempfile
import unittest
from live_executor import ExecutionStore
from run_live_campaign import readCampaignState, shouldStartSources, retireSources, requireExistingExecution, requireLiveExecutor, safeReadLiveState

class Worker:
    def __init__(self,name):self.name=name;self.dead=False
    def poll(self):return 0 if self.dead else None

class TestSourceRetirement(unittest.TestCase):
    def test_fifth_normal_fill_closes_only_new_entry_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            store=ExecutionStore(path,sacrifice=True)
            self.assertTrue(shouldStartSources(True,readCampaignState(path)))
            store.set('intakeState','CLOSED')
            self.assertFalse(shouldStartSources(True,readCampaignState(path)))
            self.assertTrue(shouldStartSources(False,readCampaignState(path)))
            self.assertFalse(store.eligible())
            store.db.close()

    def test_selector_is_retired_first_and_original_executor_is_not_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            store=ExecutionStore(path,sacrifice=True);store.set('intakeState','CLOSED');store.db.close()
            order=[]
            selector=Worker('SELECTOR');detector=Worker('DETECTOR')
            executor=Worker('EXECUTOR')
            def stop(workers):
                for w in workers:
                    order.append(w.name);w.dead=True
            retireSources(selector,detector,path,stop)
            self.assertEqual(order,['SELECTOR','DETECTOR'])
            self.assertFalse(executor.dead)
            self.assertEqual(readCampaignState(path)['workers'],'RETIRED')

    def test_resume_missing_execution_ledger_fails_without_starting_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            with self.assertRaisesRegex(RuntimeError,'MISSING_EXECUTION_LEDGER'):
                requireExistingExecution(path)
            store=ExecutionStore(path,sacrifice=True);store.db.close()
            requireExistingExecution(path)

    def test_partial_database_creation_is_unknown_not_an_engine_shutdown(self):
        """No file, then an empty SQLite file, then a fully initialized DB."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            self.assertIsNone(safeReadLiveState(path))
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE initializing(note TEXT)')
            self.assertIsNone(safeReadLiveState(path))
            # A half-built/foreign DB must stay untouched. Independently verify
            # that a fully initialized five-lamb DB is readable.
            ready=Path(tmp)/'ready.sqlite3'
            db=ExecutionStore(ready,sacrifice=True)
            db.db.close()
            self.assertEqual(safeReadLiveState(ready)['intake'],'OPEN')

    def test_child_exit_is_still_a_hard_error(self):
        class FailedChild:
            def poll(self):return 1
        class RunningChild:
            def poll(self):return None
        requireLiveExecutor(RunningChild())
        with self.assertRaisesRegex(RuntimeError,'LIVE_EXECUTOR_STOPPED'):
            requireLiveExecutor(FailedChild())

    def test_unavailable_status_reader_is_nonfatal_for_running_executor(self):
        """Only launcher's status poll may be skipped; initial resume remains strict."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            with patch('run_live_campaign.readCampaignState',
                       side_effect=RuntimeError('CAMPAIGN_STATE_UNREADABLE')):
                self.assertIsNone(safeReadLiveState(path))
            # Strict resume path is intentionally NOT converted to permissive.
            with self.assertRaisesRegex(RuntimeError,'CAMPAIGN_STATE_UNREADABLE'):
                path.write_bytes(b'bad sqlite')
                readCampaignState(path)

    def test_live_status_recovers_once_sqlite_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            path.write_bytes(b'not sqlite')
            self.assertIsNone(safeReadLiveState(path))
            path.unlink()
            store=ExecutionStore(path,sacrifice=True)
            store.db.close()
            self.assertEqual(safeReadLiveState(path)['intake'],'OPEN')

    def test_invalid_existing_campaign_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'execution.sqlite3'
            path.write_bytes(b'not sqlite')
            with self.assertRaisesRegex(RuntimeError,'CAMPAIGN_STATE_UNREADABLE'):
                readCampaignState(path)

if __name__=='__main__':unittest.main()
