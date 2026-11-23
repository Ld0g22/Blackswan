"""Sell retry pacing tests; no network or signed transactions."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from live_executor import ExecutionStore,LiveExecutor


class TestSellRetryPacing(unittest.TestCase):
    def test_rejected_quote_is_throttled_across_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'execution.sqlite3'
            store=ExecutionStore(path)
            token='0x'+'a'*40
            store.db.execute("INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)",
                             ('one',token,0,'{}','BOUGHT',None))
            store.db.execute("""INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue,trigger10)
                VALUES(?,?,?,?,?,?,?,?)""",('one',token,'100','100',0,0,0,1))
            class Swap:
                calls=0
                async def directQuotes(self,*args):
                    self.calls+=1
                    raise RuntimeError('NO_ROUTE:quote reverted')
            engine=object.__new__(LiveExecutor)
            engine.store=store
            engine.swaps=Swap()
            engine.tx=SimpleNamespace(account=SimpleNamespace(address='0x'+'b'*40))
            async def balance(token):return 100
            engine.balance=balance
            position=store.db.execute('SELECT * FROM positions').fetchone()
            with patch('live_executor.time.time',return_value=200):
                asyncio.run(engine.prepareSell(position))
            self.assertEqual(engine.swaps.calls,1)
            self.assertEqual(float(store.get('sellRetryAfter:one')),210.0)
            store.db.close()
            store=ExecutionStore(path)
            engine.store=store
            with patch('live_executor.time.time',return_value=209.9):
                asyncio.run(engine.prepareSell(store.db.execute('SELECT * FROM positions').fetchone()))
            self.assertEqual(engine.swaps.calls,1)
            with patch('live_executor.time.time',return_value=210):
                asyncio.run(engine.prepareSell(store.db.execute('SELECT * FROM positions').fetchone()))
            self.assertEqual(engine.swaps.calls,2)
            self.assertFalse(store.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0])
            store.db.close()


if __name__=='__main__':
    unittest.main()
