"""Failing-first audit requirements for the recovery build.

These MUST fail on 2026-11-23 main: that demonstrates concrete gaps.
No wallet, network, signed transaction or token balances.
"""
import inspect
import unittest

from campaign_v3 import defaultCampaign, validateCampaign
from live_executor import ExecutionStore
from test_lamb_500_block_gate import TestProbe500BlockGate


class SacrificeRequiredBehavior(unittest.IsolatedAsyncioTestCase):
    def test_new_campaign_defaults_sacrifices_on_without_changing_legacy_database_mode(self):
        # The original non-sacrifice mode must remain compatible for old runs.
        actual = inspect.signature(ExecutionStore.__init__).parameters["sacrifice"].default
        self.assertIs(actual, False)
        self.assertIs(validateCampaign(defaultCampaign("0x" + "1"*40))["sacrifice"]["enabled"], True)

    async def test_transient_rpc_429_during_survival_retries_without_losing_position(self):
        case = TestProbe500BlockGate("test_exact_500_blocks_then_fresh_quote_and_normal_buy_queue")
        await case.asyncSetUp()
        try:
            await case.confirmed()
            case.rpc.head = 510
            good = case.routes.verifiedBuyCandidates

            async def transient(_token, _amount):
                raise RuntimeError("RPC_HTTP_429")

            case.routes.verifiedBuyCandidates = transient
            await case.gate.check(case.selection())
            self.assertEqual(
                case.status(), "PROBE_CONFIRMED",
                "A temporary quote RPC_429 discarded the survivor from the scheduler",
            )
            case.routes.verifiedBuyCandidates = good
            # Simulate the bounded retry time elapsing.
            case.store.db.execute(
                "UPDATE lamb_probes SET lastCheck=lastCheck-4 WHERE selectionId=?",
                (case.selectionId,))
            await case.gate.check(case.selection())
            self.assertEqual(case.status(), "QUEUED")
        finally:
            await case.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
