"""One in-process sacrificial 1p gate. Uses the EXISTING live executor's wallet journal.

No independent key, signer, RPC client, selector, wallet nonce or trading worker.
Only receipt-confirmed probes may promote to the original executor's normal buy queue.
"""
from decimal import Decimal, ROUND_DOWN
import asyncio
import json
import time

from probe_buying_blocks import ProbeBuyingBlocks, MAX_BUYING_BLOCKS
from entry_timing import canonicalTiming, windowEnd, targetKey, entryKwargs
from swap_adapters import boundEntryTransaction

PROBE_GBP=Decimal('0.01')
TEN_PENNY_GBP=Decimal('0.10')
WAIT_SECONDS=10.0
PROBE_ENTRY_SECONDS=3.0


class LambGate:
    def __init__(self,executor,receiptReader,waitBlocks=None):
        if waitBlocks is not None and (
                type(waitBlocks) is not int or waitBlocks < 1 or
                (waitBlocks != 500 and getattr(executor.store,'configuredCampaign',None) is None)):
            raise ValueError('UNSUPPORTED_SACRIFICIAL_BLOCK_GATE')
        self.owner=executor
        self.store=executor.store
        self.receiptReader=receiptReader
        self.waitBlocks=waitBlocks
        self.headLock=asyncio.Lock()
        self.headCheckedAt=0.0
        self.headCached=None
        # Unversioned historical 500-block campaigns retain their original gate.
        # Minimal offline test stores without a policy attribute exercise the
        # new collector independently.
        filtered=((getattr(self.store,'configuredCampaign',None) is not None and
                   self.store.probeMaxBuyingBlocks is not None and
                   waitBlocks is not None and waitBlocks >= 500) or
                  (getattr(self.store,'configuredCampaign',None) is None and waitBlocks==500 and
                   getattr(self.store,'probeMaxBuyingBlocks',7)==7))
        self.activity=(ProbeBuyingBlocks(
            self.store,self.owner.rpc,maxBuyingBlocks=self.store.probeMaxBuyingBlocks
            if getattr(self.store,'configuredCampaign',None) else MAX_BUYING_BLOCKS) if filtered else None)

    @staticmethod
    def blockNumber(value):
        if isinstance(value,str) and value.startswith('0x'):
            return int(value,16)
        if type(value) is int and value>=0:
            return value
        raise ValueError('INVALID_CHAIN_BLOCK_NUMBER')

    async def chainHead(self):
        # One shared block-height poll across all pending 1p positions.
        # No local estimates of block speed and no wall-clock substitution.
        async with self.headLock:
            if (self.headCached is not None
                    and time.monotonic()-self.headCheckedAt<1.0):
                return self.headCached
            current=self.blockNumber(await self.owner.rpc.call('eth_blockNumber',[]))
            self.headCached=current
            self.headCheckedAt=time.monotonic()
            return current

    def probeReceipt(self,selectionId,probe):
        """Recover the immutable original receipt block from the existing journal.

        No schema migration and no dependence on restart time or confirmedAt.
        """
        row=self.store.db.execute(
            """SELECT txHash,state,receipt FROM transactions
               WHERE selectionId=? AND kind='PROBE_BUY'""",
            (selectionId,)).fetchone()
        if (not row or row['state']!='CONFIRMED'
                or not row['receipt'] or not probe['probeTxHash']
                or row['txHash'].lower()!=probe['probeTxHash'].lower()):
            raise ValueError('MISSING_CONFIRMED_PROBE_JOURNAL')
        receipt=json.loads(row['receipt'])
        if (self.blockNumber(receipt.get('status'))!=1
                or not isinstance(receipt.get('blockHash'),str)
                or len(receipt['blockHash'])!=66
                or not isinstance(receipt.get('transactionHash'),str)
                or receipt['transactionHash'].lower()!=row['txHash'].lower()):
            raise ValueError('UNVERIFIED_OR_INCOMPLETE_PROBE_RECEIPT')
        return row,receipt,self.blockNumber(receipt.get('blockNumber'))

    async def blockGateReady(self,selectionId,probe):
        """Allow a 10p gate only at >=500 canonical blocks after 1p receipt.

        A retryable provider failure never promotes a purchase. A missing or
        changed canonical 1p receipt permanently invalidates this probe.
        """
        try:
            row,original,receiptBlock=self.probeReceipt(selectionId,probe)
        except (ValueError,TypeError,KeyError):
            self.state(selectionId,'PROBE_UNVERIFIED','PROBE_RECEIPT_EVIDENCE_MISSING')
            return None
        target=receiptBlock+(self.waitBlocks or 0)
        try:
            head=await self.chainHead()
        except (RuntimeError,ValueError,TypeError,KeyError):
            # Retry on a later executor tick; unavailable RPC is never PASS.
            return None
        if head<target:
            return None
        try:
            canonical=await self.owner.rpc.call('eth_getTransactionReceipt',[row['txHash']])
        except (RuntimeError,ValueError,TypeError,KeyError):
            return None
        if canonical is None:
            self.state(selectionId,'PROBE_UNVERIFIED','PROBE_RECEIPT_NOT_CANONICAL')
            return None
        try:
            valid=(canonical.get('transactionHash','').lower()==row['txHash'].lower()
                   and canonical.get('blockHash','').lower()==original['blockHash'].lower()
                   and self.blockNumber(canonical.get('blockNumber'))==receiptBlock
                   and self.blockNumber(canonical.get('status'))==1)
        except (AttributeError,KeyError,ValueError,TypeError):
            valid=False
        if not valid:
            self.state(selectionId,'PROBE_UNVERIFIED','PROBE_RECEIPT_REORG_OR_MISMATCH')
            return None
        if canonicalTiming(self.store):
            # Timestamp of the exact receipt/target blocks, never time of processing.
            async def header(number):
                h=await self.owner.rpc.call('eth_getBlockByNumber',[hex(number),False])
                if (not isinstance(h,dict) or self.blockNumber(h.get('number'))!=number
                        or not isinstance(h.get('hash'),str) or len(h['hash'])!=66):
                    raise ValueError('INVALID_SURVIVAL_HEADER')
                stamp=self.blockNumber(h.get('timestamp'))
                if stamp<=0 or stamp>time.time():raise ValueError('INVALID_SURVIVAL_TIMESTAMP')
                return h,stamp
            try:
                if target==receiptBlock:
                    receiptHeader,receiptAt=await header(receiptBlock)
                    targetAt=receiptAt+getattr(self.store,'probeWaitSeconds',WAIT_SECONDS)
                else:
                    ((receiptHeader,receiptAt),(_,targetAt))=await asyncio.gather(
                        header(receiptBlock),header(target))
                    if targetAt<receiptAt:raise ValueError('INVALID_SURVIVAL_TIMESTAMP_ORDER')
                if receiptHeader['hash'].lower()!=original['blockHash'].lower():
                    self.state(selectionId,'PROBE_UNVERIFIED','PROBE_RECEIPT_REORG_OR_MISMATCH')
                    return None
                selection=self.store.db.execute('SELECT * FROM selections WHERE selectionId=?',
                                                (selectionId,)).fetchone()
                anchor=windowEnd(self.store,selection)
            except (RuntimeError,ValueError,TypeError,KeyError,AttributeError):
                # Provider failure cannot create a fresh deadline.
                return None
            if not anchor<=receiptAt<=anchor+self.store.sacrificeDeadlineSeconds:
                self.state(selectionId,'PROBE_FAILED','LATE_SACRIFICE_RECEIPT')
                return None
            if time.time()<targetAt:return None
            if time.time()>targetAt+self.store.entryDeadlineSeconds:
                self.state(selectionId,'PROBE_FAILED','MISSED_SURVIVAL_DEADLINE')
                return None
            self.store.set(targetKey(selectionId),targetAt)
        return target


    def row(self,selectionId):
        return self.store.db.execute(
            'SELECT * FROM lamb_probes WHERE selectionId=?',(selectionId,)).fetchone()

    def readyAt(self,selectionId):
        r=self.row(selectionId)
        if self.activity is not None and self.activity.passed(selectionId) is None:
            return None
        if not r or r['passedAt'] is None:return None
        if canonicalTiming(self.store):
            value=self.store.get(targetKey(selectionId),'')
            return float(value) if value else None
        return float(r['passedAt'])

    async def collectActivity(self,selection):
        """Read only the next mature 100-block range, without touching the signer."""
        if self.activity is None or selection['status']!='PROBE_CONFIRMED':
            return
        try:
            probe=self.row(selection['selectionId'])
            if not probe:return
            _,_,probeBlock=self.probeReceipt(selection['selectionId'],probe)
            head=await self.chainHead()
            await self.activity.collect(selection,probeBlock,head)
        except (RuntimeError,ValueError,TypeError,KeyError,IndexError) as exc:
            # Fail closed. An incomplete history is never a PASS.
            print('PROBE ACTIVITY RETRY',selection['token'],str(exc)[:120],flush=True)

    async def verifyActivityBeforeBuy(self,selection):
        if self.activity is None:
            return True
        probe=self.row(selection['selectionId'])
        if not probe:return False
        _,_,probeBlock=self.probeReceipt(selection['selectionId'],probe)
        return await self.activity.preBuyVerified(selection,probeBlock)

    def probeQuantity(self,selectionId):
        r=self.row(selectionId)
        return int(r['probeQty']) if r and r['probeQty'] is not None else 0

    def revoked(self,selectionId):
        return self.owner.selector.execute(
            'SELECT 1 FROM revocations WHERE eventKey=? LIMIT 1',
            (selectionId,)).fetchone() is not None

    def state(self,selectionId,code,reason=None):
        self.store.db.execute(
            "UPDATE selections SET status=?,reason=? WHERE selectionId=?",
            (code,str(reason)[:160] if reason else None,selectionId))
        self.store.db.execute(
            'UPDATE lamb_probes SET result=?,reason=? WHERE selectionId=?',
            (code,str(reason)[:160] if reason else None,selectionId))

    async def start(self,selection):
        """Sign real £0.01 early probe with the original quote builder/journal."""
        selectionId=selection['selectionId']
        token=selection['token']
        if (selection['status']!='LAMB_QUEUED' or not self.store.eligible()
                or self.revoked(selectionId)):
            return
        if self.store.transactions(selectionId,'PROBE_BUY'):
            self.state(selectionId,'PROBE_PENDING')
            return
        if hasattr(self.store,"canSpend") and not self.store.canSpend("PROBE_BUY"):
            self.state(selectionId,"PROBE_FAILED","SACRIFICE_BUDGET_LIMIT")
            return
        if (getattr(self.store,'configuredCampaign',None) is not None and
                time.time()<float(self.store.get("probeRetryAfter:"+selectionId,"0"))):
            return
        reason=self.owner.entryExclusion(token,json.loads(selection['event']))
        if reason:
            self.state(selectionId,'PROBE_FAILED',reason)
            return
        try:anchor=windowEnd(self.store,selection)
        except (ValueError,TypeError,KeyError):
            self.state(selectionId,'PROBE_FAILED','INVALID_CANONICAL_WINDOW_EVIDENCE')
            return
        budget=getattr(self.store,'sacrificeDeadlineSeconds',PROBE_ENTRY_SECONDS)
        if time.time()-anchor>budget:
            self.state(selectionId,'PROBE_FAILED','MISSED_1P_DEADLINE')
            return
        try:
            rate=await self.owner.fx.get()
            amountWei=int((getattr(self.store,'probeGbp',PROBE_GBP)/rate*10**18).to_integral_value(rounding=ROUND_DOWN))
            if amountWei<=0:raise RuntimeError('ZERO_PROBE_VALUE')
            quote=(await self.owner.swaps.verifiedBuyCandidates(token,amountWei))[0]
            if time.time()-anchor>budget:
                raise RuntimeError('MISSED_1P_DEADLINE')
            origin=await self.owner.ponsOnChain(token)
            if origin:raise RuntimeError(origin)
            if time.time()-anchor>budget:
                raise RuntimeError('MISSED_1P_DEADLINE')
            async with self.owner.intakeLock:
                fresh=self.store.db.execute('SELECT status FROM selections WHERE selectionId=?',
                                            (selectionId,)).fetchone()
                if (not fresh or fresh['status']!='LAMB_QUEUED' or
                        not self.store.eligible() or self.revoked(selectionId)):
                    return
                if hasattr(self.store,"canSpend") and not self.store.canSpend("PROBE_BUY"):
                    self.state(selectionId,"PROBE_FAILED","SACRIFICE_BUDGET_LIMIT")
                    return
                if time.time()-anchor>budget:
                    self.state(selectionId,'PROBE_FAILED','MISSED_1P_DEADLINE')
                    return
                # A durable selection-derived intent is idempotent after restart.
                transaction=self.owner.swaps.build(quote,amountWei,self.owner.tx.account.address)
                kwargs=entryKwargs(self.store,anchor,budget)
                if kwargs:transaction=boundEntryTransaction(transaction,kwargs['deadlineAt'])
                signed=await self.owner.tx.create('lamb:'+selectionId,selectionId,'PROBE_BUY',
                    transaction,amountWei,**kwargs)
                self.store.db.execute(
                    "UPDATE selections SET status='PROBE_PENDING',quoteWei=? WHERE selectionId=?",
                    (str(quote['output']),selectionId))
                if signed['state'] in ('SIGNED','SENT','UNCERTAIN'):
                    state=await self.owner.tx.broadcast(signed['intentId'])
                    if state=='EXPIRED_UNSENT':
                        self.state(selectionId,'PROBE_FAILED','MISSED_1P_DEADLINE')
                        return
                print('LAMB 1P SENT',token,signed['txHash'],flush=True)
        except (RuntimeError,ValueError,KeyError,IndexError) as exc:
            reason=str(exc)
            signed=self.store.transactions(selectionId,'PROBE_BUY')
            if signed:
                # Signing can have succeeded even if the broadcast failed.
                # Never turn an on-chain-possible outcome into a fresh order.
                self.state(selectionId,'PROBE_PENDING')
            elif (getattr(self.store,'configuredCampaign',None) is not None and
                  not any(term in reason.upper() for term in (
                      'PONS_V2','WRONG_CHAIN','UNSUPPORTED','INVALID_SELECTION','BANNED')) and
                  time.time()-anchor<budget):
                self.store.db.execute(
                    "UPDATE selections SET reason=? WHERE selectionId=? AND status='LAMB_QUEUED'",
                    (reason[:160],selectionId))
                self.store.set("probeRetryAfter:"+selectionId,time.time()+0.5)
                print('LAMB RETRYABLE',token,reason[:140],flush=True)
                return
            elif not signed:
                self.state(selectionId,'PROBE_FAILED',reason)
            print('LAMB 1P NO BUY',token,reason[:140],flush=True)

    def settle(self,transaction):
        """Consume a confirmed onchain PROBE_BUY receipt, not a theoretical quote."""
        selectionId=transaction['selectionId']
        selection=self.store.db.execute(
            'SELECT * FROM selections WHERE selectionId=?',(selectionId,)).fetchone()
        if not selection or selection['status']!='PROBE_PENDING':
            return
        if transaction['state']=='REVERTED':
            self.state(selectionId,'PROBE_FAILED','PROBE_TX_REVERTED')
            return
        if transaction['state']!='CONFIRMED':
            return
        if not transaction['receipt']:
            self.state(selectionId,'PROBE_UNVERIFIED','CONFIRMED_WITHOUT_RECEIPT')
            return
        receipt=json.loads(transaction['receipt'])
        qty=self.receiptReader(receipt,selection['token'],self.owner.tx.account.address)
        if qty<=0:
            self.state(selectionId,'PROBE_UNVERIFIED','NO_NET_PROBE_TOKEN_TRANSFER')
            return
        if self.waitBlocks is not None or canonicalTiming(self.store):
            try:
                self.blockNumber(receipt.get('blockNumber'))
                if (self.blockNumber(receipt.get('status'))!=1
                        or not isinstance(receipt.get('blockHash'),str)
                        or len(receipt['blockHash'])!=66
                        or receipt.get('transactionHash','').lower()!=transaction['txHash'].lower()):
                    raise ValueError('BAD_1P_RECEIPT')
            except (ValueError,KeyError,TypeError,AttributeError):
                self.state(selectionId,'PROBE_UNVERIFIED','BAD_1P_RECEIPT_BLOCK_EVIDENCE')
                return
        now=time.time()
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            self.store.db.execute("""INSERT OR IGNORE INTO lamb_probes
                (selectionId,token,probeTxHash,confirmedAt,probeQty,probeInputWei,result)
                VALUES(?,?,?,?,?,?,'PROBE_CONFIRMED')""",
                (selectionId,selection['token'],transaction['txHash'],now,
                 str(qty),transaction['inputAmount']))
            self.store.db.execute(
                "UPDATE selections SET status='PROBE_CONFIRMED' WHERE selectionId=?",
                (selectionId,))
            self.store.db.execute('COMMIT')
        except BaseException:
            if self.store.db.in_transaction:self.store.db.execute('ROLLBACK')
            raise
        self.store.observe(token=selection['token'],phase='PROBE_ENTRY',at=now,
            block=str(receipt.get('blockNumber')),venue_id=None,
            status='CONFIRMED_FILL',quantity=str(qty),
            out_wei=str(transaction['inputAmount']),gbp=str(getattr(self.store,'probeGbp',PROBE_GBP)),
            multiple=None,basis='RECEIPT_TOKEN_TRANSFER',reason=None)
        print('LAMB CONFIRMED 1P',selection['token'],qty,flush=True)

    async def check(self,selection):
        """After configured survival gate: fresh 1p sell and 10p two-way quote."""
        selectionId=selection['selectionId']
        if selection['status']!='PROBE_CONFIRMED':
            return
        probe=self.row(selectionId)
        if not probe or probe['confirmedAt'] is None:return
        now=time.time()
        # Fail-closed, but retry temporary provider/quote outages. A previous
        # PROBE_UNKNOWN terminal state silently discarded valid survivors.
        if (probe['result']=='PROBE_CONFIRMED' and probe['lastCheck'] is not None
                and now-float(probe['lastCheck'])<3.0):
            return
        targetBlock=None
        if self.waitBlocks is None and not canonicalTiming(self.store):
            if now<float(probe['confirmedAt'])+getattr(self.store,'probeWaitSeconds',WAIT_SECONDS):
                return
        else:
            # Missing bins are not zero buys; wait for the progressive collector
            # without making repeated canonical-receipt RPC calls.
            if self.activity is not None and len(self.activity.rows(selectionId))!=5:
                return
            targetBlock=await self.blockGateReady(selectionId,probe)
            if targetBlock is None:return
        if canonicalTiming(self.store):
            anchor=float(self.store.get(targetKey(selectionId),'0'))
            if not anchor or time.time()>anchor+self.store.entryDeadlineSeconds:
                self.state(selectionId,'PROBE_FAILED','MISSED_SURVIVAL_DEADLINE')
                return
        if self.store.get('intakeState','OPEN')!='OPEN':
            self.state(selectionId,'PROBE_FAILED','CAMPAIGN_CLOSED')
            return
        if self.revoked(selectionId):
            self.state(selectionId,'PROBE_FAILED','SELECTOR_REVOKED')
            return
        if not self.store.eligible():
            # Reservations for pending normal 10p orders are full. Recheck
            # sellability freshly if a slot becomes available later.
            return
        if self.activity is not None:
            try:
                _,_,probeBlock=self.probeReceipt(selectionId,probe)
                activityVerdict,count=await self.activity.evaluate(selection,probeBlock)
            except (RuntimeError,ValueError,TypeError,KeyError,IndexError) as exc:
                print('PROBE ACTIVITY UNKNOWN',selection['token'],str(exc)[:120],flush=True)
                return
            if activityVerdict=='FAIL':
                self.state(selectionId,'PROBE_FAILED',
                           'MAX_'+str(self.activity.maxBuyingBlocks)+'_BUYING_BLOCKS_EXCEEDED_'+str(count))
                return
            if activityVerdict!='PASS':
                return
        try:
            started=time.time()
            rate=await self.owner.fx.get()
            amountWei=int((self.store.buyGbp/rate*10**18).to_integral_value(rounding=ROUND_DOWN))
            if amountWei<=0:raise RuntimeError('ZERO_10P_VALUE')
            quote=(await self.owner.swaps.verifiedBuyCandidates(selection['token'],amountWei))[0]
            if targetBlock is not None:
                # Full 10p buy and reverse quote are pinned to the same block;
                # prohibit stale quotes predating the 500-block survival gate.
                if (self.blockNumber(quote['block'])<targetBlock
                        or self.blockNumber(quote['preBuySellBlock'])<targetBlock):
                    raise RuntimeError('STALE_500_BLOCK_BUY_QUOTE')
            sell=(await self.owner.swaps.directQuotes(
                selection['token'],int(probe['probeQty']),False,quote['block']))[0]
            if targetBlock is not None and self.blockNumber(sell['block'])<targetBlock:
                raise RuntimeError('STALE_500_BLOCK_PROBE_SELL_QUOTE')
            if int(sell['output'])<=0 or int(quote['preBuySellOutput'])<=0:
                raise RuntimeError('NOT_SELLABLE_AT_SURVIVAL_GATE')
            origin=await self.owner.ponsOnChain(selection['token'])
            if origin:raise RuntimeError(origin)
            if time.time()-started>5:
                raise RuntimeError('STALE_SURVIVAL_GATE_QUOTES')
            async with self.owner.intakeLock:
                current=self.store.db.execute('SELECT status FROM selections WHERE selectionId=?',
                    (selectionId,)).fetchone()
                if (not current or current['status']!='PROBE_CONFIRMED' or
                        self.store.get('intakeState','OPEN')!='OPEN' or self.revoked(selectionId)):
                    return
                if not self.store.eligible():return
                now=time.time()
                if canonicalTiming(self.store) and now>anchor+self.store.entryDeadlineSeconds:
                    self.state(selectionId,'PROBE_FAILED','MISSED_SURVIVAL_DEADLINE')
                    return
                self.store.db.execute('BEGIN IMMEDIATE')
                try:
                    changed=self.store.db.execute(
                        "UPDATE selections SET status='QUEUED',reason=NULL WHERE selectionId=? AND status='PROBE_CONFIRMED'",
                        (selectionId,)).rowcount
                    if changed:
                        self.store.db.execute(
                            "UPDATE lamb_probes SET result='PROBE_PASSED',passedAt=?,lastCheck=?,reason=NULL WHERE selectionId=?",
                            (now,now,selectionId))
                    self.store.db.execute('COMMIT')
                except BaseException:
                    if self.store.db.in_transaction:self.store.db.execute('ROLLBACK')
                    raise
                if changed:print('LAMB PASSED -> ORIGINAL 10P BUY',selection['token'],flush=True)
        except (RuntimeError,ValueError,TypeError,KeyError,IndexError) as exc:
            # An unavailable RPC quote is UNKNOWN, never a grant of permission.
            reason=str(exc)[:160]
            fatal=(any(x in reason.upper() for x in
                       ('PONS_V2','WRONG_CHAIN','INVALID_SELECTION','BANNED'))
                   if getattr(self.store,'configuredCampaign',None) is not None
                   else any(x in reason.upper() for x in
                       ('NOT_SELLABLE','NO_TWO_WAY','ZERO_PROJECTED','PONS_V2','NO_SELL_ROUTE')))
            if canonicalTiming(self.store) and time.time()>anchor+self.store.entryDeadlineSeconds:
                fatal=True
                reason='MISSED_SURVIVAL_DEADLINE'
            verdict='PROBE_FAILED' if fatal else 'PROBE_CONFIRMED'
            if fatal:
                self.state(selectionId,verdict,reason)
            else:
                # Keep scheduler eligibility; store retry reason and bounded
                # cooldown WITHOUT converting an unknown into a rejected buy.
                self.store.db.execute(
                    "UPDATE selections SET reason=? WHERE selectionId=?",
                    (reason,selectionId))
            self.store.db.execute(
                'UPDATE lamb_probes SET lastCheck=?,reason=? WHERE selectionId=?',
                (time.time(),reason,selectionId))
            print('LAMB REJECTED',selection['token'],verdict,reason,flush=True)
