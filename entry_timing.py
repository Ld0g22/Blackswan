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
