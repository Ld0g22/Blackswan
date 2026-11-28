"""Regression tests for the observed timing failures (no real RPC or signing)."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from test_concentration_entry_timing import CanonicalTimingTests, JournalDeadlineTests
from live_executor import LiveExecutor
from entry_timing import retryTime, PreparedEntry, remainingBlocks

class EntryLifecycleTests(CanonicalTimingTests):
    async def test_survival_transient_failure_can_retry_inside_deadline(self):
        c=self.case
        await c.confirmed()
        self.configure(int(time.time())-60)
        c.rpc.head=510
        self.targetHeader(510,int(time.time()))
        c.routes.failSell=True
        await c.gate.check(c.selection())
        self.assertEqual(c.status(),'PROBE_CONFIRMED')
        c.routes.failSell=False
        await asyncio.sleep(.12)
        proof=await c.gate.check(c.selection())
        self.assertEqual(c.status(),'QUEUED')
        self.assertIsInstance(proof,PreparedEntry)
        self.assertEqual(proof.selectionId,c.selectionId)
        self.assertEqual(proof.targetBlock,510)
        self.assertEqual(c.routes.quoteCalls,3)
        self.assertIn('NO_SELL_ROUTE',c.store.get('entryFailure:survival:'+c.selectionId))

    async def test_continuous_survival_to_buy_no_second_quote(self):
        c=self.case
        await c.confirmed()
        self.configure(int(time.time())-60)
        c.rpc.head=510
        self.targetHeader(510,int(time.time()))
        c.owner.lamb=c.gate
        c.owner.active=set()
        c.owner.entryWalletConflict=lambda:None
        c.owner.buy=AsyncMock()
        await LiveExecutor.processEntry(c.owner,c.selection())
        c.owner.buy.assert_awaited_once()
        args=c.owner.buy.await_args
        self.assertEqual(args.args[0]['status'],'QUEUED')
        self.assertIsInstance(args.kwargs['prepared'],PreparedEntry)
        self.assertEqual(c.routes.quoteCalls,2)

    async def test_restart_queued_revalidates_survival(self):
        c=self.case
        await c.confirmed()
        self.configure(int(time.time())-60)
        c.rpc.head=510
        self.targetHeader(510,int(time.time()))
        await c.gate.check(c.selection())
        c.owner.lamb=c.gate
        c.owner.active=set()
        c.owner.entryWalletConflict=lambda:None
        c.owner.buy=AsyncMock()
        c.rpc.canonical['blockHash']='0x'+'ef'*32
        await LiveExecutor.processEntry(c.owner,c.selection())
        self.assertEqual(c.status(),'PROBE_UNVERIFIED')
        c.owner.buy.assert_not_awaited()

    def test_retries_and_countdown(self):
        self.assertLess(retryTime(101.55,103,'NO_SELL_ROUTE'),103)
        self.assertLess(retryTime(102.759,103,'RPC_REVERT_OR_REJECTED'),103)
        self.assertIsNone(retryTime(102.99,103,'RPC_HTTP_429'))
        self.assertIsNone(retryTime(103,103,'NO_SELL_ROUTE'))
        self.assertIsNone(remainingBlocks(1000,500,950,100,100))
        self.assertIsNone(remainingBlocks(1000,500,1200,95,100))
        self.assertEqual(remainingBlocks(1000,500,1200,99.9,100),300)
        self.assertEqual(remainingBlocks(1000,30,1030,99.9,100),0)

class ConcurrentJournalTests(JournalDeadlineTests):
    async def test_independent_preflight_reads_overlap(self):
        old=self.journal.rpc.call
        inFlight=0
        peak=0
        async def delayed(method,params):
            nonlocal inFlight,peak
            inFlight+=1;peak=max(peak,inFlight)
            try:
                await asyncio.sleep(.03)
                return await old(method,params)
            finally:
                inFlight-=1
        self.journal.rpc.call=delayed
        await self.journal.create('i','s','BUY',self.tx,1,deadlineAt=time.time()+3)
        self.assertGreaterEqual(peak,3)

    async def test_confirmed_receipt_cannot_be_overwritten_by_send_response(self):
        await self.journal.create('i','s','BUY',self.tx,1,deadlineAt=time.time()+3)
        began=asyncio.Event()
        finish=asyncio.Event()
        async def send(*args):
            began.set()
            await finish.wait()
            return '0x'+'00'*32
        original=self.journal.rpc.call
        async def rpc(method,params):
            if method=='eth_getTransactionReceipt':return {'status':'0x1'}
            return await original(method,params)
        self.journal.rpc.call=rpc
        with patch('wallet_transactions.sendRaw',side_effect=send):
            task=asyncio.create_task(self.journal.broadcast('i'))
            await began.wait()
            await self.journal.reconcile()
            finish.set()
            await task
        self.assertEqual(self.journal.confirmed('i')['state'],'CONFIRMED')
