#!/usr/bin/env python3
"""Read-only BlackSwan timing evidence. No trading imports, keys, DB writes or process changes.

Reads committed SQLite rows, not raw signed bytes. A bounded optional read-only
Alchemy probe obtains canonical receipt/target headers and current RPC latency.
Run from the actual concentration repository. Output is local gzip JSON only.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit

SOURCES=("detector.py","five_second_selector.py","concentration_selector.py",
         "live_executor.py","sacrificial_lamb.py","entry_timing.py","wallet_transactions.py",
         "execution_peak_routes.py","swap_adapters.py","broadcast_rpc.py","peak_tracker.py",
         "campaign_v3.py","run_configured_campaign.py","monitor_snapshot.py",
         "compact_campaign_monitor.py","live_dashboard.py","concentration_monitor.py",
         "probe_buying_blocks.py")
SCRIPTS=set(SOURCES)|{"run_live_campaign.py","run_fresh_peaks.py","run_concentration_shadow.py"}
HOME=Path.home()
HEX=re.compile(r"0x[0-9a-fA-F]{1,64}\Z")
IDENT=re.compile(r"[A-Za-z0-9_.:+-]{1,512}\Z")
REASON=re.compile(r"\b(?:[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+|TimeoutError|ClientError)\b")
META_KEYS={"status","lastConnectedHead","lastBackfilledHead","recoveryStatus","executorHeartbeat",
           "manualExitHeartbeat","intakeState","selectorCursor","detectorCursor","heartbeat",
           "campaignIdentity","campaignKind","campaignVersion","buyGbp","purchaseLimit",
           "lastHead","lastHeadAt","lastSeenHead","head","headAt","lastDecisionAt"}
META_PREFIX=("entryDeadline:","canonicalSurvivalAt:","probeRetryAfter:","buyRetryAfter:")
FIELDS={"policyName","policyVersion","windowAnchor","windowSeconds","firstTradeAt",
        "firstTradeBlock","windowEndAtExclusive","decisionLagSeconds","coverageComplete",
        "hypotheticalStatus","reason","decisionAt","eventKey","selectionId","tokenKey",
        "venueKey","status","interfaceVersion","maxLargestBuyTransactionShareBps",
        "largestBuyShareBps","largestBuySharePct","buyTransactions","observedSwaps",
        "totalBuyBaseRaw","largestBuyBaseRaw","buyingBlocks","detectedAt","createdAt",
        "deadline","blockNumber","blockHash","transactionHash","logIndex","source",
        "chainId","blockTimestamp","headerTimestamp","timestamp"}
DETAILS={"currency0","currency1","token0","token1","fee","tickSpacing","hooks",
         "poolId","sqrtPriceX96","tick","baseAsset","source","id","spacing","emitter",
         "address","venueKey","token","blockNumber","blockHash","transactionHash"}
SCHEMA={
 "selections":"selectionId token decisionAt event status reason quoteWei",
 "excluded_passes":"selectionId token decisionAt source reason",
 "transactions":"intentId selectionId kind state txHash nonce createdAt lastBroadcast inputAmount receipt reason",
 "lamb_probes":"selectionId token probeTxHash confirmedAt probeQty probeInputWei lastCheck passedAt result reason",
 "positions":"selectionId token originalQty spentWei boughtAt bestGbp lastGrowth nextDue lastCheck preApprovalNext error trigger3 trigger5 trigger10 sold3 sold5 sold10",
 "sales":"selectionId stage txHash confirmedAt tokenDebited wethReceivedWei",
 "exit_slices":"selectionId stage multiple percent triggered sold soldQty",
 "decisions":"seq eventKey tokenKey venueKey status decisionAt data",
 "launches":"eventKey tokenKey venueKey source state detectedAt deadline createdAt data",
 "revocations":"seq eventKey notedAt reason",
 "concentration_progress":"eventKey nextBlock launchTimestamp lastError",
 "concentration_audits":"eventKey lastBlock lastHash lastChecked",
 "gaps":"id start_block end_block reason noted_at_utc",
 "manual_fills":"selectionId stage txHash confirmedAt tokenDebited",
 "observations":"id token phase at block venue_id status quantity out_wei gbp multiple basis reason",
}
def safeReason(value):
    if value is None:return None
    return " ".join(REASON.findall(str(value))[:12]) or "UNCLASSIFIED_DETAIL_OMITTED"

def atom(value):
    if value is None or isinstance(value,(bool,int,float)):return value
    if isinstance(value,str) and IDENT.fullmatch(value):return value
    return None

def selected(obj,fields):
    if not isinstance(obj,dict):return {}
    return {k:safeReason(v) if k in ("reason","lastError","error") else atom(v)
            for k,v in obj.items() if k in fields}

def unpack(value):
    try:
        obj=json.loads(value) if isinstance(value,str) else value
        return obj if isinstance(obj,dict) else {}
    except (ValueError,TypeError):return {}

def payload(value):
    obj=unpack(value)
    result=selected(obj,FIELDS)
    for k in ("event","venue"):
        if isinstance(obj.get(k),dict):
            result[k]=selected(obj[k],FIELDS|DETAILS)
            if isinstance(obj[k].get("details"),dict):
                result[k]["details"]=selected(obj[k]["details"],DETAILS)
    return result

def receipt(value):
    return selected(unpack(value),{"status","blockNumber","blockHash","transactionHash",
                                   "gasUsed","effectiveGasPrice","transactionIndex"})

def cleanRow(row,table):
    out={}
    for k,v in dict(row).items():
        if k in ("event","data"):out[k]=payload(v)
        elif k=="receipt":out[k]=receipt(v)
        elif k in ("reason","error","lastError"):out[k]=safeReason(v)
        else:out[k]=atom(v)
    return out

def manifest(path):
    if not path.is_file():return {}
    cfg=unpack(path.read_text())
    result=selected(cfg,{"schemaVersion","chainId","walletPublicAddress"})
    for section,fields in {
       "selector":{"name","policyVersion","windowAnchor","windowSeconds","decisionSeconds","minBuyingBlocks","maxBuyingBlocks","maxLargestBuyTransactionShareBps","firstTradeTimeoutSeconds"},
       "main":{"stakeGbp","maxPurchases"},
       "sacrifice":{"enabled","stakeGbp","activityMaxBuyingBlocks"},
       "execution":{"entryDeadlineSeconds","sacrificeDeadlineSeconds"},
       "limits":{"maxSacrifices","maxNominalSpendGbp"}}.items():
        result[section]=selected(cfg.get(section),fields)
    result["sacrifice"]["wait"]=selected(cfg.get("sacrifice",{}).get("wait"),{"unit","value"})
    return result

def runCmd(args,root):
    try:
        p=subprocess.run(args,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                         text=True,timeout=4,check=False)
        return p.stdout.strip() if p.returncode==0 else None
    except (OSError,subprocess.TimeoutExpired):return None

def provenance(root):
    result={"directory":root.name,"head":atom(runCmd(["git","rev-parse","HEAD"],root)),
            "files":{}}
    for name in SOURCES:
        path=root/name
        if not path.is_file():continue
        data=path.read_bytes()
        result["files"][name]={"sha256":hashlib.sha256(data).hexdigest(),
            "gitBlob":atom(runCmd(["git","hash-object",name],root)),
            "headBlob":atom(runCmd(["git","rev-parse","HEAD:"+name],root))}
    result["stashCount"]=len((runCmd(["git","stash","list","--format=%H"],root) or "").splitlines())
    return result

def machine():
    result={"at":time.time(),"monotonic":time.monotonic(),"python":platform.python_version(),
            "kernel":platform.release(),"cpuCount":os.cpu_count(),"loadAverage":os.getloadavg(),
            "clock":{},"proc":{},"workers":[]}
    for key in ("NTPSynchronized","NTP"):
        result["clock"][key]=atom(runCmd(["timedatectl","show","-p",key,"--value"],Path.cwd()))
    for name in ("stat","meminfo","pressure/cpu","pressure/io","pressure/memory","uptime"):
        try:
            content=(Path("/proc")/name).read_text()
            if name=="stat":content="\n".join(x for x in content.splitlines() if x.startswith(("cpu ","procs_running ","procs_blocked ")))
            if name=="meminfo":content="\n".join(x for x in content.splitlines() if x.startswith(("MemTotal:","MemAvailable:","SwapTotal:","SwapFree:")))
            result["proc"][name]=content[:3000]
        except OSError:pass
    for p in Path("/proc").glob("[0-9]*"):
        try:
            args=(p/"cmdline").read_bytes().split(b"\0")
            scripts=[Path(os.fsdecode(a)).name for a in args[1:] if a.endswith(b".py")]
            matching=[s for s in scripts if s in SCRIPTS]
            if not matching:continue
            stat=(p/"stat").read_text().rsplit(")",1)[1].split()
            result["workers"].append({"pid":int(p.name),"scripts":matching,"state":stat[0],
                "userTicks":int(stat[11]),"systemTicks":int(stat[12]),"threads":int(stat[17]),
                "startTicks":int(stat[19]),"rssPages":int(stat[21]),
                "armed":b"--arm-live" in args})
        except (OSError,ValueError,IndexError):continue
    return result

def rows(db,table,where="",params=(),order="",limit=None):
    # Identifiers are fixed by the collector, never sourced from campaign data.
    cols={r[1] for r in db.execute("PRAGMA table_info("+table+")")}
    if not cols:return None
    use=[k for k in SCHEMA[table].split() if k in cols]
    if not use:return None
    sql="SELECT "+",".join(use)+" FROM "+table
    if where:sql+=" WHERE "+where
    if order:sql+=" ORDER BY "+order
    if limit is not None:sql+=" LIMIT "+str(int(limit))
    return [cleanRow(r,table) for r in db.execute(sql,params)]

def meta(db):
    try:
        result={}
        for key,value in db.execute("SELECT key,value FROM meta"):
            if key in META_KEYS or key.startswith(META_PREFIX):
                if IDENT.fullmatch(key):result[key]=atom(value)
        return result
    except sqlite3.Error:return {}

def readDb(path,kind):
    if not path.is_file():return {"missing":True}
    out={"at":time.time(),"tables":{},"errors":[]}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=.25)) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            # No long-lived transaction or locks across network calls.
            db.execute("BEGIN")
            out["meta"]=meta(db)
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            out["tablesPresent"]=sorted(n for n in names if IDENT.fullmatch(n))
            group={"execution":("selections","excluded_passes","transactions","lamb_probes","positions","sales","exit_slices","manual_fills"),
                   "selector":("decisions","launches","revocations","concentration_progress","concentration_audits"),
                   "detector":("gaps",)}[kind]
            for table in group:
                if table not in names:continue
                try:
                    # All entry/transaction/decision history, not a display-limited subset.
                    out["tables"][table]=rows(db,table)
                except sqlite3.Error as e:out["errors"].append([table,type(e).__name__])
            queries={}
            if kind=="execution":
                queries={
                    "observationCounts":"SELECT phase,status,COUNT(*) AS count,MIN(at) AS firstAt,MAX(at) AS lastAt FROM observations GROUP BY phase,status",
                    "entryObservations":"SELECT id,token,phase,at,block,status,basis,reason FROM observations WHERE phase IN ('ENTRY','PROBE_ENTRY') ORDER BY at",
                    "nearEntryObservations":"SELECT o.id,o.token,o.phase,o.at,o.block,o.status,o.basis,o.reason FROM observations o JOIN selections s ON s.token=o.token WHERE o.at BETWEEN s.decisionAt-5 AND s.decisionAt+180 ORDER BY o.at",
                }
            if kind=="selector":
                queries={
                    "buyingEvidenceTiming":"SELECT eventKey,COUNT(*) AS count,MIN(availableAt) AS firstAt,MAX(availableAt) AS lastAt,SUM(valid) AS validCount FROM buyingEvents GROUP BY eventKey",
                    "coverageRanges":"SELECT eventKey,COUNT(*) AS chunks,MIN(startBlock) AS firstBlock,MAX(endBlock) AS lastBlock,MIN(endTimestamp) AS firstEndAt,MAX(endTimestamp) AS lastEndAt FROM concentration_chunks GROUP BY eventKey",
                    "swapEvidenceTiming":"SELECT eventKey,COUNT(*) AS swaps,MIN(blockNumber) AS firstBlock,MAX(blockNumber) AS lastBlock,MIN(blockTimestamp) AS firstAt,MAX(blockTimestamp) AS lastAt FROM concentration_swaps GROUP BY eventKey",
                }
            if kind=="detector":
                queries={"eventCounts":"SELECT source,COUNT(*) AS count,MIN(block_number) AS firstBlock,MAX(block_number) AS lastBlock,MIN(stored_at_utc) AS firstAt,MAX(stored_at_utc) AS lastAt FROM event_log GROUP BY source",
                         "decodeFailureCount":"SELECT COUNT(*) AS count FROM decode_failures"}
            for name,query in queries.items():
                try:out[name]=[cleanRow(r,name) for r in db.execute(query)]
                except sqlite3.Error as e:out["errors"].append([name,type(e).__name__])
            db.rollback()
    except sqlite3.Error as e:out["errors"].append(["database",type(e).__name__])
    out["finishedAt"]=time.time()
    return out

def collect(root):
    result=[]
    data=root/"data"
    if not data.is_dir():return result
    dirs=sorted((p for p in data.iterdir() if p.is_dir() and
                 (p/"execution.sqlite3").is_file()),key=lambda p:p.stat().st_mtime,reverse=True)
    for d in dirs:
        run={"repository":root.name,"campaign":d.name,"at":time.time()}
        try:run["config"]=manifest(d/"campaign.json")
        except (OSError,TypeError,AttributeError,ValueError):run["configError"]=True
        for kind in ("execution","selector","detector"):
            run[kind]=readDb(d/(kind+".sqlite3"),kind)
        result.append(run)
        print("Collected",root.name,d.name,flush=True)
    return result

def integer(v):
    try:return int(v,16) if isinstance(v,str) and v.startswith("0x") else int(v)
    except (ValueError,TypeError):return None

def targets(runs):
    """Prioritise all recent deadline incidents, then successful baseline entries."""
    result=[]
    for run in runs:
        txs=run.get("execution",{}).get("tables",{}).get("transactions") or []
        selections=run.get("execution",{}).get("tables",{}).get("selections") or []
        byId={}
        for tx in txs:byId.setdefault(tx.get("selectionId"),[]).append(tx)
        wait=run.get("config",{}).get("sacrifice",{}).get("wait",{})
        for s in selections:
            txList=[t for t in byId.get(s["selectionId"],[]) if t.get("kind") in ("BUY","PROBE_BUY")]
            if not txList:continue
            priority=0 if "DEADLINE" in (s.get("reason") or "") else 1
            result.append((priority,-float(s.get("decisionAt") or 0),run,s,txList,wait))
    return sorted(result,key=lambda x:x[:2])

def endpoint(path):
    values={}
    for line in path.read_text().splitlines():
        line=line.strip().removeprefix("export ").strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1)
            if k in ("ROBINHOOD_ALCHEMY_HTTP_URL","BSRH_RPC_HTTP"):
                values[k]=v.strip().strip("\"'")
    url=values.get("ROBINHOOD_ALCHEMY_HTTP_URL") or values.get("BSRH_RPC_HTTP")
    u=urlsplit(url or "")
    if (u.scheme!="https" or u.netloc!="robinhood-mainnet.g.alchemy.com" or
        u.query or u.fragment or not re.fullmatch(r"/v2/[A-Za-z0-9_-]+",u.path)):
        raise ValueError("INVALID_LOCAL_RPC")
    return url

class Probe:
    ALLOWED={"eth_chainId","eth_blockNumber","eth_getBlockByNumber","eth_getTransactionReceipt",
             "eth_getCode","eth_maxPriorityFeePerGas","eth_getTransactionCount","eth_getBalance"}
    def __init__(self,session,url,budget):
        self.session=session;self.url=url;self.budget=budget;self.samples=[];self.halted=False
        self.headers={};self.receipts={}
    async def call(self,method,params,label):
        if method not in self.ALLOWED:raise ValueError("UNSAFE_RPC_METHOD")
        if self.halted or len(self.samples)>=self.budget:return None
        record={"method":method,"label":label,"startAt":time.time()}
        self.samples.append(record)
        started=time.monotonic()
        try:
            async with self.session.post(self.url,json={"jsonrpc":"2.0","id":len(self.samples),
                    "method":method,"params":params},allow_redirects=False) as resp:
                record["httpStatus"]=resp.status
                if resp.status!=200:
                    if resp.status in (401,402,403,429) or resp.status>=500:self.halted=True
                    return None
                data=await resp.json(content_type=None)
            if not isinstance(data,dict):return None
            if data.get("error"):
                err=data["error"]
                record["rpcErrorCode"]=integer(err.get("code")) if isinstance(err,dict) else None
                if record["rpcErrorCode"]==-32005:self.halted=True
                return None
            result=data.get("result")
            record["ok"]=result is not None
            return result
        except Exception as e:
            record["errorType"]=type(e).__name__
            return None
        finally:
            record["durationMs"]=round((time.monotonic()-started)*1000,3)
            record["endAt"]=time.time()
    async def header(self,n):
        if n is None or n<0:return None
        if n not in self.headers:
            h=await self.call("eth_getBlockByNumber",[hex(n),False],"canonical-header")
            self.headers[n]=selected(h,{"number","timestamp","hash","parentHash","baseFeePerGas"})
        return self.headers[n]
    async def getReceipt(self,txHash):
        if not isinstance(txHash,str) or not HEX.fullmatch(txHash) or len(txHash)!=66:return {}
        if txHash not in self.receipts:
            r=await self.call("eth_getTransactionReceipt",[txHash],"canonical-receipt")
            self.receipts[txHash]=receipt(r)
        return self.receipts[txHash]

async def network(runs,env,budget):
    import aiohttp
    url=endpoint(env)
    out={"samples":[],"headers":{},"receiptChecks":{},"cases":[],"budget":budget}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3),trust_env=False,
                                      connector=aiohttp.TCPConnector(limit=3)) as session:
        p=Probe(session,url,budget)
        chain=integer(await p.call("eth_chainId",[],"verify-chain"))
        out["chainId"]=chain
        if chain!=4663:
            out["samples"]=p.samples
            out["error"]="CHAIN_UNVERIFIED"
            return out
        # Warm session, low-volume current latency and block cadence. No tx
        # submission, estimation or broad log ranges, no retry storms.
        for i in range(3):
            if p.halted:break
            head=integer(await p.call("eth_blockNumber",[],"head-sample"))
            if head is not None:await p.header(head)
            if i==0 and head is not None:
                out["observedHead"]=head
                for distance in (1,30,500):await p.header(head-distance)
            await asyncio.sleep(.1)
        # Real signer metadata RPCs, not a claim of full-path latency.
        wallet=next((run.get("config",{}).get("walletPublicAddress") for run in runs
                     if isinstance(run.get("config",{}).get("walletPublicAddress"),str)),None)
        for i in range(2):
            if p.halted:break
            await p.call("eth_getCode",["0x204faca1764b154221e35c0d20abb3c525710498","latest"],"router-code")
            await p.call("eth_maxPriorityFeePerGas",[],"priority-fee")
            if wallet and re.fullmatch(r"0x[0-9a-fA-F]{40}",wallet):
                await p.call("eth_getTransactionCount",[wallet,"pending"],"pending-nonce")
                await p.call("eth_getBalance",[wallet,"latest"],"wallet-balance")
        for priority,_,run,s,txList,wait in targets(runs):
            if p.halted or len(p.samples)>=budget-4:break
            case={"campaign":run["campaign"],"repository":run["repository"],
                  "selectionId":s["selectionId"],"token":s.get("token"),"transactions":[]}
            for t in txList:
                rr=await p.getReceipt(t.get("txHash"))
                n=integer(rr.get("blockNumber"))
                if n is None:n=integer(t.get("receipt",{}).get("blockNumber"))
                h=await p.header(n)
                row={"kind":t.get("kind"),"txHash":t.get("txHash"),"receiptBlock":n,
                     "receiptTimestamp":integer((h or {}).get("timestamp")),
                     "receiptMatchesHeader":bool(rr.get("blockHash") and
                          rr.get("blockHash")== (h or {}).get("hash"))}
                if t.get("kind")=="PROBE_BUY" and wait.get("unit")=="blocks" and n is not None:
                    count=integer(wait.get("value"))
                    if count is not None and count>=1:
                        target=n+count
                        row["targetBlock"]=target
                        if target<=out.get("observedHead",-1):
                            targetHeader=await p.header(target)
                            row["targetTimestamp"]=integer((targetHeader or {}).get("timestamp"))
                case["transactions"].append(row)
            out["cases"].append(case)
        out.update(samples=p.samples,headers=p.headers,receiptChecks=p.receipts,
                   halted=p.halted,calls=len(p.samples))
    return out

def selfTest():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        root=Path(tmp)
        d=root/"data"/"configured_v3_fixture";d.mkdir(parents=True)
        (d/"campaign.json").write_text(json.dumps({"schemaVersion":3,"chainId":4663,
          "selector":{"name":"concentration"},"sacrifice":{"wait":{"unit":"blocks","value":30}},
          "execution":{"entryDeadlineSeconds":3},"secret":"NEVER_EXPORT"}))
        with closing(sqlite3.connect(d/"execution.sqlite3")) as db:
            db.executescript("""CREATE TABLE selections(selectionId TEXT,token TEXT,decisionAt REAL,event TEXT,status TEXT,reason TEXT);
            CREATE TABLE transactions(intentId TEXT,selectionId TEXT,kind TEXT,state TEXT,txHash TEXT,raw TEXT,createdAt REAL,lastBroadcast REAL,receipt TEXT);
            CREATE TABLE lamb_probes(selectionId TEXT,confirmedAt REAL,passedAt REAL,result TEXT,reason TEXT);
            CREATE TABLE meta(key TEXT,value TEXT);""")
            db.execute("INSERT INTO selections VALUES(?,?,?,?,?,?)",("s","0x"+"a"*40,100,json.dumps({"firstTradeAt":65,"windowEndAtExclusive":95,"secret":"NEVER_EXPORT"}),"PROBE_FAILED","MISSED_SURVIVAL_DEADLINE https://secret.invalid/key"))
            db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?)",("i","s","PROBE_BUY","CONFIRMED","0x"+"b"*64,"NEVER_EXPORT_RAW",100,101,json.dumps({"blockNumber":"0xa","logs":["NEVER_EXPORT"]})))
            db.execute("INSERT INTO meta VALUES('canonicalSurvivalAt:s','105')")
            db.commit()
        run=collect(root)[0]
        assert run["config"]["sacrifice"]["wait"]["value"]==30
        assert run["execution"]["tables"]["selections"][0]["reason"]=="MISSED_SURVIVAL_DEADLINE"
        assert run["execution"]["tables"]["transactions"][0]["receipt"]["blockNumber"]=="0xa"
        assert run["execution"]["meta"]["canonicalSurvivalAt:s"]=="105"
        assert len(targets([run]))==1
        serial=json.dumps(run)
        assert "NEVER_EXPORT" not in serial and "secret.invalid" not in serial
        assert run["selector"]["missing"] is True
        assert not (d/"selector.sqlite3").exists()
    print("PASS timing collector fixture (read-only, redaction, 30 blocks, missing DB)")

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--offline",action="store_true")
    ap.add_argument("--rpc-budget",type=int,default=100)
    ap.add_argument("--env",type=Path,default=HOME/".config/blackswan/concentration-new-alchemy.env")
    ap.add_argument("--output",type=Path,default=HOME/"Downloads/blackswan-entry-timing-evidence.json.gz")
    args=ap.parse_args()
    if args.self_test:selfTest();return
    if not 1<=args.rpc_budget<=120:ap.error("RPC budget must be 1–120")
    roots=[]
    for root in (Path.cwd(),HOME/"blackswan-concentration-audit",HOME/"blackswan-buying-block-one",
                 HOME/"blackswanRH_guarded",HOME/"blackswan-concentration",HOME/"blackswan-modular"):
        if root.is_dir() and (root/"data").is_dir() and root.resolve() not in roots:roots.append(root.resolve())
    report={"schemaVersion":1,"startedAt":time.time(),"machineBefore":machine(),
            "repositories":[],"campaigns":[],"errors":[],
            "limitations":["Independent SQLite snapshots, not cross-database atomic.",
             "Local decisionAt/confirmedAt are not chain timestamps; lastBroadcast is last attempt.",
             "Historic queue, lock and per-RPC timings were not persisted; cannot reconstruct them exactly.",
             "Network probes measure current read-only metadata latency, not historical quote/estimate latency.",
             "Only configured known repository paths with execution.sqlite3; legacy schemas may be partial.",
             "No raw signed transactions, wallet keystore, environment contents, process command lines or logs exported."]}
    for root in roots:
        try:report["repositories"].append(provenance(root));report["campaigns"].extend(collect(root))
        except (OSError,sqlite3.Error,ValueError) as e:report["errors"].append([root.name,type(e).__name__])
    if not args.offline:
        try:report["network"]=asyncio.run(network(report["campaigns"],args.env.expanduser(),args.rpc_budget))
        except Exception as e:report["network"]={"error":type(e).__name__}
    report["machineAfter"]=machine()
    report["finishedAt"]=time.time()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive private output; never overwrite a prior report.
    path=args.output
    if path.exists():path=path.with_name("blackswan-entry-timing-evidence-"+str(time.time_ns())+".json.gz")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as raw:
        with gzip.GzipFile(fileobj=raw,mode="wb") as compressed:
            compressed.write(json.dumps(report,separators=(",",":"),allow_nan=False).encode())
    print("Report:",path,flush=True)
    print("Campaigns:",len(report["campaigns"]),"RPC calls:",report.get("network",{}).get("calls",0),
          "Collection errors:",len(report["errors"]),flush=True)

if __name__=="__main__":main()
