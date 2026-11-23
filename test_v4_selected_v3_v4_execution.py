"""Parity regressions: v4 PASS selection, unrestricted verified v3/v4 trading.

Synthetic detector events and quote sources only. No RPC, signing or funds.
"""
import asyncio
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from eth_abi import decode
from eth_utils import keccak

import peak_tracker as peak
from live_executor import ExecutionStore,LiveExecutor
from swap_adapters import (
    SwapAdapters, buildV3, NATIVE, WETH, USDG, V3_ROUTER, PONS_HOOK)

TOKEN='0x'+'b'*40
WALLET='0x'+'c'*40
POOLV3='0x'+'d'*40
POOLV4='0x'+'e'*64


def launchV3(token=TOKEN,removed=False):
    return {'chainId':4663,'source':'uniswapV3','removed':removed,
            'details':{'pool':POOLV3,'token0':min(WETH,token),
                       'token1':max(WETH,token),'fee':500}}


def launchV4(token=TOKEN):
    return {'chainId':4663,'source':'uniswapV4','removed':False,
            'details':{'poolId':POOLV4,'currency0':min(NATIVE,token),
                       'currency1':max(NATIVE,token),'fee':500,
                       'tickSpacing':10,'hooks':NATIVE}}


def selected(token=TOKEN):
    event=launchV4(token)
    venue=peak.launchVenues(event)[0][1]
    return {'interfaceVersion':1,'selectionId':'selected-v4',
            'tokenKey':'4663:'+token,'decisionAt':time.time(),
            'event':event,'venue':venue}


class TestVenueHistoryRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.tmp.name)/'execution.sqlite3')
        self.detector=sqlite3.connect(':memory:')
        self.detector.row_factory=sqlite3.Row
        self.detector.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)')
        self.e=object.__new__(LiveExecutor)
        self.e.store=self.store;self.e.detector=self.detector
        self.e.ponsOrigin=lambda token:False

    def tearDown(self):
        self.detector.close()
        self.store.db.close()
        self.tmp.cleanup()

    def event(self,n,raw):
        self.detector.execute('INSERT INTO event_log VALUES(?,?,?)',
                              (n,raw['source'],json.dumps(raw)))

    def test_v3_pool_created_before_v4_pass_survives_advanced_detector_cursor(self):
        self.event(1,launchV3())
        self.event(2,launchV4())
        self.store.set('detectorCursor',2)
        self.e._readSelectionRow(dict(status='PASS',data=json.dumps(selected())))
        rows={r['source'] for r in self.store.venues(TOKEN)}
        self.assertEqual(rows,{'uniswapV3','uniswapV4'})
        self.assertEqual(self.store.db.execute('SELECT status FROM selections').fetchone()[0],'QUEUED')
        self.assertEqual(self.store.reserved(),1)

    def test_removed_v3_pool_is_not_executable(self):
        self.event(1,launchV3())
        self.event(2,launchV3(removed=True))
        self.event(3,launchV4())
        self.e._readSelectionRow(dict(status='PASS',data=json.dumps(selected())))
        self.assertEqual([v['source'] for v in self.store.venues(TOKEN)],['uniswapV4'])

    def test_new_v3_pool_visible_before_buy_is_confirmed(self):
        self.event(1,launchV4())
        self.e._readSelectionRow(dict(status='PASS',data=json.dumps(selected())))
        self.store.set('detectorCursor',1)
        self.event(2,launchV3())
        self.e.readNewVenues()
        self.assertEqual({x['source'] for x in self.store.venues(TOKEN)},
                         {'uniswapV3','uniswapV4'})

    def test_v3_launch_pass_still_not_purchase_eligible(self):
        event=launchV3()
        v3=peak.launchVenues(event)[0][1]
        item=selected()
        item.update(event=event,venue=v3,selectionId='v3-pass')
        self.e._readSelectionRow(dict(status='PASS',data=json.dumps(item)))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM selections').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM excluded_passes').fetchone()[0],1)
        self.assertEqual(self.store.reserved(),0)


class TestBestExecutionRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.tmp.name)/'execution.sqlite3')
        self.v3=peak.launchVenues(launchV3())[0][1]
        self.v4=peak.launchVenues(launchV4())[0][1]
        self.store.setVenue(TOKEN,self.v4,True)
        self.store.setVenue(TOKEN,self.v3,True)
        self.rpc=SimpleNamespace(block=self.block)
        self.swaps=SwapAdapters(self.rpc,None,self.store)
        self.swaps.allowedExecutionSources=frozenset(('uniswapV3','uniswapV4'))

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    async def block(self):
        return '0x1234'

    async def test_buy_quotes_v3_weth_not_native_and_picks_best(self):
        calls=[]
        class Quoter:
            async def quote(self,venue,tokenIn,amount,block):
                calls.append((venue['source'],tokenIn,block))
                self_source=venue['source']
                return {'out':200 if self_source=='uniswapV3' else 100,
                        'spent':amount,'basis':'QUOTER'}
        self.swaps.quoting=Quoter()
        quotes=await self.swaps.directQuotes(TOKEN,100,True)
        self.assertEqual(quotes[0]['venue']['source'],'uniswapV3')
        self.assertEqual(quotes[0]['tokenIn'],NATIVE) # ETH funded on Router02
        self.assertIn(('uniswapV3',WETH,'0x1234'),calls) # correct Quoter tokenIn
        built=self.swaps.build(quotes[0],100,WALLET)
        self.assertEqual(built['to'],V3_ROUTER)
        self.assertEqual(built['value'],100)

    async def test_v4_buy_can_use_v3_exit_when_v4_reverse_reverts(self):
        calls=[]
        class Quoter:
            async def quote(self,venue,tokenIn,amount,block):
                calls.append((venue['source'],tokenIn,amount,block))
                if tokenIn in (NATIVE,WETH):
                    if venue['source']=='uniswapV3':
                        raise RuntimeError('no current v3 buy')
                    return {'out':200,'spent':amount,'basis':'QUOTER'}
                if venue['source']=='uniswapV4':
                    raise RuntimeError('v4 reverse unavailable')
                return {'out':180,'spent':amount,'basis':'QUOTER'}
        self.swaps.quoting=Quoter()
        quote=(await self.swaps.verifiedBuyCandidates(TOKEN,100))[0]
        self.assertEqual(quote['venue']['source'],'uniswapV4')
        self.assertEqual(quote['preBuySellSource'],'uniswapV3')
        self.assertEqual(quote['preBuySellOutput'],180)
        self.assertEqual({x[3] for x in calls},{'0x1234'})

    async def test_v3_usdg_two_hop_quote_and_route_sends_weth_on_sale(self):
        usdPool={'source':'uniswapV3','id':'0x'+'3'*40,
                 'token0':min(USDG,TOKEN),'token1':max(USDG,TOKEN),'fee':3000}
        self.store.setVenue(TOKEN,usdPool,True)
        calls=[]
        class Quoter:
            async def quote(self,venue,tokenIn,amount,block):
                calls.append((venue['source'],venue['id'],tokenIn,amount,block))
                if venue['id']=='weth-usdg-v3-500':
                    return {'out':80,'spent':amount,'basis':'V3_QUOTER'}
                if venue['id']==usdPool['id']:
                    return {'out':70 if tokenIn==USDG else 65,
                            'spent':amount,'basis':'V3_QUOTER'}
                raise RuntimeError('other pool unavailable')
        self.swaps.quoting=Quoter()
        buys=await self.swaps.directQuotes(TOKEN,100,True)
        self.assertEqual(len(buys),1)
        self.assertEqual((buys[0]['basis'],buys[0]['tokenIn'],buys[0]['output']),
                         ('V3_TWO_HOP',NATIVE,70))
        buyTx=self.swaps.build(buys[0],100,WALLET)
        self.assertEqual((buyTx['to'],buyTx['value']),(V3_ROUTER,100))
        sells=await self.swaps.directQuotes(TOKEN,100,False)
        self.assertEqual(len(sells),1)
        self.assertEqual((sells[0]['basis'],sells[0]['output']),('V3_TWO_HOP',80))
        saleTx=self.swaps.build(sells[0],100,WALLET)
        self.assertEqual((saleTx['to'],saleTx['value']),(V3_ROUTER,0))
        self.assertEqual(saleTx['data'][:10],
                         '0x'+keccak(text='exactInput((bytes,address,uint256,uint256))')[:4].hex())
        self.assertEqual({c[4] for c in calls},{'0x1234'})

    async def test_v3_is_allowed_for_selling_but_pons_curve_not_routed(self):
        pons={'source':'ponsV2','id':'0x'+'1'*40,'token0':TOKEN,'token1':NATIVE}
        self.store.setVenue(TOKEN,pons,True)
        class Quoter:
            async def quote(self,venue,tokenIn,amount,block):
                if venue['source']=='ponsV2':
                    raise AssertionError('Pons V2 route must be excluded')
                return {'out':900 if venue['source']=='uniswapV3' else 800,
                        'spent':amount,'basis':'QUOTER'}
        self.swaps.quoting=Quoter()
        quotes=await self.swaps.directQuotes(TOKEN,100,False)
        self.assertEqual([x['venue']['source'] for x in quotes],
                         ['uniswapV3','uniswapV4'])


class TestV3AtomicRouteAbi(unittest.TestCase):
    def test_exact_input_multihop_native_to_usdg_to_token(self):
        venue={'source':'uniswapV3','fee':3000,'token0':USDG,'token1':TOKEN}
        path=[(WETH,500),(USDG,3000),(TOKEN,None)]
        tx=buildV3(venue,NATIVE,1000,700,WALLET,path)
        self.assertEqual(tx['to'],V3_ROUTER)
        self.assertEqual(tx['value'],1000)
        selector='0x'+keccak(text='exactInput((bytes,address,uint256,uint256))')[:4].hex()
        self.assertEqual(tx['data'][:10],selector)
        packed,wallet,amount,minimum=decode(['(bytes,address,uint256,uint256)'],
                                             bytes.fromhex(tx['data'][10:]))[0]
        self.assertEqual((wallet.lower(),amount,minimum),(WALLET,1000,700))
        expected=bytes.fromhex(WETH[2:])+int(500).to_bytes(3,'big')+bytes.fromhex(USDG[2:])+int(3000).to_bytes(3,'big')+bytes.fromhex(TOKEN[2:])
        self.assertEqual(packed,expected)

    def test_exact_input_multihop_token_to_usdg_to_weth(self):
        venue={'source':'uniswapV3','fee':3000,'token0':USDG,'token1':TOKEN}
        path=[(TOKEN,3000),(USDG,500),(WETH,None)]
        tx=buildV3(venue,TOKEN,400,200,WALLET,path)
        self.assertEqual(tx['value'],0)
        packed,wallet,amount,minimum=decode(['(bytes,address,uint256,uint256)'],
                                             bytes.fromhex(tx['data'][10:]))[0]
        expected=bytes.fromhex(TOKEN[2:])+int(3000).to_bytes(3,'big')+bytes.fromhex(USDG[2:])+int(500).to_bytes(3,'big')+bytes.fromhex(WETH[2:])
        self.assertEqual(packed,expected)
        self.assertEqual((amount,minimum),(400,200))


if __name__=='__main__':
    unittest.main()
