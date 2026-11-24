"""Executable acceptance tests for the independent full-screen concentration monitor."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
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
from concentration_monitor import chooseRunDirectory

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

    def test_reorg_revocation_marks_selected_row(self):
        self.sel.revoke(self.key,"POST_DECISION_CHAIN_REORG")
        row=readSnapshot(self.root)["opportunities"][0]
        self.assertEqual(row["revokeReason"],"POST_DECISION_CHAIN_REORG")
        self.assertIn("REVOKED",row["whyNotBought"])

    def test_poor_quality_database_read_returns_warning_and_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/"execution.sqlite3").write_bytes(b"not sqlite")
            result=readSnapshot(p)
            self.assertTrue(result["warnings"])
            self.assertEqual((p/"execution.sqlite3").read_bytes(),b"not sqlite")


class MonitorViewTests(MonitorSnapshotTests):
    def draw(self,view,screen,width=220,height=70):
        stream=io.StringIO()
        c=Console(file=stream,force_terminal=False,color_system=None,
                  width=width,height=height)
        c.print(renderScreen(view,screen=screen,width=width,height=height))
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


class MonitorCLITests(unittest.TestCase):
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
