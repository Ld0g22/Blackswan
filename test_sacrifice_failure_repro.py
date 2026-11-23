"""Failing-first audit requirements for the recovery build.

These MUST fail on 2026-11-23 main: that demonstrates concrete gaps.
No wallet, network, signed transaction or token balances.
"""
import inspect
import unittest

from live_executor import ExecutionStore
from test_lamb_500_block_gate import TestProbe500BlockGate


class SacrificeRequiredBehavior(unittest.IsolatedAsyncioTestCase):
    def test_sacrifice_is_on_in_default_execution_store(self):
        actual = inspect.signature(ExecutionStore.__init__).parameters["sacrifice"].default
        self.assertIs(actual, True, "Sacrifice is OFF by default in the restored build")

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
            await case.gate.check(case.selection())
            self.assertEqual(case.status(), "QUEUED")
        finally:
            await case.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
