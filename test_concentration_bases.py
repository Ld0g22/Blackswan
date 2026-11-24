"""Concentration selector uses exact V4 signed-delta base units for all 3 assets."""
from pathlib import Path
import json
import tempfile
import unittest

import detector
import peak_tracker as peak
from concentration_selector import ConcentrationSelector
from five_second_selector import eligibleVenues
from test_concentration_selector import FakeRpc,swap
from test_detector import abiAddress,logTemplate
from test_sacrificial_integration import TOKEN


class ConcentrationBaseAssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_eth_weth_usdg_and_both_token_orderings(self):
        for base in (peak.NATIVE,peak.WETH,peak.USDG):
            for reversedPool in (False,True):
                with self.subTest(base=base,reversedPool=reversedPool):
                    with tempfile.TemporaryDirectory() as tmp:
                        det=Path(tmp)/"detector.sqlite3"
                        selected=Path(tmp)/"selector.sqlite3"
                        journal=detector.EventJournal(det)
                        raw=logTemplate("uniswapV4")
                        t0,t1=((TOKEN,base) if reversedPool else (base,TOKEN))
                        raw["topics"][2]="0x"+abiAddress(t0)
                        raw["topics"][3]="0x"+abiAddress(t1)
                        event=detector.decodeLog(raw)
                        journal.append(event)
                        venue=eligibleVenues(event)[0]
                        self.assertEqual(venue["baseAsset"],base)
                        selector=ConcentrationSelector(det,selected,
                            "https://not-used.invalid","wss://not-used.invalid")
                        selector.wallTime=lambda:100030
                        selector.pull()
                        selector.rpc=FakeRpc([
                            swap(venue,1,1000,0,25),
                            swap(venue,2,1001,0,25),
                            swap(venue,3,1002,0,25),
                            swap(venue,4,1002,1,25),
                        ],{1000:100000,1001:100010,1002:100020,1003:100030})
                        key=event["eventKey"]+":"+venue["venueKey"]
                        try:
                            await selector.scanOnce(key)
                            row=selector.store.db.execute(
                                "SELECT status,data FROM decisions").fetchone()
                            self.assertEqual(row["status"],"PASS")
                            data=json.loads(row["data"])
                            self.assertEqual(data["totalBuyBaseRaw"],"100")
                            self.assertEqual(data["largestBuyTransactionBaseRaw"],"25")
                        finally:
                            selector.detector.close()
                            selector.store.db.close()
                            journal.close()


if __name__=="__main__":
    unittest.main()
