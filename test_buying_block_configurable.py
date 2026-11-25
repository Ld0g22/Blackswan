"""Buying-block policy contract: no RPC, signing or real money."""
import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_v3 import defaultCampaign, validateCampaign, campaignDigest
from five_second_selector import Selector, SelectorStore, Candidate, decisionStatus, eligibleVenues
from detector import EventJournal, decodeLog
from test_detector import logTemplate
from test_concentration_selector import swap
from live_executor import ExecutionStore, LiveExecutor
from live_dashboard import snapshot, card_buying_blocks
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
                self.assertFalse(selectorReady(path,"buyingBlocks",startedAt=time.time()+1))
                self.assertFalse(selectorReady(path,"buyingBlocks",expectedPid=-1))
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


class BuyingBlockHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def exerciseWindow(self,window,minimum,maximum,expected,interrupted=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            detPath=root/"detector.sqlite3"
            selPath=root/"selector.sqlite3"
            journal=EventJournal(detPath)
            event=decodeLog(logTemplate("uniswapV4"))
            journal.append(event)
            venue=eligibleVenues(event)[0]
            sel=Selector(detPath,selPath,"https://unused.invalid","wss://unused.invalid",
                         decisionSeconds=window,minBuyingBlocks=minimum,maxBuyingBlocks=maximum)
            anchor=int(time.time())
            clock=[anchor+1.0]
            logs=[swap(venue,1,1000,0,10),swap(venue,2,1000,1,20),
                  swap(venue,3,1001,0,10),swap(venue,4,1002,0,-10)]
            calls=[]
            async def rpc(method,params,timeout=4):
                calls.append(method)
                if method=="eth_getBlockByHash":
                    return {"hash":event["blockHash"],"timestamp":hex(anchor)}
                if method=="eth_blockNumber": return hex(1002)
                if method=="eth_getLogs":
                    self.assertEqual(params[0]["fromBlock"],hex(1000))
                    self.assertEqual(params[0]["toBlock"],hex(1002))
                    return logs
                raise AssertionError(method)
            async def subscribe(candidate):
                candidate.subscriptionStarted=True
            async def sleep(seconds):
                self.assertEqual(sel.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],0)
                candidate=next(iter(sel.candidates.values()))
                self.assertEqual(candidate.deadline,anchor+window)
                self.assertEqual(seconds,window-1)
                clock[0]=candidate.deadline
                sel.acceptLog(candidate,swap(venue,5,1003,0,10),clock[0],"live")
                sel.acceptLog(candidate,swap(venue,5,1003,0,10),clock[0],"duplicate")
                wrong=swap(venue,6,1004,0,10)
                wrong["topics"][1]="0x"+"ef"*32
                sel.acceptLog(candidate,wrong,clock[0],"otherPool")
                sel.acceptLog(candidate,swap(venue,7,1005,0,10),clock[0]+.01,"late")
                candidate.subscriptionInterrupted=interrupted
            sel.rpc=rpc
            sel.subscribe=subscribe
            try:
                with (patch("five_second_selector.time.time",side_effect=lambda:clock[0]),
                      patch("five_second_selector.asyncio.sleep",side_effect=sleep)):
                    sel.pull()
                    self.assertEqual(len(sel.tasks),1)
                    await asyncio.gather(*list(sel.tasks))
                row=sel.store.db.execute("SELECT status,data FROM decisions").fetchone()
                evidence=json.loads(row["data"])
                self.assertEqual(row["status"],expected)
                self.assertEqual(evidence["buyBlockCount"],3)
                self.assertEqual(evidence["decisionSeconds"],window)
                self.assertEqual(evidence["minBuyingBlocks"],minimum)
                self.assertEqual(evidence["maxBuyingBlocks"],maximum)
                self.assertEqual(evidence["coverageComplete"],not interrupted)
                self.assertEqual(calls,["eth_getBlockByHash","eth_blockNumber","eth_getLogs"])
                view=snapshot(root)
                self.assertEqual(view["selector"]["recent"][0]["count"],3)
                self.assertEqual(view["selector"]["recent"][0]["token"],venue["tokenAddress"])
                self.assertIsNotNone(card_buying_blocks(view))
                cfg=defaultCampaign(WALLET)
                cfg["selector"].update(decisionSeconds=window,minBuyingBlocks=minimum,maxBuyingBlocks=maximum)
                store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=validateCampaign(cfg))
                engine=object.__new__(LiveExecutor)
                engine.store=store
                engine.selector=sqlite3.connect(selPath)
                engine.detector=sqlite3.connect(detPath)
                engine.selector.row_factory=engine.detector.row_factory=sqlite3.Row
                try:
                    engine.readSelections()
                    engine.readSelections()
                    rows=store.db.execute("SELECT status FROM selections").fetchall()
                    self.assertEqual([r[0] for r in rows],["LAMB_QUEUED"] if expected=="PASS" else [])
                finally:
                    engine.selector.close()
                    engine.detector.close()
                    store.db.close()
            finally:
                sel.detector.close()
                sel.store.db.close()
                journal.close()

    async def test_historical_and_initial_window(self):
        await self.exerciseWindow(20,3,10,"PASS")
        await self.exerciseWindow(30,3,10,"PASS")

    async def test_custom_range_and_incomplete_coverage(self):
        await self.exerciseWindow(45,4,8,"FAIL")
        await self.exerciseWindow(30,3,10,"UNKNOWN",interrupted=True)

    async def test_malformed_activity_never_creates_false_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            detector=root/"detector.sqlite3"
            with sqlite3.connect(detector): pass
            sel=Selector(detector,root/"selector.sqlite3","https://unused.invalid","wss://unused.invalid",
                         decisionSeconds=30,minBuyingBlocks=3,maxBuyingBlocks=10)
            event=decodeLog(logTemplate("uniswapV4"))
            venue=eligibleVenues(event)[0]
            candidate=Candidate("key",event,venue)
            try:
                sel.acceptLog(candidate,{"blockNumber":"malformed"},time.time(),"live")
                sel.acceptLog(candidate,None,time.time(),"live")
                self.assertEqual(candidate.errors,["invalidActivityEvent","invalidActivityEvent"])
                self.assertEqual(decisionStatus(3,True,True,False,candidate.errors,
                                                maxBuyingBlocks=10,minBuyingBlocks=3),"UNKNOWN")
            finally:
                sel.detector.close()
                sel.store.db.close()

    async def test_wrong_chain_never_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            detector=root/"detector.sqlite3"
            with sqlite3.connect(detector): pass
            path=root/"selector.sqlite3"
            sel=Selector(detector,path,"https://unused.invalid","wss://unused.invalid",
                         decisionSeconds=30,minBuyingBlocks=3,maxBuyingBlocks=10)
            async def wrongChain(method,params,timeout=4):return "0x1"
            sel.rpc=wrongChain
            try:
                with self.assertRaisesRegex(RuntimeError,"Wrong chain ID"):
                    await sel.run()
                self.assertFalse(selectorReady(path,"buyingBlocks"))
            finally:
                sel.detector.close()
                sel.store.db.close()

if __name__=="__main__": unittest.main()
