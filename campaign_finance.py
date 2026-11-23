"""Receipt-based campaign cash accounting for the new 3x/5x pilot.

This is called ONLY from the existing single live executor. It never signs,
sends or adjusts transactions. All figures are ETH/GBP equivalents, not
GBP bank deposits. Native ETH attribution uses qualified block balance
deltas, not transaction traces; unsolicited same-block transfers remain a
documented limitation. Ambiguous payouts remain INCOMPLETE, not zero.
"""
from decimal import Decimal
import json
import time

from swap_adapters import WETH


def gasWei(receipt):
    return int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)


class CampaignFinance:
    def __init__(self,store,rpc,fx,wallet):
        self.db=store.db
        self.rpc=rpc
        self.fx=fx
        self.wallet=wallet.lower()
        self.nextCheck=0
        self.db.execute("""CREATE TABLE IF NOT EXISTS finance_events(
            txHash TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            expenseWei TEXT,
            gasWei TEXT,
            incomeWei TEXT,
            incomeAsset TEXT,
            incomeBasis TEXT,
            fxGbpPerEth TEXT,
            fxAt REAL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            nextRetryAt REAL NOT NULL DEFAULT 0,
            reason TEXT,
            recordedAt REAL NOT NULL)""")

    async def rpcCall(self,method,params):
        return await self.rpc.call(method,params)

    async def transaction(self,row,receipt):
        tx=await self.rpcCall('eth_getTransactionByHash',[row['txHash']])
        if (not isinstance(tx,dict) or
            str(tx.get('hash','')).lower()!=row['txHash'].lower() or
            str(tx.get('from','')).lower()!=self.wallet or
            tx.get('blockNumber')!=receipt.get('blockNumber')):
            raise RuntimeError('TRANSACTION_IDENTITY_UNVERIFIED')
        return tx

    async def nativePayout(self,row,receipt,tx,fee):
        blockNumber=int(receipt['blockNumber'],16)
        if blockNumber<1:
            raise RuntimeError('SALE_BLOCK_UNAVAILABLE')
        block=await self.rpcCall('eth_getBlockByNumber',[hex(blockNumber),True])
        if not isinstance(block,dict) or not isinstance(block.get('transactions'),list):
            raise RuntimeError('FULL_SALE_BLOCK_UNAVAILABLE')
        own=[t for t in block['transactions']
             if isinstance(t,dict) and str(t.get('from','')).lower()==self.wallet]
        if len(own)!=1 or str(own[0].get('hash','')).lower()!=row['txHash'].lower():
            raise RuntimeError('SALE_BLOCK_OTHER_OWN_TRANSACTION')
        before=int(await self.rpcCall('eth_getBalance',[self.wallet,hex(blockNumber-1)]),16)
        after=int(await self.rpcCall('eth_getBalance',[self.wallet,hex(blockNumber)]),16)
        gross=after-before+fee+int(tx['value'],16)
        if gross<=0:
            raise RuntimeError('NO_POSITIVE_NATIVE_CREDIT')
        return gross

    async def measure(self,row):
        receipt=json.loads(row['receipt'])
        if (not isinstance(receipt,dict)
            or str(receipt.get('transactionHash','')).lower()!=row['txHash'].lower()
            or int(receipt['status'],16)!=(1 if row['state']=='CONFIRMED' else 0)):
            raise RuntimeError('FINANCE_RECEIPT_IDENTITY_MISMATCH')
        tx=await self.transaction(row,receipt)
        fee=gasWei(receipt)
        expense=int(tx['value'],16) if row['state']=='CONFIRMED' else 0
        rate=Decimal(str(await self.fx.get()))
        if not rate.is_finite() or rate<=0:
            raise RuntimeError('FX_UNAVAILABLE')
        income=None
        asset=None
        basis=None
        kind=row['kind']
        if row['state']=='CONFIRMED' and (kind.startswith('SELL_') or kind=='MANUAL_SELL'):
            table='manual_fills' if kind=='MANUAL_SELL' else 'sales'
            sale=self.db.execute('SELECT tokenDebited,wethReceivedWei FROM '+table+
                                 ' WHERE lower(txHash)=lower(?)',(row['txHash'],)).fetchone()
            if not sale or int(sale['tokenDebited'])<=0:
                raise RuntimeError('VERIFIED_SALE_LEDGER_PENDING')
            weth=int(sale['wethReceivedWei'] or '0')
            if weth>0:
                income,asset,basis=weth,'WETH','RECEIPT_ERC20_TRANSFER'
            else:
                income=await self.nativePayout(row,receipt,tx,fee)
                asset,basis='ETH','QUALIFIED_BLOCK_BALANCE'
        return dict(expenseWei=str(expense),gasWei=str(fee),
                    incomeWei=str(income) if income is not None else None,
                    incomeAsset=asset,incomeBasis=basis,
                    fxGbpPerEth=str(rate),fxAt=time.time())

    async def tick(self):
        now=time.time()
        if now<self.nextCheck:
            return
        self.nextCheck=now+5.0
        entries=self.db.execute("""SELECT * FROM transactions
            WHERE state IN ('CONFIRMED','REVERTED') AND receipt IS NOT NULL
            ORDER BY createdAt""").fetchall()
        for row in entries:
            previous=self.db.execute('SELECT state,attempts,nextRetryAt FROM finance_events WHERE txHash=?',
                                     (row['txHash'],)).fetchone()
            if previous and (previous['state']=='COMPLETE' or now<float(previous['nextRetryAt'])):
                continue
            attempts=(int(previous['attempts']) if previous else 0)+1
            data={}
            state='COMPLETE'
            reason=None
            try:
                data=await self.measure(row)
            except (RuntimeError,ValueError,TypeError,KeyError,IndexError,json.JSONDecodeError) as exc:
                state='INCOMPLETE'
                reason=type(exc).__name__+':'+str(exc)[:150]
            delay=0 if state=='COMPLETE' else min(300,10*(2**min(attempts,5)))
            self.db.execute("""INSERT INTO finance_events(
                txHash,kind,expenseWei,gasWei,incomeWei,incomeAsset,incomeBasis,
                fxGbpPerEth,fxAt,state,attempts,nextRetryAt,reason,recordedAt)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(txHash) DO UPDATE SET
                  expenseWei=excluded.expenseWei,gasWei=excluded.gasWei,
                  incomeWei=excluded.incomeWei,incomeAsset=excluded.incomeAsset,
                  incomeBasis=excluded.incomeBasis,fxGbpPerEth=excluded.fxGbpPerEth,
                  fxAt=excluded.fxAt,state=excluded.state,
                  attempts=excluded.attempts,nextRetryAt=excluded.nextRetryAt,
                  reason=excluded.reason,recordedAt=excluded.recordedAt""",
                (row['txHash'],row['kind'],data.get('expenseWei'),data.get('gasWei'),
                 data.get('incomeWei'),data.get('incomeAsset'),data.get('incomeBasis'),
                 data.get('fxGbpPerEth'),data.get('fxAt'),
                 state,attempts,now+delay,reason,now))
            if state!='COMPLETE':
                print('FINANCE PENDING',row['kind'],row['txHash'],reason,flush=True)
