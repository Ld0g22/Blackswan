"""New-campaign requirements. Fail first against the recovered baseline."""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from campaign_v3 import defaultCampaign, validateCampaign, campaignDigest
from live_executor import ExecutionStore

WALLET = "0x" + "1" * 40


class CampaignVersionThreeTests(unittest.TestCase):
    def valid(self):
        return defaultCampaign(WALLET)

    def test_sacrifice_on_and_eight_distinct_exit_limits_not_required(self):
        cfg = validateCampaign(self.valid())
        self.assertTrue(cfg["sacrifice"]["enabled"])
        self.assertEqual(cfg["sacrifice"]["stakeGbp"], "0.01")
        self.assertEqual(cfg["sacrifice"]["wait"], {"unit": "blocks", "value": 500})
        self.assertEqual(cfg["selector"]["name"], "buyingBlocks")
        self.assertEqual(cfg["main"]["stakeGbp"], "0.10")
        self.assertEqual(cfg["main"]["maxPurchases"], 5)
        self.assertEqual(cfg["exits"], [{"multiple": "3", "percent": "80"}, {"multiple": "5", "percent": "20"}])

    def test_stake_wait_and_custom_exit_ladder(self):
        cfg = self.valid()
        cfg["main"] = {"stakeGbp": "0.35", "maxPurchases": 3}
        cfg["sacrifice"] = {
            "enabled": True, "stakeGbp": "0.02",
            "wait": {"unit": "seconds", "value": 12}, "activityMaxBuyingBlocks": None
        }
        cfg["exits"] = [
            {"multiple": "1.5", "percent": "50"},
            {"multiple": "3", "percent": "25"},
            {"multiple": "50", "percent": "25"},
        ]
        result = validateCampaign(cfg)
        self.assertEqual(Decimal(result["sacrifice"]["stakeGbp"]), Decimal("0.02"))
        self.assertEqual(result["exits"][0]["multiple"], "1.5")
        self.assertEqual(result["exits"][1]["percent"], "25")

    def test_rejects_unknown_fields_overallocated_ladder_and_unbounded_budget(self):
        for modifier in (
            lambda x: x.update(hidden=True),
            lambda x: x["sacrifice"].update(stakeGbp="0"),
            lambda x: x["sacrifice"].update(wait={"unit": "blocks", "value": -1}),
            lambda x: x.update(exits=[{"multiple": "2", "percent": "101"}]),
            lambda x: x.update(exits=[{"multiple": "2", "percent": "80"},
                                      {"multiple": "3", "percent": "25"}]),
            lambda x: x["main"].update(maxPurchases=0),
            lambda x: x["selector"].update(name="../evil.py"),
            lambda x: x.update(chainId=1),
        ):
            cfg = self.valid()
            modifier(cfg)
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                validateCampaign(cfg)

    def test_activity_gate_is_independent_from_the_selector(self):
        cfg = self.valid()
        cfg["selector"]["name"] = "buyingBlocks"
        cfg["sacrifice"]["activityMaxBuyingBlocks"] = None
        self.assertIsNone(validateCampaign(cfg)["sacrifice"]["activityMaxBuyingBlocks"])
        cfg["sacrifice"]["activityMaxBuyingBlocks"] = 7
        self.assertEqual(validateCampaign(cfg)["sacrifice"]["activityMaxBuyingBlocks"], 7)
        cfg["sacrifice"]["wait"] = {"unit": "seconds", "value": 10}
        with self.assertRaises(ValueError):
            validateCampaign(cfg)

    def test_campaign_persisted_identity_rejects_changed_manifest_without_mutating_db(self):
        cfg = validateCampaign(self.valid())
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "execution.sqlite3"
            store = ExecutionStore(db, configuredCampaign=cfg)
            self.assertTrue(store.sacrifice)
            self.assertEqual(store.campaignKind, "CONFIGURED_V3")
            self.assertEqual(store.get("campaignIdentity"), campaignDigest(cfg))
            store.db.close()
            data = db.read_bytes()
            changed = json.loads(json.dumps(cfg))
            changed["exits"][0]["multiple"] = "4"
            with self.assertRaises(RuntimeError):
                ExecutionStore(db, configuredCampaign=validateCampaign(changed))
            self.assertEqual(db.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
