"""Full-screen Rich presentation for the read-only BlackSwan operator monitor.

Preserves the buying-block-era Rich rounded panels / cyan scorecard language,
but keeps identifiers unshortened and separates historical quotes from fills.
"""
from __future__ import annotations

from datetime import datetime,timezone
from decimal import Decimal
from itertools import chain
from pathlib import Path

from rich import box
from rich.console import Group
from rich.segment import Segment
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PAGES=("overview","decisions","positions","ledger","alerts")


class ScrollableContent:
    """Retain every field and scroll all rendered lines; never ellipsize lower
    position/exits just because a single position exceeds screen height."""
    def __init__(self,content,offset=0):
        self.content=content
        self.offset=max(0,int(offset))

    def __rich_console__(self,console,options):
        # Layout gives us its assigned viewport height. Remove that limit
        # while rendering the inner panels, then apply explicit line scrolling.
        allLines=console.render_lines(
            self.content,options.update(height=None),pad=False)
        height=options.height or options.max_height or 30
        start=min(self.offset,max(0,len(allLines)-height))
        for lineParts in allLines[start:start+height]:
            yield from lineParts
            yield Segment.line()
        for _ in range(max(0,height-(len(allLines)-start))):
            yield Segment.line()

DASH="—"


def show(value,default=DASH):
    if value is None or value=="":
        return default
    if isinstance(value,bool):
        return "YES" if value else "NO"
    return str(value)


def timeUtc(value):
    if value is None:return DASH
    try:
        return datetime.fromtimestamp(float(value),timezone.utc).strftime("%H:%M:%S UTC")
    except (TypeError,ValueError,OverflowError,OSError):
        return DASH


def age(value,now):
    if value is None:return DASH
    try:
        n=max(0,int(now-float(value)))
    except (ValueError,TypeError,OverflowError):
        return DASH
    return f"{n}s" if n<60 else f"{n//60}m {n%60:02d}s" if n<3600 else f"{n//3600}h {n%3600//60:02d}m"


def safeText(value,style=""):
    return Text(show(value),style=style,overflow="fold",no_wrap=False)


def line(*parts):
    result=Text()
    for item in parts:
        if isinstance(item,Text):result.append_text(item)
        else:result.append(str(item))
    return result


def idLine(label,value):
    return line(Text(label+"  ",style="dim cyan"),safeText(value,"bold white"),"\n")


def words(label,value,style="white"):
    return line(Text(label+": ",style="cyan"),safeText(value,style),"\n")


def emphasis(state):
    txt=show(state)
    if txt in ("PASS","BOUGHT","CONFIRMED","SOLD","LIVE_HEARTBEAT","READY","COMPLETE"):
        return "bold green"
    if (txt in ("FAIL","UNKNOWN","REVOKED","STUCK","REJECTED","UNCERTAIN",
                "REVERTED","CRITICAL","BLOCKED","UNVERIFIED","FAILED")
            or "ERROR" in txt):
        return "bold red"
    if txt in ("PENDING","QUEUED","LAMB_QUEUED","PROBE_PENDING","TRIGGERED",
               "WAITING","STALE","NOT_READY_OR_NOT_STARTED"):
        return "bold yellow"
    return "bold white"


def panel(name,content,border="bright_blue"):
    return Panel(content,title=name,title_align="left",
                 border_style=border,box=box.ROUNDED,padding=(0,1))


def viewBanner(view,screen):
    timestamp=datetime.fromtimestamp(view["now"],timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    policy=view.get("policy") or {}
    wallet=policy.get("walletPublicAddress") or "NOT CONFIGURED"
    selector=(policy.get("selector",{}).get("name") or
              view.get("inferredSelectorPolicy") or
              view.get("meta",{}).get("selector",{}).get("selectorName") or "UNKNOWN")
    chainVerified=(view.get("health") or {}).get("selectorChainId")
    chainConfig=policy.get("chainId")
    chainLabel=("CHAIN "+str(chainVerified)+" (SELECTOR RPC)" if chainVerified else
                "CHAIN "+str(chainConfig)+" (CONFIG)" if chainConfig else
                "CHAIN UNVERIFIED")
    top=line(Text("BLACK SWAN  •  CONCENTRATION CONTROL ROOM",style="bold cyan"),
        "   ",Text("READ ONLY",style="bold green"),"   ",
        Text("SCREEN "+screen.upper(),style="bold white"),"  ",timestamp,"\n")
    top+=idLine("RUN",view.get("directory"))
    top+=line(Text(chainLabel,style="cyan"),"  |  ",
              Text("SELECTOR "+show(selector),style="bold magenta"),"  |  ",
              Text("MODE "+view.get("mode","UNKNOWN"),style="bold yellow"),
              "  |  ",Text("WALLET ",style="dim"),safeText(wallet),"\n")
    return panel("OPERATOR CONTROL • independent SQLite observer",top,"cyan")


def scorecard(view,width=200):
    policy=view.get("policy") or {}
    limits=policy.get("limits") or {}
    main=policy.get("main") or {}
    summary=view.get("base",{}).get("summary") or {}
    counts=view["counts"]
    decisionCounts=view.get("health",{}).get("selectorDecisionCounts") or {}
    finance=view["finance"]
    cap=main.get("maxPurchases") or summary.get("cap")
    used=counts.get("nominalCommittedGbp")
    money=("£"+show(used) if used is not None else DASH)
    capText="£"+show(limits.get("maxNominalSpendGbp")) if limits.get("maxNominalSpendGbp") else DASH
    realised=("£"+show(finance.get("actualRealisedGbp")) if finance.get("actualRealisedGbp") is not None else "UNMEASURED")
    pnl=("£"+show(finance.get("cashPnlGbp")) if finance.get("cashPnlGbp") is not None else "UNMEASURED")
    cells=[
        ("SELECTOR",f"P {decisionCounts.get('PASS',0)}  F {decisionCounts.get('FAIL',0)}  ? {decisionCounts.get('UNKNOWN',0)}"),
        ("PURCHASES",f"{counts.get('positions',0)} / {show(cap)}"),
        ("OPEN",str(counts.get("open",0))),
        ("SACRIFICES",f"{counts.get('sacrificesCommitted',0)} / {show(limits.get('maxSacrifices'))}"),
        ("NOMINAL",money+" / "+capText),
        ("REALISED GBP",realised),
        ("CASH P/L",pnl),
        ("UNSETTLED TX",str(counts.get("unsettledTx",0))),
    ]
    columns=8 if width>=200 else 4 if width>=110 else 2
    t=Table.grid(expand=True,padding=(0,1))
    for _ in range(columns):t.add_column(ratio=1,justify="center",overflow="fold")
    for start in range(0,len(cells),columns):
        group=cells[start:start+columns]
        t.add_row(*[Text(label,style="cyan") for label,_ in group])
        t.add_row(*[Text(value,style="bold yellow" if value=="UNMEASURED" else "bold white")
                    for _,value in group])
    return panel("CAMPAIGN SCORECARD • confirmed vs estimated kept separate",t,"bright_cyan")


def healthCard(view):
    h=view.get("health") or {}
    policy=view.get("policy") or {}
    a=Text()
    a+=words("Detector",h.get("detectorStatus"),emphasis(h.get("detectorStatus")))
    a+=words("Head / Gaps / Decode",f"{show(h.get('detectorHead'))} / {show(h.get('detectorGaps'))} / {show(h.get('detectorDecodeErrors'))}")
    a+=words("Selector heartbeat",h.get("selectorStatus"),emphasis(h.get("selectorStatus")))
    a+=words("Chain / heartbeat age",f"{show(h.get('selectorChainId'))} / {show(h.get('selectorHeartbeatAgeSeconds'))} sec")
    a+=words("Backlog / revocations",f"{show(h.get('detectorBacklog'))} / {show(h.get('revoked'))}")
    a+=words("Intake / worker",f"{show(h.get('intakeState'))} / {show(h.get('workerState'))}")
    a+=words("Held-venue watcher",f"{show(h.get('watcherState'))} ({show(h.get('watcherError'))})")
    a+=words("Local PIDs (detector / selector / executor)",
             f"{show(h.get('detectorPid'))} / {show(h.get('selectorPid'))} / {show(h.get('executorPid'))}")
    a+=words("Executor process (local)",h.get("executorProcessLiveness") or "NOT INSTRUMENTED")
    a+=Text("A matching PID is process evidence, NOT a wallet/RPC heartbeat.",style="dim italic")
    return panel("PIPELINE HEALTH • ledger status ≠ process heartbeat",a,"bright_green")


def policyCard(view):
    p=view.get("policy") or {}
    m=p.get("main") or {}
    s=p.get("sacrifice") or {}
    limits=p.get("limits") or {}
    execution=p.get("execution") or {}
    selector=p.get("selector") or {}
    a=Text()
    a+=words("Main stake / limit",f"£{show(m.get('stakeGbp'))} / {show(m.get('maxPurchases'))}")
    a+=words("Sacrifice",f"{show(s.get('enabled'))}  £{show(s.get('stakeGbp'))}")
    wait=s.get("wait") or {}
    a+=words("Survival wait",f"{show(wait.get('value'))} {show(wait.get('unit'))}")
    a+=words("Sacrifice / main deadlines",f"{show(execution.get('sacrificeDeadlineSeconds'))}s / {show(execution.get('entryDeadlineSeconds'))}s")
    if selector.get("name")=="concentration":
        a+=words("Concentration",f"{show(selector.get('windowSeconds'))}s first trade | ≤{selector.get('maxLargestBuyTransactionShareBps',0)/100:g}% largest tx")
    elif selector.get("name")=="buyingBlocks":
        a+=words("Buying-block policy",f"{show(selector.get('decisionSeconds'))}s | maximum buying blocks {show(selector.get('maxBuyingBlocks'))}")
    else:
        a+=words("Selector policy","NO VALIDATED SELECTOR CONFIG")
    a+=words("Exits"," | ".join(str(e.get("percent"))+"% @ "+str(e.get("multiple"))+"×"
                               for e in p.get("exits",[])) or DASH)
    a+=words("Nominal cap",f"£{show(limits.get('maxNominalSpendGbp'))} (GAS EXCLUDED)")
    return panel("FROZEN CAMPAIGN POLICY • no active controls",a,"cyan")


def decisionCard(row,now):
    a=Text()
    a+=line(Text("SELECTION  ",style="dim cyan"),safeText(row.get("selectionId")),"\n")
    a+=idLine("TOKEN",row.get("token"))
    a+=idLine("POOL",row.get("venue"))
    a+=words("SOURCE / DETECTED",f"{show(row.get('source'))} | {timeUtc(row.get('detectedAt'))}")
    status=row.get("status")
    a+=line(Text("SELECTOR  ",style="cyan"),safeText(status,emphasis(status)),
            "  |  ",Text("EXECUTION  ",style="cyan"),
            safeText(row.get("executionStatus") or "NOT CONSUMED",emphasis(row.get("executionStatus"))),"\n")
    a+=words("WHY NOT BOUGHT / OUTCOME",row.get("whyNotBought"),emphasis(status) if status=="FAIL" else "white")
    a+=words("MAX TRANSACTION SHARE",str(row.get("largestSharePct"))+"%" if row.get("largestSharePct") is not None else "NO DATA")
    a+=words("BUYS / SWAPS / COMPLETE",f"{show(row.get('buyTransactionCount'))} / {show(row.get('observedSwapCount'))} / {show(row.get('coverageComplete'))}")
    a+=words("FIRST TRADE / WINDOW END",f"{timeUtc(row.get('firstTradeAt'))} / {timeUtc(row.get('windowEndAt'))}")
    a+=words("DECISION / LAG",f"{timeUtc(row.get('decisionAt'))} / {show(row.get('decisionLagSeconds'))} s")
    if row.get("totalBuyBaseRaw") is not None:
        a+=words("RAW BUY / LARGEST",show(row.get("totalBuyBaseRaw"))+" / "+show(row.get("largestBuyBaseRaw")))
    if row.get("largestBuyTx"):
        a+=idLine("BIGGEST BUY TX",row["largestBuyTx"])
    if row.get("revokeReason"):
        a+=words("REORG REVOCATION",row["revokeReason"],"bold red")
    if row.get("selectorReason") and row.get("selectorReason")!=row.get("whyNotBought"):
        a+=words("SELECTOR REASON",row["selectorReason"])
    return a


def decisionList(view,maxRows=None,offset=0):
    choices=view.get("opportunities",[])
    count=len(choices)
    n=count if maxRows is None else maxRows
    shown=choices[offset:offset+n]
    cards=[]
    for row in shown:
        cards.append(panel("DECISION "+show(row.get("status")),decisionCard(row,view["now"]),
                           "red" if row.get("status") in ("FAIL","UNKNOWN") else "green" if row.get("status")=="PASS" else "yellow"))
    if not cards:
        cards=[Text("No concentration decisions yet. Detector/selector can be waiting for a first-trade window.",style="yellow")]
    return panel(f"CONCENTRATION DECISIONS / OPPORTUNITIES • {offset+1 if shown else 0}–{offset+len(shown)} of {count}",
                 Group(*cards),"magenta")


def probeCard(p,view):
    probe=p.get("probe") or {}
    tx=p.get("sacrificeTx") or {}
    cfg=(view.get("policy") or {}).get("sacrifice") or {}
    wait=cfg.get("wait") or {}
    a=Text()
    a+=words("Sacrifice result",probe.get("result") or ("NOT SUBMITTED" if not tx else tx.get("state")),emphasis(probe.get("result")))
    a+=idLine("SACRIFICE TX",probe.get("probeTxHash") or tx.get("txHash"))
    a+=words("Chain receipt / quantity",f"{timeUtc(probe.get('confirmedAt'))} / {show(probe.get('probeQty'))} raw tokens")
    a+=words("Passed / why",f"{timeUtc(probe.get('passedAt'))} / {show(probe.get('reason'))}")
    currentHead=view.get("health",{}).get("detectorHead")
    receipt=tx.get("receiptBlock")
    if wait.get("unit")=="blocks":
        try:
            current=int(str(currentHead),0) if str(currentHead).startswith("0x") else int(str(currentHead))
            block=int(receipt)
            remaining=max(0,block+int(wait["value"])-current)
            a+=words("Survival gate",f"{remaining} blocks remaining (detector head)")
        except (ValueError,TypeError,KeyError):
            a+=words("Survival gate",f"{show(wait.get('value'))} blocks; receipt/head unavailable")
    else:
        a+=words("Survival gate",f"{show(wait.get('value'))} {show(wait.get('unit'))}")
    return a


def stageCard(stage):
    a=Text()
    state=stage.get("state")
    a+=line(Text(show(stage.get("multiple"))+"×  ",style="bold cyan"),
            Text(show(stage.get("percent"))+"%  ",style="bold white"),
            safeText(state,emphasis(state)),"\n")
    a+=words("Target / confirmed sold",f"{show(stage.get('targetQty'))} / {show(stage.get('soldQty'))} raw tokens")
    a+=words("Trigger / tx state",f"{show(stage.get('triggered'))} / {show(stage.get('txState'))}")
    a+=idLine("SELL TX",stage.get("txHash"))
    a+=words("Confirmed / received",f"{timeUtc(stage.get('confirmedAt'))} / "
             + (show(stage.get("receivedWei"))+" WETH-wei" if stage.get("receivedWei") is not None else "NATIVE/UNMEASURED"))
    a+=words("Realised GBP",("£"+show(stage.get("realisedGbp"))) if stage.get("realisedGbp") is not None else "UNMEASURED")
    if stage.get("error"):
        a+=words("Error",stage.get("error"),"bold red")
    return a


def sacrificeList(view,maxRows=None,offset=0):
    """Show every sacrifice even when NO MAIN BUY / position exists yet."""
    records=view.get("sacrificesDetailed",[])
    shown=records[offset:offset+maxRows if maxRows is not None else None]
    cards=[]
    for p in shown:
        a=Text()
        a+=idLine("TOKEN",p.get("token"))
        a+=idLine("VENUE",p.get("venue"))
        a+=idLine("SELECTION",p.get("selectionId"))
        a+=words("SELECTION / SACRIFICE",show(p.get("status"))+" / "+show(p.get("result")),
                 emphasis(p.get("result")))
        a+=idLine("SACRIFICE TX",p.get("txHash"))
        a+=words("TRANSACTION STATE / RECEIPT",show(p.get("txState"))+
                 " / "+show(p.get("receiptRecorded")))
        a+=words("CONFIRMED / RECEIPT BLOCK",timeUtc(p.get("confirmedAt"))+
                 " / "+show(p.get("receiptBlock")))
        a+=words("SACRIFICE QUANTITY",show(p.get("probeQty"))+" raw tokens (never main holdings)")
        a+=words("SURVIVED AT",timeUtc(p.get("passedAt")))
        if p.get("waitUnit")=="blocks":
            wait=(str(p["blocksRemaining"])+" blocks remain" if
                  p.get("blocksRemaining") is not None else "chain head/receipt block not available")
            a+=words("SURVIVAL WAIT",show(p.get("waitValue"))+" blocks; "+wait)
        else:
            a+=words("SURVIVAL WAIT",show(p.get("waitValue"))+" "+show(p.get("waitUnit")))
        if p.get("reason"):
            a+=words("WHY / ERROR",p["reason"],"bold red")
        cards.append(panel("SACRIFICE • "+show(p.get("result")),a,
                           "red" if p.get("status") in ("PROBE_FAILED","PROBE_UNVERIFIED") else "cyan"))
    if not cards:
        cards=[Text("No sacrifices queued, submitted or confirmed in this run.",style="dim yellow")]
    return panel(f"SACRIFICE / SURVIVAL GATE • {len(shown)} shown, {len(records)} recorded",
                 Group(*cards),"cyan")


def holdingCard(p,view):
    a=Text()
    a+=idLine("TOKEN",p.get("token"))
    a+=idLine("VENUE",p.get("venue"))
    a+=idLine("SELECTION",p.get("selectionId"))
    a+=words("Lifecycle / original",f"{show(p.get('status'))} | {show(p.get('originalQty'))} receipt-verified raw main tokens",emphasis(p.get("status")))
    a+=words("Remaining MAIN (probe excluded)",str(p.get("remainingMainQty"))+" raw tokens")
    a+=words("Recorded main transaction spend",show(p.get("mainSpentWei"))+" wei (nominal input, gas separate)")
    a+=words("Bought / last checked",f"{timeUtc(p.get('boughtAt'))} / {timeUtc(p.get('lastCheck'))}")
    a+=idLine("MAIN BUY TX",(p.get("mainBuyTx") or {}).get("txHash"))
    a+=words("MAIN BUY TX STATE",(p.get("mainBuyTx") or {}).get("state"))
    a+=probeCard(p,view)
    if p.get("quoteStatus")=="ONCHAIN_QUOTE_ONLY" and p.get("latestFullSizeGbp") is not None:
        a+=words("LATEST EXECUTABLE FULL-ENTRY-SIZE QUOTE",
                 "£"+str(p["latestFullSizeGbp"])+" / "+show(p.get("latestFullSizeMultiple"))+"×")
    else:
        a+=words("LATEST FULL-SIZE QUOTE","UNAVAILABLE", "bold yellow")
    a+=words("LAST SUCCESS / AT",("£"+show(p.get("lastSuccessfulFullSizeGbp"))
                if p.get("lastSuccessfulFullSizeGbp") is not None else "NONE")
             +" / "+timeUtc(p.get("lastSuccessfulQuoteAt")))
    a+=words("HISTORICAL PEAK (FULL ENTRY)",("£"+show(p.get("peakFullSizeGbp"))
               if p.get("peakFullSizeGbp") is not None else "NO QUOTE"))
    a+=words("Last quote age / state",f"{show(p.get('quoteAgeSeconds'))} seconds / "
                + ("STALE / FAILURE" if p.get("quoteStale") else "FRESH"))
    a+=words("Quote block / executable route",show(p.get("latestFullSizeBlock"))+
               " / "+show(p.get("latestFullSizeRoute")))
    a+=words("Quote basis",p.get("latestFullSizeBasis"))
    a+=words("Current *remaining* liquidation quote","NOT MEASURED by this monitor")
    if p.get("quoteError"):
        a+=words("Quote failure",p["quoteError"],"bold red")
    if p.get("lastError"):
        a+=words("Execution error",p["lastError"],"bold red")
    for stage in p.get("exitSlices",[]):
        a+=Text("\n")
        a+=stageCard(stage)
    if p.get("manualFills"):
        a+=words("Manual confirmed sales",len(p["manualFills"]))
        for entry in p["manualFills"]:
            a+=idLine("MANUAL SELL TX",entry.get("txHash"))
            a+=words("Manual debited",entry.get("tokenDebited"))
    a+=Text("\nQuotes = executable *full original entry size* only. Historical peaks are NOT cash or current remaining-position value.",
            style="italic dim")
    return a


def positionList(view,maxRows=None,offset=0):
    choices=view.get("holdings",[])
    shown=choices[offset:offset+maxRows if maxRows is not None else None]
    cards=[panel("POSITION • "+("OPEN" if p["isOpen"] else "CLOSED"),
                 holdingCard(p,view),"green" if p["isOpen"] else "blue")
           for p in shown]
    if not cards:cards=[Text("No confirmed receipt-proven main purchases.",style="yellow")]
    return panel(f"POSITIONS / PEAK TRACKER / EXITS • {len(shown)} shown, {len(choices)} total",
                 Group(*cards),"bright_blue")


def financeCard(view):
    f=view.get("finance",{})
    p=view.get("policy") or {}
    a=Text()
    a+=words("Actual qualified GBP sale proceeds",
             "£"+str(f["actualRealisedGbp"]) if f.get("actualRealisedGbp") is not None else "UNMEASURED / INCOMPLETE",
             "bold green" if f.get("actualRealisedGbp") is not None else "bold yellow")
    a+=words("Qualified partial GBP",("£"+str(f["qualifiedRealisedPartialGbp"])
                if f.get("qualifiedRealisedPartialGbp") is not None else "UNMEASURED"))
    a+=words("Verified WETH credited",str(f.get("verifiedReceivedWethWei",0))+" wei")
    a+=words("Native/unknown sale receipts",f.get("missingWethSaleCount"))
    a+=words("Expenses (GBP)",("£"+str(f["expenseGbp"])) if f.get("expenseGbp") is not None else "UNMEASURED")
    a+=words("Cash P/L (GBP)",("£"+str(f["cashPnlGbp"])) if f.get("cashPnlGbp") is not None else "UNMEASURED / INCOMPLETE")
    a+=words("Complete reconciliation",f.get("cashComplete"))
    a+=words("Nominal cap (excluding gas)",
             "£"+str((p.get("limits") or {}).get("maxNominalSpendGbp") or "UNKNOWN"))
    a+=Text("Only receipt-reconciled cashflows count. UNMEASURED native payouts, missing FX and open holdings are not profit.",
            style="dim italic")
    return panel("CONFIRMED FINANCES • NOT PAPER PEAKS",a,"green")


def receiptList(view,maxRows=None,offset=0):
    txs=view.get("transactions",[])
    shown=txs[offset:offset+maxRows if maxRows is not None else None]
    cards=[]
    for tx in shown:
        state=tx.get("state")
        a=Text()
        a+=words("ACTION / STATE",show(tx.get("kind"))+" / "+show(state),emphasis(state))
        a+=idLine("TX HASH",tx.get("txHash"))
        a+=idLine("TOKEN",tx.get("token"))
        a+=idLine("SELECTION",tx.get("selectionId"))
        a+=words("Created / last broadcast",timeUtc(tx.get("createdAt"))+" / "+timeUtc(tx.get("lastBroadcast")))
        a+=words("Receipt in ledger",tx.get("receiptConfirmed"))
        if tx.get("reason"):a+=words("Reason",tx.get("reason"),"bold red")
        cards.append(panel("JOURNAL "+show(state),a,
                     "red" if state in ("UNCERTAIN","STUCK","REJECTED","REVERTED") else "green"))
    if not cards:cards=[Text("No broadcast/signing entries in this run.",style="yellow")]
    return panel(f"TRANSACTIONS • {len(shown)} shown, {len(txs)} loaded (FULL HASHES)",
                 Group(*cards),"magenta")


def saleList(view,maxRows=None,offset=0):
    items=view.get("sales",[])
    shown=items[offset:offset+maxRows if maxRows is not None else None]
    cards=[]
    for sale in shown:
        a=Text()
        a+=idLine("TX HASH",sale.get("txHash"))
        a+=idLine("SELECTION",sale.get("selectionId"))
        a+=words("KIND / STAGE",show(sale.get("kind"))+" / "+show(sale.get("stage")))
        a+=words("CONFIRMED",timeUtc(sale.get("confirmedAt")),"green")
        a+=words("TOKEN DEBITED",sale.get("tokenDebited"))
        a+=words("WETH RECEIVED",sale.get("wethReceivedWei") if sale.get("wethReceivedWei") is not None else "UNMEASURED / NATIVE")
        a+=words("GBP RECEIVED",("£"+show(sale.get("realisedGbp"))) if sale.get("realisedGbp") is not None else "UNMEASURED")
        event=sale.get("finance") or {}
        a+=words("RECEIPT FINANCE BASIS",event.get("incomeBasis"))
        cards.append(panel("CONFIRMED SELL",a,"bright_green"))
    if not cards:cards=[Text("No confirmed sales; don't infer proceeds from quotes.",style="yellow")]
    return panel(f"REAL SALES • {len(shown)} shown, {len(items)} total",Group(*cards),"green")


def alertList(view,maxRows=None,offset=0):
    alerts=view.get("alerts",[])
    shown=alerts[offset:offset+maxRows if maxRows is not None else None]
    a=[]
    for alert in shown:
        t=Text()
        t+=line(safeText(alert.get("level"),emphasis(alert.get("level"))),"  ",
                safeText(alert.get("source"),"cyan"),"  ",timeUtc(alert.get("at")),"\n")
        t+=words("DETAIL",alert.get("message"))
        if alert.get("token"):t+=idLine("TOKEN",alert["token"])
        if alert.get("txHash"):t+=idLine("TX HASH",alert["txHash"])
        a.append(panel(show(alert.get("source")),t,
                       "red" if alert.get("level")=="CRITICAL" else "yellow"))
    if not a:a=[Text("No issues in the data reviewed. This does not prove a live process is running.",style="green")]
    return panel(f"ALERTS / RECONCILIATION • {len(shown)} shown of {len(alerts)}",
                 Group(*a),"red" if alerts else "green")


def statusFooter(view,screen,offset=0):
    t=Text("1 Overview   2 Decisions   3 Positions   4 Trades/Finance   5 Alerts\n",
           style="bold cyan")
    t+=Text("SCROLL "+str(offset)+" lines   •   n/p Page   j/k 3 lines   g Top   q/Ctrl+C Exit",
            style="bright_white")
    if view.get("warnings"):
        t+=Text("   |   DATA WARNINGS "+str(len(view["warnings"])),style="bold red")
    t+=Text("   |   refresh does not touch execution",style="dim")
    return t


def _pageContent(view,screen,width,height):
    """All data on each detail screen stays accessible by line scrolling."""
    maxRows=view.get("sampleLimit",120)
    if screen=="overview":
        left=Group(healthCard(view),decisionList(view,maxRows=1))
        right=Group(policyCard(view),sacrificeList(view,maxRows=1),
                    positionList(view,maxRows=1),alertList(view,maxRows=1))
        if width>=155:
            t=Table.grid(expand=True,padding=(0,1))
            t.add_column(ratio=1,overflow="fold")
            t.add_column(ratio=1,overflow="fold")
            t.add_row(left,right)
            return t
        return Group(healthCard(view),decisionList(view,maxRows=1),
                     sacrificeList(view,maxRows=1),positionList(view,maxRows=1),
                     alertList(view,maxRows=1))
    if screen=="decisions":
        return Group(healthCard(view),decisionList(view,maxRows=maxRows))
    if screen=="positions":
        return Group(sacrificeList(view,maxRows=maxRows),
                     positionList(view,maxRows=maxRows))
    if screen=="ledger":
        return Group(financeCard(view),sacrificeList(view,maxRows=maxRows),
                     receiptList(view,maxRows=maxRows),
                     saleList(view,maxRows=maxRows),
                     panel("MANUAL EXIT REQUESTS",
                           Group(*[words(show(r.get("operation")),
                                      show(r.get("status"))+" / "+show(r.get("reason"))+
                                      " / "+show(r.get("txHash")))
                                   for r in view.get("manualRequests",[])])
                           if view.get("manualRequests") else Text("No manual requests recorded")))
    return Group(healthCard(view),alertList(view,maxRows=maxRows))



def renderScreen(view,screen="overview",width=180,height=60,offset=0):
    if screen not in PAGES:
        raise ValueError("INVALID_MONITOR_SCREEN")
    width=max(70,int(width));height=max(28,int(height))
    root=Layout(name="root")
    scoreHeight=4 if width>=200 else 6 if width>=110 else 10
    root.split_column(Layout(name="banner",size=6),
                      Layout(name="scorecard",size=scoreHeight),
                      Layout(name="content",ratio=1),
                      Layout(name="footer",size=2))
    root["banner"].update(viewBanner(view,screen))
    root["scorecard"].update(scorecard(view,width=width))
    root["content"].update(ScrollableContent(
        _pageContent(view,screen,width,height),offset))
    root["footer"].update(statusFooter(view,screen,offset))
    return root


def renderReport(view,width=220):
    """Unclipped, noninteractive all-pages export; no hidden 'top N' semantics."""
    return Group(viewBanner(view,"FULL REPORT"),scorecard(view,width=width),
                 healthCard(view),policyCard(view),
                 decisionList(view),sacrificeList(view),positionList(view),
                 financeCard(view),receiptList(view),saleList(view),
                 panel("MANUAL EXIT REQUESTS",
                       Group(*[words(show(r.get("operation")),
                                     show(r.get("status"))+" / "+show(r.get("reason"))+
                                     " / "+show(r.get("txHash")))
                                 for r in view.get("manualRequests",[])])
                       if view.get("manualRequests") else Text("No manual requests")),
                 alertList(view),
                 Text("FULL REPORT  •  READ ONLY  •  Quote/peak values are not cash receipts.",
                      style="bold cyan"))
