"""Read-only process-to-run identity: no ps commands, secrets, RPC or wallet."""
from pathlib import Path
import tempfile
import unittest

from monitor_processes import findRunWorkers


class ProcessVisibilityTests(unittest.TestCase):
    def test_matches_only_exact_database_identity_not_other_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            run=root/"data"/"configured_v3_current"
            run.mkdir(parents=True)
            proc=root/"proc"
            proc.mkdir()
            def fake(pid,script,db):
                folder=proc/str(pid)
                folder.mkdir()
                (folder/"cmdline").write_bytes(
                    b"python3\0"+str(script).encode()+b"\0run\0--db\0"+
                    str(db).encode()+b"\0")
            fake(110,root/"live_executor.py",run/"execution.sqlite3")
            fake(120,root/"concentration_selector.py",run/"selector.sqlite3")
            fake(130,root/"detector.py",run/"detector.sqlite3")
            fake(140,root/"live_executor.py",root/"elsewhere"/"execution.sqlite3")
            fake(150,root/"wrong.py",run/"execution.sqlite3")
            result=findRunWorkers(run,proc)
            self.assertEqual(result["executor"],110)
            self.assertEqual(result["selector"],120)
            self.assertEqual(result["detector"],130)

    def test_no_proc_visibility_does_not_claim_process_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(findRunWorkers(root,root/"missing"),
                             {"executor":None,"selector":None,"detector":None,
                              "available":False})

    def test_no_running_workers_is_an_observation_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            proc=root/"proc"
            proc.mkdir()
            result=findRunWorkers(root,proc)
            self.assertTrue(result["available"])
            self.assertEqual(result["executor"],None)


if __name__=="__main__":
    unittest.main()
