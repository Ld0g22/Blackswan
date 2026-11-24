# Concentration Build One — full release-readiness audit

Date: 2026-11-24  
Scope: `concentration/build-one` -> `audit/concentration-live-readiness-20261124`  
Trading state: **NO LIVE CAMPAIGN STARTED. NO FUNDS MOVED.**  
Tier: **D1 with synthetic end-to-end integration. D2 captured-chain replay and D3 read-only network verification still required.**

## Executive verdict

The source-level detector-to-sale contract is intact and tested with deterministic RPC and receipt evidence, but this is **not yet signed off for a funded first test**. Remaining blockers depend on actual Robinhood 4663 chain/provider and the intended operator's wallet; they cannot be replaced by green mock tests. The test campaign should remain 1 × £0.05 main stake with 1 × £0.01 sacrifice, explicitly configured and authorised only after D2/D3.

The selector remains **Uniswap V4-only** and computes exactly the approved transaction-level metric:

- The first canonical V4 Swap (BUY or SELL) establishes `t0`.
- The window is `[t0,t0+30s)`. Aggregate positive buy-base volumes by `transactionHash`.
- PASS iff there is positive volume and `largest * 10000 <= total * 2500`. This is **not** wallet-level Sybil resistance or cross-pool aggregation.
- Incomplete/reorg/stale evidence is UNKNOWN, not PASS.
- The existing `SelectorStore` outbox is the sole handoff into the original execution engine. Sacrifice, quoting, signing, peak tracking and sales are NOT inside this selector.

## Adversarial findings and repairs

| Severity | Root cause | Change | Automated proof |
| --- | --- | --- | --- |
| High — entry starvation | 12 no-trade pools consumed every active candidate slot until timeout; new launches could age out | After 5s of silence, yield one candidate when fresh work is pending. Temporarily defer its scheduling for 15s. Preserve original cursor, logs and first-trade deadline in SQLite | `test_idle_pools_yield_slots_to_new_launches_without_losing_progress`; `test_active_trade_window_cannot_be_preempted_as_silent` |
| High — unnecessary restart | Closed intake was recognised only when `workerState=RETIRED`; after interruption a campaign could restart the detector/selector despite closed intake | Resume consults `intakeState` authoritatively. Existing exits and on-chain held-venue monitoring remain independent | `test_resume_closed_intake_never_restarts_sources_even_if_state_active` |
| High — false close | Configured monitor used `all auto stages sold` as its open/closed criterion; a 5% manual reserve was incorrectly shown closed | Use actual main receipt quantity minus confirmed auto and manual debits; display remaining main tokens | `test_manual_reserve_remains_an_open_holding_after_all_auto_exits` |
| Medium/high — RPC thundering herd | Each 10s heartbeat could spawn a second post-PASS audit before the previous one finished; first 100 PASSes were repeatedly audited | Only one in-flight audit job, maximum 12 recent PASSes per pass, rotate by persisted last-audited timestamp | `test_slow_post_pass_audit_never_spawns_overlapping_rpc_jobs`; `test_post_pass_audit_updates_last_checked_for_fair_rotation` |
| Operator risk — oversized first test | Default new campaign is 5 × £0.10 and permits up to 50 sacrificial orders; a copied default is inappropriate for an initial microtest | `template --micro-test` creates **1 × £0.05**, sacrifice **ON**, **1 × £0.01** maximum, **£0.06 nominal** spend limit, original 500-block wait and existing exit ladder | `test_one_position_micro_template_is_capped_and_sacrifice_on` |
| Operator risk — no offline preview | Before this patch, campaign validation required choosing a live arm path | `check --config` validates settings and prints nonsecret wallet-free, network-free summary; prominently notes nominal cap excludes gas | `test_check_validates_config_without_arm_wallet_or_processes` |
| Verification gap — no actual-provider evidence | Synthetic source logs and fake transaction receipts demonstrate software integration but not live V4 decoding, provider throughput or real fills | `run_concentration_shadow.py` starts **only detector + concentration selector**, records actual provider logs/decisions and p50/p95/p99/worst latency with counts; no wallet, signer, execution engine or transaction broadcast | `test_shadow_operator.py`, static command audit; **actual shadow run not performed yet** |

## Invariants preserved

- Original detector, 5-second/buying-block selector, `peak_tracker.py`, route/ABI builders, `wallet_transactions.py`, sacrifice transaction and survival logic, and configured exit calculations are **not rewritten**.
- Swap quoted proceeds and peak multiples never become cash profits by themselves.
- V3/V4/Pons route restrictions and the original Pons origin exclusion remain in the execution engine.
- Pending/unconfirmed signed transactions are not silently dropped or double-broadcast with new nonces. A stuck nonce is a manual-reconciliation blocker.
- New campaigns default to sacrifice ON, and only the operator's frozen campaign manifest selects a policy.
- Restarts do not re-open closed intake. Manual reserve is recorded as an open holding until a receipt-proven sale.

## Verification gates for the one-position test

| Gate | Status | What constitutes pass |
| --- | --- | --- |
| D1 — code, baseline and synthetic integration | **Passing on reviewed CI commit** | Targeted, historical and complete Python 3.12 tests + safe terminal command lint |
| D2 — captured mainline replay | **OPEN** | Replay actual Robinhood V4 Initialize and Swap logs from the same pool through this implementation; compare first-trade timestamp, exact raw buy amounts, canonical block coverage, PASS/FAIL and reorg handling. Historical library archives were located but source bytes could not be materialized in this environment |
| D3 — real read-only network | **OPEN** | Chain ID 4663; PoolManager Swap ABI from real provider; HTTP+WS compatibility; live V4 two-way quote/ABI `eth_call`; p50/p95/p99 selection lag, RPC error rate, no-data pool load and backlog under actual launch bursts |
| Wallet/nonce and exit operator | **OPEN on target laptop** | Existing encrypted keystore matches frozen public wallet, enough balance for purchase PLUS gas, no overlapping older executor, funded permissions, live-ABI approval/sale verification and a working restart/manual-exit procedure |
| D4 — funded micro round trip | **NOT AUTHORISED / NOT RUN** | Only after separate explicit user approval: confirmed sacrifice receipt, configured wait, confirmed £0.05 main buy token delta, actual sale receipt and net GBP-equivalent reconciliation |
| D5 — statistical profitability | **NOT ESTABLISHED** | Multiple independent paid-for cohorts, full net cash accounting and extreme-winner retention; no claim from historical quote peaks |

**Timing constraints to measure, not silently relax:** 30-second concentration observation; an actionable PASS no more than 15s after the window; 3s from PASS to sacrifice commitment; a separately configured 500-block survival gate by default; 3s from pass of survival gate to main buy. Latency from read-only log coverage and exact quote/estimate/signing steps must be measured against these clocks. A technically correct selector that misses the sacrifice deadline is not a trading success.

**Budget caveat:** `maxNominalSpendGbp` limits nominal input stakes/sacrifices in the transaction journal, **not total ETH spent including network gas, approvals or slippage**. Do not describe £0.06 as an all-in financial ceiling. Actual costs must be reported from confirmed receipts.

## Operator: fresh installation with the NEW Alchemy account (no prior folder required)

The previous instructions referenced `~/blackswanRH_guarded/.env`, which may contain **old Alchemy credentials**. Do not use it. The canonical **NEW** credentials source previously verified on Robinhood Chain 4663 is:

`~/blackswan-modular/newalchemy.txt`

The following creates a completely new `~/blackswan-concentration-audit` directory and a **different** restricted environment file outside Git:

```bash
test -f "$HOME/blackswan-modular/newalchemy.txt"
git clone --branch fix/explicit-new-alchemy-setup-20261124 https://github.com/Ld0g22/Blackswan.git "$HOME/blackswan-concentration-audit"
cd "$HOME/blackswan-concentration-audit"
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements_live.txt
.venv/bin/python tools/setup_new_alchemy.py --credentials "$HOME/blackswan-modular/newalchemy.txt" --output "$HOME/.config/blackswan/concentration-new-alchemy.env"
```

The setup utility accepts both HTTP/WSS URLs from `newalchemy.txt` (or its key), verifies **both** using the real provider and checks `eth_chainId == 4663` and a WSS `newHeads` subscription. It refuses conflicting HTTP/WSS API keys, non-Robinhood-Alchemy hosts, insecure schemes, an existing environment with different contents, and outputs no secret URL/key. It never touches the earlier credentials, old installations or keystores. The new env is private (`0600`). If the canonical source file no longer exists, locate or restore the NEW credentials **locally** before continuing; do not paste API keys into chat.

Run the read-only detector + concentration selector against this exact provider source:

```bash
.venv/bin/python run_concentration_shadow.py run --env "$HOME/.config/blackswan/concentration-new-alchemy.env" --credentials-source "$HOME/blackswan-modular/newalchemy.txt" --duration-seconds 180
```

This runner refuses missing `--env`, a source mismatch and an old/incorrect environment **before it starts either worker**. It starts no wallet, signer or execution process, and moves no money. It creates a fresh `data/concentration_shadow_*` directory and prints the real location of `summary.json`, `detector.sqlite3` and `selector.sqlite3`. A three-minute observation with no genuine eligible launch does not establish selector completeness. The source file and the output env remain outside the repo. Check Alchemy provider usage on its dashboard; no internal CU estimates are added.

The separate **read-only** exact calldata/transaction-preflight checks must use the SAME new file:

```bash
.venv/bin/python probe_live_execution.py --env "$HOME/.config/blackswan/concentration-new-alchemy.env" --wallet-file "$HOME/Desktop/wallet.json"
.venv/bin/python lamb_readonly_preflight.py --env "$HOME/.config/blackswan/concentration-new-alchemy.env" --wallet-file "$HOME/Desktop/wallet.json"
```

These use the public wallet identity and dummy signer for ABI testing; neither broadcasts. A missing keystore or incorrect RPC returns an error; do not infer success.

Only after read-only chain/quote evidence passes, create an **unarmed** one-position concentration campaign configuration:

```bash
.venv/bin/python run_configured_campaign.py template --selector concentration --micro-test --wallet-address YOUR_PUBLIC_ADDRESS --config concentration_micro.json
.venv/bin/python run_configured_campaign.py check --config concentration_micro.json
```

Future `start` and `resume` explicitly require both `--env` and matching `--credentials-source`, as well as separate `--arm-live` user authorisation; there is **no fallback to old provider defaults** in the new launcher. No funded start command is provided here: read-only live checks, transaction preflights and user authorisation remain release gates.

## Recovery / rollback

The implementation sits only on `audit/concentration-live-readiness-20261124`, branched from `concentration/build-one`. No live data was modified. A rollback before deployment simply means not installing or merging this branch. Once running a funded campaign, **do not delete its SQLite folders or resume with a mismatched manifest**. Keep exits active when intake is closed and reconcile uncertain signed transactions before any resubmission.

## Independent review status

Code-source adversarial checks and failing-first tests found and addressed the issues above. A separate reviewer and observed live provider behavior have **not** independently signed off the whole system. The release conclusion stays **NOT YET CLEARED FOR FUNDED TEST** until D2/D3 plus the target-wallet checks are recorded.
