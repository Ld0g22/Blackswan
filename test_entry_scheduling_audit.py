"""Test the actual collector without real funds or provider calls."""
import unittest
from tools.audit_entry_scheduling import selfTest, atom, safeReason, Probe

class TimingAuditTests(unittest.TestCase):
    def test_fixture(self):
        selfTest()
    def test_secret_url_omitted(self):
        self.assertIsNone(atom("https://secret.example/key"))
        self.assertEqual(safeReason("RPC_HTTP_429 https://secret.example/key"),"RPC_HTTP_429")
    def test_rpc_allowlist_is_read_only(self):
        self.assertNotIn("eth_sendRawTransaction",Probe.ALLOWED)
        self.assertNotIn("eth_sendTransaction",Probe.ALLOWED)
        self.assertNotIn("eth_sign",Probe.ALLOWED)
