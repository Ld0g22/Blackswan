"""Immutable, narrowly configurable BLACK SWAN 3 2x lamb campaign policy.

Only the GBP amount per main purchase and the maximum number of main buys
are operator options. All detector/selector, 1p probe, 500-block survival,
routing, exit-tranche and manual-reserve policies remain fixed.
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
import sqlite3

FLEX_KIND='FLEX_LAMB_2X_V1'
FLEX_POLICY='80-10-5:2-50-100:manual5'
FLEX_EXIT='latched_2x_50x_100x_manual5'
FLEX_TRANCHES={'2x':80,'50x':10,'100x':5,'manual':5}
FLEX_PREFIX='v4_lamb_flex2x_'
PILOT_KIND='FLEX_LAMB_3X5X_V1'
PILOT_POLICY='80-20:3-5:no-reserve'
PILOT_EXIT='latched_3x_5x_full'
PILOT_TRANCHES={'3x':80,'5x':20}
PILOT_PREFIX='v4_lamb_flex3x5x_'
# Independent early-exit experiment. DB stages 3/5 remain slot identifiers.
EARLY_KIND='FLEX_LAMB_1P5X3X_V1'
EARLY_POLICY='80-20:1.5-3:no-reserve'
EARLY_EXIT='latched_1p5x_3x_full'
EARLY_TRANCHES={'1.5x':80,'3x':20}
EARLY_PREFIX='v4_lamb_flex1p5x3x_'
EARLY_TRIGGER_MULTIPLES={3:Decimal('1.5'),5:Decimal('3')}
MIN_GBP=Decimal('0.01')
MAX_GBP=Decimal('1000.00')
MAX_POSITIONS=1000
MAX_COMMITMENT_GBP=Decimal('10000.00')


def buyAmount(value):
    """Require positive GBP with at most two decimal places. No float/exponent."""
    if isinstance(value,bool) or not isinstance(value,(str,Decimal)):
        raise ValueError('INVALID_FLEX_BUY_GBP: use pounds with at most two decimals')
    raw=str(value)
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?',raw):
        raise ValueError('INVALID_FLEX_BUY_GBP: use pounds with at most two decimals')
    try:
        amount=Decimal(raw)
    except InvalidOperation:
        raise ValueError('INVALID_FLEX_BUY_GBP') from None
    if not MIN_GBP<=amount<=MAX_GBP:
        raise ValueError('FLEX_BUY_GBP_OUT_OF_BOUNDS: £0.01 to £1000.00')
    return format(amount.quantize(Decimal('.01')),'f')


def purchaseCount(value):
    if type(value) is int:
        number=value
    elif isinstance(value,str) and re.fullmatch(r'[0-9]+',value):
        number=int(value)
    else:
        raise ValueError('INVALID_FLEX_MAX_PURCHASES: use an integer')
    if not 1<=number<=MAX_POSITIONS:
        raise ValueError('FLEX_MAX_PURCHASES_OUT_OF_BOUNDS: 1 to 1000')
    return number


def flexSettings(buy,maximum):
    amount=buyAmount(buy)
    count=purchaseCount(maximum)
    if Decimal(amount)*count>MAX_COMMITMENT_GBP:
        raise ValueError('FLEX_COMMITMENT_LIMIT: main buy budget must be <= £10000')
    return amount,count


def flexManifest(info,expectedWallet=None):
    """Validate an entire immutable manifest; return canonical stake and count.

    Reject extra policy variants rather than allowing an apparently compatible
    campaign to run under a different selector, gate, route or exit policy.
    """
    if not isinstance(info,dict):
        raise ValueError('INVALID_FLEX_MANIFEST')
    # Version 1 is the original 500-block survival-only policy. The new
    # buying-activity filter is mandatory in version 2, never silently added
    # to or removed from an existing campaign.
    version=info.get('configVersion')
    filtered=(version==2 and type(version) is int
              and type(info.get('probeMaxBuyingBlocks')) is int
              and info['probeMaxBuyingBlocks']==7)
    legacy=(version==1 and type(version) is int
            and 'probeMaxBuyingBlocks' not in info)
    if not (filtered or legacy):
        raise ValueError('FLEX_CAMPAIGN_POLICY_MISMATCH')
    amount,count=flexSettings(info.get('gbpEach'),info.get('targetPurchases'))
    if info.get('gbpEach')!=amount or type(info.get('targetPurchases')) is not int:
        raise ValueError('NONCANONICAL_FLEX_MANIFEST')
    required={
        'chainId':4663,
        'mode':'REAL_MONEY',
        'campaignKind':FLEX_KIND,
        'entryPolicy':'uniswapV4_nonPons_only',
        'quoteEngine':'original_peak_tracker_Tracker_best',
        'exitPolicy':FLEX_EXIT,
        'sellTranches':FLEX_TRANCHES,
        'probeGbp':'0.01',
        'survivalBlocks':500,
        'selectorDecisionSeconds':20,
        'selectorMaxBuyingBlocks':10,
    }
    if (any(info.get(k)!=v or type(info.get(k)) is not type(v)
            for k,v in required.items())
            or 'survivalSeconds' in info):
        raise ValueError('FLEX_CAMPAIGN_POLICY_MISMATCH')
    wallet=info.get('walletPublicAddress')
    if (not isinstance(wallet,str) or
            not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet)):
        raise ValueError('INVALID_FLEX_WALLET')
    if expectedWallet is not None and wallet.lower()!=expectedWallet.lower():
        raise ValueError('FLEX_WALLET_MISMATCH')
    return amount,count,wallet.lower()


def verifyFlexLedger(path,stake,count,wallet,expectedProbeMaxBuyingBlocks=None):
    """Fail closed on manifest-vs-database divergence BEFORE launching workers."""
    path=Path(path)
    if not path.is_file():
        raise RuntimeError('MISSING_EXECUTION_LEDGER')
    wanted={'campaignVersion':'2','campaignKind':FLEX_KIND,
            'buyGbp':stake,'maxPurchases':str(count),
            'walletPublicAddress':wallet.lower(),
            'probeGbp':'0.01','waitBlocks':'500',
            'exitPolicy':FLEX_POLICY,'flexConfigVersion':'1'}
    try:
        with sqlite3.connect(f'file:{path.resolve()}?mode=ro',
                             uri=True,timeout=5) as db:
            recorded=dict(db.execute('SELECT key,value FROM meta'))
    except sqlite3.Error as exc:
        raise RuntimeError('FLEX_LEDGER_UNREADABLE') from exc
    if expectedProbeMaxBuyingBlocks==7:
        wanted['probeMaxBuyingBlocks']='7'
    elif expectedProbeMaxBuyingBlocks is None:
        if 'probeMaxBuyingBlocks' in recorded:
            raise RuntimeError('FLEX_LEDGER_CONFIG_MISMATCH')
    else:
        raise RuntimeError('FLEX_LEDGER_CONFIG_MISMATCH')
    if any(recorded.get(k)!=v for k,v in wanted.items()):
        raise RuntimeError('FLEX_LEDGER_CONFIG_MISMATCH')
    return True


def pilotManifest(info,expectedWallet=None):
    """Immutable 3x/5x pilot; no post-probe buying-block gate."""
    if not isinstance(info,dict):
        raise ValueError('INVALID_PILOT_MANIFEST')
    amount,count=flexSettings(info.get('gbpEach'),info.get('targetPurchases'))
    required={'chainId':4663,'mode':'REAL_MONEY','campaignKind':PILOT_KIND,
        'entryPolicy':'uniswapV4_nonPons_only',
        'quoteEngine':'original_peak_tracker_Tracker_best',
        'exitPolicy':PILOT_EXIT,'sellTranches':PILOT_TRANCHES,
        'probeGbp':'0.01','survivalBlocks':500,
        'selectorDecisionSeconds':20,'selectorMaxBuyingBlocks':10,
        'configVersion':3,'postProbeBuyingBlockGate':'OFF'}
    if (any(info.get(k)!=v or type(info.get(k)) is not type(v) for k,v in required.items())
            or 'probeMaxBuyingBlocks' in info or 'survivalSeconds' in info
            or info.get('gbpEach')!=amount or type(info.get('targetPurchases')) is not int):
        raise ValueError('PILOT_CAMPAIGN_POLICY_MISMATCH')
    wallet=info.get('walletPublicAddress')
    if not isinstance(wallet,str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet):
        raise ValueError('INVALID_PILOT_WALLET')
    if expectedWallet is not None and wallet.lower()!=expectedWallet.lower():
        raise ValueError('PILOT_WALLET_MISMATCH')
    return amount,count,wallet.lower()


def verifyPilotLedger(path,stake,count,wallet):
    path=Path(path)
    if not path.is_file():
        raise RuntimeError('MISSING_EXECUTION_LEDGER')
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=5) as db:
        rows=dict(db.execute('SELECT key,value FROM meta'))
    wanted={'campaignVersion':'2','campaignKind':PILOT_KIND,
            'buyGbp':stake,'maxPurchases':str(count),
            'walletPublicAddress':wallet.lower(),'probeGbp':'0.01',
            'waitBlocks':'500','exitPolicy':PILOT_POLICY,'flexConfigVersion':'2'}
    if 'probeMaxBuyingBlocks' in rows or any(rows.get(k)!=v for k,v in wanted.items()):
        raise RuntimeError('PILOT_LEDGER_CONFIG_MISMATCH')
    return True



def earlyManifest(info,expectedWallet=None):
    """Immutable 80% at 1.5x, 20% at 3x; no post-probe buying-block cap."""
    if not isinstance(info,dict):
        raise ValueError('INVALID_EARLY_MANIFEST')
    amount,count=flexSettings(info.get('gbpEach'),info.get('targetPurchases'))
    required={'chainId':4663,'mode':'REAL_MONEY','campaignKind':EARLY_KIND,
        'entryPolicy':'uniswapV4_nonPons_only',
        'quoteEngine':'original_peak_tracker_Tracker_best',
        'exitPolicy':EARLY_EXIT,'sellTranches':EARLY_TRANCHES,
        'probeGbp':'0.01','survivalBlocks':500,
        'selectorDecisionSeconds':20,'selectorMaxBuyingBlocks':10,
        'configVersion':4,'postProbeBuyingBlockGate':'OFF'}
    if (any(info.get(k)!=v or type(info.get(k)) is not type(v) for k,v in required.items())
            or 'probeMaxBuyingBlocks' in info or 'survivalSeconds' in info
            or info.get('gbpEach')!=amount or type(info.get('targetPurchases')) is not int):
        raise ValueError('EARLY_CAMPAIGN_POLICY_MISMATCH')
    wallet=info.get('walletPublicAddress')
    if not isinstance(wallet,str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet):
        raise ValueError('INVALID_EARLY_WALLET')
    if expectedWallet is not None and wallet.lower()!=expectedWallet.lower():
        raise ValueError('EARLY_WALLET_MISMATCH')
    return amount,count,wallet.lower()


def verifyEarlyLedger(path,stake,count,wallet):
    """Reject mismatched campaign DB on resume, without writing to it."""
    path=Path(path)
    if not path.is_file():
        raise RuntimeError('MISSING_EXECUTION_LEDGER')
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro',uri=True,timeout=5) as db:
        rows=dict(db.execute('SELECT key,value FROM meta'))
    wanted={'campaignVersion':'2','campaignKind':EARLY_KIND,
            'buyGbp':stake,'maxPurchases':str(count),
            'walletPublicAddress':wallet.lower(),'probeGbp':'0.01',
            'waitBlocks':'500','exitPolicy':EARLY_POLICY,'flexConfigVersion':'3'}
    if 'probeMaxBuyingBlocks' in rows or any(rows.get(k)!=v for k,v in wanted.items()):
        raise RuntimeError('EARLY_LEDGER_CONFIG_MISMATCH')
    return True


def globalWalletLock(wallet):
    """Lock path shared by separate worktrees of the *new* flexible mode."""
    if not isinstance(wallet,str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet):
        raise ValueError('INVALID_FLEX_WALLET')
    return Path.home()/'.cache'/'blackswan3'/'wallet-locks'/(
        '4663_'+wallet.lower()+'.lock')


def otherLiveExecutors():
    """Find older or concurrently starting live executors (Linux /proc).

    Legacy worktrees do not acquire the global lock. Therefore this detects
    executors already running but cannot prevent a legacy executor started
    *after* a flexible campaign. Operator must not start the legacy executor.
    """
    proc=Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('LIVE_PROCESS_ISOLATION_UNAVAILABLE')
    current=os.getpid()
    matches=[]
    for directory in proc.iterdir():
        if not directory.name.isdigit() or int(directory.name)==current:
            continue
        try:
            args=[part.decode('utf-8','replace') for part in
                  (directory/'cmdline').read_bytes().split(b'\0') if part]
        except (OSError,PermissionError):
            continue
        if (any(Path(arg).name=='live_executor.py' for arg in args)
                and 'run' in args and '--arm-live' in args):
            matches.append(int(directory.name))
    return sorted(matches)
