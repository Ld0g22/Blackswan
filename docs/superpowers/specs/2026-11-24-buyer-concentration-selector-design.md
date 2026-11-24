# BlackSwan — Buyer-Concentration Selector V1 Design

**Date:** 2026-11-24  
**Status:** design only; no selector implemented or campaign armed  
**Target implementation branch:** `build/three-configurable-sacrifice-20261123` (not currently merged to `main`)  
**Existing contract:** `five_second_selector.SelectorStore`, `LiveExecutor.readSelections`, `campaign_v3.py` and `run_configured_campaign.py`

## Goal

Add a switchable concentration selector, with **no changes to the existing detector, sacrifice logic, quoting, signer, peak tracker or exit engine**. Reproduce the user-approved historical research feature: **the largest BUY TRANSACTION's share of total buying volume, measured over the first 30 seconds from the first trade**, and select when the share is **at most 25%**.

The research unit is **transaction hash**, not wallet address, sender, recipient or unique buyer. Multiple qualifying buy swap logs in one transaction are grouped into a single purchase for the numerator. The term “buyer concentration” is historic shorthand, not a claim about beneficial owners.

### Locked selector rule

For an eligible selected **Uniswap V4 launch pool** on Robinhood Chain 4663:

1. Find the **first valid Swap event** (buy or sell) in that pool, using its **canonical block timestamp**, `t0`. **Do not anchor the window to Initialize/pool launch time**.
2. Collect and deduplicate the pool's canonical Swap logs for blocks with timestamps in `[t0, t0 + 30 seconds)`. Exclude liquidity events; swaps with zero buy-base amount do not add to buying volume.
3. Decode each swap using the recovered, signed-delta, venue-aware `research.venue_decoders.decodeLog(raw, venue)`, retaining `buyBaseAmount` as an **integer in the pool's own base-asset smallest units**. Ignore `sellBaseAmount` for this buy-only concentration ratio. This supports ETH, WETH and USDG pools without converting currencies because **each decision is calculated for one pool/base**.
4. Group all positive `buyBaseAmount` entries by canonical `transactionHash` and sum within each transaction. `largest = max(grouped buy volumes)`, `total = sum(grouped buy volumes)`.
5. Once the full 30-second observation window is provably covered, issue:
   - `PASS` when `total > 0` and `largest / total <= 0.25`.
   - `FAIL` when `total > 0` and `largest / total > 0.25`, or a **complete** window contains **zero** buy volume (`NO_BUY_VOLUME`).
   - `UNKNOWN` if the first swap or complete canonical window cannot be established by the configured operational timeout, or if evidence is missing/unverifiable.
6. Calculate the comparison exactly with integers: **`largest * 10_000 <= total * 2_500`**. Do not use floating-point fractions, rounded percentages or a buy-count shortcut.
7. **Never PASS early:** later transactions can dilute or increase the largest-transaction share. Decide only after the true `t0+30s` window has closed and canonical backfill is complete.

Examples: four independent 25-unit buy transactions => largest/total = 25/100 => PASS. One transaction containing two 20-unit swaps and three other separate 20-unit buys => grouped totals 40/100 => FAIL. One transaction with four buy logs is **one** buyer-transaction, not four independent ones.

### Pool scope and important limitation

V1 measures the **specific V4 venue from the detector's Initialize event and its selected `venueKey`**, matching the recovered pool-based research definition and the existing selector/outbox contract. It does **not** add buys on different pools, different base assets, or across the token's lifetime. It does **not** group across wallets or trace routers to beneficial owners.

Cross-pool token-wide concentration and wallet-level beneficial ownership are **different metrics** and are explicitly out of scope. If the historical research used a broader pool universe than this, verify comparability before claiming to replicate its predictive result. One operator can split purchases among transaction hashes; this selector is **not Sybil-resistant**. Do not silently add “minimum unique wallets”, sell-pressure, buying-block, momentum or pool-structure filters to the core concentration rule.

Only V4-origin selections can be sent to the current executor; V3 and Pons launches may continue to be recorded by the detector but must not result in an actionable V4 PASS. Existing execution exclusions (Pons origin, unsafe pools, no executable route) continue to apply **after selector PASS**, independently.

## Minimal integration points

### New file: `concentration_selector.py`

Independent Python process, same general lifecycle as `five_second_selector.py`, using recovered helpers:

- Read `detector.sqlite3/event_log(seq,source,payload)` in seq order using a durable cursor.
- Build pool/venue candidates with `five_second_selector.eligibleVenues(event)`, restricted to `uniswapV4`.
- Use existing `SelectorStore` for `launches`, `decisions`, `revocations`, `meta` and cursor. Never create a second, incompatible output contract.
- Maintain new **policy-owned** SQLite tables for trade evidence and canonical time information as needed; do not alter the execution ledger. Suggested unique raw evidence key: `(eventKey, blockHash, transactionHash, logIndex)`; support removed/replaced logs. Store the exact base units, direction, block number and hash, chain timestamp, source of observation, and validity state. Group from durable raw evidence, never from event counts alone.
- Publish `SelectorStore.decide(eventKey, "PASS"|"FAIL"|"UNKNOWN", extras)` using exactly the historical V1 `interfaceVersion:1`, `selectionId`, `tokenKey`, `venueKey`, `event`, `venue`, `decisionAt`, and append-only `decisions(seq,status,data)`. The existing executor consumes the same output regardless of policy.
- On a launch reorg, append `SelectorStore.revoke(eventKey,reason)`; invalidate noncanonical swaps. If a canonical first swap or swap log changes, recompute or revoke an already published PASS. Reorgs after transaction submission cannot undo a transaction, so execution's ordinary receipt reconciliation remains authoritative.
- Resume validates the exact frozen selector policy/version/threshold and detector identity. Do not change a prior decision or restart the observation window at process restart.

### Minimal launcher/config changes

- Extend **only the selector-specific parsing/validation** in `campaign_v3.validateCampaign` to accept a distinct `selector.name="concentration"` policy with strictly validated fields such as:
  ```json
  {
    "name": "concentration",
    "policyVersion": 1,
    "windowAnchor": "firstTrade",
    "windowSeconds": 30,
    "maxLargestBuyTransactionShareBps": 2500,
    "firstTradeTimeoutSeconds": 300
  }
  ```
- `firstTradeTimeoutSeconds=300` is a **proposed operational resource bound**, not part of the observed research formula. If no first swap is provable before this limit, record `UNKNOWN: NO_FIRST_TRADE_WITHIN_OBSERVATION_BUDGET`, never treat the pool as a PASS. This is a campaign-frozen parameter, not a retroactive claim about price/returns.
- Preserve the existing `buyingBlocks` selector configuration **byte-for-byte** on resume, including hash/identity. A policy-dependent shape check can accept concentration's new fields without injecting them into existing V3 manifests. Do not force silent migration of older campaigns.
- Add one `selectorCommand` registry entry in `run_configured_campaign.py` that starts `concentration_selector.py run` with the same `--env --detector-db --db` contract and its own validated policy flags. Existing `integrationOnly` remains prohibited from live trade.
- **No configuration parameter in this module changes** the sacrifice flag, sacrifice amount/wait, campaign stake/count, exit ladder, or execution strategy. Those are in the campaign and execution modules. New campaigns still have sacrifice **ON** by default.

### Suggested durable decision payload (added fields only)

The existing `SelectorStore.decide` produces the required interface fields. Add:
```json
{
  "policyName": "concentration",
  "policyVersion": 1,
  "windowAnchor": "firstTrade",
  "windowSeconds": 30,
  "maxLargestBuyTransactionShareBps": 2500,
  "firstTradeBlock": 123456,
  "firstTradeAt": 1700000000,
  "windowEndAtExclusive": 1700000030,
  "largestBuyTransactionBaseRaw": "25000000000000000",
  "totalBuyBaseRaw": "100000000000000000",
  "largestBuyTransactionHash": "0x...",
  "buyTransactionCount": 4,
  "observedSwapCount": 7,
  "coverageComplete": true,
  "reason": "MAX_TRANSACTION_SHARE_WITHIN_LIMIT",
  "evidenceErrors": []
}
```
Example values above are **schema illustrations**, not observations or real transaction hashes.

The monitor can show concentration percentage and reasons from this payload without changing the selector/execution DB interface. Do not imply it counts unique wallets.

## Collection and timing design

- Start monitoring on the detector's V4 Initialize, but **start the calculation clock only on the first valid canonical Swap**.
- Subscribe first to V4 PoolManager `Swap` events filtered by the poolId; then backfill from the Initialize block so an early trade is not missed. Reuse a **single shared WebSocket connection**, not a new connection per candidate.
- Bound HTTP `eth_getLogs` to the provider's supported range (**currently 10 blocks per call** in the recovered selector). Deduplicate overlapping live/backfilled events. Cache block-hash -> timestamp read from `eth_getBlockByHash`; validate canonical block/hash continuity and window endpoints.
- Do not count the websocket arrival timestamp as a swap's trade time; out-of-order arrival and backfill must be supported. A window ends after a known canonical block timestamp reaches or exceeds `t0 + 30s`, and all preceding blocks in the observation window have been covered.
- On WebSocket disconnect, reorg, partial backfill, rate limit, bad ABI, missing header or incomplete coverage, recover by targeted backfill when possible; otherwise finalize `UNKNOWN` **and never PASS on a partial denominator**.
- Use bounded concurrent candidates/requests and short, provider-aware backoff on HTTP 429. No redundant full-history rescans or internal Alchemy-CU accounting. Preserve the original detector and prioritize urgent execution/network operations over low-priority selector backfill where the provider is shared. Store backlog/recovery reasons rather than dropping candidates silently.
- Evaluate exactly at the earliest provable end of the 30-second period. Track `firstTradeAt`, `decisionAt`, missed windows and p50/p95/p99 decision latency. Do not add a second 30-second waiting interval.

## State transitions and error semantics

```text
V4 launch -> WAIT_FIRST_TRADE -> COLLECTING_30S -> SEALING
SEALING -> PASS | FAIL | UNKNOWN
PASS -> normal unchanged execution path (sacrifice -> wait -> main buy -> exits)
canonical reorg after PASS -> revocation journal -> executor reconciliation
```

No first swap by configured operational bound => UNKNOWN. Complete first-trade window but no buying volume => FAIL. Any incomplete coverage => UNKNOWN. Duplicated/removed swaps => invalidated and recomputed; a changed finalized PASS is revoked, never silently edited. No selection means **no sacrifice**, and a PASS is not permission to bypass any execution safety check.

## Verification gates before enabling live campaigns

1. **Metric fidelity:** exact 25% boundary, 25.0001% fail, one transaction with multiple swaps, buys and sells in same tx, zero buys, zero-value swaps, ETH/WETH/USDG base denominations, 128-bit V4 signs, malformed logs, duplicate `blockHash/txHash/logIndex`, missing swaps, events at `t0+30` excluded, first sell as window anchor, first trade long after pool Initialize.
2. **Operational fault tests:** subscription-before-backfill, out-of-order delivery, RPC 429, 10-block-range limits, websocket disconnect/recovery, restart halfway through the 30-second window, selector crash after decision write, detector pool reorg and swap-log reorg, stale provider block headers, late backfill.
3. **Boundary tests:** controlled Build One decoded V4 Initialize -> real concentration Swap data -> existing `SelectorStore` PASS/FAIL/UNKNOWN -> unchanged `LiveExecutor.readSelections()` -> sacrifice queued only on PASS; same contract regardless of selector. Repeated candidate/read cycles never buy twice. Compare no-transaction/no-receipt cases.
4. **Statistical integrity:** reconstruct the original historical concentration cohort from equivalent V4 raw swap receipts/logs, and compare the numerator/denominator and earliest decision times **token by token**. If exact source data is unavailable, label historical comparison unverified; do not claim matching performance.
5. **Performance/read-only chain:** p50/p95/p99 and worst end-of-window -> durable decision latency, provider 429 and backlog under launch bursts; real Robinhood 4663 chain identity and read-only V4 Swap decoding and log coverage (D3). No signed transaction or real-money campaign without separate explicit authorisation.
6. **Release boundaries:** CI targeted + full suites green on exact branch SHA; existing buying-block mode and historical ledger resume unchanged; independent review of D2/D3 evidence and operator safeguards.

## Not part of this selector

Wallet/funding logic; sacrifice decisions or amount/wait; quote or route viability; signer; peak monitoring; exit percentages; feature research; additional risk scoring; pool-wide all-wallet aggregation; cross-pool merging; token-level sybil detection; empirical profitability claims.

## Design choices / trade-offs

**Chosen: a dedicated small `concentration_selector.py` process with reusable selector store and decoded V4 swaps.** This preserves the known, testable SQLite contract and does not entangle concentration calculations with the executor.

Rejected: putting concentration inside `live_executor.py` (breaks interchangeability and delays signing); changing the original buying-block selector to support two policies in one branching state machine (regression risk); inventing wallet ownership by reading pool-level V4 Swap events (not actually proven); launching a separate generic feature collector (unnecessary network traffic and prior failure).

**Open validation issue, not a silent requirement change:** confirm whether the exact historical research cohort measured one launch pool or combined all pools for a token. This design intentionally measures the selected launch pool; cross-pool aggregation is not implemented unless justified by equivalent source evidence and separately approved.
