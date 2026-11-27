#!/usr/bin/env python3
"""One-shot wallet-wide ERC-20 liquidation on Robinhood Chain (4663).

Preview never signs. Execute unlocks the configured local keystore.
No buys, no forced changes to detector, selector, or existing campaigns.
Trades use the current exact token balance and only discovered v3/v4 venues.
No implicit retry with a new nonce. Never print a private key or signed raw tx.
"""
import argparse
import asyncio
import fcntl
import getpass
import json
import os
from pathlib import Path
import sqlite3
import time

import aiohttp

import peak_tracker as peak
from manual_rpc import ManualRpc, PacedSession, ProviderUnavailable, transientFailure
from swap_adapters import SwapAdapters, calldata, NATIVE, WETH, USDG
from wallet_transactions import TransactionJournal, decodeKeystore
from live_executor import ExecutionStore


def tokenAddress(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        raise ValueError('Invalid token contract address')
    int(value[2:], 16)
    return value.lower()


def loadKnownVenues(dataDir, wanted):
    """Use the actual pool keys recorded in earlier campaigns and detections."""
    venues={token:{} for token in wanted}
    histories=[]
    for path in sorted(dataDir.glob('**/execution.sqlite3')):
        if 'liquidation_' in str(path):
            continue
        try:
            with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True) as db:
                for token,raw in db.execute('SELECT token,data FROM venues'):
                    token=token.lower()
                    if token not in venues:
                        continue
                    try:
                        venue=json.loads(raw)
                        if venue['source'] in ('uniswapV3','uniswapV4','ponsV2'):
                            venues[token][venue['id']]=venue
                    except (ValueError,KeyError):
                        continue
                for (token,) in db.execute('SELECT token FROM positions'):
                    histories.append(token.lower())
        except (sqlite3.Error,OSError):
            continue
    for path in sorted(dataDir.glob('**/detector.sqlite3')):
        try:
            with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True) as db:
                for (raw,) in db.execute('SELECT payload FROM event_log'):
                    if not any(token[2:] in raw.lower() for token in wanted):
                        continue
                    try:
                        for token,venue in peak.launchVenues(json.loads(raw)):
                            token=token.lower()
                            if token in venues and venue['source'] in ('uniswapV3','uniswapV4','ponsV2'):
                                venues[token][venue['id']]=venue
                    except (ValueError,KeyError,TypeError):
                        continue
        except (sqlite3.Error,OSError):
            continue
    if USDG in venues:
        for bridge in peak.USDG_BRIDGES:
            venues[USDG][bridge['id']]=bridge
    return {token:list(rows.values()) for token,rows in venues.items()}


def campaignTokens(dataDir):
    """Discover ALL previously purchased tokens, including unsold old and new."""
    tokens=set()
    for path in dataDir.glob('**/execution.sqlite3'):
        if 'liquidation_' in str(path):
            continue
        try:
            with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True) as db:
                tokens.update(tokenAddress(r[0]) for r in db.execute('SELECT token FROM positions'))
        except (sqlite3.Error,ValueError,OSError):
            continue
    return tokens


async def indexedWalletTokens(session, url, wallet):
    """Alchemy's paginated wallet balance API; return (tokens, complete, reason).

    Not every network/account supports the enhanced method. A partial result is
    never described as full-wallet discovery.
    """
    tokens=set()
    seen=set()
    page=None
    for _ in range(100):
        params=[wallet,'erc20',{'maxCount':100}]
        if page:
            params[2]['pageKey']=page
        request={'jsonrpc':'2.0','id':1,'method':'alchemy_getTokenBalances','params':params}
        try:
            async with session.post(url,json=request,
                    timeout=aiohttp.ClientTimeout(total=12)) as response:
                result=await response.json(content_type=None)
            if response.status!=200:
                return tokens,False,'INDEXER_HTTP_'+str(response.status)
            if not isinstance(result,dict):
                return tokens,False,'INVALID_INDEXER_RESPONSE'
            if result.get('error') is not None:
                return tokens,False,'INDEXER_RPC_ERROR'
            payload=result.get('result')
            if not isinstance(payload,dict) or not isinstance(payload.get('tokenBalances'),list):
                return tokens,False,'invalid_token_balance_response'
            invalidRows=False
            for row in payload['tokenBalances']:
                if not isinstance(row,dict) or row.get('error') is not None or row.get('tokenBalance') is None:
                    invalidRows=True
                    continue
                try:
                    if row.get('tokenBalance') and int(row['tokenBalance'],16)>0:
                        tokens.add(tokenAddress(row['contractAddress']))
                except (KeyError,ValueError,TypeError):
                    invalidRows=True
            if invalidRows:
                return tokens,False,'INVALID_INDEXER_TOKEN_ROWS'
            newPage=payload.get('pageKey')
            if newPage is not None and not isinstance(newPage,str):
                return tokens,False,'INVALID_INDEXER_CURSOR'
            if not newPage:
                return tokens,True,'complete'
            if newPage in seen:
                return tokens,False,'repeated_page_cursor'
            seen.add(newPage)
            page=newPage
        except ProviderUnavailable:
            raise
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError,TypeError) as exc:
            return tokens,False,'balance_index_error:'+type(exc).__name__
    return tokens,False,'balance_page_limit'


async def tokenBalance(rpc,token,wallet):
    result=await rpc.ethCall(token,calldata('balanceOf(address)',
        ['address'],[wallet]),'latest')
    return int(result,16)


class VenueStore:
    def __init__(self,venues):
        self.rows=venues
    def venues(self,token):
        return list(self.rows.get(token.lower(),()))


def initializeSweep(dbPath,wallet):
    store=ExecutionStore(dbPath)
    existing=store.get('liquidationWallet','')
    if existing and existing.lower()!=wallet.lower():
        raise RuntimeError('SWEEP_WALLET_MISMATCH')
    store.set('liquidationWallet',wallet.lower())
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS sweep_targets(
      token TEXT PRIMARY KEY, state TEXT NOT NULL,
      originalQty TEXT NOT NULL, observedQty TEXT,
      soldQty TEXT, txHash TEXT, reason TEXT,
      updatedAt REAL NOT NULL);
    ''')
    return store


async def ensurePriorNoncesResolved(rpc,dataDir,wallet,currentSweepDir):
    """Do not sign across another campaign's unresolved nonce."""
    latest=int(await rpc.call('eth_getTransactionCount',[wallet,'latest']),16)
    pending=int(await rpc.call('eth_getTransactionCount',[wallet,'pending']),16)
    if latest!=pending:
        # A restart of THIS exact sweep must be allowed to reconcile its own
        # pending hash, never to allocate another nonce meanwhile.
        ownPath=currentSweepDir/'execution.sqlite3'
        own=set()
        if ownPath.exists():
            with sqlite3.connect(f'file:{ownPath.resolve()}?mode=ro',uri=True) as db:
                own={int(row[0]) for row in db.execute(
                    "SELECT nonce FROM transactions WHERE state IN ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK')")}
        if latest not in own:
            raise RuntimeError('WALLET_PENDING_NONCE; reconcile existing broadcasts first')
    for path in dataDir.glob('**/execution.sqlite3'):
        if path.resolve()==(currentSweepDir/'execution.sqlite3').resolve():
            continue
        try:
            with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True) as db:
                rows=db.execute("SELECT nonce,txHash FROM transactions WHERE state IN ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK')").fetchall()
            for nonce,txHash in rows:
                if int(nonce)>=latest:
                    raise RuntimeError('UNRESOLVED_PRIOR_TRANSACTION '+txHash)
        except sqlite3.Error:
            continue


async def awaitReceipt(journal,intentId,seconds=120):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        await journal.reconcile()
        row=journal.store.db.execute('SELECT * FROM transactions WHERE intentId=?',(intentId,)).fetchone()
        if row and row['state'] in ('CONFIRMED','REVERTED','REJECTED','STUCK'):
            return row
        await asyncio.sleep(2)
    raise RuntimeError('PENDING_TRANSACTION_PRESERVED '+intentId)


# Manual operator policy is independent of the automatic campaign strategy.
from decimal import Decimal
from campaign_config import globalWalletLock, otherLiveExecutors
from wallet_transactions import eip1559FeeFields
import uuid

WEI = Decimal(10) ** 18
MINIMUM_GBP = Decimal('0.02')
SLIPPAGE_BPS = 300
APPROVAL_GAS_CAP = 180000
SALE_GAS_CAP = 1200000


def salePolicy(quotedWei, rate):
    """Require the operator’s 2p gross quote threshold."""
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate <= 0:
        return 'FX_UNAVAILABLE'
    if Decimal(quotedWei) * rate / WEI < MINIMUM_GBP:
        return 'BELOW_2P'
    return 'READY'


async def freshRate(fx):
    rate = await fx.get()
    if time.time()-fx.updated > 60 or not rate.is_finite() or rate <= 0:
        raise RuntimeError('FX_UNAVAILABLE')
    return rate


def txFee(receipt):
    return int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16)



def paidFees(store, token=None):
    query = "SELECT receipt FROM transactions WHERE receipt IS NOT NULL"
    values = ()
    if token:
        query += ' AND selectionId=?'
        values = (token,)
    return sum(txFee(json.loads(row[0])) for row in store.db.execute(query,values))


def saveResult(store, token, **result):
    previous=store.db.execute('SELECT payload FROM manual_results WHERE token=?',(token,)).fetchone()
    data=json.loads(previous[0]) if previous else {'token':token}
    data.update(result)
    data['accountingPending']=any(not fill.get('income') or int(fill.get('debited','0'))<=0 for fill in data.get('fills',[]))
    if data['accountingPending'] and data.get('state') in ('SOLD','PARTIAL','SALE_CONFIRMED'):
        data['state']='MINED_ACCOUNTING_PENDING'
    store.db.execute('INSERT OR REPLACE INTO manual_results VALUES(?,?)',(token,json.dumps(data)))
    print(token, data.get('state',''), data.get('estimatedGbp',''), data.get('reason',''),flush=True)
    return data


async def indexedCandidates(session, url, wallet):
    tokens, complete, reason=await indexedWalletTokens(session,url,wallet)
    # Index pagination is useful coverage evidence, but its unpublished watermark
    # cannot certify a completely empty wallet at the chain head.
    evidence={'alchemyPagesComplete':complete,'alchemyReason':reason,'coverage':'INDEXED_WATERMARK_UNVERIFIED'}
    try:
        nextParams={}
        seen=set()
        for _ in range(100):
            async with session.get('https://robinhoodchain.blockscout.com/api/v2/addresses/'+wallet+'/tokens',
                    params={'type':'ERC-20',**nextParams},timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status!=200:
                    raise RuntimeError('BLOCKSCOUT_HTTP_'+str(response.status))
                data=await response.json()
            if not isinstance(data,dict) or not isinstance(data.get('items'),list):
                raise RuntimeError('BLOCKSCOUT_INVALID_RESPONSE')
            for item in data['items']:
                if not isinstance(item,dict) or not isinstance(item.get('token'),dict):
                    raise RuntimeError('BLOCKSCOUT_INVALID_TOKEN_ROW')
                tokens.add(tokenAddress(item['token']['address_hash']))
            nextParams=data.get('next_page_params')
            if nextParams is not None and not isinstance(nextParams,dict):
                raise RuntimeError('BLOCKSCOUT_INVALID_CURSOR')
            if not nextParams:
                evidence['blockscoutPagesComplete']=True
                break
            cursor=json.dumps(nextParams,sort_keys=True)
            if cursor in seen:
                raise RuntimeError('BLOCKSCOUT_REPEATED_CURSOR')
            seen.add(cursor)
        else:
            raise RuntimeError('BLOCKSCOUT_PAGE_LIMIT')
    except (RuntimeError,ValueError,KeyError,TypeError,aiohttp.ClientError,asyncio.TimeoutError) as error:
        evidence['blockscoutPagesComplete']=False
        evidence['blockscoutReason']=str(error) if isinstance(error,RuntimeError) else type(error).__name__
    return tokens,evidence


async def inventory(rpc, session, url, wallet, dataDir, store, extras):
    tokens,evidence=await indexedCandidates(session,url,wallet)
    tokens.update(campaignTokens(dataDir))
    tokens.update(tokenAddress(token) for token in extras)
    tokens.update(row[0] for row in store.db.execute('SELECT token FROM manual_results'))
    tokens.update((WETH,USDG))
    tokens.discard(NATIVE)
    block=await rpc.call('eth_getBlockByNumber',['latest',False])
    if not isinstance(block,dict) or not block.get('number') or not block.get('hash'):
        raise RuntimeError('INVENTORY_BLOCK_UNAVAILABLE')
    balances={}
    print('Checking wallet balances ·',len(tokens),'token contracts',flush=True)
    for token in sorted(tokens):
        try:
            raw=await rpc.ethCall(token,calldata('balanceOf(address)',['address'],[wallet]),block['number'])
            balances[token]=int(raw,16)
        except ProviderUnavailable:
            raise
        except (RuntimeError,ValueError,TypeError):
            balances[token]=None
    evidence.update(block=block['number'],blockHash=block['hash'])
    return balances,evidence


async def discoverVenues(rpc, token, existing, issues=None):
    """Discover pools with bounded calls and preserve every successful result."""
    import detector
    from eth_abi import decode
    from eth_abi.exceptions import DecodingError
    found={venue['id']:venue for venue in existing}
    issues=issues if issues is not None else []
    semaphore=asyncio.Semaphore(4)

    async def v3Pool(base, fee):
        try:
            async with semaphore:
                raw=await rpc.ethCall(detector.V3_FACTORY,calldata('getPool(address,address,uint24)',['address','address','uint24'],[token,base,fee]),'latest')
            pool=decode(['address'],bytes.fromhex(raw[2:]))[0].lower()
            if pool!=NATIVE:
                t0,t1=sorted((token,base))
                found[pool]=dict(id=pool,source='uniswapV3',token0=t0,token1=t1,fee=fee)
        except ProviderUnavailable:
            raise
        except (RuntimeError,ValueError,TypeError,DecodingError,asyncio.TimeoutError):
            issues.append('V3_LOOKUP_FAILED')

    await asyncio.gather(*(v3Pool(base,fee) for base in (WETH,USDG) for fee in (100,500,3000,10000)))
    topic='0x'+token[2:].rjust(64,'0')
    spec=detector.SOURCES['uniswapV4']

    async def v4Pools(topics):
        try:
            logs=await rpc.call('eth_getLogs',[dict(address=spec['address'],fromBlock='0x0',toBlock='latest',topics=topics)])
            if not isinstance(logs,list):
                raise ValueError('Invalid pool logs')
            for raw in logs:
                if not isinstance(raw,dict):
                    raise ValueError('Invalid pool log')
                if raw.get('removed') or raw.get('address','').lower()!=spec['address']:
                    continue
                event=detector.decodeLog(raw,recovered=True)
                for held,venue in peak.launchVenues(event):
                    if held==token:
                        found[venue['id']]=venue
        except ProviderUnavailable:
            raise
        except (RuntimeError,ValueError,KeyError,TypeError,asyncio.TimeoutError):
            issues.append('V4_HISTORY_UNAVAILABLE')

    await asyncio.gather(v4Pools([spec['topic'],None,topic]),v4Pools([spec['topic'],None,None,topic]))
    return list(found.values())


async def closingInventory(rpc,session,url,wallet,dataDir,store,extras):
    try:
        balances,evidence=await inventory(rpc,session,url,wallet,dataDir,store,extras)
        if not evidence.get('alchemyPagesComplete') and not evidence.get('blockscoutPagesComplete'):
            evidence['coverage']='CLOSING_INVENTORY_FAILED'
        return balances,evidence
    except (RuntimeError,ValueError,KeyError,TypeError,aiohttp.ClientError,asyncio.TimeoutError) as error:
        return {}, {'coverage':'CLOSING_INVENTORY_FAILED','reason':type(error).__name__}


async def feePrice(rpc):
    block=await rpc.call('eth_getBlockByNumber',['latest',False])
    priority=await rpc.call('eth_maxPriorityFeePerGas',[])
    return eip1559FeeFields(block,priority)['maxFeePerGas']


async def receiptIncome(rpc, receipt, token, wallet):
    """Return actual debit and attributable output. Unknown native payout stays unknown."""
    from live_executor import receiptTransfers
    block=await rpc.call('eth_getBlockByNumber',[receipt['blockNumber'],False])
    if not isinstance(block,dict) or not isinstance(block.get('hash'),str) or block['hash'].lower()!=receipt['blockHash'].lower():
        raise RuntimeError('RECEIPT_REORG')
    tx=await rpc.call('eth_getTransactionByHash',[receipt['transactionHash']])
    if not isinstance(tx,dict) or str(tx.get('from','')).lower()!=wallet.lower() or str(tx.get('blockHash','')).lower()!=receipt['blockHash'].lower():
        raise RuntimeError('RECEIPT_IDENTITY_UNVERIFIED')
    debit=-receiptTransfers(receipt,token,wallet)
    weth=receiptTransfers(receipt,WETH,wallet)
    usdg=receiptTransfers(receipt,USDG,wallet)
    if weth>0:
        return debit,{'asset':'WETH','raw':str(weth),'decimals':18,'basis':'RECEIPT_TRANSFERS'}
    if usdg>0:
        return debit,{'asset':'USDG','raw':str(usdg),'decimals':None,'basis':'RECEIPT_TRANSFERS'}
    try:
        trace=await rpc.call('debug_traceTransaction',[receipt['transactionHash'],{'tracer':'callTracer'}])
        credit=0
        def walk(call):
            nonlocal credit
            if not isinstance(call,dict):
                raise ValueError('INVALID_TRACE_RESPONSE')
            if call.get('error'):
                return
            value=int(call.get('value','0x0'),16)
            if call.get('type','').upper() in ('CALL','CREATE','CREATE2','SELFDESTRUCT'):
                if call.get('to','').lower()==wallet.lower():
                    credit+=value
                if call.get('from','').lower()==wallet.lower():
                    credit-=value
            for child in call.get('calls',[]):
                walk(child)
        walk(trace)
        if credit>0:
            return debit,{'asset':'ETH','raw':str(credit),'decimals':18,'basis':'TRANSACTION_CALL_TRACE'}
    except (RuntimeError,ValueError,KeyError,TypeError):
        pass
    return debit,None


async def accountReceipts(rpc,store,wallet):
    for row in store.db.execute("SELECT * FROM transactions WHERE kind='MANUAL_SELL' AND state IN ('CONFIRMED','REVERTED')").fetchall():
        receipt=json.loads(row['receipt'])
        prior=store.db.execute('SELECT payload FROM manual_results WHERE token=?',(row['selectionId'],)).fetchone()
        data=json.loads(prior[0]) if prior else {'token':row['selectionId']}
        fills=data.get('fills',[])
        if any(fill['txHash']==row['txHash'] and fill.get('income') and int(fill.get('debited','0'))>0 for fill in fills):
            continue
        if int(receipt['status'],16)==0:
            continue
        accountingReason=None
        try:
            debit,income=await receiptIncome(rpc,receipt,row['selectionId'],wallet)
        except (RuntimeError,ValueError,KeyError,TypeError,aiohttp.ClientError,asyncio.TimeoutError) as error:
            debit,income=0,None
            accountingReason=type(error).__name__
        fills=[fill for fill in fills if fill['txHash']!=row['txHash']]
        fxData=json.loads(store.get('manualFx:'+row['intentId'],'{}'))
        fills.append({'txHash':row['txHash'],'debited':str(debit),'income':income,'gasWei':str(txFee(receipt)),'accountingReason':accountingReason,**fxData})
        saveResult(store,row['selectionId'],fills=fills,state='MINED_ACCOUNTING_PENDING' if debit<=0 or not income else 'SALE_CONFIRMED')


async def sendManual(journal,token,kind,tx,amount):
    # Intent number is persisted before a transaction is created. On restart all
    # prior journal rows reconcile before this method is reached.
    number=journal.store.db.execute('SELECT count(*) FROM transactions').fetchone()[0]
    intent=f'manual:{token}:{number}:{kind}'
    if kind=='MANUAL_SELL':
        row=journal.store.db.execute('SELECT payload FROM manual_results WHERE token=?',(token,)).fetchone()
        data=json.loads(row[0])
        journal.store.set('manualFx:'+intent,json.dumps({key:data[key] for key in ('fxGbpPerEth','fxAt')}))
    await journal.create(intent,token,kind,tx,amount)
    await journal.broadcast(intent)
    if getattr(journal.rpc.session,'paused',False):
        raise ProviderUnavailable('PROVIDER_PAUSED; signed transaction preserved')
    row=await awaitReceipt(journal,intent,seconds=45)
    if row['state'] not in ('CONFIRMED','REVERTED'):
        raise RuntimeError('PENDING_TRANSACTION_PRESERVED')
    if row['state']=='REVERTED':
        raise RuntimeError('CONFIRMED_REVERT')
    return json.loads(row['receipt'])


async def sellHolding(rpc,adapter,journal,store,wallet,token,fx):
    for chunk in range(8):
        amount=await tokenBalance(rpc,token,wallet)
        if amount==0:
            saveResult(store,token,state='SOLD',remainingRaw='0')
            return
        rate=await freshRate(fx)
        quotes=None
        for split in range(9):
            try:
                quotes=await adapter.directQuotes(token,amount,False)
                quotes=[q for q in quotes if q['venue']['source'] in ('uniswapV3','uniswapV4') and int(q['output'])>0]
                if quotes:
                    break
            except (RuntimeError,ValueError) as error:
                if transientFailure(error):
                    raise ProviderUnavailable('PROVIDER_PAUSED during quotes') from error
            amount//=2
            if amount<=0:
                break
        if not quotes:
            saveResult(store,token,state='NO_SELL_QUOTE',remainingRaw=str(await tokenBalance(rpc,token,wallet)))
            return
        quote=quotes[0]
        price=await feePrice(rpc)
        approval=await adapter.approvalNeeded(quote,token,wallet,amount)
        approvalCount=2 if approval and approval[0]=='ERC20' and quote['venue']['source']=='uniswapV4' else int(approval is not None)
        cap=(SALE_GAS_CAP+APPROVAL_GAS_CAP*approvalCount)*price
        sunk=paidFees(store,token)
        decision=salePolicy(quote['output'],rate)
        saveResult(store,token,state=decision,estimatedGbp=str(Decimal(quote['output'])*rate/WEI),estimatedRaw=str(quote['output']),estimatedAsset='WETH' if quote['venue']['source']=='uniswapV3' else 'ETH',fxGbpPerEth=str(rate),fxAt=fx.updated,remainingRaw=str(await tokenBalance(rpc,token,wallet)),maxGasGbp=str(Decimal(cap+sunk)*rate/WEI))
        if decision!='READY':
            return
        for approvalIndex in range(3):
            approval=await adapter.approvalNeeded(quote,token,wallet,amount)
            if approval is None:
                break
            if approvalIndex==2:
                raise RuntimeError('APPROVAL_DID_NOT_TAKE_EFFECT')
            await sendManual(journal,token,'MANUAL_APPROVE',adapter.buildApproval(approval),0)
        # Re-quote the approved venue and exact current amount after approval.
        current=await tokenBalance(rpc,token,wallet)
        if current<amount:
            raise RuntimeError('BALANCE_CHANGED_AFTER_APPROVAL')
        candidates=await adapter.directQuotes(token,amount,False)
        quote=next((q for q in candidates if q['venue']['id']==quote['venue']['id']),None)
        if quote is None:
            raise RuntimeError('APPROVED_ROUTE_UNAVAILABLE')
        rate=await freshRate(fx)
        price=await feePrice(rpc)
        decision=salePolicy(quote['output'],rate)
        if decision!='READY':
            saveResult(store,token,state=decision)
            return
        minimum=int(quote['output'])*9700//10000
        tx=adapter.build(quote,amount,wallet,minimumOut=minimum)
        # Router02 receives a chain deadline using its verified multicall encoder.
        from swap_adapters import boundEntryTransaction
        tx=boundEntryTransaction(tx,int(time.time())+60)
        saveResult(store,token,fxGbpPerEth=str(rate),fxAt=fx.updated)
        receipt=await sendManual(journal,token,'MANUAL_SELL',tx,amount)
        await accountReceipts(rpc,store,wallet)
        after=await tokenBalance(rpc,token,wallet)
        if after>=current:
            raise RuntimeError('NO_BALANCE_PROGRESS')
        saveResult(store,token,state='SOLD' if after==0 else 'PARTIAL',remainingRaw=str(after))
        if after==0:
            return
    saveResult(store,token,state='CHUNK_LIMIT_REACHED',remainingRaw=str(await tokenBalance(rpc,token,wallet)))


def writeReport(store,runDir):
    rows=[json.loads(row[0]) for row in store.db.execute('SELECT payload FROM manual_results ORDER BY token')]
    report={'wallet':store.get('liquidationWallet'),'run':runDir.name,'inventory':json.loads(store.get('inventory','{}')),'closingInventory':json.loads(store.get('closingInventory','{}')),'tokens':rows,'gasWei':str(paidFees(store)),'unresolved':store.db.execute("SELECT txHash,state FROM transactions WHERE state IN ('SIGNED','SENT','UNCERTAIN','REJECTED','STUCK')").fetchall()}
    report['unresolved']=[dict(row) for row in report['unresolved']]
    report['provider']=json.loads(store.get('providerStats','{}'))
    totals={}
    gbp=Decimal(0)
    complete=True
    for row in rows:
        for fill in row.get('fills',[]):
            income=fill.get('income')
            if not income or int(fill.get('debited','0'))<=0:
                complete=False
                continue
            totals[income['asset']]=totals.get(income['asset'],0)+int(income['raw'])
            if income['asset'] in ('ETH','WETH') and fill.get('fxGbpPerEth'):
                gbp+=Decimal(income['raw'])*Decimal(fill['fxGbpPerEth'])/WEI
            else:
                complete=False
    rates=[Decimal(row['fxGbpPerEth']) for row in rows if row.get('fxGbpPerEth')]
    report['proceedsRawByAsset']={k:str(v) for k,v in totals.items()}
    report['grossGbpValuedSubtotal']=str(gbp)
    report['accountingComplete']=complete
    # A single run may span several rates. Fees require per-transaction timestamps
    # for exact attribution, so publish raw gas and a labelled current-rate estimate.
    report['netGbpEstimate']=str(gbp-Decimal(report['gasWei'])*rates[-1]/WEI) if rates and complete else None
    report['walletClear']=False
    report['clearanceReason']=report['closingInventory'].get('coverage','INDEX_WATERMARK_UNVERIFIED')+'; inspect remaining balances'
    (runDir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RUN',runDir.name,'WALLET',report['wallet'],flush=True)
    print('PROCEEDS RAW',report['proceedsRawByAsset'],'GROSS GBP VALUED',report['grossGbpValuedSubtotal'],'GAS WEI',report['gasWei'],'NET GBP ESTIMATE',report['netGbpEstimate'],flush=True)
    for row in rows:
        print(row['token'],row.get('state'), 'EST GBP',row.get('estimatedGbp','?'),'REMAINING RAW',row.get('remainingRaw','?'),'REASON',row.get('reason',''),flush=True)
    print('ACCOUNTING COMPLETE',report['accountingComplete'],flush=True)
    print('COVERAGE',report['clearanceReason'],'REPORT',runDir/'report.json',flush=True)
    return report


async def run(args):
    url,wallet=peak.loadSettings(args.env,None,args.wallet_file)
    root=args.state_dir.expanduser().resolve()/wallet.lower()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    lockPath=globalWalletLock(wallet)
    lockPath.parent.mkdir(parents=True,exist_ok=True)
    with lockPath.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='execute' and otherLiveExecutors():
            raise RuntimeError('LIVE_CAMPAIGN_RUNNING')
        active=root/'active-run'
        if active.exists() and args.command=='execute':
            runDir=root/active.read_text().strip()
        else:
            runDir=root/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid.uuid4().hex[:8])
            runDir.mkdir(mode=0o700)
            if args.command=='execute':
                active.write_text(runDir.name)
        store=initializeSweep(runDir/'execution.sqlite3',wallet)
        store.db.execute('CREATE TABLE IF NOT EXISTS manual_results(token TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        if args.command=='report':
            latest=root/'latest-run'
            store.db.close()
            if not latest.exists():
                raise RuntimeError('NO_PRIOR_REPORT')
            print((root/latest.read_text().strip()/'report.json').read_text())
            return
        async with aiohttp.ClientSession() as rawSession:
            session=PacedSession(rawSession)
            rpc=ManualRpc(session,url,wallet)
            fx=peak.Fx(session)
            journal=None
            try:
                if int(await rpc.call('eth_chainId',[]),16)!=4663:
                    raise RuntimeError('WRONG_CHAIN')
                if args.command=='execute':
                    await ensurePriorNoncesResolved(rpc,args.data_dir.expanduser().resolve(),wallet,runDir)
                    password=getpass.getpass('Local wallet password: ')
                    account,key=decodeKeystore(args.wallet_file,password)
                    del password
                    if account.address.lower()!=wallet.lower():
                        raise RuntimeError('KEYSTORE_WALLET_MISMATCH')
                    journal=TransactionJournal(store,rpc,account,key)
                    await journal.reconcile()
                    if journal.unsettled():
                        raise RuntimeError('PENDING_TRANSACTION_PRESERVED; rerun same command to reconcile')
                    await accountReceipts(rpc,store,wallet)
                balances,evidence=await inventory(rpc,session,url,wallet,args.data_dir.expanduser().resolve(),store,args.token)
                store.set('inventory',json.dumps(evidence))
                venues=loadKnownVenues(args.data_dir.expanduser().resolve(),set(balances))
                cached=json.loads(store.get('manualVenues','{}'))
                for token,known in cached.items():
                    if token in venues:
                        merged={v['id']:v for v in known+venues[token]}
                        venues[token]=list(merged.values())
                adapter=SwapAdapters(rpc,fx,VenueStore(venues))
                for token,amount in balances.items():
                    if token in (WETH,USDG):
                        saveResult(store,token,state='BASE_RETAINED',remainingRaw=str(amount))
                        continue
                    if amount is None:
                        saveResult(store,token,state='BALANCE_UNVERIFIED')
                        continue
                    if amount==0:
                        continue
                    if not venues.get(token):
                        try:
                            discoveryIssues=[]
                            venues[token]=await discoverVenues(rpc,token,[],discoveryIssues)
                            if venues[token]:
                                cached[token]=venues[token]
                                store.set('manualVenues',json.dumps(cached))
                        except asyncio.TimeoutError:
                            saveResult(store,token,state='POOL_DISCOVERY_TIMEOUT',remainingRaw=str(amount))
                            continue
                    if not venues.get(token):
                        saveResult(store,token,state='NO_KNOWN_POOL',remainingRaw=str(amount),reason=','.join(sorted(set(discoveryIssues))) or 'NO_SUPPORTED_POOL_FOUND')
                        continue
                    try:
                        if args.command=='preview':
                            rate=await freshRate(fx)
                            quote=(await adapter.directQuotes(token,amount,False))[0]
                            price=await feePrice(rpc)
                            approval=await adapter.approvalNeeded(quote,token,wallet,amount)
                            cap=(SALE_GAS_CAP+2*APPROVAL_GAS_CAP*int(approval is not None))*price
                            saveResult(store,token,state=salePolicy(quote['output'],rate),estimatedGbp=str(Decimal(quote['output'])*rate/WEI),remainingRaw=str(amount),maxGasGbp=str(Decimal(cap)*rate/WEI))
                        else:
                            await asyncio.wait_for(sellHolding(rpc,adapter,journal,store,wallet,token,fx),timeout=180)
                    except (RuntimeError,ValueError,KeyError,StopIteration,asyncio.TimeoutError) as error:
                        if transientFailure(error):
                            saveResult(store,token,state='PROVIDER_PAUSED',remainingRaw=str(amount),reason='Provider retry budget exhausted; same command resumes')
                            raise ProviderUnavailable('PROVIDER_PAUSED; rerun same command to resume') from error
                        saveResult(store,token,state='DEFERRED',remainingRaw=str(amount),reason=(str(error) or type(error).__name__)[:180])
                        if journal and journal.unsettled():
                            raise RuntimeError('PENDING_TRANSACTION_PRESERVED') from error
                closing,closingEvidence=await closingInventory(rpc,session,url,wallet,args.data_dir.expanduser().resolve(),store,args.token)
                for token,amount in closing.items():
                    saveResult(store,token,remainingRaw=str(amount) if amount is not None else None)
                store.set('closingInventory',json.dumps(closingEvidence))
                if args.command=='execute':
                    pendingAccounting=any(any(not fill.get('income') or int(fill.get('debited','0'))<=0 for fill in json.loads(row[0]).get('fills',[])) for row in store.db.execute('SELECT payload FROM manual_results'))
                    if not pendingAccounting and closingEvidence.get('coverage')!='CLOSING_INVENTORY_FAILED':
                        active.unlink(missing_ok=True)
                    (root/'latest-run').write_text(runDir.name)
            finally:
                store.set('providerStats',json.dumps({'requests':session.requests,'retries':session.retries,'intervalSeconds':session.interval,'paused':session.paused}))
                if args.command=='execute':
                    (root/'latest-run').write_text(runDir.name)
                writeReport(store,runDir)
                store.db.close()


def main():
    parser=argparse.ArgumentParser(description='Reusable stopped-campaign wallet sale, gross quote at least 2p; retain base assets.')
    parser.add_argument('command',choices=['preview','execute','report'])
    parser.add_argument('--wallet-file',type=Path,default=Path.home()/'Desktop/wallet.json')
    parser.add_argument('--env',type=Path,default=Path.home()/'.config/blackswan/concentration-new-alchemy.env')
    parser.add_argument('--data-dir',type=Path,required=True,help='Existing BlackSwan data folder containing historical pool records')
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/blackswan/manual-sales')
    parser.add_argument('--token',action='append',default=[])
    args=parser.parse_args()
    try:
        asyncio.run(run(args))
    except (RuntimeError,ValueError,BlockingIOError) as error:
        parser.exit(1,'STOPPED '+str(error)+'\n')


if __name__=='__main__':
    main()
