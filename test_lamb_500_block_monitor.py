"""The 500-block sacrificial gate is displayed accurately, without RPC or wallet."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from rich.console import Console
from live_executor import ExecutionStore
from live_dashboard import snapshot,render


class TestFiveHundredBlockMonitor(unittest.TestCase):
    def test_block_countdown_and_legacy_clock_do_not_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            store=ExecutionStore(root/'execution.sqlite3',
                                 sacrifice=True,probeWaitBlocks=500)
            token='0x'+'a'*40
            store.db.execute("""INSERT INTO selections
                (selectionId,token,decisionAt,event,status)
                VALUES('fivehundred',?,100,'{}','PROBE_CONFIRMED')""",(token,))
            tx='0x'+'1'*64
            receipt=json.dumps({'blockNumber':'0x64'})
            store.db.execute("""INSERT INTO transactions(
                intentId,selectionId,kind,state,txHash,raw,nonce,
                createdAt,lastBroadcast,inputAmount,receipt)
                VALUES('probe','fivehundred','PROBE_BUY','CONFIRMED',?,'0x',0,100,100,'100',?)""",
                (tx,receipt))
            store.db.execute("""INSERT INTO lamb_probes(
                selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
                VALUES('fivehundred',?,?,100,'10','100','PROBE_CONFIRMED')""",
                (token,tx))
            store.db.close()
            view=snapshot(root,now=200)
            self.assertEqual(view['summary']['probeWaitBlocks'],500)
            self.assertEqual(view['probes'][0]['receiptBlock'],100)
            # Without detector head, read-only monitor never invents ETA.
            output=self.render(view)
            self.assertIn('HEAD UNKNOWN',output)
            self.assertNotIn('CHECK DUE',output)
            # Stale detector head is labeled as such; execution uses live RPC.
            view['detector']={'head':'599'}
            self.assertIn('1 blocks left (detector)',self.render(view))
            view['detector']={'head':'600'}
            self.assertIn('CHECK DUE (detector head)',self.render(view))

    @staticmethod
    def render(view):
        output=io.StringIO()
        console=Console(width=180,color_system=None,force_terminal=False,file=output)
        console.print(render(view,180))
        return output.getvalue()


if __name__=='__main__':
    unittest.main()
