"""Preserved purchase-signing and staged-exit regressions for twenty canaries. No broadcasts."""
import asyncio
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eth_account import Account
import rlp
from types import SimpleNamespace
from live_executor import ExecutionStore, LiveExecutor, PURCHASES, STAGES, checkSlice
from wallet_transactions import TransactionJournal
from test_live_execution import FakeRpc


class TestSmokePolicy(unittest.TestCase):
    def test_twenty_canaries_and_original_staged_ladder(self):
        self.assertEqual(PURCHASES, 20)
        self.assertEqual(STAGES, ((3,80),(5,15),(10,5)))
        self.assertEqual(checkSlice(1000,3,1000),800)
        self.assertEqual(checkSlice(1000,5,200),150)
        self.assertEqual(checkSlice(1000,10,50,800,150),50)

    def test_one_reserved_purchase_blocks_additional_buys_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'execution.sqlite3'
            store=ExecutionStore(path)
            for index in range(20):
                store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                                 (f's{index}','0x'+f'{index+1:040x}',0,'{}','BUY_PENDING',None))
            self.assertFalse(store.eligible())
            store.db.close()
            store=ExecutionStore(path)
            self.assertEqual(store.get('maxPurchases'),'20')
            self.assertFalse(store.eligible())
            store.db.close()

    def test_threshold_trigger_only_by_quote_and_latched_after_restart(self):
        from decimal import Decimal
        class FakeSwaps:
            output=350000000000000  # 0.35 GBP at 1000 GBP/ETH: 3.5x a 10p entry
            async def refreshPons(self,*args,**kwargs):pass
            async def directQuotes(self,*args,**kwargs):
                return [{'output':self.output}]
        class FakeFX:
            async def get(self):return Decimal('1000')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'execution.sqlite3'
            store=ExecutionStore(path)
            token='0x'+'2'*40
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('s1',token,1000,'{}','BOUGHT',None))
            store.db.execute('''INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
                VALUES(?,?,?,?,?,?,?)''',('s1',token,'1000','100',1000,1000,1000))
            engine=object.__new__(LiveExecutor)
            engine.store=store;engine.swaps=FakeSwaps();engine.fx=FakeFX()
            with patch('live_executor.time.time',return_value=2000):
                asyncio.run(engine.checkPosition(
                    store.db.execute('SELECT * FROM positions').fetchone()))
            self.assertEqual(store.db.execute('SELECT trigger3 FROM positions').fetchone()[0],1)
            store.db.close()
            store=ExecutionStore(path)
            engine.store=store
            engine.swaps.output=50000000000000  # 0.05 GBP: below entry, retain latched trigger
            with patch('live_executor.time.time',return_value=2001):
                asyncio.run(engine.checkPosition(
                    store.db.execute('SELECT * FROM positions').fetchone()))
            self.assertEqual(store.db.execute('SELECT trigger3 FROM positions').fetchone()[0],1)
            self.assertEqual(store.db.execute('SELECT trigger5 FROM positions').fetchone()[0],0)
            store.db.close()


    def test_at_latched_ten_x_sell_remaining_balance_without_rechecking_price(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite3')
            token='0x'+'3'*40
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('s1',token,1000,'{}','BOUGHT',None))
            store.db.execute('''INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue,trigger10)
                VALUES(?,?,?,?,?,?,?,?)''',('s1',token,'175','100',1000,1000,1060,1))
            captured=[]
            class FakeSwaps:
                async def directQuotes(self,token,amount,buy):
                    self.assertFalse(buy)
                    return [dict(output=1000,tokenIn=token,venue={'source':'uniswapV3'})]
                async def approvalNeeded(self,*args):
                    return None
                def build(self,quote,amount,wallet):
                    return {'to':'0x'+'f'*40,'data':'0x','value':0}
            class FakeTx:
                account=SimpleNamespace(address='0x'+'a'*40)
                async def create(self,intent,selectionId,kind,tx,amount=0):
                    captured.append((intent,kind,amount))
                    return {'intentId':intent,'txHash':'0x'+'4'*64}
                async def broadcast(self,intent):
                    captured.append(('broadcast',intent))
            engine=object.__new__(LiveExecutor)
            engine.store=store
            engine.swaps=FakeSwaps()
            engine.swaps.assertFalse=self.assertFalse
            engine.tx=FakeTx()
            async def balance(token):return 175
            engine.balance=balance
            position=store.db.execute('SELECT * FROM positions').fetchone()
            asyncio.run(engine.prepareSell(position))
            self.assertEqual(captured[0][1:3],('SELL_10',175))
            self.assertEqual(captured[1][0],'broadcast')
            store.db.close()


class TestActualEthAccountSigning(unittest.IsolatedAsyncioTestCase):
    async def test_lowercase_contract_recipient_is_checksummed_before_real_sign(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ExecutionStore(Path(directory)/'execution.sqlite3')
            rpc=FakeRpc()
            account=Account.from_key(bytes.fromhex('11'*32))
            journal=TransactionJournal(store,rpc,account,bytes.fromhex('11'*32))
            to='0x204faca1764b154221e35c0d20abb3c525710498'
            pending=await journal.create('smoke:one','s1','BUY',
                                         {'to':to,'data':'0x','value':123},123)
            self.assertEqual(pending['state'],'SIGNED')
            self.assertTrue(pending['raw'].startswith('0x'))
            self.assertEqual(len(pending['txHash']),66)
            self.assertEqual(len(rpc.mempool),0)
            raw=bytes.fromhex(pending['raw'][2:])
            self.assertEqual(Account.recover_transaction(raw),account.address)
            self.assertEqual(raw[0],2)  # EIP-1559 typed transaction
            fields=rlp.decode(raw[1:])
            self.assertEqual(fields[5],bytes.fromhex(to[2:]))
            store.db.close()


if __name__=='__main__':
    unittest.main()
