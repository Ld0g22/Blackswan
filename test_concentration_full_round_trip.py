"""Real concentration decision -> existing sacrifice/buy/sell journal integration.

This reuses every adversarial real-executor test against an actual concentration
selector decision, not a manually inserted PASS. RPC/signer remain deterministic
controlled fakes. No wallet or live chain.
"""
from decimal import Decimal
from pathlib import Path
import tempfile
import time

import detector
import peak_tracker as peak
from campaign_v3 import concentrationCampaign
from concentration_selector import ConcentrationSelector
from five_second_selector import eligibleVenues
from live_executor import ExecutionStore, LiveExecutor
from test_concentration_selector import FakeRpc, swap
from test_configured_pipeline import ConfiguredFullPipelineTests
from test_detector import abiAddress,logTemplate
from test_sacrificial_integration import Fx,Rpc,Routes,Journal,TOKEN,WALLET


class ConcentrationFullRoundTripTests(ConfiguredFullPipelineTests):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        detectorPath=root/"detector.sqlite3"
        selectorPath=root/"selector.sqlite3"
        self.detectorJournal=detector.EventJournal(detectorPath)
        raw=logTemplate("uniswapV4")
        raw["topics"][2]="0x"+abiAddress(peak.NATIVE)
        raw["topics"][3]="0x"+abiAddress(TOKEN)
        self.event=detector.decodeLog(raw)
        self.detectorJournal.append(self.event)
        self.venue=eligibleVenues(self.event)[0]
        self.sid=self.event["eventKey"]+":"+self.venue["venueKey"]
        self.concentration=ConcentrationSelector(
            detectorPath,selectorPath,
            "https://not-used.invalid","wss://not-used.invalid")
        self.concentration.wallTime=lambda:100030
        self.selectorStore=self.concentration.store
        self.concentration.pull()
        self.concentration.rpc=FakeRpc([
            swap(self.venue,1,1000,0,25),
            swap(self.venue,2,1001,0,25),
            swap(self.venue,3,1002,0,25),
            swap(self.venue,4,1002,1,25),
        ],{
            1000:100000,1001:100010,1002:100020,1003:100030
        })
        await self.concentration.scanOnce(self.sid)
        self.assertEqual(self.selectorStore.db.execute(
            "SELECT status FROM decisions").fetchone()[0],"PASS")
        config=concentrationCampaign(WALLET)
        config["sacrifice"]["stakeGbp"]="0.02"
        config["sacrifice"]["wait"]={"unit":"seconds","value":1}
        self.store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=config)
        self.tx=Journal(self.store)
        self.routes=Routes()
        self.engine=LiveExecutor(self.store,selectorPath,detectorPath,Rpc(),Fx(),self.tx)
        self.engine.swaps=self.routes
        async def noPons(token):return None
        self.engine.ponsOnChain=noPons
        self.engine.readSelections()
        self.balance=110
        async def balance(token):return self.balance
        self.engine.balance=balance

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.concentration.detector.close()
