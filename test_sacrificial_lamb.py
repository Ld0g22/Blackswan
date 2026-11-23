"""Offline tests for the small, in-process 1p-to-10p gate (never a second signer)."""
import asyncio
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from eth_utils import keccak
from live_executor import ExecutionStore, LiveExecutor, checkSlice, receiptTransfers, STAGES
from sacrificial_lamb import LambGate

WALLET='0x'+'1'*40
TOKEN='0x'+'a'*40
TOPIC='0x'+keccak(text='Transfer(address,address,uint256)').hex()

def receipt(token=TOKEN,quantity=10):
    return dict(status='0x1',blockNumber='0xa',logs=[dict(address=token,
        topics=[TOPIC,'0x'+'0'*64,'0x'+WALLET[2:].rjust(64,'0')],
        data='0x'+f'{quantity:064x}')])

class DummyFx:
    async def get(self):return Decimal('1000')

class DummyRoutes:
    failSell=False
    async def verifiedBuyCandidates(self,token,amount):
        return [dict(output=100,preBuySellOutput=80,block='0x10',venue=dict(source='uniswapV4'))]
    async def directQuotes(self,token,amount,buy,block=None):
        if self.failSell:raise RuntimeError('NO_SELL_ROUTE')
        return [dict(output=20,block=block or '0x10')]
    def build(self,quote,amount,wallet):return dict(to=WALLET,value=amount,data='0x')

class DummyJournal:
    def __init__(self,store):self.account=SimpleNamespace(address=WALLET);self.store=store;self.signed=[]
    async def create(self,intent,selection,kind,transaction,amount=0):
        self.signed.append((intent,selection,kind,amount))
        tx='0x'+f'{len(self.signed)+500:064x}'
        self.store.db.execute("""INSERT OR IGNORE INTO transactions
             (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,inputAmount)
             VALUES(?,?,?,'SIGNED',?,'0x',0,?,?,?)""",
             (intent,selection,kind,tx,time.time(),time.time(),str(amount)))
        return dict(self.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intent,)).fetchone())
    async def broadcast(self,intent):
        self.store.db.execute("UPDATE transactions SET state='SENT' WHERE intentId=?",(intent,))
        return 'SENT'

class TestLamb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.tmp.name)/'execution.sqlite3',sacrifice=True)
        self.selector=sqlite3.connect(':memory:')
        self.selector.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        self.owner=SimpleNamespace(store=self.store,fx=DummyFx(),swaps=DummyRoutes(),
             tx=DummyJournal(self.store),selector=self.selector,
             intakeLock=asyncio.Lock())
        self.owner.entryExclusion=lambda token,event:None
        async def noPons(token):return None
        self.owner.ponsOnChain=noPons
        self.gate=LambGate(self.owner,receiptTransfers)
        self.selection='selection-1'
        self.store.db.execute("""INSERT INTO selections
            (selectionId,token,decisionAt,event,status,reason,quoteWei)
            VALUES(?,?,?,?,?,?,NULL)""",
            (self.selection,TOKEN,time.time(),'{}','LAMB_QUEUED',None))
    async def asyncTearDown(self):
        self.selector.close();self.store.db.close();self.tmp.cleanup()
    def state(self):
        return self.store.db.execute('SELECT status FROM selections WHERE selectionId=?',
                                     (self.selection,)).fetchone()[0]
    def row(self):
        return self.store.db.execute('SELECT * FROM selections WHERE selectionId=?',
                                     (self.selection,)).fetchone()
    def confirmedProbe(self,received=10):
        self.store.db.execute("""UPDATE transactions SET state='CONFIRMED',receipt=?
                                  WHERE selectionId=? AND kind='PROBE_BUY'""",
                              (json.dumps(receipt(quantity=received)),self.selection))
        row=self.store.db.execute(
            "SELECT * FROM transactions WHERE selectionId=? AND kind='PROBE_BUY'",
            (self.selection,)).fetchone()
        self.gate.settle(row)
    async def test_exact_one_penny_and_actual_receipt(self):
        await self.gate.start(self.row())
        self.assertEqual(len(self.owner.tx.signed),1)
        self.assertEqual(self.owner.tx.signed[0][2:],
                         ('PROBE_BUY',10000000000000))
        self.assertEqual(self.state(),'PROBE_PENDING')
        self.confirmedProbe(received=73)
        self.assertEqual(self.state(),'PROBE_CONFIRMED')
        self.assertEqual(self.store.db.execute('SELECT probeQty FROM lamb_probes').fetchone()[0],'73')
        self.assertEqual(self.store.acquired(),0)
        await self.gate.start(self.row()) # no duplicate signatures on replay
        self.assertEqual(len(self.owner.tx.signed),1)
    async def test_wait_full_ten_seconds_and_fail_closed_on_bad_sell_route(self):
        await self.gate.start(self.row());self.confirmedProbe()
        await self.gate.check(self.row())
        self.assertEqual(self.state(),'PROBE_CONFIRMED')
        self.assertEqual(self.store.acquired(),0)
        self.store.db.execute("UPDATE lamb_probes SET confirmedAt=?",(time.time()-11,))
        self.owner.swaps.failSell=True
        await self.gate.check(self.row())
        self.assertIn(self.state(),('PROBE_FAILED','PROBE_UNKNOWN'))
        self.owner.swaps.failSell=False
        await self.gate.check(self.row())
        self.assertNotEqual(self.state(),'QUEUED')
    async def test_survivor_is_normal_buy_queue_and_selector_timestamp_unchanged(self):
        timestamp=self.row()['decisionAt']
        await self.gate.start(self.row());self.confirmedProbe()
        self.store.db.execute("UPDATE lamb_probes SET confirmedAt=?",(time.time()-11,))
        await self.gate.check(self.row())
        self.assertEqual(self.state(),'QUEUED')
        self.assertGreater(self.gate.readyAt(self.selection),timestamp)
        self.assertEqual(self.row()['decisionAt'],timestamp)
        self.assertEqual(len(self.owner.tx.signed),1) # no lamb 10p signer
    async def test_revocation_blocks_ten_penny_handoff(self):
        await self.gate.start(self.row());self.confirmedProbe()
        self.store.db.execute("UPDATE lamb_probes SET confirmedAt=?",(time.time()-11,))
        self.selector.execute('INSERT INTO revocations(eventKey) VALUES(?)',(self.selection,))
        await self.gate.check(self.row())
        self.assertNotEqual(self.state(),'QUEUED')
    async def test_unknown_probe_transfer_cannot_pass(self):
        await self.gate.start(self.row());self.confirmedProbe(received=0)
        self.assertEqual(self.state(),'PROBE_UNVERIFIED')
        self.assertEqual(self.store.acquired(),0)
    def test_sacrifice_mode_rejects_legacy_db_without_any_schema_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'legacy.sqlite3'
            legacy=ExecutionStore(path)
            before=set(x[0] for x in legacy.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
            legacy.db.close()
            with self.assertRaisesRegex(RuntimeError,'NOT_A_FIVE_LAMB_DB'):
                ExecutionStore(path,sacrifice=True)
            with sqlite3.connect(path) as db:
                after=set(x[0] for x in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"))
            self.assertEqual(before,after)
            self.assertNotIn('lamb_probes',after)

    def test_legacy_ladder_is_not_replaced(self):
        self.assertEqual(STAGES,((3,80),(5,15),(10,5)))
        self.assertEqual(checkSlice(101,5,101),15)
        self.assertEqual(checkSlice(101,10,101,80,15),6)
    def test_five_confirmed_ten_penny_positions_not_five_probes(self):
        self.assertTrue(self.store.eligible())
        self.assertEqual(self.store.get('intakeState'),'OPEN')
        self.assertEqual(self.store.acquired(),0)

if __name__=='__main__':
    unittest.main()
