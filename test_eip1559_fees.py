"""Fee-envelope and actual eth-account signing regression; strictly offline."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from eth_account import Account
from live_executor import ExecutionStore
from wallet_transactions import TransactionJournal, eip1559FeeFields
from test_live_execution import FakeRpc


class TestEip1559FeePolicy(unittest.TestCase):
    def test_base_fee_headroom(self):
        fields=eip1559FeeFields({'baseFeePerGas':hex(20_044_000)},hex(50_000))
        self.assertEqual(fields,{
            'type':2,
            'maxPriorityFeePerGas':50_000,
            'maxFeePerGas':40_138_000})
        self.assertGreater(fields['maxFeePerGas'],20_044_000)

    def test_no_guessing_without_eip1559_support(self):
        with self.assertRaisesRegex(RuntimeError,'EIP1559_BASE_FEE_MISSING'):
            eip1559FeeFields({},'0x1')
        with self.assertRaisesRegex(RuntimeError,'EIP1559_FEE_QUOTE_INVALID'):
            eip1559FeeFields({'baseFeePerGas':'garbage'},'0x1')
        with self.assertRaisesRegex(RuntimeError,'EIP1559_FEE_QUOTE_INVALID'):
            eip1559FeeFields({'baseFeePerGas':'0x0'},'0x1')


class TestActualType2Signing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.directory.name)/'tx.sqlite3')
        self.rpc=FakeRpc()
        self.key=bytes.fromhex('11'*32)
        self.account=Account.from_key(self.key)
        self.journal=TransactionJournal(self.store,self.rpc,self.account,self.key)

    async def asyncTearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    async def test_real_type2_signed_bytes_are_persisted_before_any_send(self):
        recipient='0x204faca1764b154221e35c0d20abb3c525710498'
        signed=await self.journal.create('intent','selection','BUY',
                                         {'to':recipient,'data':'0x','value':123},123)
        self.assertEqual(signed['state'],'SIGNED')
        raw=bytes.fromhex(signed['raw'][2:])
        self.assertEqual(raw[0],2)
        self.assertEqual(Account.recover_transaction(raw),self.account.address)
        self.assertEqual(len(self.rpc.mempool),0)
        self.assertIn('eth_maxPriorityFeePerGas',[m for m,_ in self.rpc.calls])
        self.assertIn('eth_getBlockByNumber',[m for m,_ in self.rpc.calls])

    async def test_gas_ceiling_included_in_balance_check(self):
        oldCall=self.rpc.call
        async def limited(method,params):
            if method=='eth_getBalance':return hex(100)
            return await oldCall(method,params)
        self.rpc.call=limited
        with self.assertRaisesRegex(RuntimeError,'INSUFFICIENT_ETH_FOR_SWAP_AND_GAS'):
            await self.journal.create('intent','selection','BUY',
                                      {'to':'0x'+'f'*40,'data':'0x','value':123},123)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
