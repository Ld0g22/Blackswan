# BlackSwan Concentration Build One — full-screen operator monitor

**Build:** `feat/concentration-fullscreen-monitor-20261124`  
**Scope:** Observer only. Existing detector, concentration/buying-block selectors, sacrifice, execution, peak tracker, signer, transaction journal, treasury and sale rules are unchanged.  
**Safety:** Does not read Alchemy credentials or wallet secrets. Does not call RPC, sign, send orders, fund wallets or start/stop system workers.

## What the operator actually sees

The design deliberately reuses the original buying-block monitor's rounded Rich panels, cyan/green service status, purchase scorecard, sacrifice display, position rows, sale receipts and alerts, while making them useful for the **30-second first-trade concentration** selector. **No token, pool or transaction hash is intentionally shortened.** On narrower terminals identifiers fold over multiple lines; use keys to scroll rather than hiding important fields.

### Fixed header and scorecard

- Full campaign run path, UTC capture timestamp, mode (`SHADOW`, `CAMPAIGN`, `WAITING_FOR_NEW_CAMPAIGN`, etc.), public wallet, selector policy, actual selector-verified chain ID vs configured-but-unverified chain ID.
- PASS/FAIL/UNKNOWN decisions; purchased/open/max positions; actual signed sacrifice and main-buy intents, nominated GBP spend and maximum **excluding gas**, confirmed sale proceeds and cash P/L **only when reconciled**, number of unsettled transactions.
- Responsive scorecard uses 8 columns on large monitors, 4 on mid-size and 2 on small terminals. Full data is always in detail screens and the report.

### Screen 1: Overview

- Detector and selector health, cursor backlog, failures, actual local PIDs where discoverable and honest liveness caveats.
- Frozen main/sacrifice stake, configured 500-block (or second) survival wait, selection deadlines, exact ladder targets and nominal cap (gas excluded).
- Most recent concentration decision, current pre-buy sacrifice, open holding status and critical alerts.
- This is the *at-a-glance* view; navigate to full detail screens when investigating.

### Screen 2: Opportunities / decisions

- Full selection ID, token address and V4 pool ID, detected timestamp and source.
- PASS/FAIL/UNKNOWN, precise reason, separately tracked executor status and **why a PASS did not become a buy**.
- First canonical trade, exact 30-second window end, observed swaps, buy transaction count, largest transaction buy share, raw buy values and largest-buy tx hash.
- Evidence completeness, selection latency, reorg revocation and pending/no-trade states.
- A PASS still in the selector is **not** counted as a purchase.

### Screen 3: Sacrifices / positions / quotes / exits

- **Sacrifice visible BEFORE a position exists:** stage, quote/send/receipt state, full hash, receipt block, purchased probe quantity, survival time, remaining blocks when a verified head exists, failure reason.
- Main buy receipt-confirmed quantity, expected buy quote token quantity (labelled estimate, never confused with fill), full transaction hash, original token amount, remaining **main** tokens excluding probe, user manual reserve.
- Latest successful full-*entry*-quantity executable quote and current latest attempt/status separately; quote age/block/route/error; historical best full-entry-size quote. **None is current executable liquidation value for remaining holdings**.
- Every configured exit target, %, expected raw tokens, trigger state, pending transaction status, receipt-confirmed sold quantity and full sale hash, actual WETH receipt credit and GBP equivalent only when finance reconciliation supports it.
- Strong warning on a sold flag missing the corresponding sale record. Sale failures show as errors, not synthetic proceeds.

### Screen 4: Transaction journal / actual money

- All recent signed/sent/confirmed/reverted/uncertain transactions with full hash, token and selection ID, reason and receipt status.
- Sacrifice states, **real sale receipts**, native-ETH reconciliation qualifiers, WETH received, verified GBP equivalent or `UNMEASURED`.
- Qualified realised cash and GBP P/L; incomplete records prominently labelled. The nominal budget excludes gas.
- Manual exit requests, fills, errors and remaining token reserve.

### Screen 5: Alerts and service/recovery

- Actual selector heartbeat vs stale/closed intake; current detector head/gaps/decode errors; exact local matching process IDs. No inference that the executor is alive from an old SQLite `ACTIVE` flag.
- RPC/quote errors, detected missed sacrifice/entry deadlines, reorg revocations, held position failures, unreconciled receipts, SQLite reads and manual-order blocks.
- Read-only warnings about a closed source are not blindly treated as a fatal error when the executor has already closed intake to focus on exits.

### What is *not* available from the current journal

- Executable liquidation value for **current remaining quantity**: only the existing price tracker writes **full original purchase quantity** sell quotes. The screen labels remaining-value `NOT MEASURED`; it does **not** multiply peak price by remaining balance.
- Honest live signer/RPC progress if there is no explicit heartbeat. A local PID or an `ACTIVE` database flag is not chain/signer progress.
- Cash proceeds for sales without qualified native-ETH/WETH receipt accounting: show `UNMEASURED`.
- External wallet balances in real time: no wallet or RPC connections are allowed in this monitoring process. Use the main system and separately checked preflights.

## Install into the existing clean audit clone

You already installed this repo and the NEW Alchemy credentials in `~/.config/blackswan/concentration-new-alchemy.env`. Do not clone another copy or touch the credentials.

```bash
cd ~/blackswan-concentration-audit
git fetch origin feat/concentration-fullscreen-monitor-20261124
git switch -c feat/concentration-fullscreen-monitor-20261124 FETCH_HEAD
```

The new monitor adds no dependencies; `rich` is already in `requirements.txt` and your virtual environment. To check this update locally without credentials:

```bash
.venv/bin/python -m unittest -q test_concentration_full_monitor test_monitor_processes test_configured_monitor
```

## Preview the monitor before any money is used

The genuine 24-Nov read-only concentration capture is:

`data/concentration_shadow_20261124T151125Z_11328843`

It contains the 3 completed FAIL decisions observed on your real Robinhood 4663 provider, plus detector events. No execution DB, no main buy, no sacrifices or actual proceeds are expected there; the monitor labels them absent rather than inventing them.

In a large full-screen terminal window on the second monitor:

```bash
cd ~/blackswan-concentration-audit
.venv/bin/python concentration_monitor.py --run-dir data/concentration_shadow_20261124T151125Z_11328843
```

For a **complete** human-readable report to review before looking at the screen:

```bash
.venv/bin/python concentration_monitor.py --run-dir data/concentration_shadow_20261124T151125Z_11328843 --once | less -R
```

Exit `less` with q.

### Run monitor before the *first* funded micro-campaign

This is the most useful mode for our coming one-position £0.05 main / £0.01 sacrifice test. Launch it in the monitoring terminal **first**, without arming trades. It watches for a **new** `configured_v3_*` folder and attaches the moment that campaign directory appears:

```bash
cd ~/blackswan-concentration-audit
.venv/bin/python concentration_monitor.py --follow-new --data-dir ./data --interval 0.5
```

This does **not** start the detector, selector, trader or a campaign. In another terminal, a separately authorised `run_configured_campaign.py start` (not part of these instructions) will create the new run. If that run already exists, instead select its **exact** folder with `--run-dir` or use `--latest`. The monitor clearly displays the chosen directory to avoid confusing it with a historical run. The monitor cannot prevent an actual trading failure—it only shows recorded evidence.

## Navigation

| Key | Action |
| --- | --- |
| `1` | Overview |
| `2` | All loaded selector decisions |
| `3` | Sacrifices, holdings, quote history and exit slices |
| `4` | Financial reconciliation, transactions, actual sales and manual exits |
| `5` | Alerts / health |
| `n` / `p` | Scroll a screenful down / up |
| `j` / `k` | Scroll three lines down / up |
| `g` | Back to top |
| `q` or `Ctrl+C` | Quit the monitor, **not** the trading system |

Additional options: `--view positions`, `--interval 0.5`, `--rows 120`, `--stale-after 180`, `--once`. A high `--rows` loads more historical decision/tx rows but **campaign totals always use all ledger positions and signed intents**, independent of this option.

## Verification and limitations

- Source tests include missing and corrupt SQLite, shadow without an execution DB, immutable read-only reads, long hashes and narrow screens, healthy closed intake, true line scrolling, persistent sacrifice before main buy, missing sale receipts, held manual reserve, 25% evidence, latest-vs-peak, late quote failure, noisy-pool quote starvation, nominal spend with truncated screen rows and actual configured executor simulated sacrifice→main fill→both confirmed exit receipts.
- GitHub Actions run all targeted, historical sacrifice-gate and full Python 3.12 suites plus compilation and operator shell-command lint.
- **Not yet verified on the actual user's large monitor with live funded transactions.** This remains the first on-machine visual smoke check. Code CI is not proof of hardware/terminal UI layout or realised cash flows.
- No new Alchemy CU accounting or estimator was introduced. The monitor makes zero RPC calls.
