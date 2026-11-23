"""Policy wiring checks: exact new flex campaign identity, strict old-ledger refusal.

No third-party modules, live RPC or wallet material required.
"""
import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest

from campaign_config import (
    FLEX_KIND,FLEX_EXIT,FLEX_TRANCHES,FLEX_POLICY,
    flexManifest,verifyFlexLedger,
)

WALLET="0x"+"1"*40

def validManifest():
    return {
        "chainId":4663,"mode":"REAL_MONEY","campaignKind":FLEX_KIND,
        "configVersion":2,"entryPolicy":"uniswapV4_nonPons_only",
        "quoteEngine":"original_peak_tracker_Tracker_best",
        "exitPolicy":FLEX_EXIT,"sellTranches":FLEX_TRANCHES,
        "probeGbp":"0.01","survivalBlocks":500,
        "probeMaxBuyingBlocks":7,
        "selectorDecisionSeconds":20,"selectorMaxBuyingBlocks":10,
        "gbpEach":"0.20","targetPurchases":3,
        "walletPublicAddress":WALLET,
    }

def launcherGate():
    """Execute the real helper's AST without importing the privileged launcher."""
    path=Path(__file__).with_name("run_live_campaign.py")
    tree=ast.parse(path.read_text(),filename=str(path))
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
            and n.name=="campaignSurvivalBlocks")
    module=ast.Module(body=[fn],type_ignores=[])
    ns={}
    exec(compile(module,str(path),"exec"),ns)
    return ns["campaignSurvivalBlocks"]


class TestCampaignPolicy(unittest.TestCase):
    def test_new_flex_manifest_frozen_at_seven(self):
        self.assertEqual(flexManifest(validManifest(),expectedWallet=WALLET),
                         ("0.20",3,WALLET))

    def test_missing_wrong_and_legacy_rules_cannot_start(self):
        for bad in (None,6,8,True,7.0,"7"):
            manifest=validManifest()
            if bad is None:
                manifest.pop("probeMaxBuyingBlocks")
            else:
                manifest["probeMaxBuyingBlocks"]=bad
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,"FLEX_CAMPAIGN_POLICY_MISMATCH"):
                    flexManifest(manifest)
        manifest=validManifest()
        manifest["configVersion"]=1
        with self.assertRaisesRegex(ValueError,"FLEX_CAMPAIGN_POLICY_MISMATCH"):
            flexManifest(manifest)

    def test_launcher_500_block_helper_rejects_unversioned_policy(self):
        helper=launcherGate()
        manifest=validManifest()
        self.assertEqual(helper(manifest,True),500)
        self.assertIsNone(helper(manifest,False))
        for bad in (None,8,6,"7",7.0,True):
            old=dict(manifest)
            if bad is None:old.pop("probeMaxBuyingBlocks")
            else:old["probeMaxBuyingBlocks"]=bad
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(RuntimeError,"INVALID_CAMPAIGN_SURVIVAL_POLICY"):
                    helper(old,True)

    def test_existing_ledger_requires_exact_policy_without_writing(self):
        values={"campaignVersion":"2","campaignKind":FLEX_KIND,
                "buyGbp":"0.20","maxPurchases":"3",
                "walletPublicAddress":WALLET,
                "probeGbp":"0.01","waitBlocks":"500",
                "probeMaxBuyingBlocks":"7",
                "exitPolicy":FLEX_POLICY,"flexConfigVersion":"1"}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"execution.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE meta(key TEXT,value TEXT)")
                db.executemany("INSERT INTO meta VALUES (?,?)",values.items())
            self.assertTrue(verifyFlexLedger(path,"0.20",3,WALLET,expectedProbeMaxBuyingBlocks=7))
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM meta WHERE key='probeMaxBuyingBlocks'")
            before=path.read_bytes()
            with self.assertRaisesRegex(RuntimeError,"FLEX_LEDGER_CONFIG_MISMATCH"):
                verifyFlexLedger(path,"0.20",3,WALLET,expectedProbeMaxBuyingBlocks=7)
            self.assertEqual(path.read_bytes(),before)

    def test_fresh_launcher_preserves_six_500_block_modes(self):
        src=Path(__file__).with_name("run_live_campaign.py").read_text()
        self.assertEqual(src.count("survivalBlocks=500,probeMaxBuyingBlocks=7,"),6)


if __name__=="__main__":
    unittest.main()
