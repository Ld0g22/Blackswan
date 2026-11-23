"""Broadcast admission tests: no real network, no real signing or orders."""
import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from broadcast_rpc import BroadcastError, definitiveRejection, sendRaw
from live_executor import ExecutionStore
from wallet_transactions import TransactionJournal
from test_live_execution import FakeAccount, FakeRpc


class TestErrorClassification(unittest.TestCase):
    def test_definite_pre_admission_rejection(self):
        self.assertTrue(definitiveRejection(-32000,'intrinsic gas too low'))
        self.assertTrue(definitiveRejection(-32000,'insufficient funds for gas * price + value'))
        self.assertTrue(definitiveRejection(-32602,'invalid params'))
        self.assertTrue(definitiveRejection(-32000,'transaction type not supported'))

    def test_ambiguous_errors_do_not_release_nonce(self):
        self.assertFalse(definitiveRejection(-32000,'nonce too low'))
        self.assertFalse(definitiveRejection(-32000,'already known'))
        self.assertFalse(definitiveRejection(-32000,'execution reverted'))
        self.assertFalse(definitiveRejection(-32000,'unknown error'))
        self.assertFalse(definitiveRejection(-32005,'rate limit exceeded'))


class TestRealRpcErrorParsing(unittest.IsolatedAsyncioTestCase):
    async def test_jsonrpc_error_message_preserved(self):
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def json(self,**kwargs):
                return {'jsonrpc':'2.0','id':1,'error':
                        {'code':-32000,'message':'intrinsic gas too low: have 1 want 2'}}
        class Session:
            def post(self,*args,**kwargs):return Response()
        rpc=type('Rpc',(),{'requestNumber':0,'session':Session(),'url':'https://not-sent.example'})()
        with self.assertRaises(BroadcastError) as cm:
            await sendRaw(rpc,'0x1234')
        self.assertEqual(cm.exception.kind,'RPC_REJECTED')
        self.assertTrue(cm.exception.definite)
        self.assertIn('intrinsic gas too low',str(cm.exception))
        self.assertEqual(rpc.requestNumber,1)


class TestDurableAdmissionState(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.tmp.name)/'state.sqlite3')
        self.rpc=FakeRpc()
        self.journal=TransactionJournal(self.store,self.rpc,FakeAccount(),b'never-used')

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def sign(self):
        return await self.journal.create('one','selection','BUY',{'to':'0x'+'f'*40,'value':100},100)

    async def test_definite_reject_preserves_intent_and_never_resends(self):
        signed=await self.sign()
        with patch('wallet_transactions.sendRaw',side_effect=BroadcastError(
                'RPC_REJECTED','code=-32000 message=insufficient funds',True)):
            result=await self.journal.broadcast(signed['intentId'])
            self.assertEqual(result,'REJECTED')
            self.assertEqual(self.store.db.execute(
                'SELECT state FROM transactions').fetchone()[0],'REJECTED')
            self.assertIn('insufficient funds',self.store.db.execute(
                'SELECT reason FROM transactions').fetchone()[0])
            self.journal.lastCheck=0
            await self.journal.reconcile()
        self.assertEqual(len(self.rpc.mempool),0)
        with self.assertRaisesRegex(RuntimeError,'WALLET_TRANSACTION_IN_FLIGHT'):
            await self.journal.create('two','next','BUY',{'to':'0x'+'f'*40,'value':100})

    async def test_transport_uncertain_preserves_identical_bytes(self):
        signed=await self.sign()
        with patch('wallet_transactions.sendRaw',side_effect=BroadcastError(
                'TRANSPORT_UNCERTAIN','TimeoutError',False)):
            self.assertEqual(await self.journal.broadcast(signed['intentId']),'UNCERTAIN')
        self.assertEqual(self.store.db.execute(
            'SELECT state FROM transactions').fetchone()[0],'UNCERTAIN')
        self.store.db.execute('UPDATE transactions SET lastBroadcast=0')
        await self.journal.reconcile()
        self.assertEqual(self.rpc.mempool, [signed['raw']])
        self.assertEqual(self.store.db.execute(
            'SELECT state FROM transactions').fetchone()[0],'SENT')

    async def test_success_never_claims_fill(self):
        await self.sign()
        self.assertEqual(await self.journal.broadcast('one'),'SENT')
        self.assertEqual(self.store.db.execute(
            'SELECT state FROM transactions').fetchone()[0],'SENT')
        self.assertIsNone(self.journal.confirmed('one'))


if __name__=='__main__':
    unittest.main()
