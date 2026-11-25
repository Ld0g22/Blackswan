#!/usr/bin/env python3
"""Prospective configurable-cutoff three-buying-block selector. No wallet, quotes or trades.

Decisions are an append-only interface; neither tracker nor execution engine is imported.
A FAIL requires a complete live subscription plus overlapping launch-block backfill.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sqlite3
import time

import aiohttp

from research.venue_decoders import activityFilter, decodeLog, PONS_BUY

CHAIN_ID = 4663
BASES = {
    '0x0000000000000000000000000000000000000000',
    '0x0bd7d308f8e1639fab988df18a8011f41eacad73',
    '0x5fc5360d0400a0fd4f2af552add042d716f1d168',
}
SELECTION_SECONDS = 5.0
REQUIRED_BLOCKS = 3


def validateBuyingBlockRange(minBuyingBlocks, maxBuyingBlocks):
    if type(minBuyingBlocks) is not int or minBuyingBlocks < 0:
        raise ValueError('INVALID_SELECTOR_MIN_BUY_BLOCKS')
    if maxBuyingBlocks is not None and (type(maxBuyingBlocks) is not int
                                        or maxBuyingBlocks < minBuyingBlocks):
        raise ValueError('INVALID_SELECTOR_MAX_BUY_BLOCKS')


def decisionStatus(blocks, backfillDone, subscriptionStarted, interrupted, errors,
                   maxBuyingBlocks=None, minBuyingBlocks=REQUIRED_BLOCKS):
    """Bounded inclusive range requires complete evidence; legacy unbounded rule retained."""
    validateBuyingBlockRange(minBuyingBlocks, maxBuyingBlocks)
    complete = backfillDone and subscriptionStarted and not interrupted and not errors
    if maxBuyingBlocks is not None:
        if blocks > maxBuyingBlocks:
            return 'FAIL'
        return 'PASS' if blocks >= minBuyingBlocks and complete else (
            'FAIL' if complete else 'UNKNOWN')
    if blocks >= minBuyingBlocks:
        return 'PASS'
    return 'FAIL' if complete else 'UNKNOWN'


def toTimestamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def eligibleVenues(event):
    if event.get('chainId') != CHAIN_ID:
        return []
    d, source = event['details'], event['source']
    if source == 'ponsV2':
        t0, t1, pool = d['token'].lower(), d['pairToken'].lower(), d['curve'].lower()
        extra = {}
    elif source == 'uniswapV3':
        t0, t1, pool = d['token0'].lower(), d['token1'].lower(), d['pool'].lower()
        extra = {'fee': int(d['fee'])}
    elif source == 'uniswapV4':
        t0, t1, pool = d['currency0'].lower(), d['currency1'].lower(), d['poolId'].lower()
        extra = {'fee': int(d['fee']), 'hooks': d['hooks'].lower(), 'spacing': int(d['tickSpacing'])}
    else:
        return []
    if t0 == t1:
        return []
    candidates = []
    for token, base in ((t0, t1), (t1, t0)):
        if token in BASES or base not in BASES:
            continue
        if source == 'ponsV2' and token != t0:
            continue
        candidates.append(dict(
            venueKey=f'{CHAIN_ID}:{token}:{source}:{pool}',
            tokenKey=f'{CHAIN_ID}:{token}', tokenAddress=token,
            source=source, id=pool, token0=t0, token1=t1,
            baseAsset=base, **extra,
        ))
    return candidates


class SelectorStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS launches(
            eventKey TEXT PRIMARY KEY, tokenKey TEXT NOT NULL, venueKey TEXT NOT NULL,
            source TEXT NOT NULL, state TEXT NOT NULL, detectedAt REAL NOT NULL,
            deadline REAL, createdAt REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS buyingEvents(
            eventKey TEXT NOT NULL, evidenceKey TEXT NOT NULL,
            blockHash TEXT NOT NULL, availableAt REAL NOT NULL,
            valid INTEGER NOT NULL, origin TEXT NOT NULL,
            PRIMARY KEY(eventKey,evidenceKey));
        CREATE INDEX IF NOT EXISTS buyingByLaunch ON buyingEvents(eventKey,availableAt);
        CREATE TABLE IF NOT EXISTS decisions(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, eventKey TEXT UNIQUE NOT NULL,
            tokenKey TEXT NOT NULL, venueKey TEXT NOT NULL, status TEXT NOT NULL,
            decisionAt REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS revocations(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,eventKey TEXT NOT NULL,
            notedAt REAL NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')

    def cursor(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='detectorCursor'").fetchone()
        return int(row[0]) if row else 0

    def advance(self, cursor):
        self.db.execute("INSERT INTO meta VALUES('detectorCursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(cursor),))

    def create(self, event, venue, detectedAt):
        eventKey = event['eventKey'] + ':' + venue['venueKey']
        self.db.execute('''INSERT OR IGNORE INTO launches VALUES(?,?,?,?,?, ?,NULL,?,?)''',
                        (eventKey, venue['tokenKey'], venue['venueKey'], venue['source'],
                         'PENDING', detectedAt, time.time(), json.dumps({'event': event, 'venue': venue})))
        return eventKey

    def setDeadline(self, key, deadline):
        self.db.execute('UPDATE launches SET deadline=? WHERE eventKey=?', (deadline, key))

    def observeBuy(self, key, raw, availableAt, origin):
        evidenceKey = ':'.join((raw['blockHash'].lower(), raw['transactionHash'].lower(),
                                str(int(raw['logIndex'],16))))
        self.db.execute('''INSERT INTO buyingEvents VALUES(?,?,?,?,?,?)
            ON CONFLICT(eventKey,evidenceKey) DO UPDATE SET valid=excluded.valid''',
            (key, evidenceKey, raw['blockHash'].lower(), availableAt, int(not raw.get('removed', False)), origin))

    def buyingBlocks(self, key, deadline):
        return sorted({r[0] for r in self.db.execute('''
            SELECT blockHash FROM buyingEvents
            WHERE eventKey=? AND availableAt<=? AND valid=1''', (key, deadline))})

    def decide(self, key, status, data):
        row = self.db.execute('SELECT * FROM launches WHERE eventKey=?', (key,)).fetchone()
        if row is None:
            raise ValueError('Unknown launch key')
        if row['state'] != 'PENDING':
            return False
        now = time.time()
        payload = dict(json.loads(row['data']), **data,
                       interfaceVersion=1, selectionId=key, tokenKey=row['tokenKey'], venueKey=row['venueKey'],
                       status=status, deadline=row['deadline'], decisionAt=now)
        # isolation_level=None is autocommit: a context manager alone would NOT
        # atomically pair the state transition with the durable PASS event.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute('UPDATE launches SET state=? WHERE eventKey=? AND state=?',
                                      (status,key,'PENDING')).rowcount
            if not changed:
                self.db.execute('ROLLBACK')
                return False
            self.db.execute('''INSERT INTO decisions(eventKey,tokenKey,venueKey,status,decisionAt,data)
                VALUES(?,?,?,?,?,?)''',(key,row['tokenKey'],row['venueKey'],status,now,json.dumps(payload)))
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        return True

    def revoke(self, eventKey, reason):
        self.db.execute('INSERT INTO revocations(eventKey,notedAt,reason) VALUES(?,?,?)',
                        (eventKey,time.time(),reason))

    def status(self):
        return dict(self.db.execute('SELECT state,COUNT(*) FROM launches GROUP BY state'))


class Candidate:
    def __init__(self, eventKey, event, venue):
        self.eventKey, self.event, self.venue = eventKey, event, venue
        self.deadline = None
        self.subId = None
        self.backfillDone = False
        self.subscriptionInterrupted = False
        self.subscriptionStarted = False
        self.closed = False
        self.errors = []


class Selector:
    def __init__(self, detectorPath, dbPath, httpUrl, wsUrl,
                 decisionSeconds=SELECTION_SECONDS, maxBuyingBlocks=None,
                 minBuyingBlocks=REQUIRED_BLOCKS):
        # Historical callers may pass the original integral float default.
        if (type(decisionSeconds) not in (int, float)
                or not 1 <= decisionSeconds <= 300
                or int(decisionSeconds) != decisionSeconds):
            raise ValueError('UNSUPPORTED_SELECTOR_DECISION_SECONDS')
        validateBuyingBlockRange(minBuyingBlocks, maxBuyingBlocks)
        self.decisionSeconds = int(decisionSeconds)
        self.minBuyingBlocks = minBuyingBlocks
        self.maxBuyingBlocks = maxBuyingBlocks
        self.store = SelectorStore(dbPath)
        db = self.store.db
        try:
            db.execute('BEGIN IMMEDIATE')
            metadata = dict(db.execute('SELECT key,value FROM meta'))
            historic = bool(db.execute('SELECT 1 FROM launches LIMIT 1').fetchone())
            if metadata.get('selectorName', 'buyingBlocks') != 'buyingBlocks':
                raise RuntimeError('SELECTOR_POLICY_IDENTITY_MISMATCH')
            existingPolicy = historic or any(key in metadata for key in
                ('decisionSeconds', 'minBuyingBlocks', 'maxBuyingBlocks', 'selectorName'))
            expected = {
                'decisionSeconds': str(self.decisionSeconds),
                'minBuyingBlocks': str(self.minBuyingBlocks),
                'maxBuyingBlocks': str(self.maxBuyingBlocks) if self.maxBuyingBlocks is not None else 'NONE',
            }
            defaults = {'decisionSeconds': '5', 'minBuyingBlocks': '3', 'maxBuyingBlocks': 'NONE'}
            for key, intended in expected.items():
                stored = metadata.get(key, defaults[key] if existingPolicy else intended)
                if stored != intended:
                    raise RuntimeError('SELECTOR_DECISION_WINDOW_MISMATCH' if key == 'decisionSeconds'
                                       else 'SELECTOR_BUY_BLOCK_POLICY_MISMATCH')
            expected.update(selectorName='buyingBlocks', selectorReady='0', selectorHeartbeat='0',
                            selectorPid=str(os.getpid()))
            db.executemany('INSERT INTO meta(key,value) VALUES(?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', expected.items())
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            db.close()
            raise
        self.detector = sqlite3.connect(f'file:{Path(detectorPath).resolve()}?mode=ro', uri=True, timeout=2)
        self.detector.row_factory = sqlite3.Row
        self.httpUrl, self.wsUrl = httpUrl, wsUrl
        self.candidates = {}
        self.tasks = set()
        self.ws = None
        self.connected = asyncio.Event()
        self.pending = {}
        self.subIds = {}
        self.requestId = 0
        self.rpcCounts = Counter()
        self.wsMessages = 0
        self.stopping = asyncio.Event()
        self.session = None

    def setReady(self, ready):
        self.store.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorReady',?),('selectorHeartbeat',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ('1' if ready else '0', str(time.time())))

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    # Keep authenticated RPC URLs and provider messages out of logs.
                    print('SELECTOR TASK ERROR ' + type(exc).__name__, flush=True)
        task.add_done_callback(done)
        return task

    async def rpc(self, method, params, timeout=4):
        self.requestId += 1
        self.rpcCounts[method] += 1
        async with self.session.post(self.httpUrl,
                json={'jsonrpc':'2.0','id':self.requestId,'method':method,'params':params},
                timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise RuntimeError('RPC_HTTP_ERROR')
            data = await response.json()
        if 'error' in data or 'result' not in data:
            raise RuntimeError('RPC_REJECTED')
        return data['result']

    async def websocket(self):
        while not self.stopping.is_set():
            try:
                async with self.session.ws_connect(self.wsUrl,heartbeat=8,timeout=8) as ws:
                    self.ws = ws
                    self.connected.set()
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        receivedAt = time.time()
                        try:
                            data = json.loads(message.data)
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if 'id' in data and data['id'] in self.pending:
                            future, candidate = self.pending.pop(data['id'])
                            if 'error' in data or not isinstance(data.get('result'), str):
                                if not future.done():
                                    future.set_exception(RuntimeError('WS_SUBSCRIPTION_REJECTED'))
                            else:
                                subId = data['result']
                                candidate.subId = subId
                                candidate.subscriptionStarted = True
                                self.subIds[subId] = candidate.eventKey
                                if not future.done():
                                    future.set_result(subId)
                            continue
                        if data.get('method') != 'eth_subscription':
                            continue
                        params = data.get('params', {})
                        key = self.subIds.get(params.get('subscription'))
                        cand = self.candidates.get(key)
                        if cand is not None:
                            self.acceptLog(cand, params.get('result', {}), receivedAt, 'live')
                            self.wsMessages += 1
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                pass
            finally:
                self.ws = None
                self.connected.clear()
                self.subIds.clear()
                for future, candidate in self.pending.values():
                    candidate.subscriptionInterrupted = True
                    if not future.done():
                        future.set_exception(RuntimeError('WS_DISCONNECTED'))
                self.pending.clear()
                for cand in self.candidates.values():
                    if not cand.closed:
                        cand.subscriptionInterrupted = True
                        cand.subId = None
            if not self.stopping.is_set():
                await asyncio.sleep(0.5)

    async def subscribe(self, cand):
        await asyncio.wait_for(self.connected.wait(), timeout=3)
        if self.ws is None:
            raise RuntimeError('WS_NOT_READY')
        self.requestId += 1
        reqId = self.requestId
        future = asyncio.get_running_loop().create_future()
        self.pending[reqId] = (future, cand)
        try:
            await self.ws.send_json({'jsonrpc':'2.0','id':reqId,'method':'eth_subscribe',
                                     'params':['logs',activityFilter(cand.venue)]})
            return await asyncio.wait_for(future, timeout=3)
        finally:
            self.pending.pop(reqId, None)

    async def unsubscribe(self, cand):
        if cand.subId:
            subId, cand.subId = cand.subId, None
            self.subIds.pop(subId, None)
            if self.ws and not self.ws.closed:
                try:
                    self.requestId += 1
                    await self.ws.send_json({'jsonrpc':'2.0','id':self.requestId,
                                             'method':'eth_unsubscribe','params':[subId]})
                except (RuntimeError, aiohttp.ClientError):
                    pass

    def acceptLog(self, cand, raw, receivedAt, origin):
        if cand.closed or cand.deadline is not None and receivedAt > cand.deadline:
            return
        try:
            if not isinstance(raw, dict):
                raise ValueError('Malformed activity event')
            if int(raw['blockNumber'],16) < int(cand.event['blockNumber']):
                return
            filt = activityFilter(cand.venue)
            if raw.get('address','').lower()!=filt['address'].lower():
                return
            topics = raw.get('topics', [])
            if not topics or topics[0].lower() not in filt['topics'][0]:
                return
            if cand.venue['source']=='uniswapV4' and (len(topics)<2 or topics[1].lower()!=cand.venue['id']):
                return
            decoded = decodeLog(raw, cand.venue)
            if decoded.get('kind') != 'trade':
                return
            # Pons event direction is identified by event signature, without
            # depending on unverified fee arithmetic to count a buy block.
            buy = (topics[0].lower()==PONS_BUY if cand.venue['source']=='ponsV2'
                   else int(decoded['buyBaseAmount'])>0)
            if buy:
                self.store.observeBuy(cand.eventKey,raw,receivedAt,origin)
        except (ValueError, TypeError, KeyError, AttributeError):
            cand.errors.append('invalidActivityEvent')

    async def backfill(self, cand):
        remaining = cand.deadline-time.time()
        if remaining<=0:
            return
        try:
            headResult=await self.rpc('eth_blockNumber',[],timeout=min(3,remaining))
            head=int(headResult,16)
            startBlock=int(cand.event['blockNumber'])
            if head<startBlock:
                raise ValueError('Head before launch')
            # Robinhood Alchemy eth_getLogs permits no more than 10 blocks per call.
            # The subscription is already active; overlap the launch-to-head range.
            for first in range(startBlock,head+1,10):
                remaining=cand.deadline-time.time()
                if remaining<=0:
                    return
                filt=dict(activityFilter(cand.venue),fromBlock=hex(first),
                          toBlock=hex(min(first+9,head)))
                rows=await self.rpc('eth_getLogs',[filt],timeout=min(3,remaining))
                now=time.time()
                if not isinstance(rows,list):
                    raise ValueError('Malformed log response')
                for raw in rows:
                    self.acceptLog(cand,raw,now,'boundedBackfill')
            cand.backfillDone=time.time()<=cand.deadline
        except (ValueError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError):
            cand.errors.append('backfillUnavailable')

    async def observe(self, cand):
        subscribe = self.spawn(self.subscribe(cand))
        try:
            head = await self.rpc('eth_getBlockByHash',[cand.event['blockHash'],False],timeout=4)
            if not head or head.get('hash','').lower()!=cand.event['blockHash'].lower():
                raise ValueError('Missing launch header')
            cand.deadline = int(head['timestamp'],16)+self.decisionSeconds
            self.store.setDeadline(cand.eventKey,cand.deadline)
            if time.time() >= cand.deadline:
                self.finish(cand,'UNKNOWN','launchLearnedAfterDeadline')
                return
            try:
                await asyncio.wait_for(asyncio.shield(subscribe),timeout=max(0.001,cand.deadline-time.time()))
                if time.time()<cand.deadline:
                    await asyncio.wait_for(self.backfill(cand),timeout=max(0.001,cand.deadline-time.time()))
            except (asyncio.TimeoutError, RuntimeError, aiohttp.ClientError):
                cand.errors.append('subscriptionOrBackfillTooLate')
            await asyncio.sleep(max(0,cand.deadline-time.time()))
            count = len(self.store.buyingBlocks(cand.eventKey,cand.deadline))
            complete = cand.backfillDone and cand.subscriptionStarted and not cand.subscriptionInterrupted
            status = decisionStatus(count,cand.backfillDone,cand.subscriptionStarted,
                                    cand.subscriptionInterrupted,cand.errors,
                                    maxBuyingBlocks=self.maxBuyingBlocks,
                                    minBuyingBlocks=getattr(self,'minBuyingBlocks',REQUIRED_BLOCKS))
            reason = ('buyingBlocksWithinRange' if status=='PASS' and self.maxBuyingBlocks is not None
                      else 'thresholdReached' if status=='PASS'
                      else 'aboveMaximumBuyingBlocks' if status=='FAIL'
                           and self.maxBuyingBlocks is not None and count>self.maxBuyingBlocks
                      else 'completeWindowBelowThreshold' if status=='FAIL'
                      else 'incompleteWindow')
            self.finish(cand,status,reason)
        except (ValueError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError):
            self.finish(cand,'UNKNOWN','launchHeaderUnavailable')
        finally:
            if not subscribe.done():
                subscribe.cancel()
            cand.closed = True
            await self.unsubscribe(cand)
            self.candidates.pop(cand.eventKey,None)

    def finish(self, cand, status, reason):
        blocks = self.store.buyingBlocks(cand.eventKey,cand.deadline or time.time())
        result = {'policyName':'buyingBlocks','buyBlockCount':len(blocks),'buyBlockHashes':blocks,
                  'coverageComplete':bool(cand.backfillDone and cand.subscriptionStarted
                                          and not cand.subscriptionInterrupted and not cand.errors),
                  'reason':reason,'headerTimestamp':cand.deadline-getattr(self,'decisionSeconds',SELECTION_SECONDS) if cand.deadline else None,
                  'decisionSeconds':getattr(self,'decisionSeconds',SELECTION_SECONDS),
                  'minBuyingBlocks':getattr(self,'minBuyingBlocks',REQUIRED_BLOCKS),
                  'maxBuyingBlocks':getattr(self,'maxBuyingBlocks',None),
                  'evidenceErrors':cand.errors[:6]}
        if self.store.decide(cand.eventKey,status,result):
            print(f'SELECT {status:7s} {cand.venue["source"]:10s} '
                  f'blocks={len(blocks)} token={cand.venue["tokenAddress"][:12]} '
                  f'key={cand.eventKey[:20]} reason={reason}',flush=True)

    def pull(self):
        rows = self.detector.execute('SELECT seq,payload FROM event_log WHERE seq>? ORDER BY seq LIMIT 200',
                                     (self.store.cursor(),)).fetchall()
        for row in rows:
            event = json.loads(row['payload'])
            if event.get('removed'):
                for candidate in eligibleVenues(event):
                    # Detector reorg rows preserve original event identity.
                    key = event['eventKey']+':'+candidate['venueKey']
                    if key in self.candidates:
                        self.candidates[key].subscriptionInterrupted=True
                    if self.store.db.execute('SELECT 1 FROM decisions WHERE eventKey=?',(key,)).fetchone():
                        self.store.revoke(key,'detectorLaunchRemoved')
            else:
                for venue in eligibleVenues(event):
                    key = self.store.create(event,venue,toTimestamp(event['observedAtUtc']))
                    if not self.store.db.execute('SELECT 1 FROM decisions WHERE eventKey=?',(key,)).fetchone() and key not in self.candidates:
                        if time.time()-toTimestamp(event['observedAtUtc']) > self.decisionSeconds:
                            self.store.decide(key,'UNKNOWN',{'reason':'detectionEventTooOldForLiveDecision',
                                'buyBlockCount':0,'buyBlockHashes':[],'coverageComplete':False})
                            continue
                        candidate = Candidate(key,event,venue)
                        self.candidates[key]=candidate
                        self.spawn(self.observe(candidate))
            self.store.advance(row['seq'])

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            chain = await self.rpc('eth_chainId',[],timeout=8)
            if int(chain,16)!=CHAIN_ID:
                raise RuntimeError('Wrong chain ID')
            # Never resurrect a decision window that was interrupted by a restart.
            for row in self.store.db.execute("SELECT eventKey FROM launches WHERE state='PENDING'").fetchall():
                self.store.decide(row['eventKey'],'UNKNOWN',{'reason':'selectorRestart','buyBlockCount':0,
                    'buyBlockHashes':[],'coverageComplete':False})
            wsTask=self.spawn(self.websocket())
            lastReport=0
            lastHeartbeat=0
            try:
                while not self.stopping.is_set():
                    self.pull()
                    if time.monotonic()-lastHeartbeat>=1:
                        self.setReady(self.connected.is_set())
                        lastHeartbeat=time.monotonic()
                    if time.monotonic()-lastReport>=15:
                        print('SELECTOR HEALTH '+json.dumps({'decisions':self.store.status(),
                            'active':len(self.candidates),'decisionSeconds':self.decisionSeconds,
                            'minBuyingBlocks':self.minBuyingBlocks,'maxBuyingBlocks':self.maxBuyingBlocks,
                            'httpCalls':dict(self.rpcCounts),
                            'wsActivityMessages':self.wsMessages},separators=(',',':')),flush=True)
                        lastReport=time.monotonic()
                    await asyncio.sleep(0.025)
            finally:
                self.stopping.set()
                wsTask.cancel()
                for task in list(self.tasks): task.cancel()
                await asyncio.gather(*self.tasks,return_exceptions=True)
                self.setReady(False)
                self.detector.close()
                self.store.db.close()


def settings(path):
    text=Path(path).read_text()
    values={}
    for line in text.splitlines():
        line=line.strip().removeprefix('export ').strip()
        if line and not line.startswith('#') and '=' in line:
            k,v=line.split('=',1)
            if k in ('ROBINHOOD_ALCHEMY_HTTP_URL','ROBINHOOD_ALCHEMY_WSS_URL','BSRH_RPC_HTTP','BSRH_RPC_WS'):
                values[k]=v.strip().strip('"\'')
    http=values.get('ROBINHOOD_ALCHEMY_HTTP_URL') or values.get('BSRH_RPC_HTTP')
    ws=values.get('ROBINHOOD_ALCHEMY_WSS_URL') or values.get('BSRH_RPC_WS')
    if not http or not http.startswith('https://') or not ws or not ws.startswith('wss://'):
        raise ValueError('Missing HTTPS/WSS credentials; URLs are not logged')
    return http,ws


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','status'])
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--detector-db',type=Path)
    parser.add_argument('--decision-seconds',type=int,choices=range(1,301),default=5,
                        metavar='SECONDS',help='Launch-relative cutoff, 1–300 seconds (legacy CLI default 5)')
    parser.add_argument('--min-buying-blocks',type=int,default=REQUIRED_BLOCKS,
                        help='Inclusive minimum distinct buying blocks')
    parser.add_argument('--max-buying-blocks',type=int,
                        help='Inclusive maximum; omitted for legacy unbounded rule')
    parser.add_argument('--env',type=Path,default=Path.home()/'blackswanRH_guarded/.env')
    args=parser.parse_args()
    if args.command=='status':
        conn=sqlite3.connect(f'file:{args.db.resolve()}?mode=ro',uri=True)
        print(json.dumps(dict(conn.execute('SELECT state,COUNT(*) FROM launches GROUP BY state')),indent=2))
        return
    if not args.detector_db or not args.detector_db.is_file():
        parser.error('--detector-db must point to the running detector journal')
    import fcntl
    lock = args.db.with_suffix('.lock').open('a+')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Selector already running on this database') from None
    try:
        validateBuyingBlockRange(args.min_buying_blocks,args.max_buying_blocks)
    except ValueError as exc:
        parser.error(str(exc))
    http,ws=settings(args.env)
    selector=Selector(args.detector_db,args.db,http,ws,
                      decisionSeconds=args.decision_seconds,
                      maxBuyingBlocks=args.max_buying_blocks,
                      minBuyingBlocks=args.min_buying_blocks)
    loop=None
    async def runner():
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            loop.add_signal_handler(sig,selector.stopping.set)
        await selector.run()
    try: asyncio.run(runner())
    except KeyboardInterrupt: pass
    finally: lock.close()


if __name__=='__main__':
    main()