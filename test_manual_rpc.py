import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from decimal import Decimal
from pathlib import Path
import tempfile

from manual_rpc import ManualRpc, PacedSession, ProviderUnavailable
import manual_wallet_sale as manual


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload=payload
        self.status=status
        self.headers=headers or {}
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        pass
    async def json(self,**kwargs):
        return self.payload


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_429_then_rpc_429_retries_identical_request(self):
        responses=[Response(None,429,{'Retry-After':'2'}),Response({'error':{'code':429}}),Response({'result':'0x5'})]
        raw=Mock(post=Mock(side_effect=responses))
        session=PacedSession(raw)
        rpc=ManualRpc(session,'https://example.invalid','0x'+'1'*40)
        with patch('manual_rpc.asyncio.sleep',new_callable=AsyncMock) as sleep:
            self.assertEqual(await rpc.call('eth_blockNumber',[]),'0x5')
        requests=[call.kwargs['json'] for call in raw.post.call_args_list]
        self.assertEqual(requests,[requests[0]]*3)
        self.assertEqual(session.retries,2)
        self.assertTrue(any(call.args[0]>=1.9 for call in sleep.call_args_list))
        self.assertGreater(session.interval,0.5)

    async def test_sustained_429_opens_circuit_and_bounds_all_following_calls(self):
        raw=Mock(post=Mock(return_value=Response(None,429)))
        session=PacedSession(raw,attempts=3)
        rpc=ManualRpc(session,'https://example.invalid','0x'+'1'*40)
        with patch('manual_rpc.asyncio.sleep',new_callable=AsyncMock):
            for _ in range(5):
                with self.assertRaises(ProviderUnavailable):
                    await rpc.call('eth_blockNumber',[])
        self.assertEqual(raw.post.call_count,3)
        self.assertTrue(session.paused)

    async def test_concurrent_reads_share_one_paced_gate(self):
        raw=Mock(post=Mock(return_value=Response({'result':'0x5'})))
        session=PacedSession(raw)
        rpc=ManualRpc(session,'https://example.invalid','0x'+'1'*40)
        with patch('manual_rpc.asyncio.sleep',new_callable=AsyncMock) as sleep:
            result=await asyncio.gather(*(rpc.call('eth_blockNumber',[]) for _ in range(20)))
        self.assertEqual(result,['0x5']*20)
        self.assertEqual(raw.post.call_count,20)
        self.assertGreaterEqual(sum(call.args[0]>0 for call in sleep.call_args_list),19)

    async def test_contract_revert_is_never_retried(self):
        raw=Mock(post=Mock(return_value=Response({'error':{'code':3,'message':'execution reverted'}})))
        rpc=ManualRpc(PacedSession(raw),'https://example.invalid','0x'+'1'*40)
        with self.assertRaisesRegex(RuntimeError,'RPC_REVERT_OR_REJECTED'):
            await rpc.call('eth_call',[])
        self.assertEqual(raw.post.call_count,1)

    async def test_signed_broadcast_retries_identical_bytes(self):
        from broadcast_rpc import sendRaw
        txHash='0x'+'a'*64
        raw=Mock(post=Mock(side_effect=[Response(None,429),Response({'result':txHash})]))
        rpc=ManualRpc(PacedSession(raw),'https://example.invalid','0x'+'1'*40)
        with patch('manual_rpc.asyncio.sleep',new_callable=AsyncMock):
            self.assertEqual(await sendRaw(rpc,'0x1234'),txHash)
        params=[call.kwargs['json']['params'] for call in raw.post.call_args_list]
        self.assertEqual(params,[['0x1234'],['0x1234']])

    async def test_null_response_and_503_recover(self):
        raw=Mock(post=Mock(side_effect=[Response(None),Response(None,503),Response({'result':'0x5'})]))
        rpc=ManualRpc(PacedSession(raw),'https://example.invalid','0x'+'1'*40)
        with patch('manual_rpc.asyncio.sleep',new_callable=AsyncMock):
            self.assertEqual(await rpc.call('eth_blockNumber',[]),'0x5')

    async def test_provider_outage_never_shrinks_sale_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            wallet='0x'+'2'*40
            token='0x'+'1'*40
            store=manual.initializeSweep(Path(directory)/'execution.sqlite3',wallet)
            store.db.execute('CREATE TABLE manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            adapter=Mock(directQuotes=AsyncMock(side_effect=RuntimeError('NO_ROUTE:RPC_HTTP_429')))
            with patch.object(manual,'tokenBalance',AsyncMock(return_value=1000)),patch.object(manual,'freshRate',AsyncMock(return_value=Decimal('2000'))):
                with self.assertRaises(ProviderUnavailable):
                    await manual.sellHolding(None,adapter,None,store,wallet,token,None)
            self.assertEqual(adapter.directQuotes.call_count,1)
            self.assertEqual(adapter.directQuotes.call_args.args[1],1000)
            store.db.close()

class ConsumerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_consumer_failure_after_response_is_not_retried(self):
        import aiohttp
        raw=Mock(post=Mock(return_value=Response({'result':'0x5'})))
        session=PacedSession(raw)
        with self.assertRaises(aiohttp.ClientError):
            async with session.post('https://example.invalid',json={}):
                raise aiohttp.ClientError('consumer failed')
        self.assertEqual(raw.post.call_count,1)

if __name__=='__main__':
    unittest.main()
