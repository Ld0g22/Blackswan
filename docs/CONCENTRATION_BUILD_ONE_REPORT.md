# Concentration Build One — implementation and verification report

**Date:** 2026-11-24  
**Branch:** `concentration/build-one`  
**Target:** `build/three-configurable-sacrifice-20261123` (Build Three has not yet been merged into main)  
**Status:** **D2 — controlled integration tested; NOT LIVE VERIFIED.** No live chain trading, keys, or funded deployment.

## What has been built

- `concentration_selector.py` is a runnable, independent V4-only selector. It reads Build One's detector outbox and publishes through the existing `five_second_selector.SelectorStore` decisions and revocations interface. No direct wallet, transaction, sacrifice, peak-tracking or exit code in the selector.
- The locked metric is the *largest buying transaction's share of all buy-base volume* in the **first 30 seconds after the first canonical V4 Swap** in the selected pool. Buy logs from the same tx hash are added before comparing exact integer `largest * 10000 <= total * 2500`. This is transaction-hash grouping, not unique-wallet identification.
- The authoritative evidence is obtained by 10-block-bounded V4 PoolManager Swap log RPC; WS uses the original shared socket transport for hot signal/reorg indication. Persistent SQLite checkpoints, full log identities, signed-delta decoder evidence, canonical header hash checking and replay guard restart/reorg gaps. Missing data is UNKNOWN, not PASS.
- The new launcher selects `buyingBlocks` or `concentration` from the frozen campaign manifest, without replacing the execution engine. It waits for a concentration selector that has actually verified `eth_chainId=4663`, not merely a newly created SQLite file.
- New campaign template is available via `run_configured_campaign.py template --selector concentration`; default sacrifice is **ON**, £0.01 with configured survival wait 500 blocks until the user changes those settings. Existing exit preset 80% at 3×, 20% at 5×.
- A 15-second maximum lag for an actionable PASS after the 30-second window prevents stale historical launches from entering the sacrifice queue. Too-late evidence remains logged with a non-actionable UNKNOWN decision.
- Detected launch or swap chain reorg after PASS emits a revocation; stale unverified or removed-chain evidence cannot be selected. Post-PASS header audits are bounded to recent decisions.

## Verification evidence and scope

CI Python 3.12 workflow: `Build Three — sacrifice and campaign contract` on this branch. Run `python -m unittest -q test_campaign_v3 test_sacrifice_failure_repro test_configured_exits test_configured_launcher test_configured_monitor test_configured_pipeline test_configured_manual test_concentration_selector` and `python -m unittest discover -p 'test_*.py' -q` (plus the baseline 500-block tests). Record exact counts and run ID from the final successful Actions run.

**Controlled-test behaviours exercised:**

- 25.0000% inclusive PASS; above-25% FAIL; multiple buys in one transaction; 30-second exclusive endpoint; first trade is a SELL; zero-buy FAIL; no-first-trade timeout and first trade past timeout UNKNOWN; ETH/WETH/USDG original interface coverage.
- Actual decoded detector journal → concentration `decisions` → unchanged `LiveExecutor.readSelections` → `LAMB_QUEUED`, including idempotent repeated reads and revocations.
- Full controlled concentration PASS → receipt-stubbed sacrifice → wait → main buy → receipt-stubbed 3×/5× two-stage sales → no duplicate sales, sacrifice token balance protected. Tests instantiate the actual restored execution state machine; all chain RPC, signing and receipts are controlled fakes, **not actual fills**.
- RPC HTTP 429 retry, canonical hash mismatch fail-closed, checkpoint rewind on reorg, restart from saved cursor, post-PASS revocation, policy-identity mismatch and legacy-selector DB isolation, multiple pending candidates and an independent readiness handshake.
- Previous Build Three manual-reserve failure was addressed on this branch so the current full regression suite can run; this is a prerequisite fix, not a change in concentration policy.

## Before a real-money campaign (still required)

1. Real **read-only Robinhood Chain 4663** check: chain ID, actual V4 PoolManager Swap event decoding, canonical timestamps, `eth_getLogs` coverage and provider response to bursts/disconnect/reorg. Confirm latency from end-of-window to PASS (p50/p95/p99) and total provider load at real launch rates; recent-pool and no-trade-pool RPC behaviour can be expensive under the provider's ten-block log-range limit.
2. Reconcile equivalent source data against the original historical 25% research cohort. Current implementation is *pool-specific*, not wallet ownership or cross-pool token aggregation. No demonstrated predictive profitability yet.
3. Close Build Three's separate live readiness gates, then ask for **explicit** one-position, capped real-money D4 approval. No automatic live start, wallet access, or spending in this implementation.

## Boundary guarantees

The baseline detector, original `five_second_selector.py`, peak quote builders, signer, and sell route building are not rewritten. Selector changes do not alter execution configuration, sacrifice defaults, or exit ladders. `main` remains unchanged. Test fixtures are not presented as on-chain evidence.
