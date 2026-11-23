# Engineering standard — simple, measurable, fail-safe

## Engineering principles

**Lead with the outcome.** For each task, state: what must work, observed current behaviour, non-regression invariants, latency budget, measurable acceptance criteria and what will constitute proof. Be technically inventive, but do not change the goal to suit the easiest implementation.

**Preserve a working path.** The original working detector + buying-block-selected execution and peak tracking is the reference candidate. Freeze immutable source and config; build regression fixtures from recorded events (redacted), receipts and quotes. A claimed improvement must be compared to this reference on the same data and workload.

**One responsibility per boundary.** Changes should be local and avoid new services if a function/module suffices. Dependencies are injected, not hidden in globals. New selectors implement one policy interface; no executor duplication. Config names and fields stay explicit, typed and stable; unknown config fails clearly rather than defaulting to dangerous behaviour.

**Safety and recovery are part of functionality.** Persist signed intents/position state before broadcasting. Reconcile uncertain outcomes before resending. Reorg, timeout, 429 throttling, session loss, RPC failover, missing prices, weird token decimals and fee-on-transfer behaviour need classified outcomes. Do not silently count unknown as failed or succeeded.

**Speed is an acceptance criterion.** Record event time, detection, decision, quote, sacrifice submission/receipt, main submission/receipt and first exit tracking timestamps. Separate RPC wait, compute, queue, signer and token preparation. Publish p50/p95/p99 and worst observed *with sample count* under representative load. New buys must not wait behind historical recovery or peak scanning without explicit reason. Compare against the earlier working pipeline.

**Tiny positions are not free.** Evaluate gas, slippage, router overhead, token-account overhead where applicable, on-chain restrictions, approval operations, bidirectional routing, and actual executable output at position size before live admission.

## Debugging standard

1. Read exact error/trace; determine which component produced it.
2. Trace actual inputs through the boundary (never leak secrets).
3. Compare known-good historical code, config and timing against failing revision.
4. Reproduce minimally using real captured inputs or controlled simulation; develop one falsifiable root-cause hypothesis.
5. Write regression test that fails before the fix; implement minimal fix; rerun and confirm full suite.
6. Inspect adjacent failure modes, restarts and security/performance impact. Fix related causes in the assigned scope, not just the latest symptom.
7. Provide short root-cause explanation + evidence + prevention + exact safe next step.

Never add indefinite retries to conceal errors. Bound concurrency, backoff under rate limits, and retain feed continuity during recovery. Classify unknown broadcast results and stop duplicate execution. Avoid print-and-continue exception handling that discards trade state.

## Source, branch and changes

- Use immutable commits and one clearly named branch per change. Establish upstream, baseline and exact diff before editing; protect user secrets/local changes.
- Prefer Python 3.12 compatibility where relevant, type hints, camelCase for new local identifiers unless library conventions dictate otherwise, small modules, explicit decimal arithmetic for money, UTC for chain timestamps, and deterministic tests.
- No needless dependencies or paid providers. If a dependency is necessary, show cost, quotas, failure modes and the benefit over alternatives. Avoid internal billing/accounting features the user has rejected.
- Keep execution policy, route mechanics and selector thresholds configurable without burying constants in separate implementations.
- Do not let monitor, research or historical backfill block the time-critical entry path.

## Mandatory test families for affected components

**Correctness:** exact config parsing, chain/contract IDs, pool identity, asset decimals, buy/sell ABI encoding, quote orientation, minimum received and full two-way route, selector decisions/reasons, stake and cap arithmetic, exit slices and GBP reconciliation.

**Failures:** reverted quote, token transfer tax, unsupported hook/pool, price source stale, 429, WS disconnect, short RPC timeout, detector gap, missing/duplicate event, chain reorg, failed sacrifice/main, unknown broadcast, insufficient balance, invalid credentials, expired approval, position on restart and stale DB migration.

**Integration:** captured historical detector input → each selector policy → identical execution module; fake RPC to test faults; archival/fork or read-only chain checks for deployed interfaces; tiny explicitly authorised real trade for end-to-end proof. Unit tests alone never establish live compatibility.

**Operational:** CLI install without shell termination; idle resource usage, concurrency, watchdog, head-lag, monitor update, stopped campaign and restart; validate all commands without touching funds when not authorised.

**Regression/performance:** run unchanged baseline cohort alongside proposed code on the same replay, measure missed opportunities, quote lag, transaction latency, peak coverage, error rate and net-equivalent fill simulations. Preserve original outputs where required; disclose any justified deviations.

## Review expectations

Provide an adversarial review for material money-handling changes: where can money be lost or locked, where could something double-spend, where could paper P&L be mistaken for real, what could block an exit, and how does the revised timing compare? The review must try to reject the change with evidence.

Avoid reviews that merely congratulate working tests. Critical bugs block live readiness; high-severity unknowns require deliberate narrow evidence, not optimistic defaults.

## Deliverables

For a requested change: commit and changed files; acceptance criteria; tests with actual commands/results; user-visible behaviour; specific remaining gaps; recovery/rollback; a **separate** safe install command; and a separately authorised live launch command only after appropriate operational gates. If a gate fails, stop and deliver a precise blocker, not another unverified command.
