"""V4 staged sale execution invariants. No wallet keys, RPC, or broadcasts."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from eth_abi import decode
from eth_utils import keccak

from live_executor import ExecutionStore,LiveExecutor
from swap_adapters import SwapAdapters,NATIVE,PERMIT2,V4_ROUTER,calldata

TOKEN='0x'+'a'*40
WALLET='0x'+'b'*40
VENUE=dict(source='uniswapV4',id='0x'+'c'*64,token0=NATIVE,
           token1=TOKEN,fee=500,spacing=10,hooks=NATIVE)


class TestRealApprovalLogic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter=object.__new__(SwapAdapters)
        self.quote={'venue':VENUE,'tokenIn':TOKEN,'output':500}

    async def test_erc20_then_permit2_then_none(self):
        async def noAllowance(*args):return 0
        async def noPermit(*args):return (0,0,0)
        self.adapter.allowance=noAllowance
        self.adapter.permitAllowance=noPermit
        first=await self.adapter.approvalNeeded(self.quote,TOKEN,WALLET,100)
        self.assertEqual(first,('ERC20',TOKEN,PERMIT2,100))
        tx=self.adapter.buildApproval(first)
        self.assertEqual(tx['to'],TOKEN)
        self.assertEqual(tx['value'],0)
        self.assertEqual(tx['data'][:10],'0x'+keccak(text='approve(address,uint256)')[:4].hex())
        spender,amount=decode(['address','uint256'],bytes.fromhex(tx['data'][10:]))
        self.assertEqual((spender.lower(),amount),(PERMIT2,100))

        async def hasAllowance(*args):return 100
        self.adapter.allowance=hasAllowance
        second=await self.adapter.approvalNeeded(self.quote,TOKEN,WALLET,100)
        self.assertEqual(second,('PERMIT2',TOKEN,V4_ROUTER,100))
        tx2=self.adapter.buildApproval(second)
        self.assertEqual(tx2['to'],PERMIT2)
        self.assertEqual(tx2['value'],0)
        self.assertEqual(tx2['data'][:10],
                         '0x'+keccak(text='approve(address,address,uint160,uint48)')[:4].hex())
        token,router,amount,expiry=decode(
            ['address','address','uint160','uint48'],bytes.fromhex(tx2['data'][10:]))
        self.assertEqual((token.lower(),router.lower(),amount),(TOKEN,V4_ROUTER,100))

        async def hasPermit(*args):return (100,2**48-1,0)
        self.adapter.permitAllowance=hasPermit
        self.assertIsNone(await self.adapter.approvalNeeded(self.quote,TOKEN,WALLET,100))


class TestLatchedTrancheSell(unittest.IsolatedAsyncioTestCase):
    async def test_tranche_sizes_and_no_reprice_threshold_gate(self):
        with tempfile.TemporaryDirectory() as root:
            store=ExecutionStore(Path(root)/'execution.sqlite3')
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('selection',TOKEN,0,'{}','BOUGHT',None))
            store.db.execute('''INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue,
                 trigger3,trigger5,trigger10)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                ('selection',TOKEN,'1000','200',0,0,0,1,1,1))
            recorded=[]
            class Swaps:
                async def directQuotes(self,token,amount,buy):
                    recorded.append(('quote',amount,buy))
                    # Deliberately below every threshold now: triggered stages
                    # must still be submitted with their updated executable quote.
                    return [{'venue':VENUE,'tokenIn':TOKEN,'output':amount//4}]
                async def approvalNeeded(self,*args):return None
                def build(self,quote,amount,wallet):
                    recorded.append(('built',amount))
                    return {'to':V4_ROUTER,'value':0,'data':'0x1234'}
            class Tx:
                account=SimpleNamespace(address=WALLET)
                async def create(self,intent,selectionId,kind,tx,inputAmount):
                    recorded.append(('signed',kind,inputAmount))
                    return {'intentId':intent,'txHash':'0x'+'1'*64}
                async def broadcast(self,intent):
                    recorded.append(('broadcast',intent))
                    return 'SENT'
            engine=object.__new__(LiveExecutor)
            engine.store=store;engine.swaps=Swaps();engine.tx=Tx()
            engine.balance=lambda token: asyncio.sleep(0,result=1000)

            async def dispatch(sold3,sold5,expectKind,expectAmount):
                store.db.execute('''UPDATE positions SET
                    sold3=?,sold5=?,soldQty3=?,soldQty5=?
                    WHERE selectionId='selection' ''',
                    (bool(sold3),bool(sold5),str(sold3),str(sold5)))
                row=store.db.execute('SELECT * FROM positions').fetchone()
                recorded.clear()
                await engine.prepareSell(row)
                self.assertIn(('signed',expectKind,expectAmount),recorded)
                self.assertIn(('built',expectAmount),recorded)
                self.assertEqual(sum(v[0]=='broadcast' for v in recorded),1)

            await dispatch(0,0,'SELL_3',800)
            await dispatch(800,0,'SELL_5',150)
            await dispatch(800,150,'SELL_10',50)
            store.db.close()



class TestConfirmedSaleReconciliation(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_80percent_token_debit_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as root:
            store=ExecutionStore(Path(root)/'execution.sqlite3')
            store.db.execute('INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)',
                             ('s',TOKEN,0,'{}','BOUGHT',None))
            store.db.execute('''INSERT INTO positions
                (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue,trigger3)
                VALUES(?,?,?,?,?,?,?,?)''',('s',TOKEN,'1000','123',0,0,0,1))
            transfer='0x'+keccak(text='Transfer(address,address,uint256)').hex()
            indexed=lambda address:'0x'+'0'*24+address[2:].lower()
            receipt={'status':'0x1','logs':[dict(
                address=TOKEN,
                topics=[transfer,indexed(WALLET),indexed('0x'+'f'*40)],
                data=hex(800))]}
            store.db.execute('''INSERT INTO transactions
                (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,
                 lastBroadcast,inputAmount,receipt,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                ('sell:s:3:1','s','SELL_3','CONFIRMED','0x'+'9'*64,
                 '0x02',12,0,0,'800',json.dumps(receipt)))
            class Tx:
                account=SimpleNamespace(address=WALLET)
                async def reconcile(self):return []
            engine=object.__new__(LiveExecutor)
            engine.store=store;engine.tx=Tx()
            await engine.reconcileReceipts()
            first=store.db.execute(
                'SELECT sold3,soldQty3 FROM positions WHERE selectionId=?',('s',)
            ).fetchone()
            self.assertEqual(tuple(first),(1,'800'))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],1)
            await engine.reconcileReceipts()
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sales').fetchone()[0],1)
            store.db.close()


if __name__=='__main__':
    unittest.main()
