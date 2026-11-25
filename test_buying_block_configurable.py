"""Buying-block policy contract: no RPC, signing or real money."""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from campaign_v3 import defaultCampaign, validateCampaign, campaignDigest
from five_second_selector import Selector, SelectorStore, decisionStatus
from run_configured_campaign import selectorCommand, selectorReady

WALLET = "0x" + "1" * 40

class BuyingBlockConfigTests(unittest.TestCase):
    def test_defaults_and_custom_config(self):
        cfg = defaultCampaign(WALLET)
        self.assertEqual(cfg["selector"], {"name":"buyingBlocks","decisionSeconds":30,
                          "minBuyingBlocks":3,"maxBuyingBlocks":10})
        for window, lo, hi in ((1,0,0),(5,1,5),(20,3,10),(45,5,15),(60,7,7),(300,1,1000)):
            with self.subTest(window=window, lo=lo, hi=hi):
                cfg["selector"].update(decisionSeconds=window,minBuyingBlocks=lo,maxBuyingBlocks=hi)
                self.assertEqual(validateCampaign(cfg)["selector"], cfg["selector"])
                command=selectorCommand(validateCampaign(cfg),Path("/repo"),Path("/python"),
                                        Path("/env"),Path("/detector"),Path("/selector"))
                for arg, value in (("--decision-seconds",window),("--min-buying-blocks",lo),
                                   ("--max-buying-blocks",hi)):
                    self.assertEqual(command[command.index(arg)+1],str(value))

    def test_invalid_config(self):
        for patch in ({"decisionSeconds":0},{"decisionSeconds":301},{"decisionSeconds":True},
                      {"decisionSeconds":30.0},{"minBuyingBlocks":-1},{"maxBuyingBlocks":2},
                      {"minBuyingBlocks":True},{"maxBuyingBlocks":10.0},{"maxBuyingBlocks":None}):
            cfg=defaultCampaign(WALLET)
            cfg["selector"].update(patch)
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                validateCampaign(cfg)

    def test_legacy_manifest_digest_unchanged(self):
        cfg=defaultCampaign(WALLET)
        cfg["selector"]={"name":"buyingBlocks","decisionSeconds":20,"maxBuyingBlocks":10}
        normalized=validateCampaign(cfg)
        self.assertNotIn("minBuyingBlocks",normalized["selector"])
        digest=hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(",",":"),
                                        ensure_ascii=True).encode("ascii")).hexdigest()
        self.assertEqual(campaignDigest(cfg),digest)
        cmd=selectorCommand(normalized,Path("/repo"),Path("/python"),Path("/env"),
                            Path("/detector"),Path("/selector"))
        self.assertEqual(cmd[cmd.index("--min-buying-blocks")+1],"3")

    def test_boundaries_complete_and_incomplete(self):
        for lo,hi in ((3,10),(5,15),(7,7),(0,0)):
            for n in range(17):
                with self.subTest(lo=lo,hi=hi,n=n):
                    expected="PASS" if lo<=n<=hi else "FAIL"
                    self.assertEqual(decisionStatus(n,True,True,False,[],maxBuyingBlocks=hi,
                                                   minBuyingBlocks=lo),expected)
                    expected="FAIL" if n>hi else "UNKNOWN"
                    self.assertEqual(decisionStatus(n,False,True,False,[],maxBuyingBlocks=hi,
                                                   minBuyingBlocks=lo),expected)
                    self.assertEqual(decisionStatus(n,True,True,True,[],maxBuyingBlocks=hi,
                                                   minBuyingBlocks=lo),expected)
                    self.assertEqual(decisionStatus(n,True,True,False,["bad"],maxBuyingBlocks=hi,
                                                   minBuyingBlocks=lo),expected)

    def test_deduplication_and_inclusive_availability_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SelectorStore(Path(tmp)/"selector.sqlite3")
            try:
                for block, tx, index, when in (("0xa","0x1",0,10),("0xa","0x2",1,11),
                                                ("0xb","0x3",0,30),("0xc","0x4",0,30.01)):
                    log={"blockHash":block,"transactionHash":tx,"logIndex":hex(index)}
                    store.observeBuy("k",log,when,"test")
                    store.observeBuy("k",log,when,"duplicate")
                self.assertEqual(store.buyingBlocks("k",30),["0xa","0xb"])
            finally: store.db.close()

    def test_persisted_policy_and_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            detector=root/"detector.sqlite3"
            with sqlite3.connect(detector): pass
            path=root/"selector.sqlite3"
            def make(**kw):
                return Selector(detector,path,"http://unused","ws://unused",
                                **dict({"decisionSeconds":30,"minBuyingBlocks":3,
                                        "maxBuyingBlocks":10},**kw))
            sel=make()
            try:
                self.assertFalse(selectorReady(path,"buyingBlocks"))
                sel.setReady(True)
                self.assertTrue(selectorReady(path,"buyingBlocks"))
                self.assertFalse(selectorReady(path,"concentration"))
                sel.store.db.execute("UPDATE meta SET value=? WHERE key='selectorHeartbeat'",
                                     (str(time.time()-11),))
                self.assertFalse(selectorReady(path,"buyingBlocks"))
            finally:
                sel.detector.close()
                sel.store.db.close()
            for patch in ({"decisionSeconds":31},{"minBuyingBlocks":4},{"maxBuyingBlocks":11}):
                with self.subTest(patch=patch),self.assertRaises(RuntimeError): make(**patch)
            sel=make()
            try: self.assertFalse(selectorReady(path,"buyingBlocks"))
            finally:
                sel.detector.close()
                sel.store.db.close()

if __name__=="__main__": unittest.main()
