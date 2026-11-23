# Build Two — restore old buying-block peak tracking and threshold execution

**Source provenance:** historical `Ld0g22/BlackswanNovember` at immutable commit `5bd3c2b6914d85fc703a17190d5834a7f7cf8b45` (`experiment-1p5x-3x-20261115`). This is from the original successful **buying-block selector** system, **not** the failed later modular rewrite. Exact Git blob hashes of imported source and regressions are to be checked against source.

## Preserved modules

- `peak_tracker.py`: original executable quote engine and peak tracking, including on-chain ETH / WETH / USDG direct routes and quoted bridge paths, original token quantities and GBP multiplicative accounting
- `execution_peak_routes.py`, `swap_adapters.py`: converting verified original peak route choices to on-chain calldata; mixed-v3/v4 multi-hop routes fail closed if no tested atomic path exists
- `live_executor.py`: original receipt-confirmed, durable, threshold-latched sell scheduler, sale priority over new buys, original position quantities, original journal shape
- `wallet_transactions.py`, `broadcast_rpc.py`: signed-byte persistence, nonce ordering, on-chain receipt recovery and broadcast classification
- `campaign_config.py`, `campaign_finance.py`: old stake/count and immutable campaign identity, verified payment accounting
- `manual_exit_engine.py`, `manual_sale.py`, `held_venue_watch.py`: original manual reserve, manual exits and new-pool discovery support
- `sacrificial_lamb.py`, `probe_buying_blocks.py`: **existing historical dependencies**, retained as sources for later reuse. Build Two does not activate a sacrifice or authorise a campaign
- `run_live_campaign.py`, `run_fresh_peaks.py`, `five_second_selector.py`, `research/venue_decoders.py`, `live_dashboard.py`: original **compatibility and test fixtures** for old buying-block flow; future selector is swappable, not built yet

All imports currently retain original paths to minimise transplant risk. The Build One detector at `detector.py` is retained unchanged, and its SQLite outbox schema is compatible in shape with the older producer. **Actual integrated consumer compatibility and latency remain to be verified.**

## Historically implemented thresholds

The recovered engine has persisted latch stages, precise sell slices and three tested policy presets:

| Inherited mode | Full-position quote thresholds | Sell fractions |
| --- | --- | --- |
| Original non-sacrifice ladder | 3×, 5×, 10× | 80%, 15%, remainder |
| Flexible legacy 2× ladder | 2×, 50×, 100× | 80%, 10%, 5%; remaining 5% manual |
| Flexible pilot | 3×, 5× | 80%, 20% |
| Early-exit experiment | 1.5×, 3× | 80%, 20% |

These are code-defined presets, **not arbitrary operator-configurable multipliers**. They must not be claimed as fully configurable without versioned manifest/storage/CLI wiring and tests.

The original flexible main stake GBP and maximum purchase count exist, but historically the flexible entry modes are coupled to the 1p 500-block sacrifice. Do not imply a new no-sacrifice flexible campaign is already supported. The original sacrifice amount is **fixed £0.01** and wait **500 blocks** in flexible modes, or a limited older seconds-based mode. **User desires independently configurable sacrifice sizes and wait periods at a later stage; this is not implemented in Build Two**. No unauthorised live arming or strategy changes.

## Required confidence

The user's statement that the earlier system actually bought, tracked and sold is historical evidence only until independently reconciled against original transaction hashes and the identical deployed source. The code and synthetic tests alone establish **D1 local**, not D4 live money. Keep prior campaign databases and log files unmodified.

Before running with live money: independent real quote/route checks for native ETH, wrapped WETH and 6-decimal USDG, same-block reverse routes, minimum-outs, actual sale transaction receipts, recovery after restart, mixed-protocol fail-closed, and actual service latency. Test threshold latches at exact boundaries and persistence across restarts. Preserve unresolved positions on stop.

## No changes to old running campaigns

Nothing in this import starts services, reads a private wallet, changes old campaign databases or signs a transaction. Do not execute the restored `run_live_campaign.py` simply because it is present. It is included as historical reference and for safe offline tests, **not** as a commissioning instruction.

For source-specific tests run the **Build Two workflow** on GitHub Actions; test scripts operate exclusively on fake RPC and temporary DB fixtures. Release report must explicitly disclose any failing test and limitations.
