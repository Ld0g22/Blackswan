"""Read-only concentration operator snapshots over the original buying-block monitor.

Never imports a wallet/signing/network module or opens SQLite for writing.
The old live_dashboard.snapshot remains the compatibility/financial base.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from live_dashboard import snapshot as originalSnapshot


def asInt(value,default=None):
    try:
        if value is None or isinstance(value,bool):
            return default
        return int(value,0) if isinstance(value,str) and value.lower().startswith("0x") else int(value)
    except (ValueError,TypeError,OverflowError):
        return default


def asDecimal(value):
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (TypeError,ValueError,InvalidOperation):
        return None


def unpack(value,default=None):
    try:
        item=json.loads(value)
        return item if isinstance(item,dict) else (default if default is not None else {})
    except (ValueError,TypeError):
        return default if default is not None else {}


def readonly(path):
    """Return nothing on absent file. Never create, migrate or journal-write."""
    path=Path(path)
    if not path.is_file():
        return None
    uri="file:"+quote(str(path.resolve()),safe="/")+"?mode=ro"
    db=sqlite3.connect(uri,uri=True,timeout=.15,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=150")
    return db


def safeRows(db,sql,params,view,label):
    if db is None:
        return []
    try:
        return [dict(x) for x in db.execute(sql,params)]
    except sqlite3.Error as exc:
        warning=f"{label}: {type(exc).__name__}"
        if warning not in view["warnings"]:
            view["warnings"].append(warning)
        return []


def safeMeta(db,view,label):
    return {r["key"]:r["value"] for r in safeRows(
        db,"SELECT key,value FROM meta",(),view,label)}


def baseView(directory,now):
    try:
        return originalSnapshot(directory,now=now)
    except (sqlite3.Error,ValueError,TypeError,KeyError) as exc:
        return dict(now=now,directory=str(directory),warnings=[
            "legacy monitor data unavailable: "+type(exc).__name__
        ],summary={},positions=[],sales=[],probes=[],transactions=[],
        selections=[],detector={},selector={},manualFills=[],manualRequests=[],
        selectionCounts={})


def displayToken(row,payload):
    candidate=payload.get("tokenKey") or row.get("tokenKey") or ""
    return str(candidate).split(":")[-1]


def fullVenue(payload,row=None):
    venue=payload.get("venue")
    if isinstance(venue,dict) and venue.get("id"):
        return str(venue["id"])
    if row and row.get("venueKey"):
        return str(row["venueKey"])
    return "UNAVAILABLE"


def mainSelection(row):
    return {
        "selectionId":row.get("selectionId"),
        "token":row.get("token"),
        "status":row.get("status"),
        "reason":row.get("reason"),
        "decisionAt":row.get("decisionAt"),
    }


def readSnapshot(directory,now=None,limit=50,staleAfterSeconds=180):
    """Build one conservative, immutable-by-convention view from committed DBs.

    A read per independent journal is NOT a cross-DB atomic snapshot. Missing
    or temporarily unreadable journals emit visible warnings, never zeros.
    """
    directory=Path(directory).expanduser().resolve()
    now=time.time() if now is None else now
    if not 1<=limit<=500:
        raise ValueError("MONITOR_ROW_LIMIT_OUT_OF_RANGE")
    legacy=baseView(directory,now)
    view=dict(
        now=now,directory=str(directory),base=legacy,
        warnings=list(legacy.get("warnings",[])),policy={},
        mode="SHADOW",hasCampaign=False,hasExecution=False,
        opportunities=[],positions=[],holdings=[],sacrifices=[],transactions=[],
        sales=[],manualRequests=[],manualFills=[],alerts=[],
        health={},finance={},meta={},counts={},
        sampleLimit=limit,staleAfterSeconds=staleAfterSeconds
    )

    configFile=directory/"campaign.json"
    if configFile.is_file():
        try:
            config=json.loads(configFile.read_text(encoding="utf-8"))
            if not isinstance(config,dict):
                raise ValueError("INVALID_CAMPAIGN_CONFIG")
            view["policy"]=config
            view["hasCampaign"]=True
            view["mode"]="CAMPAIGN"
        except (OSError,UnicodeError,ValueError) as exc:
            view["warnings"].append("campaign config unavailable: "+type(exc).__name__)

    conns={}
    try:
        for name in ("execution","selector","detector"):
            path=directory/(name+".sqlite3")
            try:
                conns[name]=readonly(path)
            except (OSError,sqlite3.Error) as exc:
                conns[name]=None
                view["warnings"].append(name+" unreadable: "+type(exc).__name__)
        ex,se,de=(conns.get(name) for name in ("execution","selector","detector"))
        view["hasExecution"]=ex is not None
        exMeta=safeMeta(ex,view,"execution metadata") if ex else {}
        seMeta=safeMeta(se,view,"selector metadata") if se else {}
        deMeta=safeMeta(de,view,"detector metadata") if de else {}
        view["meta"]={"execution":exMeta,"selector":seMeta,"detector":deMeta}
        if ex and not view["hasCampaign"]:
            view["mode"]="EXECUTION_WITHOUT_CAMPAIGN_MANIFEST"
            view["warnings"].append("execution exists without campaign.json; validate run directory")
        elif not ex and view["hasCampaign"]:
            view["mode"]="CAMPAIGN_NOT_YET_ARMED"

        positions=safeRows(ex,"SELECT * FROM positions ORDER BY boughtAt DESC LIMIT ?",
                           (limit,),view,"positions")
        selections=safeRows(ex,"SELECT selectionId,token,status,reason,decisionAt,event "
                           "FROM selections ORDER BY decisionAt DESC LIMIT ?",
                           (max(100,limit*3),),view,"execution selections")
        selectionById={x["selectionId"]:x for x in selections}
        transactionRows=safeRows(ex,"SELECT intentId,selectionId,kind,state,txHash,createdAt,"
                       "lastBroadcast,receipt,reason,inputAmount FROM transactions "
                       "ORDER BY createdAt DESC LIMIT ?",(max(300,limit*8),),
                       view,"transaction journal")
        for tx in transactionRows:
            tx["receiptConfirmed"]=bool(tx.pop("receipt",None))
            tx["token"]=selectionById.get(tx["selectionId"],{}).get("token")
        view["transactions"]=transactionRows
        byId=defaultdict(list)
        for tx in transactionRows:byId[tx["selectionId"]].append(tx)
        autoSales=safeRows(ex,"SELECT selectionId,stage,txHash,tokenDebited,"
                  "wethReceivedWei,confirmedAt FROM sales ORDER BY confirmedAt DESC",
                  (),view,"confirmed sale receipts")
        manualFills=safeRows(ex,"SELECT requestId,selectionId,txHash,tokenDebited,"
                    "wethReceivedWei,gasWei,confirmedAt FROM manual_fills "
                    "ORDER BY confirmedAt DESC",(),view,"manual sale receipts")
        view["manualFills"]=manualFills
        view["manualRequests"]=safeRows(ex,"SELECT requestId,selectionId,operation,"
                      "percent,status,createdAt,updatedAt,quantity,quoteGbp,txHash,reason "
                      "FROM manual_requests ORDER BY createdAt DESC LIMIT ?",
                      (limit,),view,"manual requests")
        salesById=defaultdict(list)
        for r in autoSales:
            r["kind"]="AUTO"
            salesById[r["selectionId"]].append(r)
        for r in manualFills:
            r["kind"]="MANUAL"
            r["stage"]=None
            salesById[r["selectionId"]].append(r)
        view["sales"]=sorted(autoSales+manualFills,
                             key=lambda row:row.get("confirmedAt") or 0,reverse=True)

        quotes=safeRows(ex,"SELECT id,token,phase,at,block,venue_id,status,quantity,"
                 "out_wei,gbp,multiple,basis,reason "
                 "FROM observations WHERE phase='SELL' ORDER BY at DESC,id DESC LIMIT ?",
                 (max(2000,limit*40),),view,"sell quote observations")
        quoteLatest={}
        quoteSuccessful={}
        for q in quotes:
            token=q["token"].lower()
            if token not in quoteLatest:
                quoteLatest[token]=q
            if token not in quoteSuccessful and q["status"]=="ONCHAIN_QUOTE_ONLY" and q["gbp"] is not None:
                quoteSuccessful[token]=q
        probeRows=safeRows(ex,"SELECT selectionId,token,probeTxHash,confirmedAt,"
                  "probeQty,probeInputWei,lastCheck,passedAt,result,reason "
                  "FROM lamb_probes ORDER BY COALESCE(confirmedAt,lastCheck,0) DESC",
                  (),view,"sacrifice receipts")
        probesById={p["selectionId"]:p for p in probeRows}
        view["sacrifices"]=probeRows
        venueRows=safeRows(ex,"SELECT token,venueId,data,active FROM venues",
                           (),view,"known trade venues")
        venueByToken=defaultdict(list)
        for v in venueRows:venueByToken[v["token"].lower()].append(v)

        exitRows=safeRows(ex,"SELECT selectionId,stage,multiple,percent,triggered,sold,soldQty "
                      "FROM exit_slices ORDER BY selectionId,stage",(),view,"exit ladder")
        exitsById=defaultdict(list)
        for row in exitRows:exitsById[row["selectionId"]].append(row)

        financeRows=safeRows(ex,"SELECT txHash,kind,expenseWei,gasWei,incomeWei,incomeAsset,"
                 "incomeBasis,fxGbpPerEth,fxAt,state,reason,recordedAt "
                 "FROM finance_events ORDER BY recordedAt DESC",(),view,
                 "finance events")
        financeByHash={r["txHash"].lower():r for r in financeRows if r.get("txHash")}
        saleWithFinance=[]
        verifiedWeth=0
        missingWeth=0
        for row in view["sales"]:
            record=dict(row)
            event=financeByHash.get(str(row.get("txHash") or "").lower())
            record["finance"]=event
            record["asset"]=event.get("incomeAsset") if event and event.get("state")=="COMPLETE" else (
                "WETH" if row.get("wethReceivedWei") is not None else None)
            record["realisedGbp"]=None
            if event and event.get("state")=="COMPLETE":
                amount=asDecimal(event.get("incomeWei"))
                fx=asDecimal(event.get("fxGbpPerEth"))
                if amount is not None and fx is not None and fx>0:
                    record["realisedGbp"]=str(amount*fx/Decimal(10**18))
            if row.get("wethReceivedWei") is not None:
                verifiedWeth+=asInt(row["wethReceivedWei"],0)
            else:
                missingWeth+=1
            saleWithFinance.append(record)
        view["sales"]=saleWithFinance
        completeSales=all(x["realisedGbp"] is not None for x in saleWithFinance)
        realised=sum((Decimal(x["realisedGbp"]) for x in saleWithFinance
                      if x["realisedGbp"] is not None),Decimal(0))
        view["finance"]={
            "verifiedReceivedWethWei":verifiedWeth,
            "missingWethSaleCount":missingWeth,
            "actualRealisedGbp":str(realised) if completeSales else None,
            "qualifiedRealisedPartialGbp":str(realised),
            "cashComplete":bool(completeSales and ex is not None
                                 and legacy.get("summary",{}).get("finance",{}).get("complete",False)),
            "expenseGbp":None,"cashPnlGbp":None,
            "saleCount":len(saleWithFinance),
        }
        legacyFinance=legacy.get("summary",{}).get("finance") or {}
        if legacyFinance:
            view["finance"]["expenseGbp"]=str(legacyFinance.get("expenditureGbp"))
            if view["finance"]["cashComplete"]:
                view["finance"]["cashPnlGbp"]=str(legacyFinance.get("cashPnlGbp"))

        for p in positions:
            sid=p["selectionId"]
            token=p["token"]
            stageRows=[]
            actualSaleByStage={r["stage"]:r for r in autoSales if r["selectionId"]==sid}
            for entry in exitsById.get(sid,[]):
                receipt=actualSaleByStage.get(entry["stage"])
                state=("SOLD" if receipt else "UNVERIFIED"
                       if entry.get("sold") else "TRIGGERED"
                       if entry.get("triggered") else "WAITING")
                if state=="UNVERIFIED":
                    view["warnings"].append("sale flag without verified sale ledger: "+str(sid))
                tx=next((t for t in byId.get(sid,[])
                         if t["kind"]=="SELL_"+str(entry["stage"])),None)
                stageRows.append(dict(
                    stage=entry["stage"],multiple=entry["multiple"],
                    percent=entry["percent"],state=state,
                    triggered=bool(entry["triggered"]),sold=bool(receipt),
                    soldQty=receipt["tokenDebited"] if receipt else None,
                    txHash=receipt["txHash"] if receipt else tx["txHash"] if tx else None,
                    txState=tx["state"] if tx else None,
                    confirmedAt=receipt["confirmedAt"] if receipt else None,
                    receivedWei=receipt["wethReceivedWei"] if receipt else None,
                    realisedGbp=next((r["realisedGbp"] for r in saleWithFinance
                           if receipt and r["txHash"]==receipt["txHash"]),None),
                    targetQty=(str((asInt(p["originalQty"],0)*asDecimal(entry["percent"])
                                    /Decimal(100)).to_integral_value(rounding="ROUND_DOWN"))
                               if asDecimal(entry["percent"]) is not None else None),
                    error=tx["reason"] if tx else None))
            recordedSold=sum(asInt(r["tokenDebited"],0) for r in salesById.get(sid,[]))
            original=asInt(p.get("originalQty"),0)
            remain=max(0,original-recordedSold)
            latest=quoteLatest.get(token.lower())
            success=quoteSuccessful.get(token.lower())
            probe=probesById.get(sid)
            originalSelection=selectionById.get(sid,{})
            selectionPayload=unpack(originalSelection.get("event"))
            pool=fullVenue(selectionPayload)
            if pool=="UNAVAILABLE" and venueByToken.get(token.lower()):
                pool=venueByToken[token.lower()][0]["venueId"]
            latestValid=bool(latest and latest["status"]=="ONCHAIN_QUOTE_ONLY"
                             and latest.get("gbp") is not None)
            qage=(max(0,now-float(latest["at"])) if latest else None)
            qStale=latest is None or qage>staleAfterSeconds or not latestValid
            view["holdings"].append(dict(
                selectionId=sid,token=token,venue=pool,
                status=originalSelection.get("status","BOUGHT"),
                reason=originalSelection.get("reason"),
                originalQty=original,
                mainSpentWei=p.get("spentWei"),
                boughtAt=p.get("boughtAt"),
                latestFullSizeGbp=latest["gbp"] if latestValid else None,
                latestFullSizeMultiple=latest["multiple"] if latestValid else None,
                latestFullSizeAt=latest["at"] if latestValid else None,
                latestFullSizeBlock=latest.get("block") if latestValid else None,
                latestFullSizeRoute=latest.get("venue_id") if latestValid else None,
                latestFullSizeBasis=latest.get("basis") if latestValid else None,
                quoteStatus=latest["status"] if latest else "NO_QUOTE",
                quoteError=latest.get("reason") if latest and not latestValid else None,
                quoteAgeSeconds=qage,quoteStale=qStale,
                lastSuccessfulFullSizeGbp=success.get("gbp") if success else None,
                lastSuccessfulQuoteAt=success.get("at") if success else None,
                peakFullSizeGbp=p.get("bestGbp"),
                remainingMainQty=remain,isOpen=remain>0,
                remainingLiquidationValueGbp=None,
                exitSlices=stageRows,probe=probe,
                mainBuyTx=next((tx for tx in byId.get(sid,[])
                     if tx["kind"]=="BUY"),None),
                sacrificeTx=next((tx for tx in byId.get(sid,[])
                     if tx["kind"]=="PROBE_BUY"),None),
                manualFills=[x for x in manualFills if x["selectionId"]==sid],
                lastError=p.get("error"),nextDue=p.get("nextDue"),
                lastCheck=p.get("lastCheck")
            ))
        view["holdings"].sort(key=lambda p:(not p["isOpen"],-(p["boughtAt"] or 0)))
        view["counts"]={
            "positions":len(view["holdings"]),
            "open":sum(p["isOpen"] for p in view["holdings"]),
            "unsettledTx":sum(tx["state"] in ("SIGNED","SENT","UNCERTAIN","STUCK")
                              for tx in transactionRows),
            "confirmedSales":len(saleWithFinance)
        }
        # 1p sacrifices and 5p mains are counted from durable intents, not
        # inferred from a selection PASS or a theoretical quote.
        stake=asDecimal(view["policy"].get("main",{}).get("stakeGbp"))
        sacrificeStake=asDecimal(view["policy"].get("sacrifice",{}).get("stakeGbp"))
        countedProbe={tx["selectionId"] for tx in transactionRows if tx["kind"]=="PROBE_BUY"}
        countedMain={tx["selectionId"] for tx in transactionRows if tx["kind"]=="BUY"}
        view["counts"]["sacrificesCommitted"]=len(countedProbe)
        view["counts"]["mainCommitted"]=len(countedMain)
        view["counts"]["nominalCommittedGbp"]=(
            str((stake or Decimal(0))*len(countedMain)
                +(sacrificeStake or Decimal(0))*len(countedProbe))
            if stake is not None and sacrificeStake is not None else None)

        execById={row["selectionId"]:mainSelection(row) for row in selections}
        revokeRows=safeRows(se,"SELECT eventKey,reason,notedAt FROM revocations "
                       "ORDER BY seq DESC LIMIT ?",(max(limit*4,200),),
                       view,"selector revocations")
        revById={}
        for row in revokeRows:
            revById.setdefault(row["eventKey"],row)
        decRows=safeRows(se,"SELECT d.eventKey,d.tokenKey,d.venueKey,d.status,"
                     "d.decisionAt,d.data,l.detectedAt,l.source "
                     "FROM decisions d LEFT JOIN launches l ON l.eventKey=d.eventKey "
                     "ORDER BY d.seq DESC LIMIT ?",(limit,),view,
                     "selector decisions")
        seen=set()
        for d in decRows:
            sid=d["eventKey"]
            payload=unpack(d.get("data"))
            token=displayToken(d,payload)
            execution=execById.get(sid)
            rev=revById.get(sid)
            largest=asInt(payload.get("largestBuyTransactionBaseRaw"))
            total=asInt(payload.get("totalBuyBaseRaw"))
            share=(str((Decimal(largest)*Decimal(100)/Decimal(total))
                       .quantize(Decimal("0.001"))) if total and largest is not None else None)
            status=d["status"]
            if rev:
                why="REVOKED: "+str(rev["reason"])
            elif status!="PASS":
                why=str(payload.get("reason") or status)
            elif execution is None:
                why="PASS_NOT_YET_CONSUMED_BY_EXECUTOR"
            elif execution["reason"]:
                why=str(execution["status"])+": "+str(execution["reason"])
            elif execution["status"]=="BOUGHT":
                why="BOUGHT"
            else:
                why=str(execution["status"])
            view["opportunities"].append(dict(
                selectionId=sid,token=token,venue=fullVenue(payload,d),
                source=d.get("source") or payload.get("venue",{}).get("source"),
                detectedAt=d.get("detectedAt"),status=status,
                executionStatus=execution.get("status") if execution else None,
                executionReason=execution.get("reason") if execution else None,
                whyNotBought=why,revokeReason=rev["reason"] if rev else None,
                decisionAt=d.get("decisionAt"),firstTradeAt=payload.get("firstTradeAt"),
                windowEndAt=payload.get("windowEndAtExclusive"),
                decisionLagSeconds=payload.get("decisionLagSeconds"),
                coverageComplete=payload.get("coverageComplete"),
                buyTransactionCount=payload.get("buyTransactionCount"),
                observedSwapCount=payload.get("observedSwapCount"),
                largestSharePct=share,
                largestBuyTx=payload.get("largestBuyTransactionHash"),
                totalBuyBaseRaw=payload.get("totalBuyBaseRaw"),
                largestBuyBaseRaw=payload.get("largestBuyTransactionBaseRaw"),
                selectorReason=payload.get("reason"),state=status))
            seen.add(sid)
        pending=safeRows(se,"SELECT eventKey,tokenKey,venueKey,source,detectedAt,data "
                         "FROM launches WHERE state='PENDING' ORDER BY detectedAt DESC LIMIT ?",
                         (limit,),view,"pending selector launches")
        progress=safeRows(se,"SELECT eventKey,nextBlock,lastError FROM concentration_progress "
                          "WHERE lastError IS NOT NULL ORDER BY nextBlock DESC LIMIT ?",
                          (limit,),view,"concentration progress errors")
        errorsById={row["eventKey"]:row["lastError"] for row in progress}
        for row in pending:
            if row["eventKey"] in seen:continue
            payload=unpack(row.get("data"))
            view["opportunities"].append(dict(
                selectionId=row["eventKey"],
                token=displayToken(row,payload),
                venue=fullVenue(payload,row),source=row.get("source"),
                detectedAt=row.get("detectedAt"),status="PENDING",
                executionStatus=None,executionReason=None,
                whyNotBought="WAITING_FOR_COMPLETE_FIRST_TRADE_WINDOW",
                revokeReason=revById.get(row["eventKey"],{}).get("reason"),
                decisionAt=None,firstTradeAt=None,windowEndAt=None,
                decisionLagSeconds=None,coverageComplete=False,
                buyTransactionCount=None,observedSwapCount=None,
                largestSharePct=None,largestBuyTx=None,
                totalBuyBaseRaw=None,largestBuyBaseRaw=None,
                selectorReason=errorsById.get(row["eventKey"]),state="PENDING"))
        view["opportunities"].sort(key=lambda x:x["decisionAt"] or x["detectedAt"] or 0,
                                   reverse=True)

        selectorReady=seMeta.get("selectorReady")
        heartbeat=asDecimal(seMeta.get("selectorHeartbeat"))
        heartAge=(max(0,float(Decimal(str(now))-heartbeat)) if heartbeat is not None else None)
        exeIntake=exMeta.get("intakeState")
        retired=exeIntake=="CLOSED" and exMeta.get("workerState")=="RETIRED"
        view["health"]=dict(
            detectorStatus=deMeta.get("status") or legacy.get("detector",{}).get("status"),
            detectorHead=deMeta.get("lastConnectedHead") or legacy.get("detector",{}).get("head"),
            detectorGaps=legacy.get("detector",{}).get("gaps"),
            detectorDecodeErrors=legacy.get("detector",{}).get("decode"),
            detectorCursor=asInt(seMeta.get("detectorCursor")),
            detectorBacklog=legacy.get("selector",{}).get("backlog"),
            selectorStatus=("RETIRED" if retired else
                            "LIVE_HEARTBEAT" if selectorReady=="1" and
                            heartAge is not None and heartAge<=15 else
                            "STALE" if selectorReady=="1" else "NOT_READY_OR_NOT_STARTED"),
            selectorReady=selectorReady=="1",
            selectorChainId=seMeta.get("selectorVerifiedChainId"),
            selectorHeartbeatAgeSeconds=heartAge,
            selectorDecisionCounts=legacy.get("selector",{}).get("decisions",{}),
            selectorLaunchCounts=legacy.get("selector",{}).get("launches",{}),
            revoked=legacy.get("selector",{}).get("revoked"),
            intakeState=exeIntake,
            workerState=exMeta.get("workerState"),
            watcherState=exMeta.get("venueWatchState"),
            watcherError=exMeta.get("venueWatchError"),
            executorProcessLiveness="NOT_INSTRUMENTED_BY_LEDGER",
            sourceRetired=retired
        )

        # Alerts are derived from evidence; they do not imply a live process.
        if se is not None and selectorReady=="1" and heartAge is not None and heartAge>15 and not retired:
            view["alerts"].append(dict(level="CRITICAL",source="SELECTOR",
                                      message="Selector heartbeat stale; verify process before arming",
                                      at=None,token=None,txHash=None))
        for row in view["opportunities"]:
            if row["status"]=="UNKNOWN" or row["revokeReason"]:
                view["alerts"].append(dict(level="WARNING",source="SELECTOR",
                    message=row["whyNotBought"],at=row["decisionAt"],
                    token=row["token"],txHash=None))
            if asDecimal(row["decisionLagSeconds"]) is not None and asDecimal(row["decisionLagSeconds"])>15:
                view["alerts"].append(dict(level="WARNING",source="LATENCY",
                    message="Concentration decision past actionable 15s lag: "+str(row["decisionLagSeconds"])+"s",
                    at=row["decisionAt"],token=row["token"],txHash=None))
        for row in transactionRows:
            if row["state"] in ("UNCERTAIN","STUCK","REJECTED","REVERTED"):
                view["alerts"].append(dict(level="CRITICAL" if row["state"] in ("UNCERTAIN","STUCK") else "WARNING",
                    source="TX "+row["kind"],message=row["reason"] or row["state"],
                    at=row["createdAt"],token=row.get("token"),txHash=row["txHash"]))
        for p in view["holdings"]:
            if p["lastError"] or p["quoteError"]:
                view["alerts"].append(dict(level="WARNING",source="POSITION",
                    message=p["lastError"] or p["quoteError"],
                    at=p["lastCheck"],token=p["token"],txHash=None))
        for request in view["manualRequests"]:
            if request["status"] in ("BLOCKED","UNVERIFIED","FAILED"):
                view["alerts"].append(dict(level="WARNING",source="MANUAL EXIT",
                    message=request["reason"] or request["status"],
                    at=request["createdAt"],token=None,txHash=request["txHash"]))
        for reason in view["warnings"]:
            view["alerts"].append(dict(level="DATA",source="MONITOR",message=reason,
                                        at=None,token=None,txHash=None))
        view["alerts"].sort(key=lambda x:x["at"] or 0,reverse=True)
        return view
    finally:
        for db in conns.values():
            if db is not None:
                db.close()
