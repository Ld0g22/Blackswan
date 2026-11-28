"""Strict, immutable, versioned campaign policy for new BlackSwan campaigns.

Legacy campaign configurations remain handled by campaign_config.py. Never
implicitly migrate an old ledger. Real trading is never armed by parsing.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


def defaultCampaign(wallet: str) -> dict:
    return {
        "schemaVersion": 3,
        "chainId": 4663,
        "walletPublicAddress": wallet,
        "selector": {"name": "buyingBlocks", "decisionSeconds": 20,
                     "maxBuyingBlocks": 10},
        "main": {"stakeGbp": "0.10", "maxPurchases": 5},
        "sacrifice": {"enabled": True, "stakeGbp": "0.01",
                      "wait": {"unit": "blocks", "value": 500},
                      "activityMaxBuyingBlocks": None},
        "exits": [{"multiple": "3", "percent": "80"},
                  {"multiple": "5", "percent": "20"}],
        "execution": {"entryDeadlineSeconds": 3,
                      "sacrificeDeadlineSeconds": 3},
        "limits": {"maxSacrifices": 50, "maxNominalSpendGbp": "10.00"},
    }


def _shape(value, fields, description):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("INVALID_" + description + "_FIELDS")


def _decimal(value, label, minimum, maximum, precision=None):
    if (type(value) is not str or
            not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value)):
        raise ValueError("INVALID_" + label)
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_" + label) from exc
    if not amount.is_finite() or not minimum <= amount <= maximum:
        raise ValueError("INVALID_" + label + "_BOUNDS")
    if precision is not None and amount.as_tuple().exponent < -precision:
        raise ValueError("INVALID_" + label + "_PRECISION")
    return amount


def _integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("INVALID_" + label)
    return value


def _gbp(value, label):
    v = _decimal(value, label, Decimal("0.01"), Decimal("1000"), 2)
    return format(v.quantize(Decimal("0.01")), ".2f")


def validateCampaign(raw: dict) -> dict:
    """Validate without mutating input; normalize to an unambiguous identity."""
    if not isinstance(raw,dict) or type(raw.get("schemaVersion")) is not int or raw["schemaVersion"] not in (3,4):
        raise ValueError("UNSUPPORTED_CAMPAIGN_VERSION")
    fields=("schemaVersion","chainId","walletPublicAddress","selector","main","sacrifice","exits","limits")
    _shape(raw, fields+("execution",) if raw["schemaVersion"]==3 else fields,"CAMPAIGN")
    if type(raw["chainId"]) is not int or raw["chainId"] != 4663:
        raise ValueError("WRONG_CHAIN")
    wallet = raw["walletPublicAddress"]
    if type(wallet) is not str or not re.fullmatch(r'0x[0-9a-fA-F]{40}', wallet) or int(wallet[2:], 16) == 0:
        raise ValueError("INVALID_WALLET_ADDRESS")

    selector = raw["selector"]
    if not isinstance(selector,dict):
        raise ValueError("INVALID_SELECTOR_FIELDS")
    name = selector.get("name")
    if name == "concentration":
        _shape(selector, ("name", "policyVersion", "windowAnchor", "windowSeconds",
                          "maxLargestBuyTransactionShareBps", "firstTradeTimeoutSeconds"),
               "SELECTOR")
        if (type(selector["policyVersion"]) is not int or selector["policyVersion"] != 1
                or selector["windowAnchor"] != "firstTrade"
                or type(selector["windowSeconds"]) is not int or selector["windowSeconds"] != 30
                or type(selector["maxLargestBuyTransactionShareBps"]) is not int
                or selector["maxLargestBuyTransactionShareBps"] != 2500):
            raise ValueError("UNSUPPORTED_CONCENTRATION_RULE")
        _integer(selector["firstTradeTimeoutSeconds"], "FIRST_TRADE_TIMEOUT", 30, 3600)
    elif name in ("buyingBlocks", "integrationOnly"):
        _shape(selector, ("name", "decisionSeconds", "maxBuyingBlocks"), "SELECTOR")
        _integer(selector["decisionSeconds"], "SELECTOR_WINDOW", 1, 300)
        if name == "buyingBlocks":
            if selector["decisionSeconds"] not in (5, 20) or selector["maxBuyingBlocks"] not in (None, 10):
                raise ValueError("UNSUPPORTED_BUYING_BLOCK_SELECTOR_POLICY")
        elif selector["maxBuyingBlocks"] is not None:
            raise ValueError("SELECTOR_POLICY_DOES_NOT_ACCEPT_BUYING_BLOCKS")
    else:
        raise ValueError("UNREGISTERED_SELECTOR")

    main = raw["main"]
    _shape(main, ("stakeGbp", "maxPurchases"), "MAIN")
    mainStake = _gbp(main["stakeGbp"], "MAIN_STAKE")
    count = _integer(main["maxPurchases"], "PURCHASE_LIMIT", 1, 1000)

    sacrifice = raw["sacrifice"]
    _shape(sacrifice, ("enabled", "stakeGbp", "wait", "activityMaxBuyingBlocks"), "SACRIFICE")
    if type(sacrifice["enabled"]) is not bool:
        raise ValueError("INVALID_SACRIFICE_ENABLED")
    sacrificeStake = _gbp(sacrifice["stakeGbp"], "SACRIFICE_STAKE")
    wait = sacrifice["wait"]
    _shape(wait, ("unit", "value"), "SACRIFICE_WAIT")
    if wait["unit"] not in ("blocks", "seconds"):
        raise ValueError("INVALID_SACRIFICE_WAIT_UNIT")
    _integer(wait["value"], "SACRIFICE_WAIT", 1, 200000 if wait["unit"] == "blocks" else 86400)
    cap = sacrifice["activityMaxBuyingBlocks"]
    if cap is not None:
        _integer(cap, "SACRIFICE_ACTIVITY_CAP", 0, 500)
        if wait["unit"] != "blocks" or wait["value"] < 500:
            raise ValueError("ACTIVITY_REQUIRES_500_BLOCK_COVERAGE")
    if not sacrifice["enabled"] and cap is not None:
        raise ValueError("DISABLED_SACRIFICE_CANNOT_HAVE_ACTIVITY_GATE")

    exits = raw["exits"]
    if not isinstance(exits, list) or not 1 <= len(exits) <= 10:
        raise ValueError("INVALID_EXIT_COUNT")
    seen = Decimal(0)
    last = Decimal(0)
    normalizedExits = []
    for entry in exits:
        _shape(entry, ("multiple", "percent"), "EXIT")
        multiple = _decimal(entry["multiple"], "EXIT_MULTIPLE", Decimal("0.0001"), Decimal("1000000"), 4)
        percent = _decimal(entry["percent"], "EXIT_PERCENT", Decimal("0.01"), Decimal("100"), 2)
        if multiple <= last:
            raise ValueError("EXIT_MULTIPLES_MUST_INCREASE")
        last = multiple
        seen += percent
        normalizedExits.append({
            "multiple": format(multiple.normalize(), "f"),
            "percent": format(percent.normalize(), "f"),
        })
    if not Decimal(0) < seen <= Decimal(100):
        raise ValueError("EXIT_PERCENTAGES_EXCEED_100")

    if raw["schemaVersion"]==3:
        execution=raw["execution"]
        _shape(execution,("entryDeadlineSeconds","sacrificeDeadlineSeconds"),"EXECUTION")
        for name in execution:
            _integer(execution[name],name,1,60)

    limits = raw["limits"]
    _shape(limits, ("maxSacrifices", "maxNominalSpendGbp"), "LIMITS")
    # Explicit JSON null disables the individual limit; finite legacy
    # manifests retain the same validation and canonical identity.
    maxSacrifices = (None if limits["maxSacrifices"] is None else
                     _integer(limits["maxSacrifices"], "MAX_SACRIFICES", 0, 10000))
    maxNominal = (None if limits["maxNominalSpendGbp"] is None else
                  _decimal(limits["maxNominalSpendGbp"], "MAX_NOMINAL_SPEND",
                           Decimal("0.01"), Decimal("10000"), 2))
    if maxNominal is not None and Decimal(mainStake) * count > maxNominal:
        raise ValueError("NOMINAL_LIMIT_CANNOT_COVER_MAIN_CAMPAIGN")
    if sacrifice["enabled"] and (
            (maxSacrifices is not None and maxSacrifices < count)
            or (maxNominal is not None and
                Decimal(mainStake) * count + Decimal(sacrificeStake) > maxNominal)):
        raise ValueError("SACRIFICE_CAP_INSUFFICIENT")
    result = deepcopy(raw)
    result["walletPublicAddress"] = wallet.lower()
    result["main"]["stakeGbp"] = mainStake
    result["sacrifice"]["stakeGbp"] = sacrificeStake
    result["limits"]["maxNominalSpendGbp"] = (None if maxNominal is None else format(maxNominal, ".2f"))
    result["exits"] = normalizedExits
    return result


def campaignDigest(config: dict) -> str:
    canonical = validateCampaign(config)
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()

def concentrationCampaign(wallet: str) -> dict:
    """New-campaign template; keeps sacrifice-on and all execution settings."""
    policy = defaultCampaign(wallet)
    policy["selector"] = {
        "name": "concentration",
        "policyVersion": 1,
        "windowAnchor": "firstTrade",
        "windowSeconds": 30,
        "maxLargestBuyTransactionShareBps": 2500,
        "firstTradeTimeoutSeconds": 300,
    }
    return validateCampaign(policy)

def cleanCampaign(wallet: str, selector="concentration") -> dict:
    """New campaign identity; no local trading deadlines."""
    if selector not in ("concentration","buyingBlocks"):
        raise ValueError("UNREGISTERED_SELECTOR")
    policy=concentrationCampaign(wallet) if selector=="concentration" else defaultCampaign(wallet)
    policy["schemaVersion"]=4
    del policy["execution"]
    return validateCampaign(policy)
