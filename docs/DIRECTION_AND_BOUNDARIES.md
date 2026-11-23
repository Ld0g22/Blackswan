# Direction and module boundaries — orientation, not implementation

## Mission

Discover and trade extreme right-tail outcomes early enough to capture executable upside, measuring **actual net GBP receipts**. Accuracy, timeliness, failure containment and repeatability determine whether research becomes profit. Novelty or architecture is not intrinsically valuable.

Chain: Robinhood Chain (4663). Trading universe and pool support are to be established from the candidate baseline and explicit future scope; do not assume all protocols, currencies or deployments are operational merely because tests pass.

## Candidate baseline — preserve, do not reinvent

The user reports a **working detector**, **old peak tracking and execution components**, and the **buying-block filter** ran correctly for more than a week. Those **specific historical revisions** are the candidate source of truth for the next phase. The newer modular/concentration branches are not automatically the baseline.

**To establish exact provenance in a later authorised session:** identify repository and immutable commit; record detector/selector/executor/monitor file hashes; Python/runtime/dependencies; network/provider assumptions; launch/config/CLI; wallet-policy semantics; DB migrations/schemas; baseline run folders and timestamps; observed qualifying trades, receipt hashes, quote coverage, sell outcomes and error counts; and monitor screenshot/layout. Do not claim this evidence exists until obtained.

Do not overwrite or refactor these components while reconstructing the baseline. Freeze a read-only baseline before changes. Preserve DB copies and keys separately; never publish secrets.

## Intended future pipeline

```text
Robinhood chain event stream
        |
        v
DETECTOR (unchanged, canonical launch/pool identifiers and ordering)
        |
        v
SELECTOR (pluggable decision policy)
    | buying-block selector
    | concentration selector
        |
        v
EXECUTION (one stable module)
    | candidate admission and stake/position caps
    | sacrifice policy (optional/configurable, owned here)
    | quote, simulation/preflight, buy intent and signing
    | receipt/nonce reconciliation; one stake per token as configured
    | peak tracking / guardian and executable exits
        |
        v
LEDGER + MONITOR (grounded state and verified net GBP)
```

This is **logical separation**, not a demand for more processes, databases, wallets or microservices. Use the fewest runtime moving parts that deliver correctness and speed.

## Contracts to finalise from real code later

**Detector → selector:** stable event identity (`chainId`, transaction hash, log index, pool identity, token address, launch ordering/time), supported pool identity and detector timestamp; full provenance of observations. Do not fabricate any source fields not present in the working detector. Establish reorg/gap semantics from evidence.

**Selector → execution:** one idempotent decision keyed by canonical token/pool and campaign, `selected` or `rejected`, reason, policy identity/version, observed information cutoff, measured latency, immutable feature values, and timestamp. Selector may compute/research per policy, but **cannot** spend funds, sign, approve, sacrifice or dictate exit mechanics. Policies must be replaceable via configuration/registration; run at most one active decision policy per campaign unless explicitly combining them.

**Execution:** receives selected candidate and campaign rules, enforces funding/position caps, makes sacrifice decision and any buy/sell operations, owns intent journal, signer, receipts, quote-to-fill and exit tracking. Changing selector must **not** alter execution semantics. Sacrifice is **not** a selector outcome; a selected token may fail execution safely.

**Peak tracking:** preserve historical observation cadence and state transitions until measured reasons justify changing them. An all-time high of an *executable sell quote* at a precise position size is different from a spot-chart price. Do not reintroduce unavailable unrealised peaks. Preserve outstanding positions on restart.

**Campaign config:** original flexibility for campaign size, stake value, cap, optional sacrifice, wait interval, slippage/limit, exit slices, candidate/sell quote timing, RPC choice and monitoring. Defaults and specific limits come from the frozen baseline, not guesses. The old CLI should remain compatible where possible; migration must be explicit.

**Monitor:** use old monitor layout as reference. It must show complete contract/pool IDs, intake/selector reasoning, why selections did not buy, sacrifice tx/receipt/wait/survival, stake entry, best and current executable sell quote, all exit slices, realised proceeds GBP, outstanding exposure, RPC/head/lag/recovery, warnings and last update. No made-up statuses, truncation of essential addresses or simulated balances passed off as receipts.

## Quality invariants

- A selector change does not require editing execution, detector or monitor data formats.
- All selected candidates resolve deterministically to buy, safely rejected admission, or an explicit unresolved/journaled state; no silent drop.
- Each signed intent is journalled first; nonce/receipt uncertainty cannot create duplicated buys/sells on restart.
- New buying can pause without blocking existing exits and reconciliation.
- Ineligible/untradeable or structurally unsupported pools are classified early enough to avoid wasting a sacrifice.
- Latency introduced by the new boundary must be measured against the original successful system, not merely considered small.
- Wallet preparation is an engineering responsibility where feasible; any proposed operator requirement must be necessary, supported by on-chain evidence and disclosed before implementation.

## Explicitly out of scope for this standards pack

No rebuild, migration, reconciliation, new wallet, selector switch, trading command, strategy threshold change, detector modification, new monitor, change to stake/exit parameters, or deployment. The diagram records *intent only*.
