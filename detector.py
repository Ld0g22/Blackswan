#!/usr/bin/env python3
"""Robinhood Black Swan detector v0.2: read-only, durable launch-event ingestion.

One canonical Pons V2 factory, Uniswap v3 factory, and Uniswap v4 PoolManager.
The selector owns all post-launch monitoring, valuation, and trade decisions.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

import aiohttp

CHAIN_ID = 4663
PONS_V2_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
V3_FACTORY = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"
V4_POOL_MANAGER = "0x8366a39CC670B4001A1121B8F6A443A643e40951"

# Verified against official Pons V2, Uniswap v3 and v4 ABIs.
SOURCES = {
    "ponsV2": {
        "address": PONS_V2_FACTORY.lower(),
        "topic": "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607",
        "event": "TokenLaunched",
    },
    "uniswapV3": {
        "address": V3_FACTORY.lower(),
        "topic": "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118",
        "event": "PoolCreated",
    },
    "uniswapV4": {
        "address": V4_POOL_MANAGER.lower(),
        "topic": "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438",
        "event": "Initialize",
    },
}

HTTP_HEAD_CU = 10.0  # Conservative local estimate; verify against account billing.
HTTP_LOGS_CU = 60.0  # Published Alchemy eth_getLogs cost.
WS_BYTE_CU = 0.04  # Published bandwidth-based estimate, not invoice reconciliation.
MAX_DAILY_CU = 1_000_000.0
MAX_THROUGHPUT_CU_PER_10S = 3_000.0
RECOVERY_BATCH_SIZE = 10  # Alchemy Robinhood free eth_getLogs maximum.
RECOVERY_LOOKBACK_BLOCKS = 600  # ~60s at nominal 0.1s/block; age is approximate.
RECONNECT_OVERLAP_BLOCKS = 100  # ~10s overlap guards delayed WS delivery on reconnect.
RECOVERY_SPACING_SECONDS = 0.32  # One 60 CU call per 10 blocks; shared throughput limiter applies.


def utcNow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def loadEnv(path: Path) -> dict[str, str]:
    """Read only named public-RPC credentials; never print values."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() not in ("ROBINHOOD_ALCHEMY_HTTP_URL", "ROBINHOOD_ALCHEMY_WSS_URL"):
            continue
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        result[key.strip()] = value
    for key in ("ROBINHOOD_ALCHEMY_HTTP_URL", "ROBINHOOD_ALCHEMY_WSS_URL"):
        if not result.get(key):
            raise ValueError(f"Missing {key} in {path}")
    if not result["ROBINHOOD_ALCHEMY_HTTP_URL"].startswith("https://"):
        raise ValueError("Alchemy HTTP URL must be HTTPS")
    if not result["ROBINHOOD_ALCHEMY_WSS_URL"].startswith("wss://"):
        raise ValueError("Alchemy WS URL must be WSS")
    return result


def isAddress(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 42 and value.startswith("0x")
            and all(c in "0123456789abcdefABCDEF" for c in value[2:]))


def addressWord(word: str) -> str:
    if len(word) != 64 or int(word[:24], 16) != 0:
        raise ValueError("Invalid ABI-encoded address")
    return "0x" + word[-40:].lower()


def wordArray(data: str, expected: int) -> list[str]:
    if not isinstance(data, str) or not data.startswith("0x") or len(data) != 2 + expected * 64:
        raise ValueError("Unexpected ABI event data length")
    words = [data[2 + 64 * i:2 + 64 * (i + 1)] for i in range(expected)]
    for word in words:
        int(word, 16)
    return words


def signedWord(word: str, bits: int) -> int:
    value = int(word, 16) & ((1 << bits) - 1)
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decodeLog(rawLog: dict[str, Any], *, recovered: bool = False) -> dict[str, Any]:
    """Strict ABI decode; no token rejection and no external lookups."""
    if not isinstance(rawLog, dict):
        raise ValueError("Log must be an object")
    emitter = rawLog.get("address", "")
    address = emitter.lower() if isinstance(emitter, str) else ""
    topics = rawLog.get("topics", [])
    if not isinstance(topics, list) or not topics or not isinstance(topics[0], str):
        raise ValueError("Missing or malformed topics")
    source = next((name for name, spec in SOURCES.items()
                   if address == spec["address"] and topics[0].lower() == spec["topic"]), None)
    if source is None:
        raise ValueError("Out-of-scope contract/topic (no fallback classification)")
    if len(topics) != 4 or not all(isinstance(t, str) and t.startswith("0x") and len(t) == 66 for t in topics):
        raise ValueError("Unexpected indexed event layout")
    for topic in topics:
        int(topic[2:], 16)
    blockHash, txHash = rawLog.get("blockHash"), rawLog.get("transactionHash")
    if not isinstance(blockHash, str) or len(blockHash) != 66 or not isinstance(txHash, str) or len(txHash) != 66:
        raise ValueError("Log missing valid block/transaction hash")
    blockNumber = int(rawLog["blockNumber"], 16)
    logIndex = int(rawLog["logIndex"], 16)
    removed = rawLog.get("removed", False)
    if not isinstance(removed, bool):
        raise ValueError("Invalid removed flag")
    details: dict[str, Any]
    if source == "ponsV2":
        words = wordArray(rawLog.get("data", ""), 3)
        details = {
            "token": addressWord(topics[1][2:]),
            "curve": addressWord(topics[2][2:]),
            "deployer": addressWord(topics[3][2:]),
            "pairToken": addressWord(words[0]),
            "launchConfigId": str(int(words[1], 16)),
            "graduationThreshold": str(int(words[2], 16)),
        }
    elif source == "uniswapV3":
        words = wordArray(rawLog.get("data", ""), 2)
        details = {
            "token0": addressWord(topics[1][2:]),
            "token1": addressWord(topics[2][2:]),
            "fee": int(topics[3], 16),
            "tickSpacing": signedWord(words[0], 24),
            "pool": addressWord(words[1]),
        }
    else:
        words = wordArray(rawLog.get("data", ""), 5)
        details = {
            "poolId": topics[1].lower(),
            "currency0": addressWord(topics[2][2:]),
            "currency1": addressWord(topics[3][2:]),
            "fee": int(words[0], 16),
            "tickSpacing": signedWord(words[1], 24),
            "hooks": addressWord(words[2]),
            "sqrtPriceX96": str(int(words[3], 16)),
            "tick": signedWord(words[4], 24),
        }
    eventKey = f"{CHAIN_ID}:{blockHash.lower()}:{txHash.lower()}:{logIndex}"
    return {
        "schemaVersion": 1,
        "eventKey": eventKey,
        "chainId": CHAIN_ID,
        "source": source,
        "eventType": SOURCES[source]["event"],
        "contract": address,
        "blockNumber": blockNumber,
        "blockHash": blockHash.lower(),
        "transactionHash": txHash.lower(),
        "logIndex": logIndex,
        "observedAtUtc": utcNow(),
        "recovered": bool(recovered),
        "removed": removed,
        "details": details,
        "rawLog": rawLog,  # Preserve the exact matched RPC evidence for later ABI audits.
    }


class EventJournal:
    """Single-writer SQLite journal. Selector reads events by monotonically increasing seq."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS event_state (
                event_key TEXT PRIMARY KEY,
                removed INTEGER NOT NULL,
                last_seen_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                source TEXT NOT NULL,
                block_number INTEGER NOT NULL,
                payload TEXT NOT NULL,
                stored_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_event_source_block ON event_log(source, block_number);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL,
                reason TEXT NOT NULL,
                noted_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decode_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_address TEXT,
                block_number TEXT,
                reason TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                observed_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
                at_seconds REAL NOT NULL,
                estimate_cu REAL NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_time ON usage(at_seconds);
        """)

    def append(self, event: dict[str, Any]) -> int | None:
        """Commit event + outbox together; repeated state is idempotent."""
        removed = int(event["removed"])
        serialized = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with self.db:
            old = self.db.execute("SELECT removed FROM event_state WHERE event_key=?", (event["eventKey"],)).fetchone()
            if old is not None and old[0] == removed:
                return None
            self.db.execute(
                "INSERT INTO event_state(event_key, removed, last_seen_utc) VALUES(?,?,?) "
                "ON CONFLICT(event_key) DO UPDATE SET removed=excluded.removed,last_seen_utc=excluded.last_seen_utc",
                (event["eventKey"], removed, utcNow()),
            )
            cur = self.db.execute(
                "INSERT INTO event_log(event_key,source,block_number,payload,stored_at_utc) VALUES(?,?,?,?,?)",
                (event["eventKey"], event["source"], event["blockNumber"], serialized, utcNow()),
            )
            return int(cur.lastrowid)

    def saveDecodeFailure(self, rawLog: dict[str, Any], reason: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO decode_failures(source_address,block_number,reason,raw_json,observed_at_utc) "
                "VALUES(?,?,?,?,?)",
                (str(rawLog.get("address", "unknown")), str(rawLog.get("blockNumber", "unknown")),
                 reason, json.dumps(rawLog), utcNow()),
            )

    def activeEventsInRange(self, source: str, startBlock: int, endBlock: int) -> list[dict[str, Any]]:
        """Canonical audit baseline: latest state of each event, not individual outbox rows."""
        rows = self.db.execute("""
            SELECT l.payload FROM event_log AS l
            JOIN (SELECT event_key, MAX(seq) AS last_seq FROM event_log
                  WHERE source=? AND block_number BETWEEN ? AND ? GROUP BY event_key) AS latest
              ON latest.last_seq=l.seq
            JOIN event_state AS s ON s.event_key=l.event_key
            WHERE s.removed=0
        """, (source, startBlock, endBlock)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def getMeta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def setMeta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def addGap(self, startBlock: int, endBlock: int, reason: str) -> None:
        if startBlock > endBlock:
            return
        with self.db:
            self.db.execute("INSERT INTO gaps(start_block,end_block,reason,noted_at_utc) VALUES(?,?,?,?)", (startBlock, endBlock, reason, utcNow()))

    def charge(self, cu: float, reason: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO usage(at_seconds,estimate_cu,reason) VALUES(?,?,?)", (time.time(), cu, reason))

    def recentCu(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(estimate_cu),0) FROM usage WHERE at_seconds > ?", (time.time() - 86400,)).fetchone()
        return float(row[0])

    def pruneUsage(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM usage WHERE at_seconds < ?", (time.time() - 86400,))

    def stats(self) -> dict[str, Any]:
        counts = {source: 0 for source in SOURCES}
        for source, count in self.db.execute("SELECT source,COUNT(*) FROM event_log WHERE json_extract(payload,'$.removed') = 0 GROUP BY source"):
            counts[source] = count
        total = self.db.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
        gapCount = self.db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
        decodeFailureCount = self.db.execute("SELECT COUNT(*) FROM decode_failures").fetchone()[0]
        last = self.db.execute("SELECT stored_at_utc FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
        return {"status": self.getMeta("status", "UNKNOWN"), "events": total,
                "bySource": counts, "estimatedCu24h": round(self.recentCu(), 1),
                "lastObservationUtc": last[0] if last else None,
                "lastConnectedHead": self.getMeta("lastConnectedHead", "none"),
                "reportedGaps": gapCount, "decodeFailures": decodeFailureCount}

    def close(self) -> None:
        self.db.close()


class BudgetExceeded(Exception):
    pass


class LocalBudget:
    """Self-accounting for THIS detector only; future engines require shared account budget."""

    def __init__(self, journal: EventJournal, dailyMax: float = MAX_DAILY_CU) -> None:
        self.journal = journal
        self.dailyMax = dailyMax
        self.throughput: deque[tuple[float, float]] = deque()

    def record(self, cu: float, reason: str) -> None:
        if self.journal.recentCu() + cu > self.dailyMax:
            raise BudgetExceeded("Detector estimated 24h CU limit reached")
        self.journal.charge(cu, reason)
        now = time.monotonic()
        self.throughput.append((now, cu))
        self.trim()

    def trim(self) -> None:
        now = time.monotonic()
        while self.throughput and self.throughput[0][0] <= now - 10:
            self.throughput.popleft()

    async def beforeRequest(self, cu: float) -> None:
        while True:
            self.trim()
            used = sum(amount for _, amount in self.throughput)
            if used + cu <= MAX_THROUGHPUT_CU_PER_10S:
                if self.journal.recentCu() + cu > self.dailyMax:
                    raise BudgetExceeded("Detector estimated 24h CU limit reached")
                return
            await asyncio.sleep(0.2)


class Detector:
    def __init__(self, config: dict[str, str], journal: EventJournal,
                 recoveryBlocks: int = RECOVERY_LOOKBACK_BLOCKS,
                 dailyMaxCu: float = MAX_DAILY_CU) -> None:
        self.httpUrl = config["ROBINHOOD_ALCHEMY_HTTP_URL"]
        self.wsUrl = config["ROBINHOOD_ALCHEMY_WSS_URL"]
        self.journal = journal
        self.recoveryBlocks = recoveryBlocks
        self.budget = LocalBudget(journal, dailyMaxCu)
        self.lastWsMessageAt = time.monotonic()
        self.subscriptions: dict[str, str] = {}
        self.httpLock = asyncio.Lock()
        self.eventCount = 0
        self.decodingErrors = 0
        self.startTime = time.monotonic()
        self.closed = False

    async def rpc(self, session: aiohttp.ClientSession, method: str, params: list[Any], cost: float) -> Any:
        async with self.httpLock:
            await self.budget.beforeRequest(cost)
            # Pre-charge: a timeout still incurred provider work.
            self.budget.record(cost, method)
            try:
                async with session.post(self.httpUrl, json={"jsonrpc": "2.0", "id": 1,
                                                             "method": method, "params": params},
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        raise RuntimeError(f"{method} HTTP status {response.status}")
                    result = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                # Do not emit exception text: aiohttp exceptions may include keyed URLs.
                raise RuntimeError(f"{method} request failed ({type(error).__name__})") from None
            if result.get("error"):
                raise RuntimeError(f"{method} RPC error (details redacted)")
            if "result" not in result:
                raise RuntimeError(f"{method} missing JSON-RPC result")
            return result["result"]

    async def head(self, session: aiohttp.ClientSession) -> int:
        return int(await self.rpc(session, "eth_blockNumber", [], HTTP_HEAD_CU), 16)

    async def handleLog(self, rawLog: dict[str, Any], recovered: bool = False) -> None:
        try:
            event = decodeLog(rawLog, recovered=recovered)
        except (KeyError, ValueError, TypeError, IndexError) as error:
            self.decodingErrors += 1
            # Retain malformed matching logs for reprocessing; never silently lose them.
            self.journal.saveDecodeFailure(rawLog, type(error).__name__)
            self.journal.setMeta("lastDecodeErrorUtc", utcNow())
            print("ERROR matching log could not be decoded; evidence saved in decode_failures", flush=True)
            return
        sequence = self.journal.append(event)
        if sequence is not None:
            self.eventCount += 1
            action = "REMOVED" if event["removed"] else "NEW"
            suffix = "backfill" if recovered else "live"
            print(f"{utcNow()} {action:<7} #{sequence:<6} {event['source']:<10} "
                  f"block={event['blockNumber']} {suffix} tx={event['transactionHash'][:12]}…", flush=True)

    async def recover(self, session: aiohttp.ClientSession, targetHead: int) -> None:
        previous = self.journal.getMeta("lastConnectedHead")
        if previous:
            originalStart = max(0, int(previous) - RECONNECT_OVERLAP_BLOCKS)
        else:
            originalStart = max(0, targetHead - self.recoveryBlocks + 1)
        start = max(originalStart, targetHead - self.recoveryBlocks + 1)
        if originalStart < start:
            self.journal.addGap(originalStart, start - 1, "outside-fast-recovery-window")
            print(f"WARN unrecovered stale range {originalStart}..{start - 1}; live detection continues", flush=True)
        if start > targetHead:
            return
        print(f"RECOVER {start}..{targetHead} (live feed continues)", flush=True)
        current = start
        try:
            while current <= targetHead:
                end = min(current + RECOVERY_BATCH_SIZE - 1, targetHead)
                # All three venues in one getLogs request. Address and topic0 arrays
                # each use OR matching, so validate the exact contract/topic pair below.
                combinedFilter = {
                    "fromBlock": hex(current), "toBlock": hex(end),
                    "address": [spec["address"] for spec in SOURCES.values()],
                    "topics": [[spec["topic"] for spec in SOURCES.values()]],
                }
                # One retry for a transient read failure; the WS live path continues.
                for attempt in range(2):
                    try:
                        raw = await self.rpc(session, "eth_getLogs", [combinedFilter], HTTP_LOGS_CU)
                        if not isinstance(raw, list):
                            raise RuntimeError("Malformed eth_getLogs result")
                        break
                    except RuntimeError:
                        if attempt:
                            raise
                        await asyncio.sleep(0.25)
                errorsBefore = self.decodingErrors
                matched: dict[str, list[dict[str, Any]]] = {source: [] for source in SOURCES}
                for entry in raw:
                    if not isinstance(entry, dict):
                        raise RuntimeError("Malformed eth_getLogs entry")
                    address = entry.get("address")
                    topics = entry.get("topics")
                    source = next((name for name, spec in SOURCES.items()
                                   if isinstance(address, str) and address.lower() == spec["address"]
                                   and isinstance(topics, list) and topics and isinstance(topics[0], str)
                                   and topics[0].lower() == spec["topic"]), None)
                    if source is None:
                        # Cross-product of OR address/topic filters; never treat it as a launch.
                        continue
                    matched[source].append(entry)
                    await self.handleLog(entry, recovered=True)
                # Repair absent old-chain logs only after verifying a full successful batch.
                # Avoid flagging fresh logs as orphans while provider indexes the head.
                auditEnd = min(end, targetHead - 10)
                if current <= auditEnd and self.decodingErrors == errorsBefore:
                    for source, events in matched.items():
                        canonicalKeys = {
                            decodeLog(entry, recovered=True)["eventKey"] for entry in events
                            if current <= int(entry["blockNumber"], 16) <= auditEnd
                        }
                        for previous in self.journal.activeEventsInRange(source, current, auditEnd):
                            if previous["eventKey"] not in canonicalKeys:
                                previous["removed"] = True
                                previous["rawLog"] = dict(previous["rawLog"], removed=True)
                                previous["recovered"] = True
                                previous["observedAtUtc"] = utcNow()
                                newSeq = self.journal.append(previous)
                                if newSeq is not None:
                                    print(f"{utcNow()} REMOVED #{newSeq} {source} "
                                          f"block={previous['blockNumber']} reorg-repair", flush=True)
                await asyncio.sleep(RECOVERY_SPACING_SECONDS)
                current = end + 1
            self.journal.setMeta("lastBackfilledHead", str(targetHead))
            print(f"RECOVER complete through block {targetHead}", flush=True)
        except asyncio.CancelledError:
            self.journal.addGap(current, targetHead, "recovery-cancelled")
            raise
        except (RuntimeError, BudgetExceeded, ValueError, TypeError, KeyError) as error:
            self.journal.addGap(current, targetHead, f"recovery-interrupted:{type(error).__name__}")
            print(f"WARN recovery interrupted at block {current}; range recorded as gap", flush=True)
            if isinstance(error, BudgetExceeded):
                raise

    async def consumeMessage(self, data: dict[str, Any]) -> None:
        if data.get("method") != "eth_subscription":
            return
        params = data.get("params", {})
        subscriptionId = params.get("subscription")
        if subscriptionId not in self.subscriptions:
            return
        raw = params.get("result", {})
        if isinstance(raw, dict):
            await self.handleLog(raw)

    async def connectOnce(self, session: aiohttp.ClientSession) -> None:
        self.journal.setMeta("status", "CONNECTING")
        recoveryTask: asyncio.Task | None = None
        try:
            async with session.ws_connect(self.wsUrl, heartbeat=20, max_msg_size=2_000_000) as ws:
                self.subscriptions.clear()
                for idx, (source, spec) in enumerate(SOURCES.items(), start=1):
                    await self.budget.beforeRequest(10.0)
                    # Alchemy bills eth_subscribe separately from delivered WS bytes.
                    self.budget.record(10.0, "eth_subscribe")
                    await ws.send_json({"jsonrpc": "2.0", "id": idx, "method": "eth_subscribe",
                                        "params": ["logs", {"address": spec["address"], "topics": [spec["topic"]]}]})
                pending = {index: source for index, source in enumerate(SOURCES, start=1)}
                while pending:
                    message = await ws.receive(timeout=20)
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError("WebSocket closed during subscriptions")
                    self.budget.record(len(message.data.encode("utf-8")) * WS_BYTE_CU, "ws-bytes")
                    data = json.loads(message.data)
                    if data.get("id") in pending:
                        if not isinstance(data.get("result"), str):
                            raise RuntimeError(f"Subscription rejected for {pending[data['id']]}")
                        self.subscriptions[data["result"]] = pending.pop(data["id"])
                    else:
                        await self.consumeMessage(data)
                self.journal.setMeta("status", "LIVE")
                self.journal.setMeta("lastConnectedUtc", utcNow())
                print("LIVE subscriptions active: ponsV2, uniswapV3, uniswapV4", flush=True)
                # Subscription first, then get head, then backfill. Thus no handoff race.
                target = await self.head(session)
                recoveryTask = asyncio.create_task(self.recover(session, target))
                def stopSocketOnRecoveryFailure(done: asyncio.Task) -> None:
                    if not done.cancelled() and done.exception() is not None:
                        # Propagate failure even on a quiet chain with no new log messages.
                        asyncio.create_task(ws.close(code=1011, message=b"recovery failed"))
                recoveryTask.add_done_callback(stopSocketOnRecoveryFailure)
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        if message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE,
                                            aiohttp.WSMsgType.CLOSED):
                            break
                        continue
                    self.budget.record(len(message.data.encode("utf-8")) * WS_BYTE_CU, "ws-bytes")
                    self.lastWsMessageAt = time.monotonic()
                    try:
                        data = json.loads(message.data)
                        await self.consumeMessage(data)
                    except (ValueError, TypeError):
                        self.decodingErrors += 1
                        print("ERROR invalid WebSocket message", flush=True)
                    if recoveryTask and recoveryTask.done():
                        exc = recoveryTask.exception() if not recoveryTask.cancelled() else None
                        if exc:
                            raise exc
                        recoveryTask = None
                    # Only on receiving logs; a separate housekeeping task handles idle periods.
                raise RuntimeError("WebSocket disconnected")
        finally:
            if recoveryTask is not None:
                recoveryTask.cancel()
                try:
                    await recoveryTask
                except asyncio.CancelledError:
                    pass
            self.subscriptions.clear()
            self.journal.setMeta("status", "DISCONNECTED")

    async def housekeeping(self, session: aiohttp.ClientSession) -> None:
        while not self.closed:
            await asyncio.sleep(30)
            if self.journal.getMeta("status") == "LIVE":
                try:
                    head = await self.head(session)
                    # This is a last-connected-head timestamp, NOT proof of complete log coverage.
                    previous = int(self.journal.getMeta("lastConnectedHead", "0"))
                    self.journal.setMeta("lastConnectedHead", str(max(previous, head)))
                except (RuntimeError, BudgetExceeded):
                    print("WARN periodic chain-head check failed", flush=True)
            self.journal.pruneUsage()
            info = self.journal.stats()
            print(f"HEALTH {info['status']} events={info['events']} "
                  f"cu24h~={info['estimatedCu24h']} gaps={info['reportedGaps']} "
                  f"decodeErrors={info['decodeFailures']}", flush=True)

    async def run(self) -> None:
        self.journal.setMeta("status", "STARTING")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            chainId = int(await self.rpc(session, "eth_chainId", [], HTTP_HEAD_CU), 16)
            if chainId != CHAIN_ID:
                raise RuntimeError(f"Wrong chainId {chainId}, required {CHAIN_ID}")
            housekeepingTask = asyncio.create_task(self.housekeeping(session))
            retry = 1.0
            try:
                while True:
                    try:
                        await self.connectOnce(session)
                    except BudgetExceeded:
                        self.journal.setMeta("status", "BUDGET_EXHAUSTED")
                        print("STOP estimated detector CU limit reached; manual review needed", flush=True)
                        return
                    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, json.JSONDecodeError) as error:
                        # Exception text may embed authenticated URLs. Print class only.
                        print(f"WARN connection/recovery failure ({type(error).__name__}); reconnect in {retry:.0f}s", flush=True)
                    self.journal.setMeta("status", "RECONNECTING")
                    await asyncio.sleep(retry)
                    retry = min(15.0, retry * 1.5)
            finally:
                self.closed = True
                housekeepingTask.cancel()
                try:
                    await housekeepingTask
                except asyncio.CancelledError:
                    pass


def printStatus(path: Path) -> None:
    if not path.exists():
        print(f"No database yet: {path}")
        return
    journal = EventJournal(path)
    try:
        print(json.dumps(journal.stats(), indent=2))
    finally:
        journal.close()


def tailEvents(path: Path, after: int, follow: bool, jsonOutput: bool) -> None:
    if not path.exists():
        raise ValueError(f"No detector database found at {path}")
    # Open read-only; tail never modifies a running detector.
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=10)
    try:
        while True:
            rows = db.execute("SELECT seq,payload FROM event_log WHERE seq > ? ORDER BY seq LIMIT 500", (after,)).fetchall()
            for seq, payload in rows:
                event = json.loads(payload)
                if jsonOutput:
                    print(json.dumps({"seq": seq, **event}, separators=(",", ":")), flush=True)
                else:
                    print(f"#{seq:<7} {event['source']:<10} {event['eventType']:<13} "
                          f"block={event['blockNumber']} removed={event['removed']} "
                          f"tx={event['transactionHash']}", flush=True)
                after = seq
            if not follow:
                return
            time.sleep(0.5)
    finally:
        db.close()


def runCli() -> int:
    parser = argparse.ArgumentParser(description="Robinhood Black Swan detector — no trades")
    sub = parser.add_subparsers(dest="command", required=True)
    startParser = sub.add_parser("run", help="Connect live and persist events")
    startParser.add_argument("--env", type=Path, default=Path.home() / "blackswanRH_guarded/.env")
    startParser.add_argument("--db", type=Path, default=Path("data/detector.sqlite3"))
    startParser.add_argument("--max-cu-24h", type=float, default=MAX_DAILY_CU)
    startParser.add_argument("--recovery-blocks", type=int, default=RECOVERY_LOOKBACK_BLOCKS)
    statusParser = sub.add_parser("status", help="Show journal health")
    statusParser.add_argument("--db", type=Path, default=Path("data/detector.sqlite3"))
    tailParser = sub.add_parser("tail", help="Read selector-facing durable events")
    tailParser.add_argument("--db", type=Path, default=Path("data/detector.sqlite3"))
    tailParser.add_argument("--after", type=int, default=0)
    tailParser.add_argument("--follow", action="store_true")
    tailParser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "status":
            printStatus(args.db)
            return 0
        if args.command == "tail":
            tailEvents(args.db, args.after, args.follow, args.json)
            return 0
        if args.recovery_blocks < 1 or args.max_cu_24h <= 0 or args.max_cu_24h > MAX_DAILY_CU:
            raise ValueError("Invalid recovery blocks or 24h CU cap (cannot exceed 1M)")
        config = loadEnv(args.env)
        # Linux single-writer lock: a second run cannot duplicate subscriptions.
        import fcntl
        lockPath = args.db.with_suffix(args.db.suffix + ".lock")
        lockPath.parent.mkdir(parents=True, exist_ok=True)
        with lockPath.open("a+") as lockFile:
            try:
                fcntl.flock(lockFile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Detector already running with this database") from None
            journal = EventJournal(args.db)
            detector = Detector(config, journal, args.recovery_blocks, args.max_cu_24h)
            try:
                print("Detector starting; read-only onchain; schemaVersion=1; Ctrl+C to stop", flush=True)
                asyncio.run(detector.run())
            finally:
                if journal.getMeta("status") != "BUDGET_EXHAUSTED":
                    journal.setMeta("status", "STOPPED")
                journal.close()
        return 0
    except KeyboardInterrupt:
        print("Stopped by operator", flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, BudgetExceeded) as error:
        # Redact all sensitive URLs; argument/user paths aren't secret.
        message = str(error)
        if args.command == "run":
            for url in (locals().get("config") or {}).values():
                message = message.replace(url, "[REDACTED]")
        print(f"ERROR {message}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(runCli())
