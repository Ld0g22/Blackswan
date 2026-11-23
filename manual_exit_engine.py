"""In-process operator-driven sell execution for one-50p campaigns.

The CLI never decrypts/signs. The existing executor is the ONLY wallet
signer and journal owner. Durable requests/receipts live in the same DB.
"""
import json
import time
from decimal import Decimal, ROUND_UP

from swap_adapters import WETH

ACTIVE = ('QUEUED','PREPARING','WAITING_APPROVAL','WAITING_SELL','BLOCKED','UNVERIFIED')
TERMINAL = ('CONFIRMED','FAILED')
WEI = Decimal(10)**18


def installManualSchema(store):
    """Small additive migration, only for one-50p campaign databases."""
    if not (store.oneFifty or store.twoFifty or store.oneTwenty2x or store.flexLamb):
        return
    store.db.executescript("""
    CREATE TABLE IF NOT EXISTS manual_requests(
      requestId TEXT PRIMARY KEY,
      selectionId TEXT NOT NULL,
      operation TEXT NOT NULL CHECK(operation IN ('PREVIEW','SELL')),
      percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 100),
      minGbp TEXT NOT NULL,
      slippageBps INTEGER NOT NULL,
      status TEXT NOT NULL,
      createdAt REAL NOT NULL,
      updatedAt REAL NOT NULL,
      quantity TEXT,
      quoteWei TEXT,
      quoteGbp TEXT,
      quoteBlock TEXT,
      minOutWei TEXT,
      gasEstimate TEXT,
      approvalIntent TEXT,
      sellIntent TEXT,
      txHash TEXT,
      reason TEXT);
    CREATE TABLE IF NOT EXISTS manual_fills(
      requestId TEXT PRIMARY KEY,
      selectionId TEXT NOT NULL,
      txHash TEXT NOT NULL UNIQUE,
      tokenDebited TEXT NOT NULL,
      wethReceivedWei TEXT,
      gasWei TEXT NOT NULL,
      confirmedAt REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS manual_req_status ON manual_requests(status,createdAt);
    """)
    store.set('manualExitVersion','1')


class ManualExitEngine:
    def __init__(self,owner,receiptTransfers):
        self.owner=owner
        self.store=owner.store
        self.db=self.store.db
        self.receiptTransfers=receiptTransfers

    def row(self,requestId):
        return self.db.execute("SELECT * FROM manual_requests WHERE requestId=?",
                               (requestId,)).fetchone()

    def position(self,selectionId):
        return self.db.execute("SELECT * FROM positions WHERE selectionId=?",
                               (selectionId,)).fetchone()

    def manualSold(self,selectionId):
        records=self.db.execute(
            'SELECT tokenDebited FROM manual_fills WHERE selectionId=?',
            (selectionId,)).fetchall()
        return sum(int(r[0]) for r in records)

    def hasManualFills(self,selectionId):
        return self.db.execute('SELECT 1 FROM manual_fills WHERE selectionId=? LIMIT 1',
                               (selectionId,)).fetchone() is not None

    def mainRemaining(self,position):
        sid=position['selectionId']
        stages=(3,5) if self.store.flexPilot else (2,50,100) if self.store.newLadder2x else (3,5,1000)
        automatic=sum(int(position['soldQty'+str(stage)]) for stage in stages)
        return max(0,int(position['originalQty'])-automatic-self.manualSold(sid))

    def autoSlice(self,position,stage,tradable):
        """After manual intervention, distribute unsold main tokens across the
        unfilled 80/10/10 stages. Unmodified positions use old checkSlice.
        """
        if self.store.flexPilot:
            # Two-stage 80/20 ladder. Stage 5 sells the full unsold remainder;
            # the 1p probe is excluded from tradable by the caller.
            available=min(max(0,tradable),self.mainRemaining(position))
            if stage==3:
                return min(available,int(position['originalQty'])*80//100)
            if stage==5:
                return available
            return 0
        if self.store.newLadder2x:
            # Fixed original-position slices. Manual activity NEVER redistributes
            # the 5% remainder into automatic sells, even with partial/manual exits.
            weights=((2,80),(50,10),(100,5))
            pending=[(s,w) for s,w in weights if not position['sold'+str(s)]]
            if stage not in [s for s,_ in pending]:return 0
            original=int(position['originalQty'])
            reserved=original-sum(original*w//100 for _,w in weights)
            # Operator sales consume the manual reserve first, never auto sales.
            reserveStillHeld=max(0,reserved-self.manualSold(position['selectionId']))
            available=max(0,min(max(0,tradable),self.mainRemaining(position))-reserveStillHeld)
            target=original*dict(weights)[stage]//100
            return min(available,target)
        weights=((3,80),(5,10),(1000,10))
        pending=[(s,w) for s,w in weights if not position['sold'+str(s)]]
        if stage not in [s for s,_ in pending]:
            return 0
        remaining=min(max(0,tradable),self.mainRemaining(position))
        if not remaining:
            return 0
        total=sum(w for _,w in pending)
        target=remaining if pending[-1][0]==stage else remaining*dict(pending)[stage]//total
        return min(remaining,target)

    def active(self,selectionId):
        return self.db.execute("""
            SELECT 1 FROM manual_requests
            WHERE selectionId=? AND operation='SELL'
            AND status IN ('QUEUED','PREPARING','WAITING_APPROVAL',
                           'WAITING_SELL','BLOCKED','UNVERIFIED')
            LIMIT 1""",(selectionId,)).fetchone() is not None

    def setStatus(self,requestId,status,reason=None):
        self.db.execute("""UPDATE manual_requests
            SET status=?,updatedAt=?,reason=? WHERE requestId=?""",
            (status,time.time(),reason,requestId))

    def reconcile(self):
        """Receipts are authoritative. Idempotent across process restarts."""
        requests=self.db.execute("""
            SELECT * FROM manual_requests WHERE operation='SELL'
            AND status NOT IN ('CONFIRMED','FAILED') ORDER BY createdAt""").fetchall()
        for request in requests:
            requestId=request['requestId']
            sellIntent='manual:sell:'+requestId
            sale=self.db.execute('SELECT * FROM transactions WHERE intentId=?',
                                 (sellIntent,)).fetchone()
            if sale:
                if sale['state']=='CONFIRMED':
                    try:
                        receipt=json.loads(sale['receipt'])
                        position=self.position(request['selectionId'])
                        if not position:
                            raise ValueError('POSITION_MISSING')
                        debited=-self.receiptTransfers(
                            receipt,position['token'],self.owner.tx.account.address)
                        if debited<=0 or debited>self.mainRemaining(position):
                            raise ValueError('MANUAL_SELL_TOKEN_DEBIT_UNVERIFIABLE')
                        wethOut=max(0,self.receiptTransfers(
                            receipt,WETH,self.owner.tx.account.address))
                        gas=int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)
                        self.db.execute('BEGIN IMMEDIATE')
                        try:
                            self.db.execute("""INSERT OR IGNORE INTO manual_fills
                              (requestId,selectionId,txHash,tokenDebited,wethReceivedWei,
                               gasWei,confirmedAt) VALUES(?,?,?,?,?,?,?)""",
                              (requestId,request['selectionId'],sale['txHash'],
                               str(debited),str(wethOut) if wethOut else None,
                               str(gas),time.time()))
                            self.db.execute("""UPDATE manual_requests
                              SET status='CONFIRMED',updatedAt=?,reason=NULL,
                                  txHash=?,quantity=? WHERE requestId=?""",
                              (time.time(),sale['txHash'],str(debited),requestId))
                            self.db.execute('COMMIT')
                        except BaseException:
                            if self.db.in_transaction:self.db.execute('ROLLBACK')
                            raise
                        print('MANUAL SELL CONFIRMED',position['token'],
                              'debited',debited,'tx',sale['txHash'],flush=True)
                    except (ValueError,TypeError,KeyError,RuntimeError) as exc:
                        self.setStatus(requestId,'UNVERIFIED',
                                       type(exc).__name__+':'+str(exc)[:160])
                        print('MANUAL SELL UNVERIFIED; AUTOMATIC EXITS PAUSED',
                              requestId,str(exc)[:120],flush=True)
                elif sale['state']=='REVERTED':
                    self.setStatus(requestId,'FAILED','SELL_TRANSACTION_REVERTED')
                elif sale['state'] in ('REJECTED','STUCK'):
                    self.setStatus(requestId,'BLOCKED','SELL_TX_'+sale['state']+
                                   ':'+str(sale['txHash']))
                else:
                    # Includes SIGNED/SENT/UNCERTAIN: no resubmission with new nonce.
                    if request['status']!='WAITING_SELL':
                        self.setStatus(requestId,'WAITING_SELL',
                                       'AWAITING_CHAIN_RECEIPT:'+sale['txHash'])
                continue
            if request['status']=='WAITING_APPROVAL' and request['approvalIntent']:
                approval=self.db.execute('SELECT * FROM transactions WHERE intentId=?',
                                        (request['approvalIntent'],)).fetchone()
                if not approval:
                    self.setStatus(requestId,'BLOCKED','MISSING_APPROVAL_JOURNAL')
                elif approval['state']=='CONFIRMED':
                    self.setStatus(requestId,'QUEUED')
                elif approval['state']=='REVERTED':
                    self.setStatus(requestId,'FAILED','APPROVAL_REVERTED')
                elif approval['state'] in ('REJECTED','STUCK'):
                    self.setStatus(requestId,'BLOCKED','APPROVAL_TX_'+approval['state'])
            # QUEUED and PREPARING are recovered by the normal run loop.

    def tick(self):
        self.reconcile()
        previews=self.db.execute("""
            SELECT requestId FROM manual_requests
            WHERE operation='PREVIEW' AND status='QUEUED'
            ORDER BY createdAt LIMIT 2""").fetchall()
        for row in previews:
            self.owner.spawn('MANUAL_PREVIEW:'+row['requestId'],
                             self.preview(row['requestId']))
        sells=self.db.execute("""
            SELECT requestId FROM manual_requests
            WHERE operation='SELL' AND status IN ('QUEUED','PREPARING')
            ORDER BY createdAt LIMIT 1""").fetchall()
        for row in sells:
            self.owner.spawn('MANUAL_EXIT:'+row['requestId'],
                             self.sell(row['requestId']))

    async def quote(self,request,position):
        token=position['token']
        chainBalance=await self.owner.balance(token)
        probe=self.owner.lamb.probeQuantity(position['selectionId'])
        available=min(self.mainRemaining(position),max(0,chainBalance-probe))
        if available<=0:
            raise RuntimeError('NO_REMAINING_MAIN_TOKENS')
        percent=int(request['percent'])
        quantity=available if percent==100 else available*percent//100
        if quantity<=0:
            raise RuntimeError('MANUAL_SLICE_TOO_SMALL')
        latest=(await self.owner.swaps.directQuotes(token,quantity,False))[0]
        if latest.get('tokenIn',token).lower()!=token.lower():
            raise RuntimeError('WRONG_SELL_INPUT_TOKEN')
        output=int(latest['output'])
        if output<=0:
            raise RuntimeError('NO_POSITIVE_EXECUTABLE_SELL_QUOTE')
        rate=await self.owner.fx.get()
        gbp=Decimal(output)*rate/WEI
        minGbp=Decimal(request['minGbp'])
        if minGbp<0:
            raise ValueError('NEGATIVE_MIN_GBP')
        if gbp<minGbp:
            raise RuntimeError('FRESH_QUOTE_BELOW_OPERATOR_MINIMUM')
        slippage=int(request['slippageBps'])
        if not 0<=slippage<=1000:
            raise ValueError('INVALID_MANUAL_SLIPPAGE_BPS')
        minSlippage=max(1,output*(10000-slippage)//10000)
        minGbpWei=int((minGbp*WEI/rate).to_integral_value(rounding=ROUND_UP))
        minOut=max(minSlippage,minGbpWei)
        if minOut>output:
            raise RuntimeError('FRESH_QUOTE_BELOW_OPERATOR_MINIMUM')
        swap=self.owner.swaps.build(
            latest,quantity,self.owner.tx.account.address,minimumOut=minOut)
        return quantity,latest,swap,gbp,minOut

    async def preview(self,requestId):
        request=self.row(requestId)
        if not request or request['status']!='QUEUED':return
        self.setStatus(requestId,'PREPARING')
        try:
            position=self.position(request['selectionId'])
            if not position:raise RuntimeError('NO_POSITION')
            qty,quote,swap,gbp,minOut=await self.quote(request,position)
            gas='UNAVAILABLE'
            try:
                estimate=await self.owner.rpc.call('eth_estimateGas',[dict(
                    **{'from':self.owner.tx.account.address,'to':swap['to'],
                       'data':swap['data'],'value':hex(swap.get('value',0))})])
                gas=str(int(estimate,16))
            except (RuntimeError,ValueError,TypeError):
                pass  # A new allowance may be required before swap simulation.
            self.db.execute("""UPDATE manual_requests
              SET status='PREVIEW_READY',updatedAt=?,quantity=?,quoteWei=?,
                  quoteGbp=?,quoteBlock=?,minOutWei=?,gasEstimate=?,reason=NULL
              WHERE requestId=?""",
              (time.time(),str(qty),str(quote['output']),str(gbp),
               str(quote.get('block')),str(minOut),gas,requestId))
        except (RuntimeError,ValueError,KeyError,TypeError,IndexError) as exc:
            self.setStatus(requestId,'PREVIEW_FAILED',
                           type(exc).__name__+':'+str(exc)[:160])

    async def sell(self,requestId):
        request=self.row(requestId)
        if not request or request['status'] not in ('QUEUED','PREPARING'):return
        selectionId=request['selectionId']
        # Never sign while ANY wallet transaction is unresolved, including
        # an automatic exit that reached the signer just before manual entry.
        if self.owner.tx.unsettled():
            return
        if self.db.execute('SELECT 1 FROM transactions WHERE intentId=?',
                           ('manual:sell:'+requestId,)).fetchone():
            self.setStatus(requestId,'WAITING_SELL')
            return
        self.setStatus(requestId,'PREPARING')
        try:
            position=self.position(selectionId)
            if not position:raise RuntimeError('NO_POSITION')
            qty,quote,swap,gbp,minOut=await self.quote(request,position)
            # Quote may change while another sale settles; journal nonce
            # exclusivity and fresh eth_call validation are still required.
            approval=await self.owner.swaps.approvalNeeded(
                quote,position['token'],self.owner.tx.account.address,qty)
            if approval:
                intent=('manual:approve:'+requestId+':'+str(approval[0])+':'+
                        str(approval[2]).lower())
                old=self.db.execute('SELECT state FROM transactions WHERE intentId=?',
                                    (intent,)).fetchone()
                if old and old['state']=='CONFIRMED':
                    raise RuntimeError('APPROVAL_CONFIRMED_BUT_STILL_INSUFFICIENT')
                if old and old['state'] in ('REVERTED','REJECTED','STUCK'):
                    raise RuntimeError('PREVIOUS_APPROVAL_FAILED')
                tx=await self.owner.tx.create(intent,selectionId,'MANUAL_APPROVE',
                         self.owner.swaps.buildApproval(approval))
                self.db.execute("""UPDATE manual_requests
                  SET status='WAITING_APPROVAL',updatedAt=?,approvalIntent=?,
                      reason=?,quantity=?,quoteWei=?,quoteGbp=?,minOutWei=?,
                      quoteBlock=?,txHash=? WHERE requestId=?""",
                  (time.time(),intent,'AWAITING_APPROVAL_RECEIPT',
                   str(qty),str(quote['output']),str(gbp),str(minOut),
                   str(quote.get('block')),tx['txHash'],requestId))
                if tx['state'] in ('SIGNED','SENT','UNCERTAIN'):
                    await self.owner.tx.broadcast(tx['intentId'])
                return
            intent='manual:sell:'+requestId
            tx=await self.owner.tx.create(intent,selectionId,'MANUAL_SELL',swap,qty)
            self.db.execute("""UPDATE manual_requests
              SET status='WAITING_SELL',updatedAt=?,sellIntent=?,
                  reason=?,quantity=?,quoteWei=?,quoteGbp=?,minOutWei=?,
                  quoteBlock=?,txHash=? WHERE requestId=?""",
              (time.time(),intent,'AWAITING_SELL_RECEIPT',
               str(qty),str(quote['output']),str(gbp),str(minOut),
               str(quote.get('block')),tx['txHash'],requestId))
            if tx['state'] in ('SIGNED','SENT','UNCERTAIN'):
                await self.owner.tx.broadcast(tx['intentId'])
            print('MANUAL SELL SUBMITTED',position['token'],
                  'qty',qty,'quoteGBP',gbp,'minOutWei',minOut,
                  'tx',tx['txHash'],flush=True)
        except (RuntimeError,ValueError,TypeError,KeyError,IndexError) as exc:
            if str(exc)=='WALLET_TRANSACTION_IN_FLIGHT':
                self.setStatus(requestId,'QUEUED','WAITING_FOR_WALLET_TRANSACTION')
                return
            self.setStatus(requestId,'FAILED',
                           type(exc).__name__+':'+str(exc)[:160])
            print('MANUAL SELL NOT SUBMITTED',requestId,str(exc)[:140],flush=True)
