"""Read-only concentration shadow operator safety and evidence checks."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from run_concentration_shadow import buildShadowCommands, shadowSummary


class ShadowOperatorTests(unittest.TestCase):
    def test_no_wallet_no_signer_no_executor_in_any_command(self):
        cmds=buildShadowCommands(Path("/repo"),Path("/venv/python"),
            Path("/secure.env"),Path("/data"))
        self.assertEqual(set(cmds),{"detector","selector"})
        self.assertEqual(cmds["detector"][2],"/repo/detector.py")
        self.assertEqual(cmds["selector"][2],"/repo/concentration_selector.py")
        joined=" ".join(" ".join(cmd) for cmd in cmds.values())
        for unsafe in ("wallet", "live_executor", "arm-live", "password",
                       "broadcast", "signer", "manual_sale"):
            self.assertNotIn(unsafe,joined)
        self.assertIn("/data/detector.sqlite3",joined)
        self.assertIn("/data/selector.sqlite3",joined)

    def test_summary_does_not_claim_chain_validation_without_actual_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result=shadowSummary(root)
            self.assertEqual(result["evidence"],"NO_CHAIN_DATA")
            self.assertEqual(result["decisions"],{})
            self.assertFalse(result["readOnlyChainValidated"])

    def test_command_line_safely_advertises_read_only_mode(self):
        script=Path(__file__).with_name("run_concentration_shadow.py")
        process=subprocess.run([sys.executable,str(script),"--help"],
            capture_output=True,text=True,timeout=8)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertIn("read-only",process.stdout.lower())
        self.assertNotIn("--arm-live",process.stdout)


if __name__=="__main__":
    unittest.main()
