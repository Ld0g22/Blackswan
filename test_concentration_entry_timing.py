"""Canonical concentration entry deadlines: deterministic regressions, no network."""
import json
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from campaign_v3 import concentrationCampaign, validateCampaign
from live_executor import ExecutionStore
from sacrificial_lamb import LambGate
from wallet_transactions import TransactionJournal
from test_lamb_500_block_gate import TestProbe500BlockGate
from test_sacrificial_lamb import WALLET
from pathlib import Path
from tempfile import TemporaryDirectory

class CanonicalTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case=TestProbe500BlockGate('test_exact_500_blocks_then_fresh_quote_and_normal_buy_queue')
        await self.case.asyncSetUp()
    async def asyncTearDown(self):
        await self.case.asyncTearDown()
    def configure(self,windowEnd,blocks=500):
        c=self.case
        cfg=concentrationCampaign(WALLET)
        cfg['sacrifice']['wait']['value']=blocks
        cfg['limits']={'maxSacrifices':None,'maxNominalSpendGbp':None}
        c.store.configuredCampaign=validateCampaign(cfg)
        c.gate.waitBlocks=blocks
        c.gate.activity=None
        item=dict(policyName='concentration',windowAnchor='firstTrade',windowSeconds=30,
                  firstTradeAt=windowEnd-30,windowEndAtExclusive=windowEnd,coverageComplete=True)
        c.store.db.execute('UPDATE selections SET event=?,decisionAt=? WHERE selectionId=?',
                           (json.dumps(item),time.time(),c.selectionId))
    def targetHeader(self,block,stamp):
        c=self.case; previous=c.rpc.call
        async def rpc(method,params):
            if method=='eth_getBlockByNumber':
                assert int(params[0],16)==block
                return {'number':hex(block),'timestamp':hex(stamp),'hash':'0x'+'cc'*32}
            return await previous(method,params)
        c.rpc.call=rpc
    async def test_recent_pass_does_not_rejuvenate_expired_window(self):
        c=self.case
        self.configure(int(time.time())-10)
        await c.gate.start(c.selection())
        self.assertEqual(c.status(),'PROBE_FAILED')
        self.assertEqual(len(c.owner.tx.signed),0)
        self.assertEqual(c.selection()['reason'],'MISSED_1P_DEADLINE')
    async def test_late_survival_check_cannot_promote(self):
        c=self.case
        await c.confirmed()
        self.configure(int(time.time())-60)
        c.rpc.head=510
        self.targetHeader(510,int(time.time())-10)
        await c.gate.check(c.selection())
        self.assertEqual(c.status(),'PROBE_FAILED')
        self.assertEqual(c.selection()['reason'],'MISSED_SURVIVAL_DEADLINE')
    async def test_fresh_30_block_gate_uses_canonical_timestamp(self):
        c=self.case
        await c.confirmed()
        self.configure(int(time.time())-8,30)
        c.rpc.head=39
        c.routes.block=hex(40)
        await c.gate.check(c.selection())
        self.assertEqual(c.status(),'PROBE_CONFIRMED')
        c.gate.headCached=None
        c.rpc.head=40
        stamp=int(time.time())
        self.targetHeader(40,stamp)
        await c.gate.check(c.selection())
        self.assertEqual(c.status(),'QUEUED')
        self.assertEqual(c.gate.readyAt(c.selectionId),stamp)
    def test_real_manifest_and_store_accept_30_blocks_default_500(self):
        cfg=concentrationCampaign(WALLET)
        self.assertEqual(cfg['sacrifice']['wait']['value'],500)
        cfg['sacrifice']['wait']['value']=30
        with TemporaryDirectory() as root:
            store=ExecutionStore(Path(root)/'execution.sqlite3',configuredCampaign=validateCampaign(cfg))
            try:
                self.assertEqual(store.probeWaitBlocks,30)
                gate=LambGate(SimpleNamespace(store=store,rpc=None),None,waitBlocks=30)
                self.assertEqual(gate.waitBlocks,30)
                self.assertIsNone(gate.activity)
            finally:store.db.close()

class JournalDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=TemporaryDirectory()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3')
        self.now=100.0
        self.account=SimpleNamespace(address=WALLET,sign_transaction=unittest.mock.Mock(
            return_value=SimpleNamespace(hash=bytes(32),raw_transaction=b'fake')))
        self.expireOnBalance=False
        async def rpc(method,params):
            values={'eth_chainId':hex(4663),'eth_getCode':'0x01','eth_call':'0x',
                    'eth_estimateGas':'0x5208','eth_getBlockByNumber':{'baseFeePerGas':'0x1'},
                    'eth_maxPriorityFeePerGas':'0x0','eth_getTransactionCount':'0x0','eth_getBalance':hex(10**18),
                    'eth_getTransactionReceipt':None}
            if self.expireOnBalance and method=='eth_getBalance':self.now=104
            return values[method]
        self.journal=TransactionJournal(self.store,SimpleNamespace(call=rpc),self.account,None)
        self.tx={'to':'0x'+'2'*40,'data':'0x','value':1}
    async def asyncTearDown(self):
        self.store.db.close();self.temp.cleanup()
    async def test_slow_preflight_cannot_sign_after_expiry(self):
        self.expireOnBalance=True
        with patch('wallet_transactions.time.time',side_effect=lambda:self.now):
            with self.assertRaisesRegex(RuntimeError,'MISSED_ENTRY_SIGNING_DEADLINE'):
                await self.journal.create('i','s','BUY',self.tx,1,deadlineAt=103)
        self.account.sign_transaction.assert_not_called()
        self.assertFalse(self.journal.unsettled())
    async def test_expired_never_submitted_intent_not_broadcast_even_after_restart(self):
        with patch('wallet_transactions.time.time',side_effect=lambda:self.now),patch('wallet_transactions.sendRaw',new_callable=AsyncMock) as send:
            await self.journal.create('i','s','PROBE_BUY',self.tx,1,deadlineAt=103)
            self.now=104
            restarted=TransactionJournal(self.store,self.journal.rpc,self.account,None)
            self.assertEqual(await restarted.broadcast('i'),'EXPIRED_UNSENT')
            send.assert_not_awaited()
            self.assertFalse(restarted.unsettled())
    async def test_submitted_intent_not_resent_after_deadline_stays_reconcilable(self):
        with patch('wallet_transactions.time.time',side_effect=lambda:self.now),patch('wallet_transactions.sendRaw',new_callable=AsyncMock) as send:
            send.return_value='0x'+'00'*32
            await self.journal.create('i','s','BUY',self.tx,1,deadlineAt=103)
            self.assertEqual(await self.journal.broadcast('i'),'SENT')
            self.now=111
            await self.journal.reconcile()
            self.assertEqual(send.await_count,1)
            self.assertTrue(self.journal.unsettled())

if __name__=='__main__':unittest.main()
