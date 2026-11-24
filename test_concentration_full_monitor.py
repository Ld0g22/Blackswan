"""Executable acceptance tests for the independent full-screen concentration monitor."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from campaign_v3 import concentrationCampaign
from detector import EventJournal,decodeLog
from five_second_selector import SelectorStore,eligibleVenues
from live_executor import ExecutionStore
from test_detector import logTemplate

from monitor_snapshot import readSnapshot
from monitor_view import renderScreen,renderReport
from concentration_monitor import chooseRunDirectory,nextNewRun

from rich.console import Console

WALLET="0x"+"1"*40
TX="0x"+"d"*64


class MonitorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.cfg=concentrationCampaign(WALLET)
        (self.root/"campaign.json").write_text(json.dumps(self.cfg))
        self.exe=ExecutionStore(self.root/"execution.sqlite3",configuredCampaign=self.cfg)
        self.det=EventJournal(self.root/"detector.sqlite3")
        self.event=decodeLog(logTemplate("uniswapV4"))
        self.det.append(self.event)
        self.venue=eligibleVenues(self.event)[0]
        self.key=self.event["eventKey"]+":"+self.venue["venueKey"]
        self.token=self.venue["tokenKey"].split(":")[-1]
        self.sel=SelectorStore(self.root/"selector.sqlite3")
        self.sel.create(self.event,self.venue,time.time())
        self.sel.decide(self.key,"PASS",{
            "policyName":"concentration","policyVersion":1,
            "coverageComplete":True,"reason":"MAX_TRANSACTION_SHARE_WITHIN_LIMIT",
            "firstTradeAt":100000,"windowSeconds":30,
            "windowEndAtExclusive":100030,
            "totalBuyBaseRaw":"1000","largestBuyTransactionBaseRaw":"250",
            "largestBuyTransactionHash":TX,
            "buyTransactionCount":4,"observedSwapCount":6,"decisionLagSeconds":2.5
        })
        self.exe.db.execute(
            "INSERT INTO selections(selectionId,token,decisionAt,event,status) VALUES(?,?,?,?,?)",
            (self.key,self.token,time.time(),json.dumps({"venue":self.venue}),"LAMB_QUEUED"))

    def tearDown(self):
        self.sel.db.close()
        self.det.close()
        self.exe.db.close()
        self.tmp.cleanup()

    def test_reads_full_selector_evidence_and_real_handoff(self):
        view=readSnapshot(self.root)
        row=view["opportunities"][0]
        self.assertEqual(row["token"],self.token)
        self.assertEqual(row["venue"],self.venue["id"])
        self.assertEqual(row["largestSharePct"],"25.000")
        self.assertEqual(row["buyTransactionCount"],4)
        self.assertEqual(row["executionStatus"],"LAMB_QUEUED")
        self.assertEqual(row["whyNotBought"],"LAMB_QUEUED")
        self.assertTrue(row["coverageComplete"])
        self.assertEqual(view["policy"]["selector"]["name"],"concentration")
        self.assertEqual(view["policy"]["sacrifice"]["wait"]["value"],500)

    def test_readonly_no_ledger_mutation(self):
        before={p.name:p.stat().st_size for p in self.root.glob("*.sqlite3")}
        a=readSnapshot(self.root)
        b=readSnapshot(self.root)
        self.assertEqual(before,{p.name:p.stat().st_size for p in self.root.glob("*.sqlite3")})
        self.assertEqual(a["opportunities"][0]["selectionId"],b["opportunities"][0]["selectionId"])
        self.assertFalse((self.root/"monitor.sqlite3").exists())

    def test_empty_directory_and_shadow_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            empty=readSnapshot(folder)
            self.assertEqual(empty["positions"],[])
            self.assertFalse(empty["hasExecution"])
            self.assertFalse(empty["hasCampaign"])
            self.assertTrue(any("execution" in x.lower() for x in empty["warnings"]))
            self.assertEqual(list(folder.iterdir()),[])

            (folder/"selector.sqlite3").write_bytes((self.root/"selector.sqlite3").read_bytes())
            # This is an opened-on-disk snapshot; avoid using WAL as an assumed
            # proof of all rows. The reader must never migrate/open for write.
            shallow=readSnapshot(folder)
            self.assertFalse(shallow["hasExecution"])

    def test_latest_quote_not_peak_and_missing_quote_not_zero(self):
        self.exe.db.execute("""INSERT INTO positions(
            selectionId,token,originalQty,spentWei,boughtAt,bestGbp,lastGrowth,nextDue,lastCheck)
            VALUES(?,?,?,'1',100,'25',100,101,101)""",
            (self.key,self.token,"100"))
        self.exe.seedExitStages(self.key)
        self.exe.observe(token=self.token,phase="SELL",at=101,block="0x20",
            venue_id=self.venue["id"],status="ONCHAIN_QUOTE_ONLY",
            quantity="100",out_wei="10",gbp="1.25",multiple="25",basis="EXECUTABLE",reason=None)
        self.exe.observe(token=self.token,phase="SELL",at=102,block="0x21",
            venue_id=None,status="UNAVAILABLE",
            quantity="100",out_wei=None,gbp=None,multiple=None,basis=None,reason="RPC_HTTP_429")
        view=readSnapshot(self.root,now=103)
        p=view["holdings"][0]
        self.assertEqual(p["peakFullSizeGbp"],"25")
        self.assertIsNone(p["latestFullSizeGbp"])
        self.assertEqual(p["lastSuccessfulFullSizeGbp"],"1.25")
        self.assertEqual(p["quoteError"],"RPC_HTTP_429")
        self.assertEqual(p["remainingMainQty"],100)
        self.assertIsNone(p["remainingLiquidationValueGbp"])

    def test_quieter_position_quote_not_lost_behind_other_tokens_history(self):
        self.exe.db.execute("""INSERT INTO positions(
            selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,?,'1',100,100,100)""",(self.key,self.token,"100"))
        self.exe.db.execute(
            "UPDATE selections SET quoteWei='80' WHERE selectionId=?",(self.key,))
        self.exe.observe(token=self.token,phase="SELL",at=10,block="0x10",
            venue_id=self.venue["id"],status="ONCHAIN_QUOTE_ONLY",
            quantity="100",out_wei="100",gbp="1.23",multiple="12.3",
            basis="EXECUTABLE",reason=None)
        # A hot competing pool can have thousands of newer observations
        # while this position still needs its most recent quote displayed.
        rows=[("0x"+"f"*40,"SELL",i+11,"UNAVAILABLE",None)
              for i in range(2002)]
        self.exe.db.executemany(
            "INSERT INTO observations(token,phase,at,status,reason) VALUES(?,?,?,?,?)",
            rows)
        view=readSnapshot(self.root,now=3000,limit=1)
        holding=view["holdings"][0]
        self.assertEqual(holding["latestFullSizeGbp"],"1.23")
        self.assertEqual(holding["entryQuotedTokenQtyRaw"],"80")
        self.assertEqual(holding["originalQty"],100)
        self.assertTrue(holding["quoteStale"])

    def test_sold_receipt_and_manual_reserve_remain_open(self):
        self.exe.db.execute("""INSERT INTO positions(
            selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,?,'1',100,100,100)""",(self.key,self.token,"100"))
        self.exe.seedExitStages(self.key)
        self.exe.db.execute(
            "UPDATE exit_slices SET triggered=1,sold=1,soldQty=CASE stage WHEN 1 THEN '80' ELSE '15' END WHERE selectionId=?",
            (self.key,))
        for stage,qty in ((1,80),(2,15)):
            txhash="0x"+str(stage)*64
            self.exe.db.execute(
                "INSERT INTO sales(selectionId,stage,txHash,tokenDebited,wethReceivedWei,confirmedAt) VALUES(?,?,?,?,?,?)",
                (self.key,stage,txhash,str(qty),"1000000000000000",time.time()))
        view=readSnapshot(self.root)
        p=view["holdings"][0]
        self.assertEqual(p["remainingMainQty"],5)
        self.assertTrue(p["isOpen"])
        self.assertEqual([x["state"] for x in p["exitSlices"]],["SOLD","SOLD"])
        self.assertEqual(p["exitSlices"][0]["txHash"],"0x"+"1"*64)
        self.assertIsNone(view["finance"]["actualRealisedGbp"]) # No qualified FX receipt
        self.assertEqual(view["finance"]["verifiedReceivedWethWei"],2000000000000000)

    def test_missing_sale_receipt_does_not_report_sold(self):
        self.exe.db.execute("""INSERT INTO positions(
            selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,?,'1',100,100,100)""",(self.key,self.token,"100"))
        self.exe.seedExitStages(self.key)
        self.exe.db.execute(
            "UPDATE exit_slices SET triggered=1,sold=1,soldQty='80' WHERE selectionId=? AND stage=1",
            (self.key,))
        view=readSnapshot(self.root)
        self.assertEqual(view["holdings"][0]["exitSlices"][0]["state"],"UNVERIFIED")
        self.assertTrue(any("sale" in x.lower() for x in view["warnings"]))

    def test_missed_lamb_deadline_surfaces_as_high_priority_alert(self):
        self.exe.db.execute(
            "UPDATE selections SET status='PROBE_FAILED',reason='MISSED_1P_DEADLINE' WHERE selectionId=?",
            (self.key,))
        result=readSnapshot(self.root)
        self.assertTrue(any("MISSED_1P_DEADLINE" in item["message"]
                            for item in result["alerts"]))
        self.assertEqual(result["opportunities"][0]["executionStatus"],"PROBE_FAILED")

    def test_buying_blocks_config_renders_its_policy_not_fake_concentration(self):
        self.cfg["selector"]={"name":"buyingBlocks","decisionSeconds":20,"maxBuyingBlocks":10}
        (self.root/"campaign.json").write_text(json.dumps(self.cfg))
        output=io.StringIO()
        Console(file=output,force_terminal=False,width=200).print(
            renderReport(readSnapshot(self.root),width=200))
        self.assertIn("buyingBlocks",output.getvalue())
        self.assertIn("20",output.getvalue())
        self.assertNotIn("≤0% largest tx",output.getvalue())

    def test_closed_intake_with_active_exit_watcher_is_not_stale_selector_alert(self):
        self.exe.set("intakeState","CLOSED")
        self.exe.set("workerState","ACTIVE")
        self.sel.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorReady','1')")
        self.sel.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorHeartbeat',?)",
            (str(time.time()-200),))
        view=readSnapshot(self.root)
        self.assertEqual(view["health"]["selectorStatus"],"INTAKE_CLOSED")
        self.assertFalse(any("heartbeat stale" in a["message"].lower()
                             for a in view["alerts"]))

    def test_all_position_counts_independent_of_screen_rows_limit(self):
        for i in range(4):
            key="extra:"+str(i)
            token="0x"+f"{i+5:040x}"
            self.exe.db.execute(
                "INSERT INTO selections(selectionId,token,decisionAt,event,status)"
                " VALUES(?,?,?,?,?)",(key,token,float(i),"{}","BOUGHT"))
            self.exe.db.execute(
                "INSERT INTO positions(selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)"
                " VALUES(?,?,?,?,?,?,?)",(key,token,"100","100",float(i),float(i),float(i)))
            self.exe.seedExitStages(key)
        view=readSnapshot(self.root,limit=1)
        self.assertEqual(view["counts"]["positions"],4)
        self.assertEqual(view["counts"]["open"],4)
        self.assertEqual(len(view["holdings"]),4)

    def test_nominal_budget_counts_all_intents_not_only_visible_recent_txs(self):
        for i in range(4):
            kind="BUY" if i<2 else "PROBE_BUY"
            sid="entry:"+str(i)
            self.exe.db.execute("""INSERT INTO transactions(
                intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,inputAmount)
                VALUES(?,?,?,'SENT',?,'0x',?,?,?,'100')""",
                (sid,sid,kind,"0x"+f"{i+1:064x}",i,float(i),float(i)))
        view=readSnapshot(self.root,limit=1)
        self.assertEqual(view["counts"]["sacrificesCommitted"],2)
        self.assertEqual(view["counts"]["mainCommitted"],2)
        self.assertEqual(view["counts"]["nominalCommittedGbp"],"0.22")

    def test_reorg_revocation_marks_selected_row(self):
        self.sel.revoke(self.key,"POST_DECISION_CHAIN_REORG")
        row=readSnapshot(self.root)["opportunities"][0]
        self.assertEqual(row["revokeReason"],"POST_DECISION_CHAIN_REORG")
        self.assertIn("REVOKED",row["whyNotBought"])

    def test_shadow_policy_comes_from_actual_decision_even_without_manifest(self):
        (self.root/"campaign.json").unlink()
        view=readSnapshot(self.root)
        output=io.StringIO()
        Console(file=output,force_terminal=False,width=200).print(
            renderReport(view,width=200))
        self.assertIn("SELECTOR concentration",output.getvalue())
        self.assertIn("CHAIN",output.getvalue())

    def test_empty_folder_does_not_claim_unverified_chain_as_4663(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=io.StringIO()
            Console(file=output,force_terminal=False,width=160).print(
                renderReport(readSnapshot(Path(tmp)),width=160))
            self.assertIn("CHAIN UNVERIFIED",output.getvalue())
            self.assertNotIn("CHAIN 4663",output.getvalue())

    def test_poor_quality_database_read_returns_warning_and_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/"execution.sqlite3").write_bytes(b"not sqlite")
            result=readSnapshot(p)
            self.assertTrue(result["warnings"])
            self.assertEqual((p/"execution.sqlite3").read_bytes(),b"not sqlite")


class MonitorViewTests(unittest.TestCase):
    setUp=MonitorSnapshotTests.setUp
    tearDown=MonitorSnapshotTests.tearDown
    def draw(self,view,screen,width=220,height=70,offset=0):
        stream=io.StringIO()
        c=Console(file=stream,force_terminal=False,color_system=None,
                  width=width,height=height)
        c.print(renderScreen(view,screen=screen,width=width,height=height,offset=offset))
        return stream.getvalue()

    def test_all_pages_render_complete_identifiers(self):
        view=readSnapshot(self.root)
        for page in ("overview","decisions","positions","ledger","alerts"):
            with self.subTest(page=page):
                output=self.draw(view,page,width=250)
                self.assertIn("READ ONLY",output)
                if page in ("overview","decisions"):
                    self.assertIn(self.token,output)
                    self.assertIn(self.venue["id"],output)
        self.assertIn(TX,self.draw(view,"decisions"))

    def test_narrow_view_keeps_identifier_without_ellipsis(self):
        view=readSnapshot(self.root)
        output=self.draw(view,"decisions",width=92,height=48)
        # A 66-hex pool ID may fold across lines but not be shortened.
        self.assertEqual(self.venue["id"].replace("0x","")[:12],self.venue["id"][2:14])
        self.assertNotIn(self.venue["id"][:8]+"…",output)
        self.assertNotIn(self.token[:8]+"…",output)

    def test_scrolling_long_position_screen_reveals_hidden_exit_details(self):
        self.exe.db.execute("""INSERT INTO positions(
            selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,?,'1',100,100,100)""",(self.key,self.token,"100"))
        self.exe.seedExitStages(self.key)
        view=readSnapshot(self.root)
        above=self.draw(view,"positions",width=130,height=29,offset=0)
        below=self.draw(view,"positions",width=130,height=29,offset=48)
        self.assertNotEqual(above,below)
        self.assertIn("READ ONLY",below)
        self.assertIn("SCROLL",below)

    def test_full_report_all_pages_and_no_false_cash_profit(self):
        view=readSnapshot(self.root)
        report=renderReport(view,width=240)
        stream=io.StringIO()
        Console(file=stream,force_terminal=False,width=240).print(report)
        out=stream.getvalue()
        for title in ("DECISIONS","POSITIONS","SACRIFICE","TRANSACTIONS","ALERTS"):
            self.assertIn(title,out)
        self.assertIn(self.venue["id"],out)
        self.assertIn(self.token,out)
        self.assertIn("UNMEASURED",out)


from test_configured_pipeline import ConfiguredFullPipelineTests


class RealLifecycleMonitorTests(unittest.IsolatedAsyncioTestCase):
    """Use the REAL existing executor state machine, mocked only at signer/RPC."""

    asyncSetUp=ConfiguredFullPipelineTests.asyncSetUp
    asyncTearDown=ConfiguredFullPipelineTests.asyncTearDown
    selection=ConfiguredFullPipelineTests.selection
    position=ConfiguredFullPipelineTests.position
    confirm=ConfiguredFullPipelineTests.confirm

    async def test_sacrifice_buy_and_both_receipt_sales_are_visible(self):
        root=Path(self.temp.name)
        (root/"campaign.json").write_text(json.dumps(self.store.configuredCampaign))
        self.assertEqual(readSnapshot(root)["opportunities"][0]["executionStatus"],
                         "LAMB_QUEUED")
        await self.engine.lamb.start(self.selection())
        a=readSnapshot(root)
        self.assertEqual(a["opportunities"][0]["executionStatus"],"PROBE_PENDING")
        self.assertEqual(a["transactions"][0]["kind"],"PROBE_BUY")
        self.assertEqual(len(a["sacrificesDetailed"]),1)
        self.assertEqual(a["sacrificesDetailed"][0]["status"],"PROBE_PENDING")
        self.assertEqual(a["sacrificesDetailed"][0]["txHash"],a["transactions"][0]["txHash"])
        report=io.StringIO()
        Console(file=report,force_terminal=False,width=210).print(renderReport(a))
        self.assertIn("SACRIFICE TX",report.getvalue())
        self.assertIn(a["transactions"][0]["txHash"],report.getvalue())
        self.confirm("PROBE_BUY",10)
        await self.engine.reconcileReceipts()
        b=readSnapshot(root)
        self.assertEqual(b["sacrifices"][0]["result"],"PROBE_CONFIRMED")
        self.assertEqual(b["sacrificesDetailed"][0]["result"],"PROBE_CONFIRMED")
        self.assertEqual(b["sacrificesDetailed"][0]["receiptBlock"],21)
        self.store.db.execute(
            "UPDATE lamb_probes SET confirmedAt=? WHERE selectionId=?",
            (time.time()-2,self.sid))
        await self.engine.lamb.check(self.selection())
        await self.engine.buy(self.selection())
        self.confirm("BUY",100)
        await self.engine.reconcileReceipts()
        c=readSnapshot(root)
        self.assertEqual(c["holdings"][0]["originalQty"],100)
        self.assertEqual(c["holdings"][0]["remainingMainQty"],100)
        self.assertEqual(c["holdings"][0]["mainBuyTx"]["state"],"CONFIRMED")
        self.routes.valueGbp=Decimal("0.30")
        await self.engine.checkPosition(self.position())
        await self.engine.prepareSell(self.position())
        self.confirm("SELL_1",80,buy=False)
        await self.engine.reconcileReceipts()
        d=readSnapshot(root)
        self.assertEqual(d["holdings"][0]["remainingMainQty"],20)
        self.assertEqual(d["holdings"][0]["exitSlices"][0]["state"],"SOLD")
        self.assertIsNotNone(d["holdings"][0]["latestFullSizeGbp"])
        self.balance=30
        self.routes.valueGbp=Decimal("0.50")
        await self.engine.checkPosition(self.position())
        await self.engine.prepareSell(self.position())
        self.confirm("SELL_2",20,buy=False)
        await self.engine.reconcileReceipts()
        e=readSnapshot(root)
        self.assertEqual(e["holdings"][0]["remainingMainQty"],0)
        self.assertFalse(e["holdings"][0]["isOpen"])
        self.assertEqual([row["state"] for row in e["holdings"][0]["exitSlices"]],
                         ["SOLD","SOLD"])
        self.assertEqual(e["counts"]["confirmedSales"],2)
        self.assertEqual(e["sacrifices"][0]["probeQty"],"10")
        self.assertEqual(e["finance"]["saleCount"],2)
        self.assertIsNone(e["finance"]["actualRealisedGbp"])


class MonitorCLITests(unittest.TestCase):
    def test_follow_new_campaign_when_no_directory_exists_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/"data"
            self.assertIsNone(nextNewRun(parent,set()))
            parent.mkdir()
            old=parent/"configured_v3_prior"
            old.mkdir()
            seen={old.resolve()}
            self.assertIsNone(nextNewRun(parent,seen))
            (parent/"concentration_shadow_older").mkdir()
            self.assertIsNone(nextNewRun(parent,seen))
            fresh=parent/"configured_v3_new"
            fresh.mkdir()
            self.assertEqual(nextNewRun(parent,seen),fresh.resolve())

    def test_monitor_has_follow_new_option_but_no_wallet_or_trade_options(self):
        script=Path(__file__).with_name("concentration_monitor.py")
        proc=subprocess.run([sys.executable,str(script),"--help"],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn("--follow-new",proc.stdout)
        self.assertNotIn("--wallet-file",proc.stdout)
        self.assertNotIn("--arm-live",proc.stdout)

    def test_explicit_folder_and_absent_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            self.assertEqual(chooseRunDirectory(folder,None),folder.resolve())
            with self.assertRaisesRegex(ValueError,"RUN_DIRECTORY"):
                chooseRunDirectory(folder/"missing",None)
            self.assertEqual(list(folder.iterdir()),[])

    def test_once_no_secret_wallet_network_or_db_writes(self):
        script=Path(__file__).with_name("concentration_monitor.py")
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            p=subprocess.run([sys.executable,str(script),"--run-dir",str(folder),
                "--once","--view","overview"],capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertIn("READ ONLY",p.stdout)
            self.assertEqual(list(folder.iterdir()),[])

    def test_rejects_refresh_out_of_bounds(self):
        script=Path(__file__).with_name("concentration_monitor.py")
        p=subprocess.run([sys.executable,str(script),"--run-dir","/tmp",
            "--once","--interval","0.01"],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(p.returncode,0)
        self.assertIn("interval",p.stderr.lower())


if __name__=="__main__":
    unittest.main()
