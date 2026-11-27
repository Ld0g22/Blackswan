import asyncio
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import manual_wallet_sale as manual

TOKEN='0x'+'11'*20
WALLET='0x'+'22'*20

class PolicyTests(unittest.TestCase):
    def test_2p_boundary_uses_raw_precision(self):
        rate=Decimal('2000')
        self.assertEqual(manual.salePolicy(9999999999999,rate),'BELOW_2P')
        self.assertEqual(manual.salePolicy(10000000000000,rate),'READY')
        self.assertEqual(manual.salePolicy(10000000000001,rate),'READY')

    def test_invalid_fx(self):
        self.assertEqual(manual.salePolicy(10**18,Decimal('NaN')),'FX_UNAVAILABLE')
        self.assertEqual(manual.salePolicy(10**18,0),'FX_UNAVAILABLE')

class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_below_2p_never_approves_or_signs(self):
        with tempfile.TemporaryDirectory() as directory:
            store=manual.initializeSweep(Path(directory)/'execution.sqlite3',WALLET)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            adapter=type('Adapter',(),{})()
            adapter.directQuotes=AsyncMock(return_value=[dict(output=10**12,venue=dict(source='uniswapV3',id=TOKEN))])
            adapter.approvalNeeded=AsyncMock(return_value=None)
            rpc=type('Rpc',(),{})()
            journal=type('Journal',(),{'create':AsyncMock(),'broadcast':AsyncMock()})()
            with patch.object(manual,'tokenBalance',AsyncMock(return_value=100)),patch.object(manual,'freshRate',AsyncMock(return_value=Decimal('2000'))),patch.object(manual,'feePrice',AsyncMock(return_value=1)):
                await manual.sellHolding(rpc,adapter,journal,store,WALLET,TOKEN,type('Fx',(),{'updated':time.time()})())
            journal.create.assert_not_called()
            row=json.loads(store.db.execute('SELECT payload FROM manual_results').fetchone()[0])
            self.assertEqual(row['state'],'BELOW_2P')
            self.assertEqual(Decimal(row['estimatedGbp']),Decimal('.002'))
            store.db.close()

    async def test_no_quote_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store=manual.initializeSweep(Path(directory)/'execution.sqlite3',WALLET)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            adapter=type('Adapter',(),{})()
            adapter.directQuotes=AsyncMock(side_effect=RuntimeError('NO_ROUTE'))
            with patch.object(manual,'tokenBalance',AsyncMock(return_value=10**18)),patch.object(manual,'freshRate',AsyncMock(return_value=Decimal('2000'))):
                await manual.sellHolding(None,adapter,None,store,WALLET,TOKEN,None)
            self.assertEqual(adapter.directQuotes.call_count,9)
            store.db.close()

    async def test_missing_payout_remains_pending_after_zero_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            store=manual.initializeSweep(Path(directory)/'execution.sqlite3',WALLET)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            fill={'txHash':'0xabc','debited':'20','income':None}
            manual.saveResult(store,TOKEN,state='MINED_ACCOUNTING_PENDING',fills=[fill])
            result=manual.saveResult(store,TOKEN,state='SOLD',remainingRaw='0')
            self.assertEqual(result['state'],'MINED_ACCOUNTING_PENDING')
            self.assertTrue(result['accountingPending'])
            store.db.close()

    async def test_credit_without_debit_is_excluded_from_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=manual.initializeSweep(root/'execution.sqlite3',WALLET)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            fill={'txHash':'0xabc','debited':'0','income':{'asset':'WETH','raw':str(10**18)},'fxGbpPerEth':'2000'}
            manual.saveResult(store,TOKEN,state='SOLD',fills=[fill],fxGbpPerEth='9000')
            report=manual.writeReport(store,root)
            self.assertEqual(report['proceedsRawByAsset'],{})
            self.assertFalse(report['accountingComplete'])
            store.db.close()

    async def test_fill_keeps_original_fx_after_later_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=manual.initializeSweep(root/'execution.sqlite3',WALLET)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            fill={'txHash':'0xabc','debited':'20','income':{'asset':'WETH','raw':str(10**18)},'fxGbpPerEth':'2000'}
            manual.saveResult(store,TOKEN,state='SOLD',fills=[fill],fxGbpPerEth='9000')
            report=manual.writeReport(store,root)
            self.assertEqual(Decimal(report['grossGbpValuedSubtotal']),Decimal('2000'))
            store.db.close()

    async def test_stale_fx_rejected(self):
        fx=type('Fx',(),{'get':AsyncMock(return_value=Decimal('2000')),'updated':time.time()-65})()
        with self.assertRaisesRegex(RuntimeError,'FX_UNAVAILABLE'):
            await manual.freshRate(fx)

    async def test_native_receipt_payout_uses_trace_and_ignores_reverted_children(self):
        receipt=dict(blockNumber='0x5',blockHash='0xabc',transactionHash='0xdef',logs=[])
        rpc=type('Rpc',(),{})()
        rpc.call=AsyncMock(side_effect=[{'hash':'0xabc'},{'from':WALLET,'blockHash':'0xabc'},dict(type='CALL',**{'from':WALLET,'to':TOKEN},value='0x0',calls=[dict(type='CALL',to=WALLET,value='0x64'),dict(type='CALL',to=WALLET,value='0x100',error='revert')])])
        with patch('live_executor.receiptTransfers',return_value=-20):
            debit,income=await manual.receiptIncome(rpc,receipt,TOKEN,WALLET)
        self.assertEqual(debit,20)
        self.assertEqual(income['raw'],'100')

    async def test_unknown_native_payout_stays_unknown(self):
        receipt=dict(blockNumber='0x5',blockHash='0xabc',transactionHash='0xdef',logs=[])
        rpc=type('Rpc',(),{})()
        rpc.call=AsyncMock(side_effect=[{'hash':'0xabc'},{'from':WALLET,'blockHash':'0xabc'},RuntimeError('trace unavailable')])
        with patch('live_executor.receiptTransfers',return_value=-20):
            debit,income=await manual.receiptIncome(rpc,receipt,TOKEN,WALLET)
        self.assertEqual(debit,20)
        self.assertIsNone(income)

if __name__=='__main__':
    unittest.main()
