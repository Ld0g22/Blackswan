"""V3 manual reserve must be a real supervised executor path, not a config label."""
import json
from pathlib import Path
import tempfile
import unittest

from campaign_v3 import defaultCampaign, validateCampaign
from live_executor import ExecutionStore
from manual_sale import connect, remainingMain

WALLET="0x"+"1"*40
TOKEN="0x"+"a"*40


class ConfiguredManualReserveTests(unittest.TestCase):
    def test_five_percent_remains_for_manual_selling_after_auto_stage_slices(self):
        cfg=defaultCampaign(WALLET)
        cfg["exits"]=[{"multiple":"2","percent":"80"},
                      {"multiple":"10","percent":"15"}]
        configured=validateCampaign(cfg)
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/"campaign.json").write_text(json.dumps(configured))
            store=ExecutionStore(directory/"execution.sqlite3",configuredCampaign=configured)
            store.db.execute("""INSERT INTO selections(selectionId,token,decisionAt,event,status)
                VALUES('s',?,1,'{}','BOUGHT')""",(TOKEN,))
            store.db.execute("""INSERT INTO positions(
                 selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                 VALUES('s',?,'100','1',1,1,1)""",(TOKEN,))
            store.seedExitStages("s")
            store.db.execute("""UPDATE exit_slices SET triggered=1,sold=1,soldQty='80'
                WHERE selectionId='s' AND stage=1""")
            store.db.execute("""UPDATE exit_slices SET triggered=1,sold=1,soldQty='15'
                WHERE selectionId='s' AND stage=2""")
            self.assertEqual(store.get("manualExitVersion"),"1")
            store.db.close()
            db,meta=connect(directory)
            try:
                position=db.execute("SELECT * FROM positions WHERE selectionId='s'").fetchone()
                self.assertEqual(remainingMain(db,position),5)
            finally:
                db.close()

    def test_explicit_reserve_does_not_count_as_closed_exposure(self):
        cfg=defaultCampaign(WALLET)
        cfg["exits"]=[{"multiple":"3","percent":"80"},
                      {"multiple":"10","percent":"15"}]
        configured=validateCampaign(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            store=ExecutionStore(Path(tmp)/"execution.sqlite3",configuredCampaign=configured)
            store.db.execute("""INSERT INTO selections(selectionId,token,decisionAt,event,status)
                VALUES('s',?,1,'{}','BOUGHT')""",(TOKEN,))
            store.db.execute("""INSERT INTO positions(
                selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                VALUES('s',?,'100','1',1,1,1)""",(TOKEN,))
            store.seedExitStages("s")
            store.db.execute("UPDATE exit_slices SET sold=1 WHERE selectionId='s'")
            self.assertEqual(store.status()["openPositions"],1)
            store.db.close()


if __name__ == "__main__":
    unittest.main()
