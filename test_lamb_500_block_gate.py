"""Safety and end-to-end tests: 500 canonical Robinhood blocks after 1p receipt.

Old 10-second campaigns, original executor, and selector are tested separately.
These tests send no transactions and require no RPC credentials.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from live_executor import ExecutionStore,receiptTransfers
from sacrificial_lamb import LambGate
from test_sacrificial_lamb import (
    DummyFx,DummyJournal,DummyRoutes,receipt,WALLET,TOKEN
)
from run_live_campaign import campaignSurvivalBlocks


class OfflineRpc:
    def __init__(self):
        self.head=509
        self.calls=[]
        self.canonical=None
        self.failHead=False
        self.failReceipt=False

    async def call(self,method,params):
        self.calls.append((method,params))
        if method=='eth_blockNumber':
            if self.failHead:raise RuntimeError('RPC_OFFLINE')
            return hex(self.head)
        if method=='eth_getTransactionReceipt':
            if self.failReceipt:raise RuntimeError('RPC_OFFLINE')
            return self.canonical
        raise AssertionError('Unexpected JSON RPC '+method)


class BlockRoutes(DummyRoutes):
    block='0x1fe'  # 510 decimal; canonical 1p receipt at block 10

    def __init__(self):
        self.quoteCalls=0

    async def verifiedBuyCandidates(self,token,amount):
        self.quoteCalls+=1
        values=(await super().verifiedBuyCandidates(token,amount))[0]
        values['block']=self.block
        values['preBuySellBlock']=self.block
        return [values]

    async def directQuotes(self,token,amount,buy,block=None):
        values=(await super().directQuotes(token,amount,buy,block))[0]
        values['block']=block or self.block
        return [values]


class TestProbe500BlockGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3',
                                  sacrifice=True,probeWaitBlocks=500)
        self.selector=sqlite3.connect(':memory:')
        self.selector.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        self.rpc=OfflineRpc()
        self.routes=BlockRoutes()
        self.owner=SimpleNamespace(store=self.store,selector=self.selector,
             rpc=self.rpc,fx=DummyFx(),swaps=self.routes,
             tx=DummyJournal(self.store),intakeLock=asyncio.Lock())
        async def noPons(token):return None
        self.owner.ponsOnChain=noPons
        self.owner.entryExclusion=lambda token,event:None
        self.gate=LambGate(self.owner,receiptTransfers,waitBlocks=500)
        self.selectionId='position-500'
        self.store.db.execute("""INSERT INTO selections
            (selectionId,token,decisionAt,event,status)
            VALUES(?,?,?,?,?)""",
            (self.selectionId,TOKEN,time.time(),'{}','LAMB_QUEUED'))

    async def asyncTearDown(self):
        self.selector.close()
        self.store.db.close()
        self.temp.cleanup()

    def selection(self):
        return self.store.db.execute(
            'SELECT * FROM selections WHERE selectionId=?',
            (self.selectionId,)).fetchone()

    def status(self):
        return self.selection()['status']

    async def confirmed(self):
        await self.gate.start(self.selection())
        self.assertEqual(self.status(),'PROBE_PENDING')
        tx=self.store.db.execute(
            "SELECT txHash FROM transactions WHERE kind='PROBE_BUY'").fetchone()
        self.assertIsNotNone(tx)
        data=receipt(quantity=73)
        data.update(transactionHash=tx['txHash'],
                    blockHash='0x'+'ab'*32)
        self.rpc.canonical=dict(data)
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt=? WHERE kind='PROBE_BUY'",
            (json.dumps(data),))
        row=self.store.db.execute(
            "SELECT * FROM transactions WHERE kind='PROBE_BUY'").fetchone()
        self.gate.settle(row)
        self.assertEqual(self.status(),'PROBE_CONFIRMED')
        self.assertEqual(len(self.owner.tx.signed),1)

    async def test_499_blocks_cannot_pass_even_if_10_seconds_elapsed(self):
        await self.confirmed()
        self.store.db.execute('UPDATE lamb_probes SET confirmedAt=?',
                              (time.time()-3600,))
        self.rpc.head=509
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_CONFIRMED')
        self.assertEqual(self.routes.quoteCalls,1)  # initial 1p entry only
        self.assertFalse(any(m=='eth_getTransactionReceipt' for m,_ in self.rpc.calls))
        self.assertEqual(self.store.acquired(),0)

    async def test_exact_500_blocks_then_fresh_quote_and_normal_buy_queue(self):
        await self.confirmed()
        self.rpc.head=510
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'QUEUED')
        self.assertIsNotNone(self.gate.readyAt(self.selectionId))
        self.assertEqual(self.routes.quoteCalls,2)  # 1p first, new 10p quote after 500
        self.assertEqual(len(self.owner.tx.signed),1)  # no duplicate 1p and no separate 10p signer
        self.assertEqual(len([x for x in self.rpc.calls if x[0]=='eth_getTransactionReceipt']),1)
        self.assertEqual(self.store.acquired(),0)

    async def test_stale_head_read_retries_without_spending(self):
        await self.confirmed()
        self.rpc.failHead=True
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_CONFIRMED')
        self.rpc.failHead=False
        self.rpc.head=510
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'QUEUED')

    async def test_intermittent_receipt_rpc_retries_without_promotion(self):
        await self.confirmed()
        self.rpc.head=510
        self.rpc.failReceipt=True
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_CONFIRMED')
        self.rpc.failReceipt=False
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'QUEUED')

    async def test_reorg_or_receipt_mismatch_is_never_promoted(self):
        await self.confirmed()
        self.rpc.head=510
        self.rpc.canonical['blockHash']='0x'+'cd'*32
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_UNVERIFIED')
        self.assertEqual(len(self.owner.tx.signed),1)

    async def test_missing_original_receipt_block_hash_never_passes(self):
        await self.confirmed()
        tx=self.store.db.execute(
            "SELECT receipt FROM transactions WHERE kind='PROBE_BUY'").fetchone()
        raw=json.loads(tx['receipt'])
        del raw['blockHash']
        self.store.db.execute(
            "UPDATE transactions SET receipt=? WHERE kind='PROBE_BUY'",
            (json.dumps(raw),))
        self.rpc.head=510
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_UNVERIFIED')
        self.assertEqual(self.store.acquired(),0)

    async def test_stale_quote_before_gate_never_buys(self):
        await self.confirmed()
        self.rpc.head=510
        self.routes.block='0x1fd'
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'PROBE_UNKNOWN')
        self.assertEqual(len(self.owner.tx.signed),1)

    async def test_no_sell_route_prevents_10p(self):
        await self.confirmed()
        self.rpc.head=510
        self.routes.failSell=True
        await self.gate.check(self.selection())
        self.assertNotEqual(self.status(),'QUEUED')
        self.assertEqual(self.store.acquired(),0)

    async def test_restart_same_campaign_recovers_receipt_block_and_gate(self):
        await self.confirmed()
        self.store.db.close()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3',
                                  sacrifice=True,probeWaitBlocks=500)
        self.owner.store=self.store
        self.owner.tx.store=self.store
        self.gate=LambGate(self.owner,receiptTransfers,waitBlocks=500)
        self.rpc.head=510
        await self.gate.check(self.selection())
        self.assertEqual(self.status(),'QUEUED')
        self.assertEqual(len(self.owner.tx.signed),1)

    async def test_revoke_during_wait_never_promotes(self):
        await self.confirmed()
        self.selector.execute("INSERT INTO revocations(eventKey) VALUES(?)",
                              (self.selectionId,))
        self.rpc.head=510
        await self.gate.check(self.selection())
        self.assertNotEqual(self.status(),'QUEUED')


class TestNewPolicyIdentity(unittest.TestCase):
    def test_new_manifest_is_500_blocks_and_old_manifest_remains_10_seconds(self):
        self.assertEqual(campaignSurvivalBlocks(
            {'survivalBlocks':500},True),500)
        self.assertIsNone(campaignSurvivalBlocks(
            {'survivalSeconds':10},True))
        self.assertIsNone(campaignSurvivalBlocks(
            {'survivalSeconds':10},False))
        for bad in (0,499,501,10,True,500.0,'500',None):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(RuntimeError,'INVALID_CAMPAIGN_SURVIVAL_POLICY'):
                    campaignSurvivalBlocks({'survivalBlocks':bad},True)
        with self.assertRaisesRegex(RuntimeError,'INVALID_CAMPAIGN_SURVIVAL_POLICY'):
            campaignSurvivalBlocks({'survivalBlocks':500,'survivalSeconds':10},True)

    def test_new_and_old_db_policy_are_immutable_across_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'old.sqlite3'
            new=Path(tmp)/'new.sqlite3'
            legacy=ExecutionStore(old,sacrifice=True)
            self.assertEqual(legacy.get('waitSeconds'),'10')
            legacy.db.close()
            saved=old.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'FIVE_LAMB_PROBE_WAIT_POLICY_MISMATCH'):
                ExecutionStore(old,sacrifice=True,probeWaitBlocks=500)
            self.assertEqual(old.read_bytes(),saved)
            updated=ExecutionStore(new,sacrifice=True,probeWaitBlocks=500)
            self.assertEqual(updated.get('waitBlocks'),'500')
            self.assertEqual(updated.get('waitSeconds','NONE'),'NONE')
            updated.db.close()
            original=new.read_bytes()
            with self.assertRaisesRegex(RuntimeError,'FIVE_LAMB_PROBE_WAIT_POLICY_MISMATCH'):
                ExecutionStore(new,sacrifice=True)
            self.assertEqual(new.read_bytes(),original)

    def test_invalid_probe_policy_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'not-created.sqlite3'
            with self.assertRaisesRegex(ValueError,'UNSUPPORTED_PROBE_SURVIVAL_POLICY'):
                ExecutionStore(file,sacrifice=True,probeWaitBlocks=499)
            self.assertFalse(file.exists())


if __name__=='__main__':
    unittest.main()
