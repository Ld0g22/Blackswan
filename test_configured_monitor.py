"""Live monitor must display v3 receipt-truth and arbitrary exit stages."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from campaign_v3 import defaultCampaign, validateCampaign
from live_executor import ExecutionStore
from live_dashboard import snapshot,card_positions,card_lamb

WALLET="0x"+"1"*40
TOKEN="0x"+"a"*40


class ConfiguredMonitorTests(unittest.TestCase):
    def test_configured_sacrifice_and_slice_labels_not_legacy_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            cfg=defaultCampaign(WALLET)
            cfg["exits"]=[
                {"multiple":"1.5","percent":"50"},
                {"multiple":"50","percent":"25"},
                {"multiple":"100","percent":"25"},
            ]
            store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=validateCampaign(cfg))
            store.db.execute("""INSERT INTO selections(selectionId,token,decisionAt,event,status)
                 VALUES('s',?,0,'{}','BOUGHT')""",(TOKEN,))
            store.db.execute("""INSERT INTO positions(
                 selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                 VALUES('s',?,'100','1',1,1,1)""",(TOKEN,))
            store.seedExitStages("s")
            store.db.execute("UPDATE exit_slices SET triggered=1 WHERE selectionId='s' AND stage=1")
            store.db.close()
            view=snapshot(root)
            self.assertTrue(view["summary"]["configured"])
            self.assertTrue(view["summary"]["sacrifice"])
            self.assertEqual([x["multiple"] for x in view["positions"][0]["exitSlices"]],["1.5","50","100"])
            self.assertEqual(view["summary"]["open"],1)
            self.assertIsNotNone(card_positions(view))
            self.assertIsNotNone(card_lamb(view))

    def test_manual_reserve_remains_an_open_holding_after_all_auto_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            cfg=defaultCampaign(WALLET)
            cfg["exits"]=[
                {"multiple":"2","percent":"80"},
                {"multiple":"10","percent":"15"},
            ]
            store=ExecutionStore(root/"execution.sqlite3",configuredCampaign=validateCampaign(cfg))
            store.db.execute("""INSERT INTO selections(selectionId,token,decisionAt,event,status)
                VALUES('reserve',?,1,'{}','BOUGHT')""",(TOKEN,))
            store.db.execute("""INSERT INTO positions(
                selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                VALUES('reserve',?,'100','1',1,1,1)""",(TOKEN,))
            store.seedExitStages("reserve")
            store.db.execute("""UPDATE exit_slices SET sold=1,triggered=1,
                soldQty=CASE stage WHEN 1 THEN '80' ELSE '15' END
                WHERE selectionId='reserve'""")
            self.assertEqual(store.status()["openPositions"],1)
            store.db.close()
            monitor=snapshot(root)
            # 5 unsold main tokens belong to the user, not to the sacrifice.
            self.assertEqual(monitor["summary"]["open"],1)


if __name__=="__main__":
    unittest.main()
