"""Durable Robinhood transaction journal. Never submits a replacement nonce implicitly.

A signed raw transaction is committed to SQLite BEFORE network submission. A timed-out
broadcast is retried with the identical signed bytes, and receipts are authoritative.
"""
import asyncio
import json
import math
import time
from pathlib import Path

from broadcast_rpc import BroadcastError, sendRaw

CHAIN_ID = 4663


def eip1559FeeFields(block, suggestedPriority):
    """Use live base fee and RPC priority estimate, not a fixed legacy gas price.

    2*baseFee headroom reduces near-term base fee race rejections.
    MaxFeePerGas is a ceiling, not the actual price charged.
    Fail closed on missing/malformed EIP-1559 data.
    """
    if not isinstance(block, dict) or block.get('baseFeePerGas') is None:
        raise RuntimeError('EIP1559_BASE_FEE_MISSING')
    try:
        base=int(block['baseFeePerGas'],16)
        tip=int(suggestedPriority,16)
    except (TypeError,ValueError) as exc:
        raise RuntimeError('EIP1559_FEE_QUOTE_INVALID') from exc
    if base<=0 or tip<0:
        raise RuntimeError('EIP1559_FEE_QUOTE_INVALID')
    cap=2*base+tip
    if cap<=tip:
        raise RuntimeError('EIP1559_FEE_CAP_INVALID')
    return {'type':2,'maxPriorityFeePerGas':tip,'maxFeePerGas':cap}



def decodeKeystore(walletFile, password):
    # Delayed import: read-only commands/tests never load a signing dependency.
    from eth_account import Account
    data = json.loads(Path(walletFile).read_text())
    privateKey = Account.decrypt(data, password)
    account = Account.from_key(privateKey)
    published = data.get('address', '')
    published = published if published.startswith('0x') else '0x' + published
    if account.address.lower() != published.lower():
        raise ValueError('Keystore address mismatch')
    return account, privateKey


class TransactionJournal:
    def __init__(self, store, rpc, account, privateKey):
        self.store, self.rpc = store, rpc
        self.account, self.privateKey = account, privateKey
        self.lock = asyncio.Lock()
        self.lastCheck = 0.0

    async def rpcCall(self, method, params):
        return await self.rpc.call(method, params)

    def unsettled(self):
        return self.store.db.execute("SELECT * FROM transactions WHERE state IN ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK') ORDER BY createdAt").fetchall()

    def entryDeadline(self,intentId):
        # Existing ledgers already have the meta table. No schema migration.
        row=self.store.db.execute("SELECT value FROM meta WHERE key=?",('entryDeadline:'+intentId,)).fetchone()
        return float(row[0]) if row else None

    @staticmethod
    def requireDeadline(deadlineAt):
        if deadlineAt is not None and (not math.isfinite(deadlineAt) or time.time()>deadlineAt):
            raise RuntimeError('MISSED_ENTRY_SIGNING_DEADLINE')

    async def create(self, intentId, selectionId, kind, transaction, inputAmount=0, *, deadlineAt=None):
        """Return existing intent unchanged on replay. Only one in-flight transaction.

        A FAILED transaction may be retried by the caller with a fresh attempt ID;
        never alter a signed transaction's nonce/raw bytes.
        """
        async with self.lock:
            existing = self.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intentId,)).fetchone()
            if existing:
                return dict(existing)
            if deadlineAt is not None and kind not in ('BUY','PROBE_BUY'):
                raise ValueError('DEADLINE_ONLY_FOR_ENTRY')
            self.requireDeadline(deadlineAt)
            if self.unsettled():
                raise RuntimeError('WALLET_TRANSACTION_IN_FLIGHT')
            chainId = int(await self.rpcCall('eth_chainId', []), 16)
            if chainId != CHAIN_ID:
                raise RuntimeError('WRONG_CHAIN')
            tx = dict(transaction)
            tx['chainId'] = CHAIN_ID
            tx['from'] = self.account.address
            if 'to' not in tx or not tx['to']:
                raise ValueError('No recipient contract')
            code = await self.rpcCall('eth_getCode',[tx['to'],'latest'])
            if not code or code == '0x':
                raise RuntimeError('TARGET_CONTRACT_HAS_NO_CODE')
            # Simulate exact calldata from this address; do not sign known reverts.
            await self.rpcCall('eth_call',[{
                'from':tx['from'],'to':tx['to'],'data':tx.get('data','0x'),
                'value':hex(tx.get('value',0))},'latest'])
            gasEstimate = int(await self.rpcCall('eth_estimateGas',[{
                'from':tx['from'],'to':tx['to'],'data':tx.get('data','0x'),
                'value':hex(tx.get('value',0))}]),16)
            # Leave room for state changes without treating gas as a trading signal.
            tx['gas'] = gasEstimate * 125 // 100 + 15000
            # This chain exposes EIP-1559 baseFee + priority fees. A legacy
            # gasPrice copied directly from eth_gasPrice can be under the next
            # block's base fee by the time eth_sendRawTransaction is processed.
            block=await self.rpcCall('eth_getBlockByNumber',['latest',False])
            suggestedPriority=await self.rpcCall('eth_maxPriorityFeePerGas',[])
            tx.update(eip1559FeeFields(block,suggestedPriority))
            nonce = int(await self.rpcCall('eth_getTransactionCount',[tx['from'],'pending']),16)
            # One local pending transaction at a time, and no duplicate nonce reuse.
            tx['nonce'] = nonce
            if int(await self.rpcCall('eth_getBalance',[tx['from'],'latest']),16) < tx['value']+tx['gas']*tx['maxFeePerGas']:
                raise RuntimeError('INSUFFICIENT_ETH_FOR_SWAP_AND_GAS')
            tx.pop('from')
            # eth-account 0.13.7 rejects a lowercase hexadecimal recipient.
            # Keep RPC calls as-is; checksum the exact same 20 bytes for signing.
            from eth_utils import to_checksum_address
            tx['to'] = to_checksum_address(tx['to'])
            self.requireDeadline(deadlineAt)
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed,'raw_transaction',None)
            if raw is None:  # Support only explicitly verified SDK attributes.
                raise RuntimeError('eth-account API probe failed: raw_transaction missing')
            txHash = '0x'+bytes(signed.hash).hex()
            rawHex = '0x'+bytes(raw).hex()
            self.requireDeadline(deadlineAt)
            if deadlineAt is not None:
                self.store.db.execute('BEGIN IMMEDIATE')
            try:
                with self.store.db:
                    self.store.db.execute('''INSERT INTO transactions
                        (intentId,selectionId,kind,state,txHash,raw,nonce,createdAt,lastBroadcast,
                        inputAmount,receipt,reason) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)''',
                        (intentId,selectionId,kind,'SIGNED',txHash,rawHex,nonce,time.time(),0.0,str(inputAmount)))
                    if deadlineAt is not None:
                        self.store.db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                                              ('entryDeadline:'+intentId,str(deadlineAt)))
            except BaseException:
                if deadlineAt is not None and self.store.db.in_transaction:self.store.db.rollback()
                raise
            return dict(self.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intentId,)).fetchone())

    async def broadcast(self, intentId):
        """Submit a signed transaction and record the ACTUAL response.

        Rejected: known pre-admission RPC error (do not retry or reuse nonce).
        Uncertain: network/provider failure; same raw bytes may be retried.
        Sent: node returned matching hash. None of these means confirmed fill.
        """
        row=self.store.db.execute(
            'SELECT * FROM transactions WHERE intentId=?',(intentId,)).fetchone()
        if not row or row['state'] not in ('SIGNED','SENT','UNCERTAIN'):
            return None
        now=time.time()
        deadline=self.entryDeadline(intentId) if row['kind'] in ('BUY','PROBE_BUY') else None
        if deadline is not None:
            if now>deadline:
                if row['state']=='SIGNED' and not row['lastBroadcast']:
                    self.store.db.execute("UPDATE transactions SET state='EXPIRED_UNSENT',reason='ENTRY_DEADLINE_EXPIRED_BEFORE_SUBMISSION' WHERE intentId=?",(intentId,))
                    return 'EXPIRED_UNSENT'
                # Do not retransmit stale entries; keep possible submission reconcilable.
                return row['state']
            # Record the attempt BEFORE the external side effect. A crash here
            # may be uncertain but can never be mistaken for definitely unsent.
            self.store.db.execute("UPDATE transactions SET state='UNCERTAIN',lastBroadcast=?,reason='SUBMISSION_ATTEMPT_STARTED' WHERE intentId=?",(now,intentId))
            if time.time()>deadline:
                # No external call occurred in this invocation. Prior attempts,
                # if any, must remain reconcilable.
                if row['state']=='SIGNED' and not row['lastBroadcast']:
                    self.store.db.execute("UPDATE transactions SET state='EXPIRED_UNSENT',reason='ENTRY_DEADLINE_EXPIRED_BEFORE_SUBMISSION' WHERE intentId=?",(intentId,))
                    return 'EXPIRED_UNSENT'
                return 'UNCERTAIN'
        try:
            returned=await sendRaw(self.rpc,row['raw'])
            if returned.lower()!=row['txHash'].lower():
                raise BroadcastError('HASH_MISMATCH','Node returned a different transaction hash',False)
        except BroadcastError as exc:
            state='REJECTED' if exc.definite else 'UNCERTAIN'
            self.store.db.execute(
                'UPDATE transactions SET state=?,lastBroadcast=?,reason=? WHERE intentId=?',
                (state,now,str(exc),intentId))
            print('TX '+state, row['kind'], row['txHash'],str(exc),flush=True)
            return state
        except Exception as exc:
            # An arbitrary failure does not prove whether the node accepted it.
            reason='UNCERTAIN_EXCEPTION:'+type(exc).__name__+':'+str(exc)[:160]
            self.store.db.execute(
                "UPDATE transactions SET state='UNCERTAIN',lastBroadcast=?,reason=? WHERE intentId=?",
                (now,reason,intentId))
            print('TX UNCERTAIN',row['kind'],row['txHash'],reason,flush=True)
            return 'UNCERTAIN'
        self.store.db.execute(
            "UPDATE transactions SET state='SENT',lastBroadcast=?,reason=NULL WHERE intentId=?",
            (now,intentId))
        print('TX ACCEPTED_BY_RPC',row['kind'],row['txHash'],flush=True)
        return 'SENT'

    async def reconcile(self):
        now=time.time()
        if now-self.lastCheck < 1.0:
            return []
        self.lastCheck=now
        completed=[]
        for row in self.unsettled():
            try:
                receipt=await self.rpcCall('eth_getTransactionReceipt',[row['txHash']])
            except RuntimeError:
                continue
            if receipt:
                state='CONFIRMED' if int(receipt['status'],16)==1 else 'REVERTED'
                self.store.db.execute('''UPDATE transactions SET state=?,receipt=?,reason=?
                    WHERE intentId=?''',(state,json.dumps(receipt),None if state=='CONFIRMED' else 'receipt status 0',row['intentId']))
                completed.append(row['intentId'])
            elif (row['kind'] in ('BUY','PROBE_BUY') and row['state']=='SIGNED'
                  and not row['lastBroadcast'] and self.entryDeadline(row['intentId']) is not None
                  and time.time()>self.entryDeadline(row['intentId'])):
                await self.broadcast(row['intentId'])
            elif now-row['createdAt']>120:
                # An old swap cannot safely be resurrected indefinitely: leave
                # its nonce unresolved until a human checks chain/mempool state.
                if row['state']!='STUCK':
                    self.store.db.execute("UPDATE transactions SET state='STUCK',reason='UNCONFIRMED_AFTER_120S_MANUAL_RECONCILIATION_REQUIRED' WHERE intentId=?",(row['intentId'],))
                    print('CRITICAL STUCK TRANSACTION',row['intentId'],row['txHash'],flush=True)
            elif row['state'] in ('SIGNED','SENT','UNCERTAIN') and now-row['lastBroadcast']>=10:
                # Resend identical signed bytes only within the freshness window.
                await self.broadcast(row['intentId'])
        return completed

    def confirmed(self, intentId):
        row=self.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intentId,)).fetchone()
        if row and row['state'] in ('CONFIRMED','REVERTED'):
            return dict(row)
        return None