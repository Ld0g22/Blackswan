"""Execution-only Pons V2 exclusion tests. No detector/selector mutation or funds."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_abi import encode
from live_executor import ExecutionStore, LiveExecutor
from swap_adapters import NATIVE, PONS_HOOK

TOKEN='0x'+'a'*40
OTHER='0x'+'b'*40
ZERO='0x'+'0'*40
TYPES=['address','address','address','address','address','uint256','uint24',
       'int24','uint16','bool','uint8','uint256','uint256','uint256','bool']


def selectorItem(source='uniswapV4',hooks=ZERO,selectionId='s1',token=TOKEN):
    venue=dict(id='0x'+'9'*64 if source=='uniswapV4' else '0x'+'8'*40,
               source=source,token0=min(token,NATIVE),token1=max(token,NATIVE))
    if source=='uniswapV4':venue['hooks']=hooks
    return dict(interfaceVersion=1,selectionId=selectionId,tokenKey='4663:'+token,
                decisionAt=time.time(),event={'source':source,'chainId':4663},
                venue=venue)


def makeEngine(store,knownOrigin=False):
    engine=object.__new__(LiveExecutor)
    engine.store=store
    engine.ponsOrigin=lambda token: knownOrigin
    return engine


class TestPonsExecutionExclusion(unittest.TestCase):
    def test_direct_pons_pass_skipped_without_consuming_only_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'execution.sqlite3'
            store=ExecutionStore(path)
            engine=makeEngine(store)
            pons=selectorItem('ponsV2')
            ordinary=selectorItem('uniswapV4',selectionId='s2',token=OTHER)
            LiveExecutor._readSelectionRow(engine,dict(status='PASS',data=json.dumps(pons)))
            result=store.db.execute('SELECT status,reason FROM selections WHERE selectionId=?',('s1',)).fetchone()
            self.assertEqual(result['status'],'SKIPPED_PONS')
            self.assertEqual(result['reason'],'PONS_V2_DIRECT')
            self.assertTrue(store.eligible())
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM venues').fetchone()[0],0)
            # Match the existing selector-qualified venue; do not need a live detector.
            with patch('live_executor.peak.launchVenues',return_value=[(OTHER,ordinary['venue'])]):
                LiveExecutor._readSelectionRow(engine,dict(status='PASS',data=json.dumps(ordinary)))
            self.assertEqual(store.db.execute('SELECT status FROM selections WHERE selectionId=?',('s2',)).fetchone()[0],'QUEUED')
            self.assertEqual(store.reserved(),1)
            self.assertTrue(store.eligible())
            store.db.close()
            store=ExecutionStore(path)
            self.assertEqual(store.get('maxPurchases'),'20')
            self.assertEqual(store.reserved(),1)
            self.assertTrue(store.eligible())
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)
            store.db.close()

    def test_pons_graduation_hook_skips_v4_even_if_selector_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'execution.sqlite3')
            engine=makeEngine(store)
            item=selectorItem('uniswapV4',PONS_HOOK.upper())
            LiveExecutor._readSelectionRow(engine,dict(status='PASS',data=json.dumps(item)))
            self.assertEqual(store.db.execute('SELECT status FROM selections').fetchone()[0],'SKIPPED_PONS')
            self.assertTrue(store.eligible())
            store.db.close()

    def test_old_pons_origin_excluded_even_in_normal_v3_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'execution.sqlite3')
            engine=makeEngine(store,knownOrigin=True)
            item=selectorItem('uniswapV3')
            LiveExecutor._readSelectionRow(engine,dict(status='PASS',data=json.dumps(item)))
            self.assertEqual(store.db.execute('SELECT reason FROM selections').fetchone()[0],
                             'PONS_V2_DETECTOR_ORIGIN')
            self.assertTrue(store.eligible())
            store.db.close()

    def test_unknown_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'execution.sqlite3')
            engine=makeEngine(store)
            item=selectorItem()
            del item['venue']['source']
            LiveExecutor._readSelectionRow(engine,dict(status='PASS',data=json.dumps(item)))
            self.assertEqual(store.db.execute('SELECT status FROM selections').fetchone()[0],
                             'SKIPPED_UNSUPPORTED')
            store.db.close()


class TestOnChainPonsOrigin(unittest.IsolatedAsyncioTestCase):
    async def test_factory_exists_and_nonexistent_and_network_error(self):
        class FakeRpc:
            def __init__(self):self.response=None
            async def ethCall(self,*args):
                if isinstance(self.response,Exception):raise self.response
                return self.response
        rpc=FakeRpc()
        engine=object.__new__(LiveExecutor)
        engine.rpc=rpc
        make=lambda exists: '0x'+encode(TYPES,(TOKEN,ZERO,ZERO,ZERO,ZERO,0,0,0,0,False,0,0,0,0,exists)).hex()
        rpc.response=make(True)
        self.assertEqual(await engine.ponsOnChain(TOKEN),'PONS_V2_FACTORY_ORIGIN')
        rpc.response=make(False)
        self.assertIsNone(await engine.ponsOnChain(TOKEN))
        rpc.response=RuntimeError('RPC unavailable')
        self.assertEqual(await engine.ponsOnChain(TOKEN),'PONS_V2_ORIGIN_UNVERIFIED')
        rpc.response='0x'
        self.assertEqual(await engine.ponsOnChain(TOKEN),'PONS_V2_ORIGIN_UNVERIFIED')

    async def test_pre_sign_factory_denial_blocks_purchase_and_frees_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'execution.sqlite3')
            item=selectorItem()
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('s1',TOKEN,time.time(),json.dumps(item),'QUEUED',None))
            engine=object.__new__(LiveExecutor)
            engine.store=store
            engine.entryExclusion=lambda token,item:None
            async def factoryDeny(token):return 'PONS_V2_FACTORY_ORIGIN'
            engine.ponsOnChain=factoryDeny
            class QuoteSource:
                async def verifiedBuyCandidates(self,token,amount):
                    return [dict(output=123,venue={'source':'uniswapV4'},tokenIn=NATIVE)]
            class Fx:
                async def get(self):
                    from decimal import Decimal
                    return Decimal(1000)
            class NeverSign:
                account=SimpleNamespace(address=OTHER)
                async def create(self,*args):
                    raise AssertionError('Trade must not be signed')
            engine.swaps=QuoteSource()
            engine.fx=Fx()
            engine.tx=NeverSign()
            await LiveExecutor.buy(engine,store.db.execute('SELECT * FROM selections').fetchone())
            self.assertEqual(store.db.execute('SELECT status FROM selections').fetchone()[0],
                             'SKIPPED_PONS')
            self.assertTrue(store.eligible())
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)
            store.db.close()


if __name__=='__main__':
    unittest.main()
