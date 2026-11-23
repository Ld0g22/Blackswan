"""Actual original-executor adjacency tests: PASS -> 1p -> 10s -> ordinary 10p -> exits."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from eth_utils import keccak
import peak_tracker as peak
from live_executor import LiveExecutor, ExecutionStore

TOKEN='0x'+'a'*40
WALLET='0x'+'1'*40
TOPIC='0x'+keccak(text='Transfer(address,address,uint256)').hex()

def receipt(qty):
    return dict(status='0x1',blockNumber='0x15',logs=[dict(address=TOKEN,
        topics=[TOPIC,'0x'+'0'*64,'0x'+WALLET[2:].rjust(64,'0')],
        data='0x'+f'{qty:064x}')])

class Fx:
    async def get(self):return Decimal('1000')

class Rpc:
    def __init__(self):self.session=SimpleNamespace()
class Routes:
    valueGbp=Decimal('1')
    def __init__(self):self.calls=[];self.fail=False
    async def verifiedBuyCandidates(self,token,wei):
        self.calls.append(('BUY',wei))
        return [dict(output=100,preBuySellOutput=90,preBuySellBlock='0x1',
           preBuySellSource='uniswapV4',peakRoute='route',block='0x1',
           venue=dict(source='uniswapV4'))]
    async def directQuotes(self,token,qty,buy,block=None):
        self.calls.append(('SELL_QUOTE',qty))
        if self.fail:raise RuntimeError('NO_SELL_ROUTE')
        return [dict(output=int(self.valueGbp/Decimal('1000')*Decimal(10**18)),
                     peakRoute='route',block='0x1',basis='LIVE')]
    def build(self,quote,amount,wallet):
        return dict(to=WALLET,value=amount,data='0x')
    async def approvalNeeded(self,*args):return None
    async def refreshPons(self,*args,**kwargs):return None
class Journal:
    def __init__(self,store):self.account=SimpleNamespace(address=WALLET);self.store=store;self.orders=[]
    async def create(self,intent,selection,kind,transaction,amount=0):
        self.orders.append((kind,int(amount)))
        h='0x'+f'{len(self.orders)+700:064x}'
        self.store.db.execute("""INSERT OR IGNORE INTO transactions
           (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,inputAmount)
           VALUES(?,?,?,'SIGNED',?,'0x',0,?,?,?)""",
           (intent,selection,kind,h,time.time(),time.time(),str(amount)))
        return dict(self.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intent,)).fetchone())
    async def broadcast(self,intent):
        self.store.db.execute("UPDATE transactions SET state='SENT' WHERE intentId=?",(intent,))
        return 'SENT'
    async def reconcile(self):return []

class TestOriginalExecutorLambIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.store=ExecutionStore(root/'execution.sqlite3',sacrifice=True)
        self.selectorPath=root/'selector.sqlite3'
        with sqlite3.connect(self.selectorPath) as db:
            db.execute('CREATE TABLE decisions(seq INTEGER PRIMARY KEY,status TEXT,data TEXT)')
            db.execute('CREATE TABLE revocations(seq INTEGER PRIMARY KEY,eventKey TEXT)')
        detector=root/'detector.sqlite3'
        with sqlite3.connect(detector) as db:
            db.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,source TEXT,payload TEXT)')
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO meta VALUES('lastConnectedHead','100')")
        self.tx=Journal(self.store);self.swaps=Routes()
        self.engine=LiveExecutor(self.store,self.selectorPath,detector,Rpc(),Fx(),self.tx)
        self.engine.swaps=self.swaps
        async def noPons(token):return None
        self.engine.ponsOnChain=noPons
        event=dict(chainId=4663,source='uniswapV4',details=dict(
            currency0='0x'+'0'*40,currency1=TOKEN,fee=3000,tickSpacing=60,
            hooks='0x'+'0'*40,poolId='0x'+'f'*64))
        venue=peak.launchVenues(event)[0][1]
        self.sid='v4-pass'
        self.passedAt=time.time()
        item=dict(interfaceVersion=1,selectionId=self.sid,tokenKey='4663:'+TOKEN,
                  venueKey='v4:pool',status='PASS',event=event,venue=venue,
                  decisionAt=self.passedAt)
        with sqlite3.connect(self.selectorPath) as db:
            db.execute('INSERT INTO decisions(seq,status,data) VALUES(1,?,?)',
                       ('PASS',json.dumps(item)))
        self.engine.readSelections()
    async def asyncTearDown(self):
        await self.engine.close();self.store.db.close();self.tmp.cleanup()
    def selection(self):
        return self.store.db.execute('SELECT * FROM selections WHERE selectionId=?',
                                     (self.sid,)).fetchone()
    def confirm(self,kind,qty):
        self.store.db.execute(
            "UPDATE transactions SET state='CONFIRMED',receipt=? WHERE selectionId=? AND kind=?",
            (json.dumps(receipt(qty)),self.sid,kind))

    async def test_normal_ten_pence_path_unreachable_before_real_probe(self):
        self.assertEqual(self.selection()['status'],'LAMB_QUEUED')
        await self.engine.buy(self.selection())
        self.assertEqual(self.tx.orders,[])
        await self.engine.lamb.start(self.selection())
        self.assertEqual([k for k,_ in self.tx.orders],['PROBE_BUY'])
        self.confirm('PROBE_BUY',10)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.selection()['status'],'PROBE_CONFIRMED')
        await self.engine.buy(self.selection())
        self.assertEqual([k for k,_ in self.tx.orders],['PROBE_BUY'])
        self.assertEqual(self.store.acquired(),0)

    async def test_ten_second_handoff_feeds_unchanged_buy_receipt_and_tracker(self):
        await self.engine.lamb.start(self.selection())
        self.confirm('PROBE_BUY',10)
        await self.engine.reconcileReceipts()
        self.store.db.execute(
            'UPDATE lamb_probes SET confirmedAt=? WHERE selectionId=?',
            (time.time()-11,self.sid))
        await self.engine.lamb.check(self.selection())
        self.assertEqual(self.selection()['status'],'QUEUED')
        self.assertGreater(self.engine.lamb.readyAt(self.sid),self.passedAt)
        await self.engine.buy(self.selection())
        self.assertEqual([k for k,_ in self.tx.orders],['PROBE_BUY','BUY'])
        self.assertEqual(self.tx.orders[1][1],100000000000000)
        self.confirm('BUY',100)
        await self.engine.reconcileReceipts()
        self.assertEqual(self.selection()['status'],'BOUGHT')
        self.assertEqual(self.store.acquired(),1)
        self.assertEqual(self.store.db.execute('SELECT originalQty FROM positions').fetchone()[0],'100')
        self.assertEqual(self.engine.lamb.probeQuantity(self.sid),10)
        await self.engine.reconcileReceipts()  # no duplicate fill after restart-like replay
        self.assertEqual(self.store.acquired(),1)

    async def test_actual_three_five_one_thousand_sale_orders_never_use_probe_qty(self):
        self.store.db.execute("UPDATE selections SET status='BOUGHT' WHERE selectionId=?",(self.sid,))
        self.store.db.execute("""INSERT INTO lamb_probes
            (selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
            VALUES(?,?,?,1,'10','100','PROBE_PASSED')""",
            (self.sid,TOKEN,'0x'+'8'*64))
        self.store.db.execute("""INSERT INTO positions
            (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue,
             trigger3,trigger5,trigger1000)
            VALUES(?,?,'100','100',1,1,1,1,1,1)""",(self.sid,TOKEN))
        wallet=[110]
        async def balance(token):return wallet[0]
        self.engine.balance=balance
        for stage,newBalance in ((3,30),(5,20),(1000,10)):
            p=self.store.db.execute('SELECT * FROM positions').fetchone()
            await self.engine.prepareSell(p)
            self.store.db.execute(f'UPDATE positions SET sold{stage}=1,soldQty{stage}=?',
                                  (str({3:80,5:10,1000:10}[stage]),))
            wallet[0]=newBalance
            # Fake settled sell intents so the original journal allows next nonce.
            self.store.db.execute(
                "UPDATE transactions SET state='CONFIRMED' WHERE kind=?",(f'SELL_{stage}',))
        self.assertEqual([x for x in self.tx.orders if x[0].startswith('SELL_')],
                         [('SELL_3',80),('SELL_5',10),('SELL_1000',10)])
        self.assertEqual(wallet[0],10)

    async def test_ten_x_does_not_trigger_final_one_thousand_exit(self):
        self.store.db.execute("UPDATE selections SET status='BOUGHT' WHERE selectionId=?",(self.sid,))
        self.store.db.execute("""INSERT INTO positions
            (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
            VALUES(?,?,'100','100',1,1,1)""",(self.sid,TOKEN))
        self.swaps.valueGbp=Decimal('1.00')
        await self.engine.checkPosition(self.store.db.execute('SELECT * FROM positions').fetchone())
        got=self.store.db.execute('SELECT trigger3,trigger5,trigger1000 FROM positions').fetchone()
        self.assertEqual(tuple(got),(1,1,0))
        self.swaps.valueGbp=Decimal('100.00')
        await self.engine.checkPosition(self.store.db.execute('SELECT * FROM positions').fetchone())
        got=self.store.db.execute('SELECT trigger3,trigger5,trigger1000 FROM positions').fetchone()
        self.assertEqual(tuple(got),(1,1,1))

    async def test_fifth_real_ten_penny_receipt_closes_but_not_probe_receipt(self):
        for i in range(4):
            token='0x'+f'{i+20:040x}'
            sid='other-'+str(i)
            self.store.db.execute("""INSERT INTO selections
              (selectionId,token,decisionAt,event,status) VALUES(?,?,0,'{}','BOUGHT')""",(sid,token))
            self.store.db.execute("""INSERT INTO positions
              (selectionId,token,originalQty,spentWei,boughtAt,lastGrowth,nextDue)
              VALUES(?,?,'100','100',1,1,1)""",(sid,token))
        self.store.db.execute("UPDATE selections SET status='BUY_PENDING' WHERE selectionId=?",(self.sid,))
        self.store.db.execute("""INSERT INTO transactions
          (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,inputAmount,receipt)
          VALUES('buy:fifth',?,'BUY','CONFIRMED',?,'0x',1,1,1,'100',?)""",
          (self.sid,'0x'+'9'*64,json.dumps(receipt(100))))
        await self.engine.reconcileReceipts()
        self.assertEqual(self.store.acquired(),5)
        self.assertEqual(self.store.get('intakeState'),'CLOSED')
        self.assertFalse(self.store.eligible())

if __name__=='__main__':
    unittest.main()
