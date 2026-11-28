#!/usr/bin/env python3
"""One-off WETH withdrawal to the SAME wallet, using the existing transaction
journal and wallet lock. No campaign starts, no token swap, no approvals.
Existing signed intent is receipt-checked only, never automatically resubmitted.
"""
import argparse
import asyncio
import fcntl
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import aiohttp
import peak_tracker as peak
from campaign_config import globalWalletLock, otherLiveExecutors
from swap_adapters import calldata
from tools.setup_new_alchemy import requireSameNewCredentials, SOURCE_DEFAULT
from wallet_transactions import TransactionJournal, decodeKeystore

WALLET='0xed74464ac875e3a7c68a7abe44a2ea3da3d27df0'
MAX_WEI=1618868525244218
INTENT='unwrap-20261128'

def amountText(wei):
    return f'{wei//10**18}.{wei%10**18:018d}'

async def balance(rpc,wallet):
    return int(await rpc.call('eth_getBalance',[wallet,'latest']),16)

async def wrapped(rpc,wallet):
    return int(await rpc.ethCall(peak.WETH,calldata('balanceOf(address)',['address'],[wallet]),'latest'),16)

def openJournal(path):
    db=sqlite3.connect(path,isolation_level=None,timeout=2)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.executescript("""CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS transactions(
        intentId TEXT PRIMARY KEY,selectionId TEXT NOT NULL,kind TEXT NOT NULL,
        state TEXT NOT NULL,txHash TEXT NOT NULL,raw TEXT NOT NULL,nonce INTEGER NOT NULL,
        createdAt REAL NOT NULL,lastBroadcast REAL NOT NULL,inputAmount TEXT NOT NULL,
        receipt TEXT,reason TEXT);""")
    return db

async def verifyReceipt(db,rpc,wallet,tx):
    print('WITHDRAWAL TX:',tx['txHash'],flush=True)
    for _ in range(40):
        receipt=await rpc.call('eth_getTransactionReceipt',[tx['txHash']])
        if receipt is not None:
            if (receipt.get('transactionHash','').lower()!=tx['txHash'].lower() or
                receipt.get('from','').lower()!=wallet.lower() or
                receipt.get('to','').lower()!=peak.WETH.lower()):
                raise RuntimeError('WITHDRAWAL_RECEIPT_MISMATCH')
            ok=int(receipt['status'],16)==1
            db.execute('UPDATE transactions SET state=?,receipt=? WHERE intentId=?',
                       ('CONFIRMED' if ok else 'REVERTED',json.dumps(receipt),INTENT))
            if not ok:raise RuntimeError('WITHDRAWAL_REVERTED')
            native=await balance(rpc,wallet)
            weth=await wrapped(rpc,wallet)
            initial=int(db.execute("SELECT value FROM meta WHERE key='initialWeth'").fetchone()[0])
            initialNative=int(db.execute("SELECT value FROM meta WHERE key='initialNative'").fetchone()[0])
            amount=int(tx['inputAmount'])
            if weth>initial-amount or native<=initialNative:
                raise RuntimeError('WITHDRAWAL_BALANCE_DELTA_NOT_VERIFIED')
            print('NATIVE ETH:',amountText(native),flush=True)
            print('WETH:',amountText(weth),flush=True)
            print('PASS WITHDRAWAL_CONFIRMED',flush=True)
            return
        await asyncio.sleep(.5)
    raise RuntimeError('WITHDRAWAL_RECEIPT_PENDING: rerun this tool to check only; do not start campaign')

async def run(args,db):
    requireSameNewCredentials(args.env,SOURCE_DEFAULT)
    url,wallet=peak.loadSettings(args.env,None,args.wallet_file)
    if wallet.lower()!=WALLET:raise RuntimeError('UNEXPECTED_WALLET')
    # Prevent an inherited environment variable from changing the verified RPC.
    import detector
    if url!=detector.loadEnv(args.env)['ROBINHOOD_ALCHEMY_HTTP_URL']:
        raise RuntimeError('RPC_ENVIRONMENT_MISMATCH')
    async with aiohttp.ClientSession() as session:
        rpc=peak.Rpc(session,url,wallet)
        if int(await rpc.call('eth_chainId',[]),16)!=4663:raise RuntimeError('WRONG_CHAIN')
        old=db.execute('SELECT * FROM transactions WHERE intentId=?',(INTENT,)).fetchone()
        if old:
            # Never send again even after a crash or an uncertain response.
            await verifyReceipt(db,rpc,wallet,old)
            return
        if otherLiveExecutors():raise RuntimeError('STOP_EXISTING_LIVE_EXECUTOR_FIRST')
        latest,pending=await asyncio.gather(
            rpc.call('eth_getTransactionCount',[wallet,'latest']),
            rpc.call('eth_getTransactionCount',[wallet,'pending']))
        if int(latest,16)!=int(pending,16):raise RuntimeError('WALLET_PENDING_TRANSACTION')
        native,weth=await asyncio.gather(balance(rpc,wallet),wrapped(rpc,wallet))
        amount=min(weth,MAX_WEI)
        print('NATIVE ETH BEFORE:',amountText(native),flush=True)
        print('WETH BEFORE:',amountText(weth),flush=True)
        if amount<=0:raise RuntimeError('NO_WETH_TO_WITHDRAW')
        print('UNWRAP WETH:',amountText(amount),flush=True)
        tx={'to':peak.WETH,'value':0,'data':calldata('withdraw(uint256)',['uint256'],[amount])}
        if not args.apply:
            await rpc.call('eth_call',[{'from':wallet,'to':tx['to'],'data':tx['data'],'value':'0x0'},'latest'])
            gas=int(await rpc.call('eth_estimateGas',[{'from':wallet,'to':tx['to'],'data':tx['data'],'value':'0x0'}]),16)
            print('PASS READ_ONLY_WITHDRAWAL_SIMULATION | gas estimate',gas,flush=True)
            return
        account,key=decodeKeystore(args.wallet_file,getpass.getpass('Local encrypted wallet password: '))
        if account.address.lower()!=wallet.lower():raise RuntimeError('WALLET_MISMATCH')
        journal=TransactionJournal(SimpleNamespace(db=db),rpc,account,key)
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('initialWeth',str(weth)))
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('initialNative',str(native)))
        signed=await journal.create(INTENT,INTENT,'UNWRAP_WETH',tx,amount)
        # The actual transaction is zero native value and withdraws only WETH
        # from this wallet. Persist possible submission BEFORE sending.
        db.execute("UPDATE transactions SET state='UNCERTAIN',lastBroadcast=?,reason='SUBMISSION_ATTEMPT_STARTED' WHERE intentId=?",
                   (time.time(),INTENT))
        print('SIGNED WITHDRAWAL:',signed['txHash'],flush=True)
        await journal.broadcast(INTENT)
        await verifyReceipt(db,rpc,wallet,signed)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env',type=Path,default=Path.home()/'.config/blackswan/concentration-new-alchemy.env')
    p.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    os.umask(0o077)
    lockPath=globalWalletLock(WALLET)
    lockPath.parent.mkdir(parents=True,exist_ok=True)
    path=Path.home()/'.local/state/blackswan/unwrap-20261128.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    with lockPath.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if otherLiveExecutors():raise RuntimeError('STOP_EXISTING_LIVE_EXECUTOR_FIRST')
        db=openJournal(path)
        try:asyncio.run(run(args,db))
        finally:db.close()

if __name__=='__main__':main()
