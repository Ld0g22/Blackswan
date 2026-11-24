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
MAX_ACTIVE_CANDIDATES = 12
MAX_RPC_CONCURRENCY = 2


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
        self.lastAudit = 0.0
        self.stats = Counter()
        self.validatedCheckpoints = set()
        self.headLock = asyncio.Lock()
        self.headAt = 0.0
        self.cachedHead = None

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
        """Fetch at most ten canonical blocks and advance a durable checkpoint."""
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
            last=min(head,nextBlock+9)
            filt={"address":V4_MANAGER,"topics":[V4_SWAP,venue["id"]],
                  "fromBlock":hex(nextBlock),"toBlock":hex(last)}
            logs=await self.rpc("eth_getLogs",[filt],timeout=4)
            if not isinstance(logs,list):
                raise RuntimeError("INVALID_RPC_LOGS_RESULT")
            endHash,endTimestamp=await self._header(last)
            verified={}
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

        first=self.store.db.execute(
            "SELECT blockNumber,blockTimestamp FROM concentration_swaps WHERE eventKey=? "
            "ORDER BY blockNumber,logIndex LIMIT 1",(key,)).fetchone()
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
        self.finish(key,result.pop("status"),result)

    async def auditFinal(self):
        """Conservative post-PASS canonicality monitor, never a buy instruction."""
        rows=self.store.db.execute(
            "SELECT a.eventKey,a.lastBlock,a.lastHash FROM concentration_audits a "
            "JOIN decisions d ON a.eventKey=d.eventKey "
            "WHERE d.status='PASS' AND d.decisionAt >= ? AND NOT EXISTS "
            "(SELECT 1 FROM revocations r WHERE r.eventKey=a.eventKey)",
            (time.time()-180,)
            ).fetchall()
        for row in rows[:100]:
            try:
                current,_=await self._header(row["lastBlock"])
                if current!=row["lastHash"]:
                    self.revokeOnce(row["eventKey"],"POST_DECISION_CHAIN_REORG")
            except (ValueError,RuntimeError,TypeError,KeyError):
                self.stats["auditUnknown"]+=1

    async def observe(self,cand):
        """Attempt subscription before sequential overlap-proof backfill."""
        subscribed=self.spawn(self.subscribe(cand))
        lastSubscriptionAttempt=time.monotonic()
        await asyncio.sleep(0)
        try:
            while not self.stopping.is_set():
                launch=self.store.db.execute(
                    "SELECT state,detectedAt FROM launches WHERE eventKey=?",
                    (cand.eventKey,)).fetchone()
                if launch is None or launch["state"]!="PENDING":
                    return
                # Stalled chain/RPC evidence must become an explicit UNKNOWN,
                # never a silent, indefinitely waiting opportunity.
                if time.time()-float(launch["detectedAt"]) > (
                        self.firstTradeTimeoutSeconds + self.windowSeconds +
                        MAX_SEAL_DELAY_SECONDS):
                    state=self.store.db.execute(
                        "SELECT lastError FROM concentration_progress WHERE eventKey=?",
                        (cand.eventKey,)).fetchone()
                    self.finish(cand.eventKey,"UNKNOWN",dict(
                        reason="OBSERVATION_COVERAGE_TIMEOUT",
                        coverageComplete=False,
                        evidenceErrors=[str(state[0])[:90]] if state and state[0] else []))
                    return
                if (cand.subId is None and subscribed.done()
                        and time.monotonic()-lastSubscriptionAttempt>1.0):
                    subscribed=self.spawn(self.subscribe(cand))
                    lastSubscriptionAttempt=time.monotonic()
                try:
                    async with self.httpGate:
                        await self.scanOnce(cand.eventKey)
                    await asyncio.sleep(0.08)
                except (RuntimeError,ValueError,KeyError,IndexError,TypeError,
                        aiohttp.ClientError,asyncio.TimeoutError,sqlite3.Error) as exc:
                    # Partial RPC/ABI evidence can never authorize a PASS.
                    reason=type(exc).__name__+":"+str(exc)[:90]
                    self.stats["retries"]+=1
                    self.store.db.execute(
                        "UPDATE concentration_progress SET lastError=? WHERE eventKey=?",
                        (reason,cand.eventKey))
                    await asyncio.sleep(0.65)
        finally:
            if not subscribed.done():
                subscribed.cancel()
            cand.closed=True
            await self.unsubscribe(cand)
            self.candidates.pop(cand.eventKey,None)

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self.session=session
            chain=await self.rpc("eth_chainId",[],timeout=8)
            if int(chain,16)!=CHAIN_ID:
                raise RuntimeError("WRONG_CHAIN")
            # Pending decisions are resumed from the saved chain block cursor,
            # never rewritten as UNKNOWN merely because the process restarted.
            wsTask=self.spawn(self.websocket())
            reportAt=0.0
            try:
                while not self.stopping.is_set():
                    self.pull()
                    capacity=MAX_ACTIVE_CANDIDATES-len(self.candidates)
                    if capacity>0:
                        rows=self.store.db.execute(
                            "SELECT eventKey,data FROM launches WHERE state='PENDING' "
                            "ORDER BY detectedAt LIMIT ?",(capacity,)).fetchall()
                        for row in rows:
                            if row["eventKey"] in self.candidates:continue
                            item=json.loads(row["data"])
                            cand=Candidate(row["eventKey"],item["event"],item["venue"])
                            self.candidates[row["eventKey"]]=cand
                            self.spawn(self.observe(cand))
                    if time.monotonic()-self.lastAudit>10:
                        self.lastAudit=time.monotonic()
                        self.spawn(self.auditFinal())
                    if time.monotonic()-reportAt>15:
                        reportAt=time.monotonic()
                        print("CONCENTRATION HEALTH",json.dumps({
                            "decisions":self.store.status(),
                            "active":len(self.candidates),
                            "rpcCalls":dict(self.rpcCounts),
                            "wsMessages":self.wsMessages,
                            "stats":dict(self.stats)}),flush=True)
                    await asyncio.sleep(0.1)
            finally:
                self.stopping.set()
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
