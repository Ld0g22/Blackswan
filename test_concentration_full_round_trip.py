"""Real concentration decision -> existing sacrifice/buy/sell journal integration.

This reuses every adversarial real-executor test against an actual concentration
selector decision, not a manually inserted PASS. RPC/signer remain deterministic
controlled fakes. No wallet or live chain.
"""
import json
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
from swap_adapters import calldata, abiTools, V4_ROUTER
from entry_timing import targetKey


class CanonicalRpc(Rpc):
    def __init__(self,windowEnd):
        super().__init__()
        self.windowEnd=windowEnd
        self.canonical=None
        self.head=21

    async def call(self,method,params):
        if method=='eth_blockNumber':return hex(self.head)
        if method=='eth_getTransactionReceipt':
            assert self.canonical and params[0]==self.canonical['transactionHash']
            return self.canonical
        if method=='eth_getBlockByNumber':
            number=int(params[0],16)
            assert number==21
            return {'number':hex(number),'hash':'0x'+'ab'*32,
                    'timestamp':hex(self.windowEnd)}
        raise AssertionError('Unexpected JSON RPC '+method)


class CanonicalRoutes(Routes):
    async def verifiedBuyCandidates(self,token,wei):
        quotes=await super().verifiedBuyCandidates(token,wei)
        for q in quotes:q.update(block=hex(21),preBuySellBlock=hex(21))
        return quotes

    async def directQuotes(self,token,qty,buy,block=None):
        quotes=await super().directQuotes(token,qty,buy,block)
        for q in quotes:q['block']=block or hex(21)
        return quotes

    def build(self,quote,amount,wallet):
        if 'preBuySellOutput' in quote:
            return dict(to=V4_ROUTER,value=amount,
                        data=calldata('execute(bytes,bytes[],uint256)',
                        ['bytes','bytes[]','uint256'],[bytes([16]),[b'fixture'],int(time.time())+120]))
        return super().build(quote,amount,wallet)


class CanonicalJournal(Journal):
    def __init__(self,store):
        super().__init__(store)
        self.deadlines={}

    async def create(self,intent,selection,kind,transaction,amount=0,*,deadlineAt=None):
        if kind in ('BUY','PROBE_BUY'):
            assert deadlineAt is not None and time.time()<=deadlineAt
            _,decode,_=abiTools()
            _,_,encoded=decode(['bytes','bytes[]','uint256'],bytes.fromhex(transaction['data'][10:]))
            assert encoded==int(deadlineAt)
            self.deadlines[kind]=deadlineAt
        else:
            assert deadlineAt is None
        return await super().create(intent,selection,kind,transaction,amount)


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
        self.windowEnd=int(time.time())-1
        self.concentration.wallTime=lambda:self.windowEnd
        self.selectorStore=self.concentration.store
        self.concentration.pull()
        self.concentration.rpc=FakeRpc([
            swap(self.venue,1,1000,0,25),
            swap(self.venue,2,1001,0,25),
            swap(self.venue,3,1002,0,25),
            swap(self.venue,4,1002,1,25),
        ],{
            1000:self.windowEnd-30,1001:self.windowEnd-20,
            1002:self.windowEnd-10,1003:self.windowEnd
        })
        await self.concentration.scanOnce(self.sid)
        self.assertEqual(self.selectorStore.db.execute(
            "SELECT status FROM decisions").fetchone()[0],"PASS")
        config=concentrationCampaign(WALLET)
        config["sacrifice"]["stakeGbp"]="0.02"
        config["sacrifice"]["wait"]={"unit":"seconds","value":1}
        self.store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=config)
        self.tx=CanonicalJournal(self.store)
        self.routes=CanonicalRoutes()
        self.rpc=CanonicalRpc(self.windowEnd)
        self.engine=LiveExecutor(self.store,selectorPath,detectorPath,self.rpc,Fx(),self.tx)
        self.engine.swaps=self.routes
        async def noPons(token):return None
        self.engine.ponsOnChain=noPons
        self.engine.readSelections()
        self.balance=110
        async def balance(token):return self.balance
        self.engine.balance=balance

    def confirm(self,kind,qty,buy=True):
        super().confirm(kind,qty,buy)
        if kind=='PROBE_BUY':
            row=self.store.db.execute("SELECT txHash,receipt FROM transactions WHERE selectionId=? AND kind=?",
                                      (self.sid,kind)).fetchone()
            data=json.loads(row['receipt'])
            data.update(transactionHash=row['txHash'],blockHash='0x'+'ab'*32)
            self.rpc.canonical=dict(data)
            self.store.db.execute("UPDATE transactions SET receipt=? WHERE selectionId=? AND kind=?",
                                  (json.dumps(data),self.sid,kind))

    async def test_temporary_rpc_429_during_main_buy_keeps_queued_then_recovers(self):
        # Unlike the legacy synthetic QUEUED fixture, exercise the actual
        # canonical probe -> survival -> main anchor handoff.
        await self.engine.lamb.start(self.selection())
        self.confirm('PROBE_BUY',10)
        await self.engine.reconcileReceipts()
        await self.engine.lamb.check(self.selection())
        self.assertEqual(self.selection()['status'],'QUEUED')
        anchor=self.windowEnd+1
        self.assertEqual(self.engine.lamb.readyAt(self.sid),anchor)
        old=self.routes.verifiedBuyCandidates
        async def unavailable(token,amount):raise RuntimeError('RPC_HTTP_429')
        self.routes.verifiedBuyCandidates=unavailable
        await self.engine.buy(self.selection())
        self.assertEqual(self.selection()['status'],'QUEUED')
        self.assertEqual(len(self.tx.orders),1)
        self.routes.verifiedBuyCandidates=old
        self.store.set('buyRetryAfter:'+self.sid,'0')
        await self.engine.buy(self.selection())
        self.assertEqual(self.selection()['status'],'BUY_PENDING')
        self.assertEqual(self.tx.orders[1],('BUY',100_000_000_000_000))
        self.assertEqual(self.tx.deadlines['BUY'],anchor+self.store.entryDeadlineSeconds)
        self.assertEqual(self.tx.deadlines['PROBE_BUY'],self.windowEnd+self.store.sacrificeDeadlineSeconds)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.concentration.detector.close()
