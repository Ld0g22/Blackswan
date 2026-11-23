"""Proof that LIVE quotes use unchanged prospective Tracker.best, not new math.

Offline only. No user wallet, keys, network calls, signing or broadcasts.
"""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import peak_tracker as peak
from execution_peak_routes import PeakExecutionRoutes
from live_executor import ExecutionStore,LiveExecutor,PURCHASES,BUY_GBP,STAGES
from swap_adapters import NATIVE,WETH,USDG,PONS_HOOK

TOKEN='0x'+'a'*40
WALLET='0x'+'b'*40
V3=dict(id='0x'+'c'*40,source='uniswapV3',
        token0=min(WETH,TOKEN),token1=max(WETH,TOKEN),fee=500)
V4=dict(id='0x'+'d'*64,source='uniswapV4',
        token0=min(NATIVE,TOKEN),token1=max(NATIVE,TOKEN),
        fee=500,spacing=10,hooks=NATIVE)


class StubRpc:
    wallet=WALLET
    def __init__(self):
        self.blockCalls=0
    async def block(self):
        self.blockCalls+=1
        return '0x123'


class TestOriginalPeakParity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3')
        self.store.setVenue(TOKEN,V3,True)
        self.store.setVenue(TOKEN,V4,True)
        self.rpc=StubRpc()
        self.routes=PeakExecutionRoutes(self.rpc,None,self.store)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_selection_is_original_tracker_best_not_adapter_rankings(self):
        calls=[]
        async def originalBest(context,token,amount,buy):
            calls.append((token,amount,buy,await context.rpc.block()))
            self.assertEqual({v['source'] for v in context.store.venues(token)},
                             {'uniswapV3','uniswapV4'})
            return dict(venue=V3['id'],out=12345,spent=amount,
                        basis='V3_QUOTER',block=await context.rpc.block(),
                        routesChecked=2)
        with patch.object(peak.Tracker,'best',originalBest):
            # If the old independently ranked adapter were called, this
            # patch would abort; only original Tracker.best is allowed.
            with patch.object(self.routes.execution,'directQuotes',
                              side_effect=AssertionError('duplicate peak tracker must not run')):
                quoted=(await self.routes.directQuotes(TOKEN,10**14,True))[0]
        self.assertEqual(calls,[(TOKEN,10**14,True,'0x123')])
        self.assertEqual(quoted['output'],12345)
        self.assertEqual(quoted['peakRoute'],V3['id'])
        self.assertEqual(quoted['venue']['source'],'uniswapV3')
        self.assertEqual(quoted['tokenIn'],NATIVE)  # ETH-funded v3 WETH
        self.assertEqual(quoted['block'],'0x123')
        self.assertEqual(self.rpc.blockCalls,1)

    async def test_fails_closed_on_best_mixed_v3_v4_bridge(self):
        pool=dict(id='0x'+'e'*64,source='uniswapV4',
                  token0=min(USDG,TOKEN),token1=max(USDG,TOKEN),
                  fee=500,spacing=10,hooks=NATIVE)
        self.store.setVenue(TOKEN,pool,True)
        async def originalBest(context,token,amount,buy):
            return dict(venue='weth-usdg-v3-500>'+pool['id'],
                        out=456,spent=amount,basis='V3_QUOTER>V4_QUOTER',
                        block='0x123',routesChecked=4)
        with patch.object(peak.Tracker,'best',originalBest):
            with self.assertRaisesRegex(RuntimeError,'PEAK_ROUTE_NOT_ATOMIC_MIXED_PROTOCOL'):
                await self.routes.directQuotes(TOKEN,100,True)

    async def test_original_v4_usdg_route_converts_to_known_atomic_path(self):
        pool=dict(id='0x'+'e'*64,source='uniswapV4',
                  token0=min(USDG,TOKEN),token1=max(USDG,TOKEN),
                  fee=500,spacing=10,hooks=NATIVE)
        self.store.setVenue(TOKEN,pool,True)
        async def originalBest(context,token,amount,buy):
            bridge='eth-usdg-v4-500-10-nohook'
            return dict(venue=(bridge+'>'+pool['id'] if buy
                               else pool['id']+'>'+bridge),
                        out=789,spent=amount,basis='V4_QUOTER>V4_QUOTER',
                        block='0x123',routesChecked=1)
        with patch.object(peak.Tracker,'best',originalBest):
            buy=(await self.routes.directQuotes(TOKEN,100,True))[0]
            sell=(await self.routes.directQuotes(TOKEN,200,False))[0]
        self.assertEqual(buy['venue']['id'],pool['id'])
        self.assertEqual(buy['tokenIn'],NATIVE)
        self.assertEqual([part[0] for part in buy['path']],[USDG,TOKEN])
        self.assertEqual([part[0] for part in sell['path']],[USDG,NATIVE])
        self.assertEqual(sell['tokenIn'],TOKEN)

    async def test_original_v3_usdg_route_converts_to_known_atomic_path(self):
        pool=dict(id='0x'+'e'*40,source='uniswapV3',
                  token0=min(USDG,TOKEN),token1=max(USDG,TOKEN),fee=3000)
        self.store.setVenue(TOKEN,pool,True)
        async def originalBest(context,token,amount,buy):
            bridge='weth-usdg-v3-500'
            return dict(venue=(bridge+'>'+pool['id'] if buy
                               else pool['id']+'>'+bridge),
                        out=500,spent=amount,basis='V3_QUOTER>V3_QUOTER',
                        block='0x123',routesChecked=1)
        with patch.object(peak.Tracker,'best',originalBest):
            buy=(await self.routes.directQuotes(TOKEN,100,True))[0]
            sell=(await self.routes.directQuotes(TOKEN,200,False))[0]
        self.assertEqual([p[0] for p in buy['path']],[WETH,USDG,TOKEN])
        self.assertEqual([p[0] for p in sell['path']],[TOKEN,USDG,WETH])
        self.assertEqual(buy['tokenIn'],NATIVE)
        self.assertEqual(sell['tokenIn'],TOKEN)

    async def test_same_block_reverse_quote_can_use_different_peak_venue(self):
        calls=[]
        async def originalBest(context,token,amount,buy):
            block=await context.rpc.block()
            calls.append((amount,buy,block))
            return dict(venue=V4['id'] if buy else V3['id'],
                        out=888 if buy else 800,spent=amount,
                        basis='V4_QUOTER' if buy else 'V3_QUOTER',
                        block=block,routesChecked=2)
        built=[]
        def fakeBuild(quote,amount,wallet):
            built.append((quote['venue']['source'],amount,wallet))
            return {'to':wallet,'data':'0x','value':0}
        self.routes.execution.build=fakeBuild
        with patch.object(peak.Tracker,'best',originalBest):
            result=(await self.routes.verifiedBuyCandidates(TOKEN,100))[0]
        self.assertEqual(calls,[(100,True,'0x123'),(888,False,'0x123')])
        self.assertEqual(result['venue']['source'],'uniswapV4')
        self.assertEqual(result['preBuySellSource'],'uniswapV3')
        self.assertEqual(result['preBuySellOutput'],800)
        self.assertEqual(built,[('uniswapV4',100,WALLET),('uniswapV3',888,WALLET)])
        self.assertEqual(self.rpc.blockCalls,1)

    async def test_pons_hooks_not_passed_to_original_peak(self):
        blocked=dict(V4,hooks=PONS_HOOK,id='0x'+'f'*64)
        self.store.setVenue(TOKEN,blocked,True)
        async def originalBest(context,token,amount,buy):
            ids={v['id'] for v in context.store.venues(token)}
            self.assertNotIn(blocked['id'],ids)
            return dict(venue=V4['id'],out=50,spent=amount,basis='V4_QUOTER',
                        block='0x123',routesChecked=2)
        with patch.object(peak.Tracker,'best',originalBest):
            await self.routes.directQuotes(TOKEN,100,True)


class TestActualQuantityAndQuoteJournal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3')
        self.store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                              ('s',TOKEN,time.time(),'{}','BOUGHT',None))
        self.store.db.execute('''INSERT INTO positions
             (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
             VALUES(?,?,?,?,?,?,?)''',
             ('s',TOKEN,'800','123',time.time(),time.time(),time.time()))
        self.engine=SimpleNamespace(store=self.store)
        self.engine.fx=SimpleNamespace(get=self.exchangeRate)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def exchangeRate(self):
        return Decimal('1000')

    async def test_full_actual_fill_quantity_uses_original_peak_and_latches(self):
        received=[]
        class MockQuotes:
            async def refreshPons(self,*args,**kwargs):pass
            async def directQuotes(self,token,amount,buy):
                received.append((token,amount,buy))
                return [dict(output=400000000000000,block='0x44',
                             basis='V3_QUOTER',peakRoute=V3['id'])]
        self.engine.swaps=MockQuotes()
        row=self.store.db.execute('SELECT * FROM positions').fetchone()
        await LiveExecutor.checkPosition(self.engine,row)
        self.assertEqual(received,[(TOKEN,800,False)])
        position=self.store.db.execute('SELECT * FROM positions').fetchone()
        self.assertEqual((position['trigger3'],position['trigger5'],position['trigger10']),
                         (1,0,0))
        obs=dict(self.store.db.execute('SELECT * FROM observations WHERE phase="SELL"').fetchone())
        self.assertEqual(obs['quantity'],'800')
        self.assertEqual(obs['multiple'],'4')
        self.assertEqual(obs['venue_id'],V3['id'])
        self.assertEqual(obs['status'],'ONCHAIN_QUOTE_ONLY')
        self.assertEqual(PURCHASES,20)
        self.assertEqual(BUY_GBP,Decimal('0.10'))
        self.assertEqual(STAGES,((3,80),(5,15),(10,5)))

    async def test_quote_failure_is_retained_not_disguised_as_good_check(self):
        class Broken:
            async def refreshPons(self,*args,**kwargs):pass
            async def directQuotes(self,*args,**kwargs):
                raise RuntimeError('NO_EXECUTABLE_QUOTE:old pool reverted')
        self.engine.swaps=Broken()
        await LiveExecutor.checkPosition(
            self.engine,self.store.db.execute('SELECT * FROM positions').fetchone())
        obs=dict(self.store.db.execute('SELECT * FROM observations').fetchone())
        self.assertEqual(obs['status'],'UNAVAILABLE')
        self.assertIn('reverted',obs['reason'])
        self.assertIsNone(obs['out_wei'])


if __name__=='__main__':
    unittest.main()
