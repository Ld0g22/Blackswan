#!/usr/bin/env python3
"""V4 first-trade buyer-transaction concentration selector for BlackSwan.

Read-only: emits the exact SelectorStore decisions/revocations interface.
No wallet, swap quoting, signing, sacrifice or execution code.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import time

import aiohttp

from five_second_selector import Candidate, Selector, SelectorStore, eligibleVenues, settings, toTimestamp
from research.venue_decoders import V4_MANAGER, V4_SWAP, decodeLog

CHAIN_ID = 4663
POLICY_VERSION = 1
DEFAULT_WINDOW = 30
DEFAULT_SHARE_BPS = 2500
DEFAULT_FIRST_TRADE_TIMEOUT = 300
MAX_SEAL_DELAY_SECONDS = 60
MAX_ACTIVE_CANDIDATES = 64
SILENT_POOL_TIMESLICE_SECONDS = 5
SILENT_POOL_YIELD_SECONDS = 2
MAX_RPC_CONCURRENCY = 2
MAX_SCAN_BLOCKS = 512
MIN_SCAN_BLOCKS = 10
SILENT_SCAN_SECONDS = 5.0
ACTIVE_RETRY_SECONDS = 0.75
TRADE_HINT_SECONDS = 10.0
MAX_ACTIONABLE_DECISION_LAG_SECONDS = 15
MAX_POST_PASS_AUDITS_PER_CYCLE = 12


def concentrationDecision(swaps, firstTradeAt, windowSeconds=30, thresholdBps=2500):
    """Classify a COMPLETE, canonical observation window with integer arithmetic.

    Observations must have transactionHash, buyBaseRaw and blockTimestamp. The
    caller, not this arithmetic function, must prove complete chain coverage.
    """
    if type(firstTradeAt) is not int or type(windowSeconds) is not int or windowSeconds < 1:
        raise ValueError("INVALID_CONCENTRATION_WINDOW")
    if type(thresholdBps) is not int or not 0 < thresholdBps <= 10000:
        raise ValueError("INVALID_CONCENTRATION_THRESHOLD")
    grouped = defaultdict(int)
    count = 0
    for item in swaps:
        ts, amount = item["blockTimestamp"], item["buyBaseRaw"]
        if type(ts) is not int or type(amount) is not int or amount < 0:
            raise ValueError("INVALID_SWAP_EVIDENCE")
        if not firstTradeAt <= ts < firstTradeAt + windowSeconds:
            continue
        count += 1
        if amount:
            tx = item["transactionHash"]
            if type(tx) is not str or not tx:
                raise ValueError("INVALID_TRANSACTION_HASH")
            grouped[tx.lower()] += amount
    total = sum(grouped.values())
    largestHash, largest = (min(
        ((tx,amount) for tx,amount in grouped.items()),
        key=lambda entry: (-entry[1],entry[0])
    ) if grouped else (None,0))
    passed = total > 0 and largest * 10000 <= total * thresholdBps
    reason = ("NO_BUY_VOLUME" if total == 0 else
              "MAX_TRANSACTION_SHARE_WITHIN_LIMIT" if passed else
              "MAX_TRANSACTION_SHARE_EXCEEDED")
    return dict(
        status="PASS" if passed else "FAIL",
        reason=reason,
        largestBuyTransactionBaseRaw=str(largest),
        totalBuyBaseRaw=str(total),
        largestBuyTransactionHash=largestHash,
        buyTransactionCount=len(grouped),
        observedSwapCount=count,
        maxLargestBuyTransactionShareBps=thresholdBps,
    )


class ConcentrationSelector(Selector):
    """Reuse the original WS multiplexor and RPC transport; own policy state."""

    def __init__(self, detectorPath, dbPath, httpUrl, wsUrl, *,
                 windowSeconds=DEFAULT_WINDOW, thresholdBps=DEFAULT_SHARE_BPS,
                 firstTradeTimeoutSeconds=DEFAULT_FIRST_TRADE_TIMEOUT):
        if (type(windowSeconds) is not int or windowSeconds != DEFAULT_WINDOW
                or type(thresholdBps) is not int or thresholdBps != DEFAULT_SHARE_BPS
                or type(firstTradeTimeoutSeconds) is not int
                or not 30 <= firstTradeTimeoutSeconds <= 3600):
            raise ValueError("UNSUPPORTED_CONCENTRATION_POLICY")
        self.windowSeconds = windowSeconds
        self.thresholdBps = thresholdBps
        self.firstTradeTimeoutSeconds = firstTradeTimeoutSeconds
        # Reject old or unrelated selector DBs BEFORE opening them writable.
        existingPath = Path(dbPath)
        if existingPath.is_file():
            try:
                with sqlite3.connect(
                    f"file:{existingPath.resolve()}?mode=ro",uri=True,timeout=2
                ) as previous:
                    identified = previous.execute(
                        "SELECT value FROM meta WHERE key='concentrationPolicyHash'"
                    ).fetchone()
                if identified is None:
                    raise RuntimeError("CONCENTRATION_POLICY_OR_DATABASE_MISMATCH")
            except sqlite3.Error as exc:
                raise RuntimeError("CONCENTRATION_SELECTOR_DB_UNREADABLE") from exc
        self.store = SelectorStore(dbPath)
        self.detectorPath = Path(detectorPath).resolve()
        self.detector = sqlite3.connect(
            f"file:{self.detectorPath}?mode=ro",uri=True,timeout=2)
        self.detector.row_factory = sqlite3.Row
        self.httpUrl, self.wsUrl = httpUrl, wsUrl
        self.candidates = {}
        self.observerTasks = {}
        self.deferUntil = {}
        self.rotationClock = time.monotonic
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
        self.httpGate = asyncio.Semaphore(MAX_RPC_CONCURRENCY)
        # Silent scans and background audits share ONE slot. A traded pool
        # can always contend for the other slot; network waits remain bounded.
        self.silentGate = asyncio.Semaphore(1)
        self.rpcPauseUntil = 0.0
        self.rpcFailureStreak = 0
        self.lastRpcThrottleAt = 0.0
        self.rpcErrors = Counter()
        self.scanRangeByKey = {}
        self.lastAudit = 0.0
        self.auditTask = None
        self.stats = Counter()
        self.validatedCheckpoints = set()
        self.headLock = asyncio.Lock()
        self.headAt = 0.0
        self.cachedHead = None
        self.wallTime = time.time

        policy = json.dumps({
            "kind":"concentration","version":POLICY_VERSION,
            "windowAnchor":"firstTrade","windowSeconds":windowSeconds,
            "thresholdBps":thresholdBps,
            "firstTradeTimeoutSeconds":firstTradeTimeoutSeconds,
            "detectorDb":str(self.detectorPath),
        },sort_keys=True,separators=(",",":"))
        digest = hashlib.sha256(policy.encode()).hexdigest()
        existing = self.store.db.execute(
            "SELECT value FROM meta WHERE key='concentrationPolicyHash'").fetchone()
        launched = self.store.db.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
        other = self.store.db.execute("SELECT value FROM meta WHERE key='decisionSeconds'").fetchone()
        if (existing is not None and existing[0] != digest
                or existing is None and (launched or other is not None)):
            self.detector.close()
            self.store.db.close()
            raise RuntimeError("CONCENTRATION_POLICY_OR_DATABASE_MISMATCH")
        if existing is None:
            self.store.db.execute(
                "INSERT INTO meta(key,value) VALUES('concentrationPolicyHash',?)",(digest,))
            self.store.db.execute(
                "INSERT INTO meta(key,value) VALUES('selectorName','concentration')")
        # A previously running process may have set READY. This new process
        # is *not* ready until it verifies the actual target chain.
        self.store.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorReady','0') "
            "ON CONFLICT(key) DO UPDATE SET value='0'")
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS concentration_progress(
          eventKey TEXT PRIMARY KEY,nextBlock INTEGER NOT NULL,
          launchTimestamp INTEGER, lastError TEXT,
          FOREIGN KEY(eventKey) REFERENCES launches(eventKey));
        CREATE TABLE IF NOT EXISTS concentration_chunks(
          eventKey TEXT NOT NULL,startBlock INTEGER NOT NULL,endBlock INTEGER NOT NULL,
          endHash TEXT NOT NULL,endTimestamp INTEGER NOT NULL,
          PRIMARY KEY(eventKey,startBlock));
        CREATE TABLE IF NOT EXISTS concentration_swaps(
          eventKey TEXT NOT NULL,blockNumber INTEGER NOT NULL,
          blockHash TEXT NOT NULL,transactionHash TEXT NOT NULL,
          logIndex INTEGER NOT NULL,blockTimestamp INTEGER NOT NULL,
          buyBaseRaw TEXT NOT NULL,raw TEXT NOT NULL,
          PRIMARY KEY(eventKey,blockHash,transactionHash,logIndex));
        CREATE INDEX IF NOT EXISTS concentration_by_pool_block
          ON concentration_swaps(eventKey,blockNumber,logIndex);
        CREATE TABLE IF NOT EXISTS concentration_audits(
          eventKey TEXT PRIMARY KEY,lastBlock INTEGER NOT NULL,
          lastHash TEXT NOT NULL,lastChecked REAL NOT NULL);
        """)

    def pauseRpc(self,retryAfter=None):
        now=time.monotonic()
        if now-self.lastRpcThrottleAt>30:
            self.rpcFailureStreak=0
        self.lastRpcThrottleAt=now
        self.rpcFailureStreak=min(6,self.rpcFailureStreak+1)
        delay=min(16.0,0.5*2**self.rpcFailureStreak)
        try:
            delay=max(delay,float(retryAfter))
        except (TypeError,ValueError,OverflowError):
            pass
        self.rpcPauseUntil=max(self.rpcPauseUntil,time.monotonic()+delay)

    async def rpc(self,method,params,timeout=4):
        """Preserve exact status/code without logging URL, credentials or body."""
        if time.monotonic()<self.rpcPauseUntil:
            raise RuntimeError("RPC_BACKOFF_ACTIVE")
        self.requestId+=1
        self.rpcCounts[method]+=1
        try:
            async with self.session.post(self.httpUrl,
                    json={"jsonrpc":"2.0","id":self.requestId,"method":method,"params":params},
                    timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status!=200:
                    reason="RPC_HTTP_"+str(response.status)
                    self.rpcErrors[method+":"+reason]+=1
                    if response.status==429 or response.status>=500:
                        self.pauseRpc(response.headers.get("Retry-After"))
                    elif response.status in (401,402,403):
                        # Authentication/quota failures cannot be fixed by a
                        # storm of retries. Leave a durable explicit reason.
                        self.pauseRpc(30)
                    raise RuntimeError(reason)
                data=await response.json()
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError) as exc:
            reason="RPC_TRANSPORT_"+type(exc).__name__
            self.rpcErrors[method+":"+reason]+=1
            self.pauseRpc()
            raise RuntimeError(reason) from None
        if not isinstance(data,dict) or "error" in data or "result" not in data:
            error=data.get("error") if isinstance(data,dict) else None
            code=error.get("code") if isinstance(error,dict) else None
            reason="RPC_JSONRPC_"+str(code) if type(code) is int else "RPC_INVALID_RESPONSE"
            self.rpcErrors[method+":"+reason]+=1
            if code in (429,-32016):
                self.pauseRpc()
            raise RuntimeError(reason)
        return data["result"]

    def hasTradeHint(self,cand):
        return getattr(cand,"tradeHintUntil",0)>time.monotonic()

    def firstTrade(self,key):
        return self.store.db.execute(
            "SELECT blockNumber,blockTimestamp FROM concentration_swaps WHERE eventKey=? "
            "ORDER BY blockNumber,logIndex LIMIT 1",(key,)).fetchone()

    def nextScanDelay(self,cand):
        first=self.firstTrade(cand.eventKey)
        if first is not None:
            cutoff=first["blockTimestamp"]+self.windowSeconds
            # Once the canonical anchor is known, no incremental RPC is
            # required until the complete observation window can be sealed.
            return max(ACTIVE_RETRY_SECONDS,cutoff-self.wallTime())
        return ACTIVE_RETRY_SECONDS if self.hasTradeHint(cand) else SILENT_SCAN_SECONDS

    async def waitForWake(self,cand,delay):
        try:
            await asyncio.wait_for(cand.wake.wait(),timeout=max(.05,delay))
        except asyncio.TimeoutError:
            pass

    def acceptLog(self,cand,raw,receivedAt,origin):
        """Websocket is a fast hint. Canonical HTTP backfill is decision truth."""
        if not isinstance(raw,dict):
            return
        try:
            if (raw.get("address","").lower()!=V4_MANAGER
                    or raw.get("topics",[])[0].lower()!=V4_SWAP
                    or raw["topics"][1].lower()!=cand.venue["id"]):
                return
            # Removed logs can invalidate a previously emitted PASS. Fail
            # closed immediately; the detached HTTP auditor is a second guard.
            if raw.get("removed") and self.store.db.execute(
                    "SELECT 1 FROM decisions WHERE eventKey=? AND status='PASS'",
                    (cand.eventKey,)).fetchone():
                self.revokeOnce(cand.eventKey,"V4_SWAP_REMOVED")
            if raw.get("removed"):
                cand.subscriptionInterrupted = True
                if hasattr(cand,"wake"):
                    cand.wake.set()
            elif not getattr(cand,"observedFirstTrade",False):
                # WS only wakes/prioritises a pool. A notification is never
                # inserted as canonical evidence or permitted to trigger PASS.
                cand.tradeHintUntil=time.monotonic()+TRADE_HINT_SECONDS
                if not hasattr(cand,"wake"):
                    cand.wake=asyncio.Event()
                cand.wake.set()
        except (IndexError,AttributeError,TypeError,KeyError):
            cand.errors.append("MALFORMED_WS_SWAP")

    def revokeOnce(self,key,reason):
        already = self.store.db.execute(
            "SELECT 1 FROM revocations WHERE eventKey=? LIMIT 1",(key,)).fetchone()
        if not already:
            self.store.revoke(key,reason)

    def pull(self):
        cursor = self.store.cursor()
        rows = self.detector.execute(
            "SELECT seq,payload FROM event_log WHERE seq>? ORDER BY seq LIMIT 200",
            (cursor,)).fetchall()
        for row in rows:
            event=json.loads(row["payload"])
            if event.get("source")=="uniswapV4":
                for venue in eligibleVenues(event):
                    key=event["eventKey"]+":"+venue["venueKey"]
                    if event.get("removed"):
                        active=self.candidates.get(key)
                        if active:active.subscriptionInterrupted=True
                        if self.store.db.execute(
                                "SELECT 1 FROM decisions WHERE eventKey=?",(key,)).fetchone():
                            self.revokeOnce(key,"DETECTOR_LAUNCH_REMOVED")
                        else:
                            existing=self.store.db.execute(
                                "SELECT 1 FROM launches WHERE eventKey=?",(key,)).fetchone()
                            if existing:
                                self.store.decide(key,"UNKNOWN",{
                                    "policyName":"concentration","policyVersion":1,
                                    "coverageComplete":False,
                                    "reason":"DETECTOR_LAUNCH_REMOVED"})
                    else:
                        self.store.create(event,venue,toTimestamp(event["observedAtUtc"]))
            self.store.advance(row["seq"])

    async def head(self):
        """A single shared provider-head query for all active launch pools."""
        now=time.monotonic()
        if self.cachedHead is not None and now-self.headAt<0.35:
            return self.cachedHead
        async with self.headLock:
            now=time.monotonic()
            if self.cachedHead is None or now-self.headAt>=0.35:
                self.cachedHead=int(await self.rpc(
                    "eth_blockNumber",[],timeout=4),16)
                self.headAt=time.monotonic()
            return self.cachedHead

    async def _header(self,number):
        result=await self.rpc("eth_getBlockByNumber",[hex(number),False],timeout=4)
        if (not isinstance(result,dict)
                or int(result.get("number","0x0"),16)!=number
                or type(result.get("hash")) is not str
                or len(result["hash"])!=66):
            raise RuntimeError("MISSING_CANONICAL_BLOCK_HEADER")
        return result["hash"].lower(),int(result["timestamp"],16)

    async def validateChunks(self,key):
        """A changed boundary hash implies an earlier batch was from a fork."""
        chunks=self.store.db.execute(
            "SELECT startBlock,endBlock,endHash FROM concentration_chunks "
            "WHERE eventKey=? ORDER BY startBlock",(key,)).fetchall()
        for chunk in chunks:
            canonical,_=await self._header(chunk["endBlock"])
            if canonical != chunk["endHash"]:
                self.store.db.execute("BEGIN IMMEDIATE")
                try:
                    self.store.db.execute(
                        "DELETE FROM concentration_swaps WHERE eventKey=? AND blockNumber>=?",
                        (key,chunk["startBlock"]))
                    self.store.db.execute(
                        "DELETE FROM concentration_chunks WHERE eventKey=? AND startBlock>=?",
                        (key,chunk["startBlock"]))
                    self.store.db.execute(
                        "UPDATE concentration_progress SET nextBlock=?,lastError=? WHERE eventKey=?",
                        (chunk["startBlock"],"REORG_REPLAY",key))
                    self.store.db.execute("COMMIT")
                except BaseException:
                    if self.store.db.in_transaction:self.store.db.execute("ROLLBACK")
                    raise
                return False
        return True

    def _decision(self,key,first,rows):
        samples=[dict(transactionHash=row["transactionHash"],
                      buyBaseRaw=int(row["buyBaseRaw"]),
                      blockTimestamp=row["blockTimestamp"]) for row in rows]
        result=concentrationDecision(samples,first["blockTimestamp"],
            self.windowSeconds,self.thresholdBps)
        result.update(policyName="concentration",policyVersion=1,
                      windowAnchor="firstTrade",
                      windowSeconds=self.windowSeconds,
                      firstTradeAt=first["blockTimestamp"],
                      firstTradeBlock=first["blockNumber"],
                      windowEndAtExclusive=first["blockTimestamp"]+self.windowSeconds,
                      coverageComplete=True,evidenceErrors=[])
        return result

    def finish(self,key,status,extras):
        payload=dict(policyName="concentration",policyVersion=POLICY_VERSION,
                     windowAnchor="firstTrade",windowSeconds=self.windowSeconds,
                     maxLargestBuyTransactionShareBps=self.thresholdBps)
        payload.update(extras)
        if self.store.decide(key,status,payload):
            self.stats[status]+=1
            print("CONCENTRATION",status,"key",key[:27],
                  "reason",payload.get("reason"),flush=True)

    async def scanOnce(self,key):
        """Read a bounded canonical range; adapt if the provider rejects its size."""
        launch=self.store.db.execute(
            "SELECT * FROM launches WHERE eventKey=?",(key,)).fetchone()
        if launch is None or launch["state"]!="PENDING":
            return
        meta=json.loads(launch["data"])
        event,venue=meta["event"],meta["venue"]
        if venue["source"]!="uniswapV4":
            self.finish(key,"UNKNOWN",dict(reason="NOT_V4",coverageComplete=False))
            return
        start=int(event["blockNumber"])
        state=self.store.db.execute(
            "SELECT * FROM concentration_progress WHERE eventKey=?",(key,)).fetchone()
        if state is None:
            self.store.db.execute(
                "INSERT OR IGNORE INTO concentration_progress(eventKey,nextBlock) VALUES(?,?)",
                (key,start))
            state=self.store.db.execute(
                "SELECT * FROM concentration_progress WHERE eventKey=?",(key,)).fetchone()
        if state["launchTimestamp"] is None:
            _,launchAt=await self._header(start)
            self.store.db.execute(
                "UPDATE concentration_progress SET launchTimestamp=? WHERE eventKey=?",
                (launchAt,key))
        else:
            launchAt=state["launchTimestamp"]
        # A restarted candidate never discards the original first-trade
        # observation or treats an unverified batch as complete.
        if key not in self.validatedCheckpoints:
            if not await self.validateChunks(key):
                return
            self.validatedCheckpoints.add(key)
        state=self.store.db.execute(
            "SELECT * FROM concentration_progress WHERE eventKey=?",(key,)).fetchone()
        head=await self.head()
        nextBlock=state["nextBlock"]
        if head>=nextBlock:
            rangeSize=self.scanRangeByKey.get(key,MAX_SCAN_BLOCKS)
            while True:
                last=min(head,nextBlock+rangeSize-1)
                filt={"address":V4_MANAGER,"topics":[V4_SWAP,venue["id"]],
                      "fromBlock":hex(nextBlock),"toBlock":hex(last)}
                # Fence the getLogs read with canonical boundary hashes,
                # including EMPTY ranges: a fork cannot silently erase swaps.
                endHash,endTimestamp=await self._header(last)
                try:
                    logs=await self.rpc("eth_getLogs",[filt],timeout=4)
                except RuntimeError as exc:
                    if (str(exc) in ("RPC_HTTP_400","RPC_HTTP_413",
                                      "RPC_JSONRPC_-32005","RPC_JSONRPC_-32602")
                            and last-nextBlock+1>MIN_SCAN_BLOCKS):
                        # Reduce to a strict power-of-two bound, never retry
                        # the same rejected range at a different nominal size.
                        rangeSize=max(MIN_SCAN_BLOCKS,min(rangeSize//2,
                            1 << ((last-nextBlock).bit_length()-1)))
                        self.scanRangeByKey[key]=rangeSize
                        self.stats["rangeReductions"]+=1
                        continue
                    raise
                break
            if not isinstance(logs,list):
                raise RuntimeError("INVALID_RPC_LOGS_RESULT")
            verified={last:(endHash,endTimestamp)}
            newRows=[]
            for raw in logs:
                if raw.get("removed"):
                    raise RuntimeError("RPC_LOG_MARKED_REMOVED")
                decoded=decodeLog(raw,venue)
                if decoded["kind"]!="trade":
                    raise RuntimeError("NON_SWAP_IN_SWAP_BACKFILL")
                number=decoded["blockNumber"]
                if not nextBlock<=number<=last:
                    raise RuntimeError("LOG_OUTSIDE_REQUESTED_RANGE")
                if number not in verified:
                    verified[number]=await self._header(number)
                blockHash,blockTimestamp=verified[number]
                if (decoded["blockHash"]!=blockHash
                        or blockTimestamp>endTimestamp):
                    raise RuntimeError("NONCANONICAL_SWAP_LOG")
                newRows.append((key,number,blockHash,decoded["transactionHash"],
                                decoded["logIndex"],blockTimestamp,
                                str(decoded["buyBaseAmount"]),
                                json.dumps(raw,sort_keys=True)))
            finalHash,finalTimestamp=await self._header(last)
            if (finalHash,finalTimestamp)!=(endHash,endTimestamp):
                raise RuntimeError("RANGE_REORG_DURING_GETLOGS")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                for entry in newRows:
                    self.store.db.execute("""INSERT OR REPLACE INTO concentration_swaps
                        (eventKey,blockNumber,blockHash,transactionHash,logIndex,
                        blockTimestamp,buyBaseRaw,raw) VALUES(?,?,?,?,?,?,?,?)""",entry)
                self.store.db.execute("""INSERT OR REPLACE INTO concentration_chunks(
                    eventKey,startBlock,endBlock,endHash,endTimestamp)
                    VALUES(?,?,?,?,?)""",(key,nextBlock,last,endHash,endTimestamp))
                self.store.db.execute(
                    "UPDATE concentration_progress SET nextBlock=?,lastError=NULL WHERE eventKey=?",
                    (last+1,key))
                self.store.db.execute("COMMIT")
            except BaseException:
                if self.store.db.in_transaction:self.store.db.execute("ROLLBACK")
                raise

        first=self.firstTrade(key)
        lastChunk=self.store.db.execute(
            "SELECT endBlock,endHash,endTimestamp FROM concentration_chunks WHERE eventKey=? "
            "ORDER BY endBlock DESC LIMIT 1",(key,)).fetchone()
        if lastChunk is None:
            return
        if first is None:
            if lastChunk["endTimestamp"]>=launchAt+self.firstTradeTimeoutSeconds:
                if await self.validateChunks(key):
                    self.finish(key,"UNKNOWN",dict(
                        reason="NO_FIRST_TRADE_WITHIN_OBSERVATION_BUDGET",
                        coverageComplete=True,firstTradeAt=None,evidenceErrors=[]))
            return
        if first["blockTimestamp"]>launchAt+self.firstTradeTimeoutSeconds:
            if await self.validateChunks(key):
                self.finish(key,"UNKNOWN",dict(
                    reason="FIRST_TRADE_AFTER_OBSERVATION_BUDGET",
                    coverageComplete=True,firstTradeAt=first["blockTimestamp"],
                    evidenceErrors=[]))
            return
        cutoff=first["blockTimestamp"]+self.windowSeconds
        if lastChunk["endTimestamp"]<cutoff:
            # A delayed or stalled provider must not cause a false PASS.
            # The normal scheduler retries and retains the original anchor.
            return
        if not await self.validateChunks(key):
            return
        # Final canonical block hash is a chain commitment to all prior
        # blocks. Verify every saved batch boundary against the present chain.
        rows=self.store.db.execute(
            "SELECT transactionHash,buyBaseRaw,blockTimestamp FROM concentration_swaps "
            "WHERE eventKey=? ORDER BY blockNumber,logIndex",(key,)).fetchall()
        result=self._decision(key,first,rows)
        self.store.db.execute("""INSERT OR REPLACE INTO concentration_audits(
            eventKey,lastBlock,lastHash,lastChecked) VALUES(?,?,?,?)""",
            (key,lastChunk["endBlock"],lastChunk["endHash"],time.time()))
        status=result.pop("status")
        latency=self.wallTime()-cutoff
        result["decisionLagSeconds"]=round(max(0,latency),3)
        if status=="PASS" and latency>MAX_ACTIONABLE_DECISION_LAG_SECONDS:
            # Historical backfill is evidence, not permission to enter late.
            result["hypotheticalStatus"]="PASS"
            result["reason"]="CONCENTRATION_WINDOW_TOO_OLD"
            status="UNKNOWN"
        self.finish(key,status,result)

    async def auditFinal(self):
        """Conservative post-PASS canonicality monitor, never a buy instruction."""
        rows=self.store.db.execute(
            "SELECT a.eventKey,a.lastBlock,a.lastHash FROM concentration_audits a "
            "JOIN decisions d ON a.eventKey=d.eventKey "
            "WHERE d.status='PASS' AND d.decisionAt >= ? AND NOT EXISTS "
            "(SELECT 1 FROM revocations r WHERE r.eventKey=a.eventKey) "
            "ORDER BY a.lastChecked,a.eventKey LIMIT ?",
            (time.time()-180,MAX_POST_PASS_AUDITS_PER_CYCLE)
            ).fetchall()
        for row in rows:
            try:
                async with self.silentGate:
                    async with self.httpGate:
                        current,_=await self._header(row["lastBlock"])
                if current!=row["lastHash"]:
                    self.revokeOnce(row["eventKey"],"POST_DECISION_CHAIN_REORG")
            except (ValueError,RuntimeError,TypeError,KeyError):
                self.stats["auditUnknown"]+=1
            finally:
                # Rotate through recent PASSes rather than repeatedly auditing
                # the first N while later decisions never receive a check.
                self.store.db.execute(
                    "UPDATE concentration_audits SET lastChecked=? WHERE eventKey=?",
                    (time.time(),row["eventKey"]))

    def scheduleAuditIfIdle(self,now):
        """Bound reorg audit concurrency even during slow/429 provider responses."""
        if now-self.lastAudit<=10:
            return False
        if self.auditTask is not None and not self.auditTask.done():
            return False
        self.lastAudit=now
        self.auditTask=self.spawn(self.auditFinal())
        return True

    def coverageTimeout(self,key):
        state=self.store.db.execute(
            "SELECT nextBlock,lastError FROM concentration_progress WHERE eventKey=?",
            (key,)).fetchone()
        chunk=self.store.db.execute(
            "SELECT endBlock,endTimestamp FROM concentration_chunks WHERE eventKey=? "
            "ORDER BY endBlock DESC LIMIT 1",(key,)).fetchone()
        first=self.firstTrade(key)
        errors=([str(state["lastError"])[:90]] if state and state["lastError"] else
                ["SCHEDULER_NOT_STARTED"] if state is None else
                ["CANONICAL_COVERAGE_INCOMPLETE"])
        self.finish(key,"UNKNOWN",dict(reason="OBSERVATION_COVERAGE_TIMEOUT",
            coverageComplete=False,evidenceErrors=errors,
            firstTradeAt=first["blockTimestamp"] if first else None,
            coveredThroughBlock=chunk["endBlock"] if chunk else None,
            coveredThroughTimestamp=chunk["endTimestamp"] if chunk else None,
            nextBlock=state["nextBlock"] if state else None))

    def expireUnstarted(self,now):
        cutoff=now-(self.firstTradeTimeoutSeconds+self.windowSeconds+MAX_SEAL_DELAY_SECONDS)
        rows=self.store.db.execute(
            "SELECT eventKey FROM launches WHERE state='PENDING' AND detectedAt<?",
            (cutoff,)).fetchall()
        for row in rows:
            if row["eventKey"] not in self.candidates:
                self.coverageTimeout(row["eventKey"])

    async def observe(self,cand):
        """Canonical scans only when useful, with priority for active trading."""
        if not hasattr(cand,"wake"):
            cand.wake=asyncio.Event()
        subscribed=self.spawn(self.subscribe(cand))
        lastSubscriptionAttempt=time.monotonic()
        await asyncio.sleep(0)
        try:
            while not self.stopping.is_set():
                cand.wake.clear()
                launch=self.store.db.execute(
                    "SELECT state,detectedAt FROM launches WHERE eventKey=?",
                    (cand.eventKey,)).fetchone()
                if launch is None or launch["state"]!="PENDING":
                    return
                if time.time()-float(launch["detectedAt"]) > (
                        self.firstTradeTimeoutSeconds+self.windowSeconds+MAX_SEAL_DELAY_SECONDS):
                    self.coverageTimeout(cand.eventKey)
                    return
                if cand.subId is None and subscribed.done() and time.monotonic()-lastSubscriptionAttempt>3:
                    subscribed=self.spawn(self.subscribe(cand))
                    lastSubscriptionAttempt=time.monotonic()
                first=self.firstTrade(cand.eventKey)
                cand.observedFirstTrade=first is not None
                active=cand.observedFirstTrade or self.hasTradeHint(cand)
                pause=self.rpcPauseUntil-time.monotonic()
                if pause>0:
                    # Do not wake on websocket traffic during provider backoff.
                    await asyncio.sleep(min(pause,2))
                    continue
                if not active and self.silentGate.locked():
                    # Do not queue a silent task behind many silent pools;
                    # a new WS hint can immediately promote it to active.
                    await self.waitForWake(cand,.25)
                    continue
                try:
                    if active:
                        async with self.httpGate:
                            await self.scanOnce(cand.eventKey)
                    else:
                        async with self.silentGate:
                            async with self.httpGate:
                                await self.scanOnce(cand.eventKey)
                    state=self.store.db.execute(
                        "SELECT state FROM launches WHERE eventKey=?",(cand.eventKey,)).fetchone()
                    if state is None or state["state"]!="PENDING":
                        return
                    cand.observedFirstTrade=self.firstTrade(cand.eventKey) is not None
                    delay=self.nextScanDelay(cand)
                    if cand.observedFirstTrade:
                        # Clear normal swap wakeups that arrived during the
                        # initial anchor scan; canonical scan at window end.
                        if not cand.subscriptionInterrupted:
                            cand.wake.clear()
                    await self.waitForWake(cand,delay)
                except (RuntimeError,ValueError,KeyError,IndexError,TypeError,
                        aiohttp.ClientError,asyncio.TimeoutError,sqlite3.Error) as exc:
                    reason=(type(exc).__name__+":"+str(exc)[:90]
                            if isinstance(exc,RuntimeError) else type(exc).__name__)
                    self.stats["retries"]+=1
                    if str(exc)!="RPC_BACKOFF_ACTIVE":
                        self.store.db.execute(
                            "UPDATE concentration_progress SET lastError=? WHERE eventKey=?",
                            (reason,cand.eventKey))
                    await asyncio.sleep(max(ACTIVE_RETRY_SECONDS,
                        min(2,self.rpcPauseUntil-time.monotonic())))
        finally:
            if not subscribed.done():
                subscribed.cancel()
            cand.closed=True
            await self.unsubscribe(cand)
            self.candidates.pop(cand.eventKey,None)
            self.observerTasks.pop(cand.eventKey,None)
            self.validatedCheckpoints.discard(cand.eventKey)
            state=self.store.db.execute(
                "SELECT state FROM launches WHERE eventKey=?",(cand.eventKey,)).fetchone()
            if state is None or state["state"]!="PENDING":
                self.scanRangeByKey.pop(cand.eventKey,None)
                self.deferUntil.pop(cand.eventKey,None)

    def pendingToStart(self, limit):
        """Exclude active and temporarily deferred pools BEFORE applying LIMIT."""
        if limit<=0:
            return []
        now=self.rotationClock()
        blocked=set(self.candidates)
        blocked.update(key for key,until in self.deferUntil.items() if until>now)
        if blocked:
            placeholders=",".join("?" for _ in blocked)
            sql=("SELECT eventKey,data FROM launches WHERE state='PENDING' "
                 "AND eventKey NOT IN ("+placeholders+") "
                 "ORDER BY detectedAt,eventKey LIMIT ?")
            return self.store.db.execute(sql,(*sorted(blocked),limit)).fetchall()
        return self.store.db.execute(
            "SELECT eventKey,data FROM launches WHERE state='PENDING' "
            "ORDER BY detectedAt,eventKey LIMIT ?",(limit,)).fetchall()

    def chooseSilentPreemption(self,now):
        """Yield one long-silent pool if a different waiting launch needs a slot.

        The candidate and all scanned canonical blocks stay in SQLite; no
        selector decision or discard is generated by preemption.
        """
        if len(self.candidates)<MAX_ACTIVE_CANDIDATES:
            return None
        if not self.pendingToStart(1):
            return None
        eligible=[]
        for key,cand in self.candidates.items():
            if now-getattr(cand,"startedAt",now)<SILENT_POOL_TIMESLICE_SECONDS:
                continue
            hasSwap=self.store.db.execute(
                "SELECT 1 FROM concentration_swaps WHERE eventKey=? LIMIT 1",
                (key,)).fetchone()
            if not hasSwap and not self.hasTradeHint(cand):
                eligible.append((cand.startedAt,key))
        if not eligible:
            return None
        return min(eligible)[1]

    def markReady(self,chainId):
        if chainId!=CHAIN_ID:
            raise RuntimeError("WRONG_CHAIN")
        self.store.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorHeartbeat',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),))
        self.store.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorReady','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'")
        self.store.db.execute(
            "INSERT INTO meta(key,value) VALUES('selectorVerifiedChainId',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(chainId),))

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session=session
            chain=await self.rpc("eth_chainId",[],timeout=8)
            if int(chain,16)!=CHAIN_ID:
                raise RuntimeError("WRONG_CHAIN")
            self.markReady(int(chain,16))
            # Pending decisions are resumed from the saved chain block cursor,
            # never rewritten as UNKNOWN merely because the process restarted.
            wsTask=self.spawn(self.websocket())
            reportAt=0.0
            heartAt=0.0
            try:
                while not self.stopping.is_set():
                    self.pull()
                    self.expireUnstarted(time.time())
                    now=self.rotationClock()
                    self.deferUntil={key:until for key,until in self.deferUntil.items() if until>now}
                    capacity=MAX_ACTIVE_CANDIDATES-len(self.candidates)
                    if capacity==0:
                        victim=self.chooseSilentPreemption(self.rotationClock())
                        if victim:
                            self.deferUntil[victim]=(
                                self.rotationClock()+SILENT_POOL_YIELD_SECONDS)
                            task=self.observerTasks.get(victim)
                            if task is not None:task.cancel()
                    if capacity>0:
                        for row in self.pendingToStart(capacity):
                            item=json.loads(row["data"])
                            cand=Candidate(row["eventKey"],item["event"],item["venue"])
                            cand.startedAt=self.rotationClock()
                            self.candidates[row["eventKey"]]=cand
                            self.observerTasks[row["eventKey"]]=self.spawn(self.observe(cand))
                    if time.monotonic()-heartAt>5:
                        heartAt=time.monotonic()
                        self.store.db.execute(
                            "UPDATE meta SET value=? WHERE key='selectorHeartbeat'",
                            (str(time.time()),))
                    self.scheduleAuditIfIdle(time.monotonic())
                    if time.monotonic()-reportAt>15:
                        reportAt=time.monotonic()
                        self.store.db.execute(
                            "INSERT INTO meta(key,value) VALUES('concentrationRpcErrors',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(dict(self.rpcErrors),sort_keys=True),))
                        print("CONCENTRATION HEALTH",json.dumps({
                            "decisions":self.store.status(),
                            "active":len(self.candidates),
                            "rpcCalls":dict(self.rpcCounts),
                            "rpcErrors":dict(self.rpcErrors),
                            "rpcBackoffSeconds":round(max(0,self.rpcPauseUntil-time.monotonic()),2),
                            "wsMessages":self.wsMessages,
                            "stats":dict(self.stats)}),flush=True)
                    await asyncio.sleep(0.1)
            finally:
                self.stopping.set()
                self.store.db.execute(
                    "UPDATE meta SET value='0' WHERE key='selectorReady'")
                wsTask.cancel()
                for task in list(self.tasks):task.cancel()
                if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)
                self.detector.close()
                self.store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("run","status"))
    parser.add_argument("--db",type=Path,required=True)
    parser.add_argument("--detector-db",type=Path)
    parser.add_argument("--env",type=Path,default=Path.home()/"blackswanRH_guarded/.env")
    parser.add_argument("--window-seconds",type=int,default=30)
    parser.add_argument("--threshold-bps",type=int,default=2500)
    parser.add_argument("--first-trade-timeout-seconds",type=int,default=300)
    args=parser.parse_args()
    if args.command=="status":
        with sqlite3.connect(f"file:{args.db.resolve()}?mode=ro",uri=True) as db:
            print(json.dumps(dict(db.execute(
                "SELECT state,COUNT(*) FROM launches GROUP BY state")),indent=2))
        return
    if not args.detector_db or not args.detector_db.is_file():
        parser.error("DETECTOR_JOURNAL_MISSING")
    import fcntl
    args.db.parent.mkdir(parents=True,exist_ok=True)
    lock=args.db.with_suffix(".lock").open("a+")
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("CONCENTRATION_SELECTOR_ALREADY_RUNNING") from None
    http,ws=settings(args.env)
    selector=ConcentrationSelector(args.detector_db,args.db,http,ws,
        windowSeconds=args.window_seconds,thresholdBps=args.threshold_bps,
        firstTradeTimeoutSeconds=args.first_trade_timeout_seconds)
    async def runner():
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):
            loop.add_signal_handler(sig,selector.stopping.set)
        await selector.run()
    try:asyncio.run(runner())
    except KeyboardInterrupt:pass
    finally:lock.close()


if __name__=="__main__":
    main()
