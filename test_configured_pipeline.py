"""Actual Build One journal -> selector -> sacrifice -> main -> full sale test.

Real decode, SQLite outboxes, original executor, position and sales journal.
Only network/keystore/tx signer are deterministic fake dependencies. No money.
"""
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from eth_utils import keccak

import detector
import peak_tracker as peak
from campaign_v3 import defaultCampaign, validateCampaign
from five_second_selector import SelectorStore, eligibleVenues
from live_executor import ExecutionStore, LiveExecutor
from test_detector import abiAddress, logTemplate
from test_sacrificial_integration import Fx, Rpc, Routes, Journal, TOKEN, WALLET, receipt


class ConfiguredFullPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.detectorJournal=detector.EventJournal(root/"detector.sqlite3")
        raw=logTemplate("uniswapV4")
        raw["topics"][2]="0x"+abiAddress(peak.NATIVE)
        raw["topics"][3]="0x"+abiAddress(TOKEN)
        self.event=detector.decodeLog(raw)
        self.detectorJournal.append(self.event)
        self.selectorStore=SelectorStore(root/"selector.sqlite3")
        self.venue=eligibleVenues(self.event)[0]
        self.sid=self.selectorStore.create(self.event,self.venue,time.time())
        self.assertTrue(self.selectorStore.decide(self.sid,"PASS",{"policyName":"buyingBlocks"}))
        cfg=defaultCampaign(WALLET)
        cfg["sacrifice"]["stakeGbp"]="0.02"
        cfg["sacrifice"]["wait"]={"unit":"seconds","value":1}
        self.store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=validateCampaign(cfg))
        self.tx=Journal(self.store)
        self.routes=Routes()
        self.engine=LiveExecutor(self.store,root/"selector.sqlite3",root/"detector.sqlite3",Rpc(),Fx(),self.tx)
        self.engine.swaps=self.routes
        async def noPons(token):return None
        self.engine.ponsOnChain=noPons
        self.engine.readSelections()
        self.balance=110
        async def balance(token):return self.balance
        self.engine.balance=balance

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.db.close()
        self.selectorStore.db.close()
        self.detectorJournal.close()
        self.temp.cleanup()

    def selection(self):
        return self.store.db.execute("SELECT * FROM selections WHERE selectionId=?",(self.sid,)).fetchone()

    def position(self):
        return self.store.db.execute("SELECT * FROM positions WHERE selectionId=?",(self.sid,)).fetchone()

    def confirm(self,kind,qty,buy=True):
        if buy:
            data=receipt(qty)
        else:
            t="0x"+keccak(text="Transfer(address,address,uint256)").hex()
            data={"status":"0x1","blockNumber":"0x17","logs":[{"address":TOKEN,
                "topics":[t,"0x"+WALLET[2:].rjust(64,"0"),"0x"+"0"*64],
                "data":"0x"+f"{qty:064x}"}]}
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt=? WHERE selectionId=? AND kind=?",
            (json.dumps(data),self.sid,kind))

    async def test_configured_sacrifice_deadline_applies_after_quote_before_signing(self):
        self.store.sacrificeDeadlineSeconds=10
        self.store.db.execute(
            "UPDATE selections SET decisionAt=? WHERE selectionId=?",
            (time.time()-5,self.sid))
        await self.engine.lamb.start(self.selection())
        self.assertEqual(self.selection()["status"],"PROBE_PENDING")
        self.assertEqual(len(self.tx.orders),1)

    async def test_temporary_rpc_429_during_sacrifice_can_recover_within_deadline(self):
        old=self.routes.verifiedBuyCandidates
        async def unavailable(token,amount):
            raise RuntimeError("RPC_HTTP_429")
        self.routes.verifiedBuyCandidates=unavailable
        await self.engine.lamb.start(self.selection())
        self.assertEqual(self.selection()["status"],"LAMB_QUEUED")
        self.assertFalse(self.tx.orders)
        self.routes.verifiedBuyCandidates=old
        self.store.set("probeRetryAfter:"+self.sid,"0")
        await self.engine.lamb.start(self.selection())
        self.assertEqual(self.selection()["status"],"PROBE_PENDING")
        self.assertEqual(len(self.tx.orders),1)

    async def test_temporary_rpc_429_during_main_buy_keeps_queued_then_recovers(self):
        self.store.db.execute("UPDATE selections SET status='QUEUED' WHERE selectionId=?",(self.sid,))
        self.store.db.execute("""INSERT INTO lamb_probes(
             selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,passedAt,result)
             VALUES(?,?,?,1,'10','1',?,'PROBE_PASSED')""",
             (self.sid,TOKEN,'0x'+'8'*64,time.time()))
        old=self.routes.verifiedBuyCandidates
        async def unavailable(token,amount):
            raise RuntimeError("RPC_HTTP_429")
        self.routes.verifiedBuyCandidates=unavailable
        await self.engine.buy(self.selection())
        self.assertEqual(self.selection()["status"],"QUEUED")
        self.assertFalse(self.tx.orders)
        self.routes.verifiedBuyCandidates=old
        self.store.set("buyRetryAfter:"+self.sid,"0")
        await self.engine.buy(self.selection())
        self.assertEqual(self.selection()["status"],"BUY_PENDING")
        self.assertEqual(self.tx.orders,[("BUY",100_000_000_000_000)])

    async def test_temporary_missing_survival_sell_route_does_not_discard_survivor(self):
        await self.engine.lamb.start(self.selection())
        self.confirm("PROBE_BUY",10)
        await self.engine.reconcileReceipts()
        self.store.db.execute("UPDATE lamb_probes SET confirmedAt=? WHERE selectionId=?",
                              (time.time()-2,self.sid))
        self.routes.fail=True
        await self.engine.lamb.check(self.selection())
        self.assertEqual(self.selection()["status"],"PROBE_CONFIRMED")
        self.routes.fail=False
        self.store.db.execute("UPDATE lamb_probes SET lastCheck=lastCheck-4 WHERE selectionId=?",
                              (self.sid,))
        await self.engine.lamb.check(self.selection())
        self.assertEqual(self.selection()["status"],"QUEUED")

    async def test_sacrifice_budget_cannot_create_another_signed_intent(self):
        self.store.configuredCampaign["limits"]["maxSacrifices"]=1
        self.store.db.execute("""INSERT INTO transactions(
            intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,inputAmount)
            VALUES('unrelated','other','PROBE_BUY','SENT',?,'0x',0,1,1,'10')""",
            ('0x'+'b'*64,))
        await self.engine.lamb.start(self.selection())
        self.assertEqual(self.selection()["status"],"PROBE_FAILED")
        self.assertEqual(self.tx.orders,[])

    async def test_two_stage_real_receipt_ledger_and_probe_retained(self):
        self.assertEqual(self.selection()["status"],"LAMB_QUEUED")
        await self.engine.lamb.start(self.selection())
        self.assertEqual(self.selection()["status"],"PROBE_PENDING")
        self.assertEqual(self.tx.orders[0],("PROBE_BUY",20_000_000_000_000))
        self.confirm("PROBE_BUY",10)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.selection()["status"],"PROBE_CONFIRMED")
        self.store.db.execute(
            "UPDATE lamb_probes SET confirmedAt=? WHERE selectionId=?",(time.time()-2,self.sid))
        await self.engine.lamb.check(self.selection())
        self.assertEqual(self.selection()["status"],"QUEUED")
        await self.engine.buy(self.selection())
        self.assertEqual(self.tx.orders[1],("BUY",100_000_000_000_000))
        self.confirm("BUY",100)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.selection()["status"],"BOUGHT")
        self.assertEqual(self.position()["originalQty"],"100")
        self.assertEqual(self.store.acquired(),1)
        self.routes.valueGbp=Decimal("0.30")
        await self.engine.checkPosition(self.position())
        await self.engine.prepareSell(self.position())
        self.assertEqual(self.tx.orders[2],("SELL_1",80))
        self.confirm("SELL_1",80,buy=False)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.store.stageRow(self.sid,1)["sold"],1)
        self.balance=30
        self.routes.valueGbp=Decimal("0.50")
        await self.engine.checkPosition(self.position())
        await self.engine.prepareSell(self.position())
        self.assertEqual(self.tx.orders[3],("SELL_2",20))
        self.confirm("SELL_2",20,buy=False)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.store.stageRow(self.sid,2)["sold"],1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0],2)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0],2)
        self.balance=10 # Probe tokens remain; no fake sale proceeds claimed.
        self.assertEqual(self.engine.lamb.probeQuantity(self.sid),10)


if __name__=="__main__":
    unittest.main()
