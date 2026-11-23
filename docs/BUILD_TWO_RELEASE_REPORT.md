# Build Two — recovery, evidence and selector integration

**Source:** `Ld0g22/BlackswanNovember` revision `5bd3c2b6914d85fc703a17190d5834a7f7cf8b45` (15 November 2026), the historical **buying-block filter** stack. Recovered modules were imported without rewriting peak quotations or transaction mechanics. **Build One detector remains unchanged.**

**Status:** source recovered and automated historical regressions **D1 (offline tested)**; **NOT LIVE-NETWORK VERIFIED** in this new repository. No service, private wallet or transaction was started.

## What was imported

- **Peak tracking:** `peak_tracker.py`, with direct ETH/WETH and bridged USDG valuations, observed executable quotes, real position quantity and full original venue choice
- **Execution:** `live_executor.py`, `execution_peak_routes.py`, `swap_adapters.py`, `wallet_transactions.py`, `broadcast_rpc.py`, `campaign_finance.py`
- **Support:** `campaign_config.py`, `manual_exit_engine.py`, `manual_sale.py`, `held_venue_watch.py`, original dashboard, historical launcher, and the read-only preflight scripts it actually calls
- **Historical sacrifice dependencies:** `sacrificial_lamb.py` and `probe_buying_blocks.py` imported intact solely to satisfy legacy execution paths. Sacrifice remains **disabled unless a future campaign explicitly configures it**.
- **Original selector** and venue decoder retained for regression/compatibility; the restored executor **does not import a selector implementation**. The new concentration selector has **not been implemented**.
- Original source tests and fresh selector contract regression tests

## Exact integration contract

```text
Build One detector (event_log: seq, source, payload; schemaVersion=1)
     → selector (any policy, same interfaceVersion=1 decision contract)
     → selector.sqlite3:
          decisions(seq, status, data)
          revocations(seq, eventKey)
     → LiveExecutor.readSelections / readRevocations
     → candidate admission → quote, buy intent, receipt → executable peak checks → latched sales
```

For `PASS`, `data` must contain: `interfaceVersion: 1`, `selectionId` (stable and unique), `tokenKey` (`4663:0x...token`), `decisionAt` (Unix seconds), the **actual detector event** under `event`, and the **matching v4 eligible launch venue** under `venue` (including `id`, `source`, `hooks`, `fee`, `spacing`, base/token identities). The executor reads rows by increasing `seq` and durably checkpoints `selectionCursor`. Reorganisations must supply revocations whose `eventKey` equals `selectionId`. Do not alter this schema when creating buying-block and concentration selectors.

**Proven by new offline test** `test_build_two_selector_contract.py`: actual Build One `decodeLog` + `EventJournal` → `SelectorStore` publishing `buyingBlocks` or `concentration` policy-labelled `PASS` → real `LiveExecutor.readSelections`. For native ETH, WETH and USDG base pools, each event becomes a single `QUEUED` admission with recovered venue; replay does not duplicate; removal invalidates pool and selector revocation cancels queued admission. All six permutations passed. **These are policy-output simulations, not a new concentration-selection implementation or a live trading test.**

**Important constraints:** the executor intentionally accepts only valid v4 launch selections, although execution routes may use **v3 or v4** pools. It excludes Pons-origin tokens and some legacy sacrificial patterns. The old `run_live_campaign.py` still launches `five_second_selector.py` directly: to **switch selector implementations using the old launcher**, a small, config-driven launcher selector-entrypoint choice and identity freeze are still required. The engine **itself** does not need rewriting. Only source/SQLite compatibility has been established, not subscription latency, cost or live order success.

## Thresholds and campaign configuration — proven versus pending

The restored code implements latched executable-quote thresholds and original slices:

| Original policy | Thresholds | Slices |
|---|---|---|
| Non-sacrifice original | 3× / 5× / 10× | 80% / 15% / remainder |
| Flexible original | 2× / 50× / 100× | 80% / 10% / 5%, with 5% manual reserve |
| Flexible pilot | 3× / 5× | 80% / 20% |
| Early-exit experiment | 1.5× / 3× | 80% / 20% |

These profiles are **selectable historical configurations**, **not** arbitrary numeric threshold parameters. Full user-configurable threshold values still need an explicit, versioned manifest/CLI mapping and tests; not silently added to trading code. The historic flexible campaign can configure **main stake GBP** and **purchase count**, but its startup is coupled to the historic sacrifice policy. The legacy sacrifice is £0.01, 500 blocks (or an older limited 10s path); **arbitrary sacrifice size and wait are not implemented**.

A threshold latches on the full **actual receipt-confirmed quantity** quoted for executable sale, not a spot chart price. Existing exits have priority over buys; signed transactions are persisted before broadcast; quote-only “peaks” never count as realised proceeds.

## Test result

On `build/two-peak-execution` CI (Python 3.12, pinned historic dependencies plus required `rich` dashboard library):

- **170/170** legacy peak/route/execution/sacrifice regression and selector interchange tests
- **16/16** preserved Build One detector tests
- **19/19** Build One and project standards tests
- Test run `45280675534` passed. Original 169-suite first failed four dashboard tests solely because the original dependency file omitted `rich`; adding `rich>=13,<15` made the suite pass without touching trade logic.

## What is **not** proved

No real RPC inspection, actual on-chain swap, end-to-end latency benchmark, successful sacrifice, revenue calculation, configurable arbitrary threshold, or new selector deployment was performed. Original historical trades are not automatically attributed to this new Build Two source without on-chain receipt and wallet reconciliation. Full release readiness requires D2-D4 evidence as applicable, including actual v3/v4 route simulations and recovery/nonce safety with the intended deployed wallet and provider (read-only first). The old `run_live_campaign.py` and `live_executor.py` are preserved **for reference/testing, not authorisation to arm live money**.

## Next integration task

Implement the minimal **configurable selector entrypoint** in the existing launcher with campaign-identity pinning, then use the concentration selector implementation conforming to this tested outbox; add independently configurable numeric exit thresholds as a separate backward-compatible, versioned feature. Sacrifice-size and survival-wait configuration can come later as requested. None of those changes requires redesigning peak quotations or the transaction engine.
