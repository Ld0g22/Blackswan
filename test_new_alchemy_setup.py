"""New Alchemy 4663 bootstrap guarantees explicit credentials provenance.

No network access or private key is used by these tests.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.setup_new_alchemy import (parseCredentials,requireSameNewCredentials,
                                     prepareEnvFile,validateAlchemyUrls)

HOST="robinhood-mainnet.g.alchemy.com"
def http(token):return "https://"+HOST+"/v2/"+token
def wss(token):return "wss://"+HOST+"/v2/"+token


class NewAlchemyCredentialTests(unittest.TestCase):
    def test_two_plain_endpoints_from_newalchemy_text(self):
        h,w=parseCredentials(http("brandnewKey") + "\n" + wss("brandnewKey") + "\n")
        self.assertEqual((h,w),(http("brandnewKey"),wss("brandnewKey")))

    def test_json_newalchemy_supported(self):
        source=json.dumps({"httpUrl":http("newKey"),"websocketUrl":wss("newKey")})
        self.assertEqual(parseCredentials(source),(http("newKey"),wss("newKey")))

    def test_env_newalchemy_supported(self):
        source=f"ROBINHOOD_ALCHEMY_HTTP_URL={http('K123')}\nROBINHOOD_ALCHEMY_WSS_URL={wss('K123')}\n"
        self.assertEqual(parseCredentials(source),(http("K123"),wss("K123")))

    def test_key_only_supported_without_logging_secret(self):
        self.assertEqual(parseCredentials("ALCHEMY_API_KEY=New0123\n"),
                         (http("New0123"),wss("New0123")))

    def test_actual_prior_source_shape_key_and_http_only_derives_wss(self):
        # 2026-11-20: newalchemy.txt contained API key AND HTTP endpoint.
        # The working old installer derived WS rather than requiring it.
        src="New Alchemy API key: realNewKey123\\nHTTP endpoint: "+http("realNewKey123")+"\\n"
        self.assertEqual(parseCredentials(src),
                         (http("realNewKey123"),wss("realNewKey123")))

    def test_only_http_endpoint_derives_wss(self):
        self.assertEqual(parseCredentials("HTTP endpoint: "+http("onlyHttp")+"\\n"),
                         (http("onlyHttp"),wss("onlyHttp")))

    def test_key_and_http_mismatch_rejected(self):
        src="API key: NEWKEY\\nHTTP: "+http("DIFFERENT")+"\\n"
        with self.assertRaisesRegex(ValueError,"MISMATCH"):
            parseCredentials(src)

    def test_source_file_with_http_only_still_matches_created_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"newalchemy.txt"
            source.write_text("API key: NEWKEY\\nHTTP: "+http("NEWKEY")+"\\n")
            env=Path(tmp)/"protected.env"
            prepareEnvFile(env,http("NEWKEY"),wss("NEWKEY"))
            self.assertTrue(requireSameNewCredentials(env,source))

    def test_mismatched_http_wss_keys_rejected(self):
        with self.assertRaisesRegex(ValueError,"MISMATCH"):
            validateAlchemyUrls(http("newKey"),wss("oldKey"))

    def test_other_provider_or_wrong_chain_endpoint_rejected(self):
        for invalid in ("https://eth-mainnet.g.alchemy.com/v2/something",
                        "http://"+HOST+"/v2/123",
                        "https://attacker.invalid/v2/123",
                        "https://"+HOST+"/v2",
                        "https://"+HOST+"/v2/key?token=another"):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                validateAlchemyUrls(invalid,wss("something"))

    def test_runtime_refuses_old_env_even_when_valid_alchemy_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"newalchemy.txt"
            source.write_text(http("newKey")+"\n"+wss("newKey")+"\n")
            env=Path(tmp)/".env"
            env.write_text(f"ROBINHOOD_ALCHEMY_HTTP_URL={http('OLD')}\n"
                           f"ROBINHOOD_ALCHEMY_WSS_URL={wss('OLD')}\n")
            with self.assertRaisesRegex(RuntimeError,"MISMATCH"):
                requireSameNewCredentials(env,source)
            env.write_text(f"ROBINHOOD_ALCHEMY_HTTP_URL={http('newKey')}\n"
                           f"ROBINHOOD_ALCHEMY_WSS_URL={wss('newKey')}\n")
            self.assertTrue(requireSameNewCredentials(env,source))

    def test_env_file_secure_nonoverwrite_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=Path(tmp)/"secret"/"new.env"
            contents=f"ROBINHOOD_ALCHEMY_HTTP_URL={http('newKey')}\nROBINHOOD_ALCHEMY_WSS_URL={wss('newKey')}\n"
            prepareEnvFile(env,http("newKey"),wss("newKey"))
            self.assertEqual(env.stat().st_mode & 0o777,0o600)
            self.assertEqual(env.read_text(),contents)
            prepareEnvFile(env,http("newKey"),wss("newKey"))
            with self.assertRaisesRegex(RuntimeError,"EXISTS"):
                prepareEnvFile(env,http("different"),wss("different"))
            self.assertEqual(env.read_text(),contents)

    def test_read_only_runner_refuses_implicit_legacy_env(self):
        script=Path(__file__).with_name("run_concentration_shadow.py")
        p=subprocess.run([sys.executable,str(script),"run","--duration-seconds","30"],
                         text=True,capture_output=True,timeout=10)
        self.assertNotEqual(p.returncode,0)
        self.assertIn("supply --env",p.stderr.lower())
        self.assertNotIn("blackswanRH_guarded",p.stderr)


if __name__=="__main__":
    unittest.main()
