# Concentration Build One — full-screen operator monitor

**Status:** implementation contract, 24 November 2026  
**Reference:** existing `live_dashboard.py` (the buying-block-era black/teal/green Rich widescreen execution control room). Preserve its receipt-first accounting, detailed sacrifice/position/exit display, and read-only behaviour.  
**User requirement:** a human-readable live control room on the large second monitor, with **full token addresses, full venue/pool identities, full transaction hashes** and complete operational explanations. No new trading functionality.

## Operational purpose and layout

The monitor tells an operator: (1) what the detector saw; (2) what 30-second concentration measured and why it passed/failed/was unknown; (3) why a PASS did or did not become a sacrifice and a main buy; (4) what is held and how it is valued; (5) exactly what was submitted and what receipts prove; (6) which sales are due/confirmed; (7) what is wrong or late; (8) what has actually been spent and received.

One Rich **alternate-screen terminal** app at a configurable 0.5-second refresh by default. Based on the original scoreboard/panels. Do not force a terminal resize; adapt to the available width/height. On a 200+ column terminal use top metrics, two-column health/policy, opportunities, holdings/quotes, exit ladder, journal, alerts. Where vertical space is insufficient, use **page selection** rather than drop fields: (1) overview, (2) decisions, (3) positions/quotes, (4) transactions/exits/finance, (5) alerts/health. Support user keys 1–5, q; `--view` to pick a page, and `--once` for a full noninteractive evidence report. The overview focuses on current attention but detail pages never elide full identifiers. Text may wrap/fold; must not trim identifiers in any view.

### Page 1: OVERVIEW
- A high-contrast read-only banner with run folder, snapshot UTC timestamp, chain ID 4663, selector policy `concentration` or `buyingBlocks` (config-derived, never assumed), public wallet, and SHADOW vs CAMPAIGN state.
- A metric strip: selected PASS/FAIL/UNKNOWN, positions used/max, active positions, sacrifices used/max, nominal funding spent/reserved/limit (gas *excluded*), confirmed realised GBP-equivalent and P/L when complete, unreconciled receipts indicator.
- Health and policy: detector state, connected head, gaps and decode errors; selector chain-ID, heartbeat and detector cursor/backlog; execution intake/worker/venue watch state (report not instrumented where no authoritative heartbeat); main stake, sacrifice enabled/size/wait, deadlines, exit ladder.
- Recent opportunities: full token and venue IDs, decision and precise rejection/no-buy reason, first-trade and window end, buy share, swap/tx count, coverage and measured lag.
- Most important open-position cards: full token, selected event and venue, sacrifice state with actual receipt hash and remaining wait, main buy confirmation, remaining *main* tokens, last executable full-size sell quote and peak, exit states, real fills, errors.
- High-signal latest alerts and receipts with full transaction hashes.

### Page 2: DECISIONS & SELECTOR
- All recent launches and decisions (default 30, operator-configurable). Key: token, pool, source, detector timestamp, trade timestamp, 30s end, decision timestamp, lag, strongest transaction share calculated from *raw integer* totals, counts, evidence complete, collector error, missing data, reorg revocations.
- Distinguish SELECTOR PASS from CONSUMED, LAMB_QUEUED, PROBE_PENDING, PROBE_FAILED, BUY_PENDING, BOUGHT, CAP_FULL, SKIPPED, REVOKED. A detected token that never generated an execution selection has reason `NOT_CONSUMED_OR_NO_DECISION` rather than invented diagnosis.
- `0` buy volumes and missing values display NO DATA/unknown—not 0.000% where the denominator is unavailable.
- One pool with repeated swaps per tx remains one buying transaction by the selector's audited algorithm.

### Page 3: POSITIONS & QUOTES
- Every open and recent closed position, token and pool IDs, unique selection ID (full), exact receipt-confirmed original quantity, purchased timestamp, main amount, sacrifice result, full tx hash, remaining token quantity, pending sale.
- Last **actual on-chain executable quote for the ORIGINAL entry quantity** vs highest observed quote for that quantity; explicitly label both **full-size reference valuations** and not cash, not an executable quote for remaining holdings. Quote timestamp, block, route, basis, age, errors and stale indication. Do not turn `bestGbp` into a latest quote.
- Last quote errors stand alone with the last successful quote time and an independent stale flag; no false zero.
- Exit targets and status. Unconfigured exit stages support arbitrary length and target percentages, including partial manual reserves.

### Page 4: SACRIFICE, SALES & FINANCES
- Each sacrifice separately: sent/pending/confirmed/waiting/survived/failed and why, confirmed receipt block, configured 500-block (or seconds) countdown using chain head, own `probeQty`; never merge sacrifice quantity into main holdings.
- Each exit slice: exact configured multiple and %, triggered/sold, targeted/actual sold token quantity, sale tx full hash, actual received asset (WETH receipt credit or measured native balance), gas, GBP-equivalent **only** when finance journal qualifies it, errors, submission state; sold statuses derived from **receipts**, not flags alone.
- Transaction log: full tx hash, kind, state, time, reason, receipt verified/unverified, associated token.
- Financial summary: cost, realised returns and net *confirmed* GBP, cash only and incomplete reconciliation markers. Unknown native ETH receipt is `UNMEASURED`, never `£0` or made-up profits. Nominal stake cap excludes network gas.
- Manual requests/fills and unsold reserve are first-class, not hidden after the final automatic stage.

### Page 5: ALERTS / SERVICE / RECOVERY
- Missing DB, SQLITE_BUSY, missing schema, inconsistent identity/config/chain, detector recovery/gaps/decoding, selector stale heartbeat/backlog, decision latency >15s, RPC failures, reorg revocations, exit quote errors, stale quotes, signed/unsettled/reverted transactions, missing receipts, monitoring paused/retired and manual states.
- Service status is based on actual journal metadata. A row labelled `ACTIVE` in a DB does not prove process heartbeat; use `UNVERIFIED PROCESS LIVENESS` where no heartbeat is recorded. A retired detector is not a fatal error after intake closes.
- Operator action guidance distinguishes urgent `VERIFY/RESUME`, transient `RETRY`, and permanent `CLOSED/RETIRED`.

## Source of truth and data handling

Read-only transactions from `campaign.json` + `execution.sqlite3` + `selector.sqlite3` + `detector.sqlite3`. Reuse `live_dashboard.snapshot` for the original receipt-reconciled finance/sales/position baseline. Join extra data by immutable `selectionId/eventKey`, **never** via token suffixes. In code, all connections use `file:...?mode=ro`, `PRAGMA query_only=ON`, timeout <= 0.2s, closed each refresh. No .env, keystore, RPC, trading imports, subprocess, network, modifications, migration or journal-lock ownership. Treat the databases as asynchronously committed sources; do not claim an atomic cross-DB snapshot. On a transient read failure, carry forward last successful screen with a visible stale-data age/error flag; never blank to false zero.

- `live_dashboard.snapshot()`: baseline financial/sales/probe/positions/ledger/warnings with old layout-compatible data, no writes.
- `monitor_snapshot.py`: enrich with `campaign.json` policy, selector decisions/launches/progress/revocations, execution selection statuses/transactions/observations/exits, receipt-based real proceeds, activity/pending. Immutable snapshot dict with explicit `None` for unavailable measurements.
- `monitor_view.py`: Rich Panel/Table/Page renderers, safe dynamic text, complete identifiers, explicit quote vs cash distinction, proper wide terminal folding, sorted order and graceful empty data.
- `concentration_monitor.py`: CLI/lifecycle only. `--run-dir` explicit to avoid wrong campaign; `--latest` optional with visible matching mode; `--view`, `--interval`, `--rows`, `--once`. Keyboard 1–5 and q in a real terminal; Ctrl+C always restores terminal. Never spawn or stop an executor.

## Verification and no-spend acceptance
1. Tests first: no DB, partial/corrupt DB, SQLite busy, database not created, shadow-only selector, fake 25% / zero buy FAIL and >15s unknown, full token and 66-hex V4 pool identity, missed deadline vs actual selector FAIL.
2. Fake genuine `ExecutionStore` campaign and `SelectorStore` DB with PROBE_PENDING, PROBE_CONFIRMED, BOUGHT, live observation success/failure, actual sale receipt, configurable 80/20 stages, manual reserve and finance reconciliation. Assert no quote/peak is reported as realised.
3. Test narrow/wide Rich render with no address/hash truncation and all 5 views. Snapshot is read-only (DB bytes/inode and no creation), terminates cleanly after ctrl-C, no credentials read.
4. Run existing 500-block sacrifice, all module tests and CI; static command lint; no trade execution changes.
5. A one-position live test is **separate** and not armed by monitor installation. The monitor is a prerequisite, not a safety guarantee.
