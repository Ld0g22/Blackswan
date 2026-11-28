"""Absolute entry timing for configured concentration campaigns only.

The selector publishes chain-time evidence; execution owns expiry. A selector
swap, legacy manifest, sale or approval never inherits this policy implicitly.
"""
import json
import time

def canonicalTiming(store):
    cfg=getattr(store,'configuredCampaign',None)
    return bool(cfg and cfg.get('selector',{}).get('name')=='concentration')

def windowEnd(store,selection):
    if not canonicalTiming(store):return float(selection['decisionAt'])
    item=json.loads(selection['event'])
    first=item.get('firstTradeAt')
    end=item.get('windowEndAtExclusive')
    window=store.configuredCampaign['selector']['windowSeconds']
    if (item.get('policyName')!='concentration' or item.get('windowAnchor')!='firstTrade'
            or item.get('coverageComplete') is not True
            or type(first) is not int or first<0 or type(end) is not int
            or item.get('windowSeconds')!=window or end!=first+window
            or end>time.time()):
        raise ValueError('INVALID_CANONICAL_WINDOW_EVIDENCE')
    return float(end)

def targetKey(selectionId):
    return 'canonicalSurvivalAt:'+selectionId

def entryKwargs(store,anchor,budget):
    return {'deadlineAt':anchor+budget} if canonicalTiming(store) else {}


from dataclasses import dataclass
import asyncio


@dataclass(frozen=True)
class PreparedEntry:
    """In-memory proof for ONE immediate attempt; never an authorization on disk."""
    selectionId: str
    token: str
    amountWei: int
    quote: dict
    targetBlock: int
    anchor: float
    verifiedAt: float


def retryTime(now,deadline,reason):
    # The next attempt must fit inside the original window. Never sleep for
    # the entire budget, or spin against a throttled/auth-failing provider.
    delay=1.0 if any(s in reason for s in ('429','RPC_HTTP_5','RPC_HTTP_401','RPC_HTTP_403')) else .05
    return now+delay if now+delay<deadline else None


def recordFailure(store,selectionId,stage,reason):
    # Preserve the initiating failure when a subsequent deadline expires.
    key='entryFailure:'+stage+':'+selectionId
    if not store.get(key,''):
        store.set(key,str(reason)[:160])
    store.set('entryLastFailure:'+stage+':'+selectionId,str(reason)[:160])


def remainingBlocks(receiptBlock,wait,head,headAt,now):
    if (head is None or headAt is None or receiptBlock is None or
            head<receiptBlock or not 0<=now-headAt<=2.0):
        return None
    return max(0,receiptBlock+wait-head)


async def parallelReads(*coroutines):
    # All are read-only. Drain each wave even if one fails, avoiding orphan
    # RPC requests accumulating across attempts.
    results=await asyncio.gather(*coroutines,return_exceptions=True)
    for value in results:
        if isinstance(value,BaseException):raise value
    return results
