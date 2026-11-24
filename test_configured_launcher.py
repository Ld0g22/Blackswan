"""Configured launcher must select an engine without shell substitution or spending."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from campaign_v3 import defaultCampaign, concentrationCampaign, validateCampaign
from run_configured_campaign import selectorCommand

WALLET="0x"+"1"*40


class ConfiguredLauncherTests(unittest.TestCase):
    def test_selector_registry_uses_same_detector_and_outbox_contract(self):
        cfg=validateCampaign(defaultCampaign(WALLET))
        cmd=selectorCommand(cfg,Path("/repo"),Path("/venv/python"),Path("/secure.env"),
                            Path("/run/detector.sqlite3"),Path("/run/selector.sqlite3"))
        self.assertEqual(cmd[2],"/repo/five_second_selector.py")
        self.assertIn("--decision-seconds",cmd)
        self.assertEqual(cmd[cmd.index("--db")+1],"/run/selector.sqlite3")
        self.assertEqual(cmd[cmd.index("--detector-db")+1],"/run/detector.sqlite3")

    def test_nonexistent_selector_fails_closed_not_runs_arbitrary_code(self):
        cfg=concentrationCampaign(WALLET)
        command=selectorCommand(cfg,Path("/repo"),Path("/venv/python"),
                            Path("/secure.env"),Path("/run/detector"),Path("/run/selector"))
        self.assertEqual(command[2],"/repo/concentration_selector.py")
        self.assertEqual(command[command.index("--threshold-bps")+1],"2500")
        self.assertEqual(command[command.index("--window-seconds")+1],"30")
        cfg=defaultCampaign(WALLET)
        cfg["selector"]["name"]="integrationOnly"
        cfg["selector"]["maxBuyingBlocks"]=None
        with self.assertRaisesRegex(ValueError,"INTEGRATION_SELECTOR_NOT_LIVE"):
            selectorCommand(validateCampaign(cfg),Path("/repo"),Path("/venv/python"),
                            Path("/secure.env"),Path("/run/detector"),Path("/run/selector"))

    def test_dry_template_generation_does_not_require_wallet(self):
        script=Path(__file__).with_name("run_configured_campaign.py")
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/"campaign.json"
            proc=subprocess.run([sys.executable,str(script),"template",
                                 "--wallet-address",WALLET,"--config",str(config)],
                                capture_output=True,text=True,timeout=10)
            self.assertEqual(proc.returncode,0,proc.stderr)
            saved=validateCampaign(json.loads(config.read_text()))
            self.assertTrue(saved["sacrifice"]["enabled"])
            self.assertEqual(saved["walletPublicAddress"],WALLET)

    def test_concentration_template_is_one_operator_setting_and_preserves_sacrifice(self):
        script=Path(__file__).with_name("run_configured_campaign.py")
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/"concentration.json"
            result=subprocess.run([
                sys.executable,str(script),"template","--wallet-address",WALLET,
                "--selector","concentration","--config",str(output)],
                capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            cfg=validateCampaign(json.loads(output.read_text()))
            self.assertEqual(cfg["selector"]["name"],"concentration")
            self.assertEqual(cfg["selector"]["maxLargestBuyTransactionShareBps"],2500)
            self.assertTrue(cfg["sacrifice"]["enabled"])

    def test_start_requires_explicit_arm_without_creating_campaign(self):
        script=Path(__file__).with_name("run_configured_campaign.py")
        with tempfile.TemporaryDirectory() as tmp:
            conf=Path(tmp)/"input.json"
            conf.write_text(json.dumps(defaultCampaign(WALLET)))
            proc=subprocess.run([sys.executable,str(script),"start","--config",str(conf),
                                 "--data-dir",str(Path(tmp)/"data")],
                                 capture_output=True,text=True,timeout=10)
            self.assertNotEqual(proc.returncode,0)
            self.assertIn("arm-live",proc.stderr)
            self.assertFalse((Path(tmp)/"data").exists())


if __name__=="__main__":
    unittest.main()
