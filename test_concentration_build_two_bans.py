"""Concentration Build Two entry exclusions; no RPC or wallet required."""
import unittest
from types import SimpleNamespace
from live_executor import LiveExecutor

ZERO = "0x" + "0"*40
TOKEN = "0x" + "a"*40
BANNED = "0x" + "a"*36 + "7777"

class ConcentrationEntryBanTests(unittest.TestCase):
    def reason(self, token=TOKEN, fee=3000, spacing=60, hooks=ZERO,
               sacrifice=True, selector="concentration"):
        store = SimpleNamespace(sacrifice=sacrifice, flexLamb=False,
                                configuredCampaign={"selector":{"name":selector}})
        owner = SimpleNamespace(store=store, ponsOrigin=lambda token: False)
        item = {"event":{"source":"uniswapV4"},
                "venue":{"source":"uniswapV4","fee":fee,"spacing":spacing,"hooks":hooks}}
        return LiveExecutor.entryExclusion(owner, token, item)

    def test_suffix_blocked_with_and_without_sacrifice(self):
        for sacrifice in (True, False):
            with self.subTest(sacrifice=sacrifice):
                self.assertEqual(self.reason(token=BANNED.upper(), sacrifice=sacrifice),
                                 "EXPERIMENT_BANNED_TOKEN_SUFFIX_7777")

    def test_exact_rug_template_blocked_with_and_without_sacrifice(self):
        for sacrifice in (True, False):
            with self.subTest(sacrifice=sacrifice):
                self.assertEqual(self.reason(fee=10000, spacing=200, sacrifice=sacrifice),
                                 "BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK")

    def test_near_matches_remain_eligible(self):
        for kwargs in ({"fee":10000,"spacing":199},
                       {"fee":9999,"spacing":200},
                       {"fee":10000,"spacing":200,"hooks":"0x"+"1"*40},
                       {"token":"0x"+"7777"+"a"*36}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(self.reason(**kwargs))

    def test_buying_blocks_unchanged(self):
        self.assertIsNone(self.reason(token=BANNED, selector="buyingBlocks"))
        self.assertIsNone(self.reason(fee=10000, spacing=200, sacrifice=False,
                                      selector="buyingBlocks"))
        self.assertEqual(self.reason(fee=10000, spacing=200, selector="buyingBlocks"),
                         "BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK")
