import asyncio
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, AsyncMock, patch
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


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload=payload
        self.status=status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self, **kwargs):
        return self.payload


class ProviderResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_and_malformed_index_responses_preserve_partial_inventory(self):
        first={'result':{'tokenBalances':[{'contractAddress':TOKEN,'tokenBalance':'0x1'}],'pageKey':'second'}}
        for payload in (None, [], 'bad gateway', {'result':None}, {'result':[]}, {'result':{'tokenBalances':[None]}}):
            with self.subTest(payload=payload):
                session=type('Session',(),{'post':Mock(side_effect=[FakeResponse(first),FakeResponse(payload)])})()
                tokens,complete,reason=await manual.indexedWalletTokens(session,'https://example.invalid',WALLET)
                self.assertEqual(tokens,{TOKEN})
                self.assertFalse(complete)
                self.assertTrue(reason)

    async def test_null_http_error_and_blockscout_fallback_do_not_crash(self):
        session=type('Session',(),{
            'post':Mock(return_value=FakeResponse(None,429)),
            'get':Mock(return_value=FakeResponse({'items':[{'token':{'address_hash':TOKEN}}],'next_page_params':None}))})()
        tokens,evidence=await manual.indexedCandidates(session,'https://example.invalid',WALLET)
        self.assertEqual(tokens,{TOKEN})
        self.assertFalse(evidence['alchemyPagesComplete'])
        self.assertTrue(evidence['blockscoutPagesComplete'])

    async def test_null_blockscout_response_is_reported(self):
        session=type('Session',(),{
            'get':Mock(return_value=FakeResponse(None))})()
        with patch.object(manual,'indexedWalletTokens',AsyncMock(return_value=({TOKEN},False,'unavailable'))):
            tokens,evidence=await manual.indexedCandidates(session,'https://example.invalid',WALLET)
        self.assertEqual(tokens,{TOKEN})
        self.assertIn('blockscoutReason',evidence)

class SweepRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_closing_scan_returns_reportable_status(self):
        with patch.object(manual,'inventory',AsyncMock(side_effect=RuntimeError('RPC unavailable'))):
            balances,evidence=await manual.closingInventory(None,None,None,WALLET,None,None,())
        self.assertEqual(balances,{})
        self.assertEqual(evidence['coverage'],'CLOSING_INVENTORY_FAILED')

    async def test_failed_indexes_keep_run_recoverable(self):
        with patch.object(manual,'inventory',AsyncMock(return_value=({TOKEN:1},{'alchemyPagesComplete':False,'blockscoutPagesComplete':False}))):
            balances,evidence=await manual.closingInventory(None,None,None,WALLET,None,None,())
        self.assertEqual(balances,{TOKEN:1})
        self.assertEqual(evidence['coverage'],'CLOSING_INVENTORY_FAILED')

    async def test_v3_pool_survives_refused_v4_history(self):
        from eth_abi import encode
        pool='0x3333333333333333333333333333333333333333'
        rpc=type('Rpc',(),{'ethCall':AsyncMock(return_value='0x'+encode(['address'],[pool]).hex()),'call':AsyncMock(side_effect=RuntimeError('range refused'))})()
        issues=[]
        venues=await manual.discoverVenues(rpc,TOKEN,[],issues)
        self.assertEqual(len(venues),1)
        self.assertEqual(venues[0]['id'],pool)
        self.assertIn('V4_HISTORY_UNAVAILABLE',issues)

    async def test_current_mainnet_env_is_default(self):
        import inspect
        self.assertIn('.config/blackswan/concentration-new-alchemy.env',inspect.getsource(manual.main))

class ReceiptNullTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_trace_preserves_verified_debit_without_claiming_proceeds(self):
        receipt=dict(blockNumber='0x5',blockHash='0xabc',transactionHash='0xdef',logs=[])
        rpc=type('Rpc',(),{'call':AsyncMock(side_effect=[{'hash':'0xabc'},{'from':WALLET,'blockHash':'0xabc'},None])})()
        with patch('live_executor.receiptTransfers',return_value=-20):
            debit,income=await manual.receiptIncome(rpc,receipt,TOKEN,WALLET)
        self.assertEqual(debit,20)
        self.assertIsNone(income)

class FullRunRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_closing_failure_resumes_same_run_without_signing(self):
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=SimpleNamespace(command='execute',env=root/'env',wallet_file=root/'wallet',data_dir=root/'data',state_dir=root/'state',token=[])
            rpc=SimpleNamespace(call=AsyncMock(return_value='0x1237'))
            journal=SimpleNamespace(reconcile=AsyncMock(),unsettled=Mock(return_value=[]),create=AsyncMock())
            good=({TOKEN:0},{'coverage':'INDEXED_WATERMARK_UNVERIFIED','alchemyPagesComplete':True})
            with patch.object(manual.peak,'loadSettings',return_value=('https://example.invalid',WALLET)),patch.object(manual,'globalWalletLock',return_value=root/'lock'),patch.object(manual,'otherLiveExecutors',return_value=[]),patch.object(manual,'ManualRpc',return_value=rpc),patch.object(manual,'ensurePriorNoncesResolved',AsyncMock()),patch.object(manual.getpass,'getpass',return_value='fixture'),patch.object(manual,'decodeKeystore',return_value=(SimpleNamespace(address=WALLET),None)),patch.object(manual,'TransactionJournal',return_value=journal),patch.object(manual,'accountReceipts',AsyncMock()),patch.object(manual,'freshRate',AsyncMock(return_value=Decimal('2000'))),patch.object(manual,'inventory',AsyncMock(side_effect=[good,RuntimeError('unavailable'),good,good])):
                await manual.run(args)
                walletRoot=args.state_dir/WALLET
                active=(walletRoot/'active-run').read_text()
                report=json.loads((walletRoot/active/'report.json').read_text())
                self.assertEqual(report['closingInventory']['coverage'],'CLOSING_INVENTORY_FAILED')
                await manual.run(args)
                self.assertFalse((walletRoot/'active-run').exists())
                self.assertEqual((walletRoot/'latest-run').read_text(),active)
                journal.create.assert_not_awaited()

    async def test_empty_abi_result_does_not_abort_pool_discovery(self):
        rpc=type('Rpc',(),{'ethCall':AsyncMock(return_value='0x'),'call':AsyncMock(return_value=[])})()
        issues=[]
        self.assertEqual(await manual.discoverVenues(rpc,TOKEN,[],issues),[])
        self.assertIn('V3_LOOKUP_FAILED',issues)

if __name__=='__main__':
    unittest.main()
