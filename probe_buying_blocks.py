"""Durable 500-block v4 buying-activity evidence for the existing 1p gate.

This module ONLY reads chain history; it never signs, buys or sells.
Exactly the same distinct-buying-block convention as audit_all_sold_winners.py.
Five contiguous 100-block bins, each read in 10-block eth_getLogs ranges.
"""
import asyncio
import hashlib
import json
import re
import time

MAX_BUYING_BLOCKS=7
WINDOW_BLOCKS=500
MANAGER="0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP="0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
HEX_ADDRESS=re.compile(r"0x[0-9a-fA-F]{40}\Z")
HEX_TOPIC=re.compile(r"0x[0-9a-fA-F]{64}\Z")


def chainBlock(value):
    if type(value) is int and value>=0:
        return value
    if isinstance(value,str) and re.fullmatch(r"0x[0-9a-fA-F]+",value):
        return int(value,16)
    raise ValueError("INVALID_ACTIVITY_BLOCK_NUMBER")


def venueFor(selection):
    """Immutable selected launch venue, not a later or migrated pool."""
    event=json.loads(selection["event"])
    venue=event.get("venue") if isinstance(event,dict) else None
    token=selection["token"].lower()
    if not isinstance(venue,dict) or venue.get("source")!="uniswapV4":
        raise ValueError("INVALID_ACTIVITY_V4_VENUE")
    pool=venue.get("id")
    base=str(venue.get("baseAsset","")).lower()
    token0=str(venue.get("token0","")).lower()
    token1=str(venue.get("token1","")).lower()
    if (not isinstance(pool,str) or not HEX_TOPIC.fullmatch(pool)
            or not HEX_ADDRESS.fullmatch(base)
            or not HEX_ADDRESS.fullmatch(token0)
            or not HEX_ADDRESS.fullmatch(token1)
            or token not in (token0,token1)
            or base not in (token0,token1) or base==token):
        raise ValueError("INVALID_ACTIVITY_POOL_CURRENCIES")
    return pool.lower(), (0 if base==token0 else 1)


def buyingBlockFromSwap(log,pool,baseIndex,low,high):
    """Return buy block or None for a sell/zero swap; reject malformed RPC."""
    if (not isinstance(log,dict)
            or str(log.get("address","")).lower()!=MANAGER
            or not isinstance(log.get("topics"),list)
            or len(log["topics"])<2
            or str(log["topics"][0]).lower()!=SWAP
            or str(log["topics"][1]).lower()!=pool):
        raise ValueError("INVALID_ACTIVITY_SWAP_EMITTER")
    block=chainBlock(log.get("blockNumber"))
    if not low<=block<=high:
        raise ValueError("ACTIVITY_SWAP_OUT_OF_RANGE")
    data=log.get("data")
    if not isinstance(data,str) or not data.startswith("0x") or len(data)!=386:
        raise ValueError("INVALID_ACTIVITY_SWAP_ABI")
    try:
        bytes.fromhex(data[2:])
    except ValueError as exc:
        raise ValueError("INVALID_ACTIVITY_SWAP_ABI") from exc
    word=int(data[2+64*baseIndex:2+64*(baseIndex+1)],16)
    amount=word & ((1<<128)-1)
    if amount & (1<<127):
        amount-=1<<128
    return block if amount<0 else None


class ProbeBuyingBlocks:
    def __init__(self,store,rpc):
        if not HEX_ADDRESS.fullmatch(MANAGER) or not HEX_TOPIC.fullmatch(SWAP):
            raise RuntimeError("INVALID_ACTIVITY_CHAIN_CONSTANTS")
        self.store=store
        self.rpc=rpc
        self.collectLock=asyncio.Lock()
        self.requestPause=.35
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS probe_activity_bins(
                selectionId TEXT NOT NULL,probeBlock INTEGER NOT NULL,
                poolId TEXT NOT NULL,binIndex INTEGER NOT NULL,
                fromBlock INTEGER NOT NULL,toBlock INTEGER NOT NULL,
                buyingBlocksJson TEXT NOT NULL,endHash TEXT NOT NULL,
                PRIMARY KEY(selectionId,binIndex));
            CREATE TABLE IF NOT EXISTS probe_activity_verdicts(
                selectionId TEXT PRIMARY KEY,probeBlock INTEGER NOT NULL,
                poolId TEXT NOT NULL,count INTEGER NOT NULL,
                verdict TEXT NOT NULL,endHash TEXT NOT NULL,
                evidenceHash TEXT NOT NULL,checkedAt REAL NOT NULL);
        """)

    async def blockHash(self,block):
        header=await self.rpc.call("eth_getBlockByNumber",[hex(block),False])
        if (not isinstance(header,dict)
                or chainBlock(header.get("number"))!=block
                or not isinstance(header.get("hash"),str)
                or not HEX_TOPIC.fullmatch(header["hash"])):
            raise ValueError("UNVERIFIED_ACTIVITY_BLOCK_HEADER")
        return header["hash"].lower()

    def rows(self,selectionId):
        return self.store.db.execute(
            "SELECT * FROM probe_activity_bins WHERE selectionId=? ORDER BY binIndex",
            (selectionId,)).fetchall()

    def clear(self,selectionId):
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            self.store.db.execute("DELETE FROM probe_activity_bins WHERE selectionId=?",
                                  (selectionId,))
            self.store.db.execute("DELETE FROM probe_activity_verdicts WHERE selectionId=?",
                                  (selectionId,))
            self.store.db.execute("COMMIT")
        except BaseException:
            if self.store.db.in_transaction:self.store.db.execute("ROLLBACK")
            raise

    async def collect(self,selection,probeBlock,head):
        """Collect ONE due 100-block bin per call; never return provisional PASS."""
        if selection["status"]!="PROBE_CONFIRMED":
            return
        selectionId=selection["selectionId"]
        pool,baseIndex=venueFor(selection)
        async with self.collectLock:
            rows=self.rows(selectionId)
            for row in rows:
                if (row["probeBlock"]!=probeBlock or row["poolId"]!=pool
                        or row["binIndex"] not in range(5)
                        or row["fromBlock"]!=probeBlock+1+100*row["binIndex"]
                        or row["toBlock"]!=probeBlock+100+100*row["binIndex"]):
                    raise ValueError("STALE_ACTIVITY_BIN_IDENTITY")
            if len(rows)==5:return
            missing=next(i for i in range(5) if i not in {r["binIndex"] for r in rows})
            low=probeBlock+1+100*missing
            high=low+99
            if head<high:
                return
            # Block-hash brackets prove the 100-block scan did not cross a reorg.
            before=await self.blockHash(high)
            blocks=set()
            for first in range(low,high+1,10):
                last=first+9
                logs=await self.rpc.call("eth_getLogs",[{
                    "address":MANAGER,
                    "fromBlock":hex(first),"toBlock":hex(last),
                    "topics":[SWAP,pool],
                }])
                if not isinstance(logs,list):
                    raise ValueError("INVALID_ACTIVITY_LOG_RESPONSE")
                for log in logs:
                    boughtAt=buyingBlockFromSwap(log,pool,baseIndex,first,last)
                    if boughtAt is not None:
                        blocks.add(boughtAt)
                # Low-priority RPC traffic; sell execution continues independently.
                await asyncio.sleep(self.requestPause)
            after=await self.blockHash(high)
            if before!=after:
                raise RuntimeError("REORG_DURING_ACTIVITY_COLLECTION")
            current=self.store.db.execute(
                "SELECT status FROM selections WHERE selectionId=?",(selectionId,)).fetchone()
            if not current or current["status"]!="PROBE_CONFIRMED":
                return
            self.store.db.execute("""INSERT OR IGNORE INTO probe_activity_bins
                (selectionId,probeBlock,poolId,binIndex,fromBlock,toBlock,
                 buyingBlocksJson,endHash) VALUES(?,?,?,?,?,?,?,?)""",
                (selectionId,probeBlock,pool,missing,low,high,
                 json.dumps(sorted(blocks)),after))
            print("PROBE ACTIVITY",selection["token"],"BIN",missing+1,"/5",
                  "BUYING_BLOCKS",len(blocks),flush=True)

    async def evaluate(self,selection,probeBlock):
        """Verify five canonical bin anchors, then persist exact PASS/FAIL."""
        selectionId=selection["selectionId"]
        pool,_=venueFor(selection)
        rows=self.rows(selectionId)
        if len(rows)!=5:
            return "UNKNOWN",None
        seen=set()
        evidence=[]
        for index,row in enumerate(rows):
            low=probeBlock+1+100*index
            high=low+99
            if (row["binIndex"]!=index or row["probeBlock"]!=probeBlock
                    or row["poolId"]!=pool
                    or row["fromBlock"]!=low or row["toBlock"]!=high):
                return "UNKNOWN",None
            blocks=json.loads(row["buyingBlocksJson"])
            if (not isinstance(blocks,list)
                    or any(type(b) is not int or not low<=b<=high for b in blocks)
                    or len(blocks)!=len(set(blocks))
                    or not HEX_TOPIC.fullmatch(row["endHash"])):
                return "UNKNOWN",None
            actual=await self.blockHash(high)
            if actual!=row["endHash"]:
                # A true reorg invalidates ALL cached history and any old verdict.
                self.clear(selectionId)
                return "UNKNOWN",None
            seen.update(blocks)
            evidence.append([index,low,high,row["endHash"],blocks])
        count=len(seen)
        verdict="PASS" if count<=MAX_BUYING_BLOCKS else "FAIL"
        digest=hashlib.sha256(json.dumps(
            [selectionId,probeBlock,pool,evidence],
            separators=(",",":"),sort_keys=True).encode()).hexdigest()
        self.store.db.execute("""INSERT INTO probe_activity_verdicts
            (selectionId,probeBlock,poolId,count,verdict,endHash,evidenceHash,checkedAt)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(selectionId) DO UPDATE SET
             probeBlock=excluded.probeBlock,poolId=excluded.poolId,
             count=excluded.count,verdict=excluded.verdict,
             endHash=excluded.endHash,evidenceHash=excluded.evidenceHash,
             checkedAt=excluded.checkedAt""",
            (selectionId,probeBlock,pool,count,verdict,rows[-1]["endHash"],
             digest,time.time()))
        print("PROBE ACTIVITY GATE",selection["token"],"BUYING_BLOCKS",count,
              verdict,flush=True)
        return verdict,count

    def passed(self,selectionId):
        row=self.store.db.execute("""
            SELECT v.count,v.verdict,v.probeBlock,v.poolId,v.endHash
              FROM probe_activity_verdicts v WHERE v.selectionId=?""",
            (selectionId,)).fetchone()
        return (row if row and row["verdict"]=="PASS"
                and 0<=row["count"]<=MAX_BUYING_BLOCKS else None)

    async def preBuyVerified(self,selection,probeBlock):
        """Final fail-closed guard immediately BEFORE the main BUY is signed."""
        row=self.passed(selection["selectionId"])
        if row is None:
            return False
        pool,_=venueFor(selection)
        if row["probeBlock"]!=probeBlock or row["poolId"]!=pool:
            return False
        return await self.blockHash(probeBlock+WINDOW_BLOCKS)==row["endHash"]
