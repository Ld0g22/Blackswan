"""Two-way entry gate regression: don't sign a token that cannot be quoted out.

All tests use only fake RPC / SQLite and never broadcast.
"""
import asyncio
import json
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from live_executor import ExecutionStore, LiveExecutor
from swap_adapters import SwapAdapters, NATIVE, WETH, USDG


TOKEN='0x'+'a'*40
WALLET='0x'+'b'*40
VENUE=dict(source='uniswapV4',id='0x'+'f'*64,
           token0=NATIVE,token1=TOKEN,fee=10000,spacing=200,
           hooks=NATIVE)


class TestBuyOnlyPoolsAreExcluded(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.temp.name)/'execution.sqlite3')
        self.store.setVenue(TOKEN,VENUE,True)
        self.rpc=SimpleNamespace()
        async def block():return '0x101'
        self.rpc.block=block
        self.swaps=SwapAdapters(self.rpc,None,self.store)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_buy_quote_good_sell_quote_reverted_no_route(self):
        seen=[]
        class Quotes:
            async def quote(self,venue,tokenIn,amount,block):
                seen.append((tokenIn,amount,block))
                if tokenIn==TOKEN:
                    raise RuntimeError('UnexpectedRevertBytes: no sell')
                return dict(out=123456,spent=amount,basis='V4_QUOTER')
        self.swaps.quoting=Quotes()
        with self.assertRaisesRegex(RuntimeError,'NO_TWO_WAY_EXIT_QUOTE'):
            await self.swaps.verifiedBuyCandidates(TOKEN,200)
        self.assertEqual(seen,[(NATIVE,200,'0x101'),(TOKEN,123456,'0x101')])

    async def test_both_directions_valid_same_block(self):
        seen=[]
        class Quotes:
            async def quote(self,venue,tokenIn,amount,block):
                seen.append((tokenIn,amount,block))
                return dict(out=555 if tokenIn==NATIVE else 444,
                            spent=amount,basis='V4_QUOTER')
        self.swaps.quoting=Quotes()
        rows=await self.swaps.verifiedBuyCandidates(TOKEN,200)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['preBuySellOutput'],444)
        self.assertEqual(rows[0]['preBuySellBlock'],'0x101')
        self.assertEqual(seen,[(NATIVE,200,'0x101'),(TOKEN,555,'0x101')])

    async def test_executor_never_signs_when_reverse_quote_reverts(self):
        item=dict(interfaceVersion=1,selectionId='selected',
                  tokenKey='4663:'+TOKEN,decisionAt=time.time(),
                  event=dict(source='uniswapV4'),
                  venue=VENUE)
        self.store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                              ('selected',TOKEN,item['decisionAt'],json.dumps(item),'QUEUED',None))
        class FakeFX:
            async def get(self):return Decimal('1000')
        class Quotes:
            async def quote(self,venue,tokenIn,amount,block):
                if tokenIn==TOKEN:raise RuntimeError('sell impossible')
                return dict(out=555,spent=amount,basis='V4_QUOTER')
        self.swaps.quoting=Quotes()
        class NeverSign:
            account=SimpleNamespace(address=WALLET)
            async def create(self,*args,**kwargs):
                raise AssertionError('MUST NOT SIGN OR SEND')
        engine=object.__new__(LiveExecutor)
        engine.store=self.store
        engine.entryExclusion=lambda token,item:None
        engine.fx=FakeFX()
        engine.swaps=self.swaps
        engine.tx=NeverSign()
        await LiveExecutor.buy(engine,self.store.db.execute('SELECT * FROM selections').fetchone())
        row=self.store.db.execute('SELECT status,reason FROM selections').fetchone()
        self.assertEqual(row['status'],'SKIPPED')
        self.assertIn('NO_TWO_WAY_EXIT_QUOTE',row['reason'])
        self.assertEqual(self.store.acquired(),0)
        self.assertEqual(self.store.reserved(),0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)
