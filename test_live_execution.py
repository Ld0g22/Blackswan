"""Independent regression tests for real-execution state, no RPC or orders."""
import asyncio
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest

# Minimal import surface for running the pure logic without network SDKs.
# On the user's machine the real inherited peak_tracker module is used.
try:
    import peak_tracker
except ImportError:
    peak=types.ModuleType('peak_tracker')
    peak.NATIVE='0x'+'0'*40
    peak.WETH='0x'+'1'*40
    peak.USDG='0x'+'2'*40
    class NoQuotes: pass
    peak.Quotes=NoQuotes
    sys.modules['peak_tracker']=peak

from live_executor import ExecutionStore, checkSlice, nextInterval, receiptTransfers
from wallet_transactions import TransactionJournal


class FakeSignature:
    raw_transaction=b'\xf8\x10\x01'
    hash=b'\x01'*32

class FakeAccount:
    address='0x'+'a'*40
    def __init__(self):self.signed=[]
    def sign_transaction(self,tx):
        self.signed.append(tx.copy());return FakeSignature()

class FakeRpc:
    def __init__(self):self.mempool=[];self.receipt=None;self.calls=[];self.sendFails=0
    async def call(self,method,params):
        self.calls.append((method,params))
        if method=='eth_chainId':return hex(4663)
        if method=='eth_getCode':return '0x6000'
        if method=='eth_call':return '0x'
        if method=='eth_estimateGas':return hex(90000)
        if method=='eth_gasPrice':return hex(1000)
        if method=='eth_getBlockByNumber':return {'baseFeePerGas':hex(1000)}
        if method=='eth_maxPriorityFeePerGas':return hex(100)
        if method=='eth_getTransactionCount':return hex(8)
        if method=='eth_getBalance':return hex(10**19)
        if method=='eth_sendRawTransaction':
            self.mempool.append(params[0])
            if self.sendFails:
                self.sendFails-=1
                raise RuntimeError('response lost')
            return '0x'+'01'*32
        if method=='eth_getTransactionReceipt':return self.receipt
        raise AssertionError('Unexpected '+method)


class TestPolicy(unittest.TestCase):
    def test_fixed_ladder_and_no_wallet_drain(self):
        self.assertEqual(checkSlice(101,3,500),80)
        self.assertEqual(checkSlice(101,5,500),15)
        self.assertEqual(checkSlice(101,10,500,80,15),6)
        self.assertEqual(checkSlice(101,10,3,80,15),3)
        self.assertEqual(nextInterval(0,1799),3)
        self.assertEqual(nextInterval(0,1800),60)

    def test_campaign_reservations_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'execution.sqlite3'
            store=ExecutionStore(db)
            for index in range(20):
                store.db.execute(
                    'INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                    (str(index),'0x'+f'{index:040x}',float(index),'{}','QUEUED',None))
            self.assertEqual(store.reserved(),20)
            self.assertFalse(store.eligible())
            store.db.execute("UPDATE selections SET status='SKIPPED' WHERE selectionId='19'")
            self.assertTrue(store.eligible())
            store.db.close()
            other=ExecutionStore(db)
            self.assertEqual(other.reserved(),19)
            self.assertEqual(other.get('buyGbp'),'0.10')
            other.db.close()

    def test_wallet_transfer_counts_net_only(self):
        from unittest.mock import patch
        import sys
        # No keccak package needed for test; fake its returned topic selector.
        ethUtils=types.ModuleType('eth_utils')
        ethUtils.keccak=lambda **k: b'\x77'*32
        wallet='0x'+'a'*40; token='0x'+'b'*40
        topics=lambda fromAddress,toAddress:['0x'+'77'*32,
                    '0x'+'0'*24+fromAddress[2:], '0x'+'0'*24+toAddress[2:]]
        receipt={'logs':[{'address':token,'topics':topics('0x'+'c'*40,wallet),'data':hex(400)},
                         {'address':token,'topics':topics(wallet,'0x'+'d'*40),'data':hex(250)}]}
        with patch.dict(sys.modules,{'eth_utils':ethUtils}):
            self.assertEqual(receiptTransfers(receipt,token,wallet),150)


class TestJournal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ExecutionStore(Path(self.tmp.name)/'state.sqlite3')
        self.rpc=FakeRpc();self.account=FakeAccount()
        self.tx=TransactionJournal(self.store,self.rpc,self.account,b'secret')
        self.target='0x'+'f'*40

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    async def test_signed_persisted_and_broadcast_reuses_same_bytes(self):
        first=await self.tx.create('purchaseA','selectA','BUY',{'to':self.target,'data':'0x1234','value':100},100)
        self.assertEqual(first['state'],'SIGNED')
        self.assertEqual(len(self.account.signed),1)
        self.rpc.sendFails=1
        await self.tx.broadcast(first['intentId'])
        self.assertEqual(len(self.rpc.mempool),1)
        self.assertEqual(self.store.db.execute('SELECT state FROM transactions').fetchone()[0],'UNCERTAIN')
        self.assertEqual(self.rpc.mempool[0],first['raw'])
        # After lost response only identical raw transaction can be resent.
        self.store.db.execute("UPDATE transactions SET lastBroadcast=0 WHERE intentId='purchaseA'")
        await self.tx.reconcile()
        self.assertEqual(self.rpc.mempool[0],self.rpc.mempool[-1])
        self.assertEqual(len(self.account.signed),1)
        self.rpc.receipt={'status':'0x1','logs':[],'blockNumber':'0x1'}
        self.tx.lastCheck=0
        await self.tx.reconcile()
        self.assertEqual(self.tx.confirmed('purchaseA')['state'],'CONFIRMED')

    async def test_inflight_blocks_another_nonce_and_replay_is_idempotent(self):
        tx={'to':self.target,'data':'0x','value':0}
        await self.tx.create('A','selA','BUY',tx)
        with self.assertRaisesRegex(RuntimeError,'WALLET_TRANSACTION_IN_FLIGHT'):
            await self.tx.create('B','selB','BUY',tx)
        replay=await self.tx.create('A','selA','BUY',tx)
        self.assertEqual(replay['nonce'],8)
        self.assertEqual(len(self.account.signed),1)

    async def test_wrong_chain_never_signs(self):
        original=self.rpc.call
        async def bad(method,params):
            if method=='eth_chainId':return '0x1'
            return await original(method,params)
        self.rpc.call=bad
        with self.assertRaisesRegex(RuntimeError,'WRONG_CHAIN'):
            await self.tx.create('A','selA','BUY',{'to':self.target,'value':0})
        self.assertFalse(self.account.signed)


if __name__=='__main__':unittest.main()

class TestSelectorAndThresholdRegression(unittest.TestCase):
    def test_selector_journal_cursor_and_revocation(self):
        from live_executor import LiveExecutor
        import json
        from unittest.mock import patch
        from types import SimpleNamespace
        token='0x'+'1'*40
        venue=dict(id='0x'+'2'*64,source='uniswapV4',token0='0x'+'0'*40,token1=token,fee=500,spacing=10,hooks='0x'+'0'*40)
        payload=dict(interfaceVersion=1,selectionId='sel:one',tokenKey='4663:'+token,
                     decisionAt=100.0,event={'source':'uniswapV4'},venue=venue)
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'e.sqlite3')
            selector=sqlite3.connect(':memory:');selector.row_factory=sqlite3.Row
            selector.executescript('CREATE TABLE decisions(seq INTEGER,status TEXT,data TEXT);'
                                   'CREATE TABLE revocations(seq INTEGER,eventKey TEXT);')
            selector.execute('INSERT INTO decisions VALUES(?,?,?)',(1,'PASS',json.dumps(payload)))
            selector.execute('INSERT INTO decisions VALUES(?,?,?)',(2,'FAIL',json.dumps(payload)))
            selector.execute('INSERT INTO revocations VALUES(?,?)',(1,'sel:one'))
            class NoPonsDetector:
                def execute(self,query,args):return []
            engine=SimpleNamespace(store=store,selector=selector,detector=NoPonsDetector())
            engine.entryExclusion=lambda token,item: LiveExecutor.entryExclusion(engine,token,item)
            engine.ponsOrigin=lambda token: False
            with patch.object(sys.modules['peak_tracker'],'launchVenues',
                              lambda event:[(token,venue)],create=True):
                LiveExecutor.readSelections(engine)
                LiveExecutor.readSelections(engine)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM selections').fetchone()[0],1)
            self.assertEqual(store.get('selectionCursor'),'2')
            LiveExecutor.readRevocations(engine)
            self.assertEqual(store.db.execute('SELECT status FROM selections').fetchone()[0],'REVOKED')
            selector.close();store.db.close()

    def test_latched_threshold_survives_price_collapse(self):
        from live_executor import LiveExecutor
        from types import SimpleNamespace
        from decimal import Decimal
        token='0x'+'f'*40
        class FakeSwap:
            output=550000000000000
            async def refreshPons(self,*args,**kwargs):pass
            async def directQuotes(self,*args):return [{'output':self.output}]
        class FakeFx:
            async def get(self):return Decimal('1000')
        with tempfile.TemporaryDirectory() as temp:
            store=ExecutionStore(Path(temp)/'e.sqlite3')
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',('s',token,0,'{}','BOUGHT',None))
            store.db.execute('INSERT INTO positions (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue) VALUES(?,?,?,?,?,?,?)',('s',token,'100','123',0,0,0))
            swaps=FakeSwap()
            engine=SimpleNamespace(swaps=swaps,fx=FakeFx(),store=store)
            asyncio.run(LiveExecutor.checkPosition(engine,store.db.execute('SELECT * FROM positions').fetchone()))
            row=store.db.execute('SELECT * FROM positions').fetchone()
            self.assertEqual((row['trigger3'],row['trigger5'],row['trigger10']),(1,1,0))
            swaps.output=50000000000000
            asyncio.run(LiveExecutor.checkPosition(engine,row))
            row=store.db.execute('SELECT * FROM positions').fetchone()
            self.assertEqual((row['trigger3'],row['trigger5'],row['trigger10']),(1,1,0))
            store.db.close()
