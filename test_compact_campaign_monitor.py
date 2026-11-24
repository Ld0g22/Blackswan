"""Tests for the read-only, campaign-wide operator overview."""
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from rich.console import Console
from compact_campaign_monitor import readTotals, overview

class CompactMonitorTests(unittest.TestCase):
    def test_counts_all_decisions_and_reasons_not_recent_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with sqlite3.connect(root/"detector.sqlite3") as db:
                db.execute("CREATE TABLE event_log(source TEXT)")
                db.executemany("INSERT INTO event_log VALUES(?)", [("uniswapV4",)]*150+[("uniswapV3",)])
            with sqlite3.connect(root/"selector.sqlite3") as db:
                db.execute("CREATE TABLE decisions(status TEXT,data TEXT)")
                db.execute("CREATE TABLE launches(state TEXT)")
                db.executemany("INSERT INTO decisions VALUES(?,?)",
                    [("FAIL",'{"reason":"MAX_TRANSACTION_SHARE_EXCEEDED"}')]*140+
                    [("PASS",'{"reason":"MAX_TRANSACTION_SHARE_WITHIN_LIMIT"}')]*5+
                    [("UNKNOWN",'{"reason":"NO_FIRST_TRADE"}')])
                db.executemany("INSERT INTO launches VALUES(?)",[("PENDING",)]*3)
            with sqlite3.connect(root/"execution.sqlite3") as db:
                db.execute("CREATE TABLE selections(status TEXT,reason TEXT)")
                db.execute("CREATE TABLE excluded_passes(reason TEXT)")
                db.execute("CREATE TABLE lamb_probes(result TEXT)")
                db.executemany("INSERT INTO selections VALUES(?,?)",
                    [("SKIPPED_UNSUPPORTED","EXPERIMENT_BANNED_TOKEN_SUFFIX_7777"),
                     ("SKIPPED_UNSUPPORTED","BANNED_V4_RUG_TEMPLATE_1PCT_200_ZERO_HOOK"),
                     ("PROBE_FAILED","MISSED_1P_DEADLINE")])
                db.execute("INSERT INTO excluded_passes VALUES('V3_LAUNCH_EXCLUDED')")
                db.execute("INSERT INTO lamb_probes VALUES('PROBE_PASSED')")
            result=readTotals(root)
            self.assertEqual(result["detector"],{"uniswapV3":1,"uniswapV4":150})
            self.assertEqual(result["decisions"],{"FAIL":140,"PASS":5,"UNKNOWN":1})
            self.assertEqual(result["pending"],3)
            self.assertIn(("FAIL","MAX_TRANSACTION_SHARE_EXCEEDED",140),result["selectorReasons"])
            self.assertIn(("SKIPPED_UNSUPPORTED","EXPERIMENT_BANNED_TOKEN_SUFFIX_7777",1),result["executionReasons"])
            self.assertIn(("EXCLUDED","V3_LAUNCH_EXCLUDED",1),result["executionReasons"])
            self.assertEqual(result["probes"],{"PROBE_PASSED":1})
            self.assertEqual(result["errors"],[])
    def test_missing_journals_unknown_and_no_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            result=readTotals(root)
            self.assertIsNone(result["detector"])
            self.assertIsNone(result["decisions"])
            self.assertIsNone(result["executionReasons"])
            self.assertEqual(list(root.iterdir()),[])
    def test_overview_shows_all_five_full_token_addresses_and_totals(self):
        tokens=["0x"+str(i)*40 for i in range(1,6)]
        view={"directory":"/tmp/configured_v3_test","now":1000,"mode":"CAMPAIGN",
              "health":{},"counts":{"positions":5,"open":5},"finance":{},
              "policy":{"main":{"stakeGbp":"0.05","maxPurchases":5}},
              "holdings":[{"token":token,"isOpen":True,"originalQty":100,
                           "remainingMainQty":100,"latestFullSizeGbp":"0.10",
                           "latestFullSizeMultiple":"2","peakFullSizeGbp":"0.15",
                           "quoteAgeSeconds":1,"exitSlices":[]} for token in tokens],
              "sacrificesDetailed":[],"opportunities":[],"warnings":[],"alerts":[],
              "totals":{"detector":{"uniswapV4":150},"decisions":{"PASS":5,"FAIL":140},
                        "pending":3,"selectorReasons":[("FAIL","MAX_TRANSACTION_SHARE_EXCEEDED",140)],
                        "executionReasons":[],"probes":{},"errors":[]}}
        stream=io.StringIO()
        Console(file=stream,width=160,force_terminal=False).print(overview(view,160))
        text=stream.getvalue()
        for token in tokens:self.assertIn(token,text)
        for label in ("150","140","PASS","FAIL","MAX_TRANSACTION_SHARE_EXCEEDED","READ ONLY"):
            self.assertIn(label,text)
