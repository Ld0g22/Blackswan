"""Real decoder tests for focused held-token pool discovery; never a second signer."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest

import detector
from held_venue_watch import HeldVenueWatcher
from live_executor import ExecutionStore

TOKEN='0x'+'a'*40
POOL='0x'+'f'*64
NATIVE='0x'+'0'*40

def topic(addr):return '0x'+addr[2:].rjust(64,'0')

def initialize(token=TOKEN,removed=False,block=101):
    words=[500,10,0,2**96,0]
    return dict(
        address=detector.V4_POOL_MANAGER,
        topics=[detector.SOURCES['uniswapV4']['topic'],POOL,topic(NATIVE),topic(token)],
        blockHash='0x'+'1'*64,transactionHash='0x'+'2'*64,
        blockNumber=hex(block),logIndex='0x0',removed=removed,
        data='0x'+''.join(f'{w:064x}' for w in words))

class Rpc:
    def __init__(self,logs):self.logs=logs;self.calls=[];self.failAfter=None
    async def call(self,method,params):
        self.calls.append((method,params))
        if self.failAfter is not None and len(self.calls)>self.failAfter:
            raise RuntimeError('RPC_UNAVAILABLE')
        low=int(params[0]['fromBlock'],16)
        high=int(params[0]['toBlock'],16)
        return [x for x in self.logs if low<=int(x['blockNumber'],16)<=high]

class TestHeldVenueWatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.store=ExecutionStore(root/'execution.sqlite3',sacrifice=True)
        self.detector=sqlite3.connect(root/'detector.sqlite3')
        self.detector.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        self.detector.execute("INSERT INTO meta VALUES('lastConnectedHead','100')")
        self.store.db.execute("""INSERT INTO selections
          (selectionId,token,decisionAt,event,status) VALUES('held',?,0,'{}','BOUGHT')""",
          (TOKEN,))
        self.store.db.execute("""INSERT INTO positions
          (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
          VALUES('held',?,'100','100',0,0,0)""",(TOKEN,))
        self.rpc=Rpc([initialize()])
        self.watcher=HeldVenueWatcher(SimpleNamespace(
            store=self.store,detector=self.detector,rpc=self.rpc))
    async def asyncTearDown(self):
        await self.watcher.close();self.detector.close();self.store.db.close();self.tmp.cleanup()

    async def test_backfill_is_contiguous_and_adds_new_held_v4_pool(self):
        await self.watcher._backfill(102)
        venues=self.store.venues(TOKEN)
        self.assertEqual(len(venues),1)
        self.assertEqual(venues[0]['id'],POOL)
        self.assertEqual(self.store.get('heldVenueCursor'),'102')
        blocks=[(int(c[1][0]['fromBlock'],16),int(c[1][0]['toBlock'],16))
                for c in self.rpc.calls]
        self.assertEqual(blocks,[(81,90),(91,100),(101,102)])

    async def test_malformed_response_does_not_skip_failed_batch(self):
        self.rpc.failAfter=1
        with self.assertRaisesRegex(RuntimeError,'RPC_UNAVAILABLE'):
            await self.watcher._backfill(102)
        self.assertEqual(self.store.get('heldVenueCursor'),'90')

    async def test_reorg_removed_event_deactivates_pool(self):
        self.watcher._apply(initialize())
        self.assertEqual(len(self.store.venues(TOKEN)),1)
        self.watcher._apply(initialize(removed=True))
        self.assertEqual(self.store.venues(TOKEN),[])

    async def test_other_token_pool_does_not_pollute_held_catalogue(self):
        self.watcher._apply(initialize('0x'+'b'*40))
        self.assertEqual(self.store.venues(TOKEN),[])

    async def test_no_background_task_until_campaign_intake_closed(self):
        self.assertIsNone(self.watcher.task)

if __name__=='__main__':unittest.main()
