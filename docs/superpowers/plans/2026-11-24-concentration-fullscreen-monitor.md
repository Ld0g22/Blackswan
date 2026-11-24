# Full-screen Concentration Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the original buying-block-era control room design with comprehensive, accurate data for Concentration Build One, ready before a separately approved funded test.

**Architecture:** Keep execution code and journals untouched. Extend the existing `live_dashboard.snapshot` with read-only selector/observation/financial joins in `monitor_snapshot.py`; present it with Rich standalone page layout in `monitor_view.py`; drive it with `concentration_monitor.py`.

**Tech Stack:** Python 3.12, stdlib sqlite3, Rich already in repository requirements, existing campaign/selector/executor SQLite schemas.

**Spec:** `docs/superpowers/specs/2026-11-24-concentration-fullscreen-monitor.md`

## Global Constraints
- Read-only; no .env, RPC, keystore, signing, transactions, dotenv or changed database schema.
- No new runtime dependencies; use existing Rich and standard Python.
- Full 42-character token addresses, full 66-character pool IDs and 66-character transaction hashes; never ellipsize identifiers.
- Realised cash comes only from qualified, confirmed transaction finance/receipt entries; live quotes and historical peaks separate.
- Empty/locked/retired states are visible and do not kill the monitor.
- No private paths or secrets in Git; no live campaign launched.

## Review Focus
- The old snapshot opens earlier legacy tables; missing partial DB must yield warnings instead of app crash.
- `bestGbp` is historical peak, not latest quote; `observations` provides latest successful value only for original receipt size.
- Selector PASS may not be admitted to executor, and a selection may be reorg-revoked; rows must never mask that.
- Native ETH receipts and missing FX are not zero proceeds. A 5% manual reserve remains open.
- Full ids must not be cropped by Rich with limited terminal width; fall back to word-folding.

## Tasks
### Task 1. Schema-contracted snapshot tests and implementation
- [ ] Write tests that construct the real `ExecutionStore` / `SelectorStore` schemas, missing/shadow-only run folders and authoritative receipt/finance rows.
- [ ] Run the tests red because `monitor_snapshot` is not implemented.
- [ ] Implement `monitor_snapshot.py` backed by `live_dashboard.snapshot` and read-only independent joins.
- [ ] Run focused tests green; verify no files/rows modified.

### Task 2. Full-screen Rich views
- [ ] Write tests that assert all 5 views render, include complete identifiers, don't misstate quote/cash, retain all configured stages and report unknown fields visibly.
- [ ] Run red; implement `monitor_view.py` responsive page layout, old color/Panel conventions and overflow folding.
- [ ] Run green under CI and render both wide and narrow `Console` widths.

### Task 3. Stable CLI/lifecycle
- [ ] Test `--help`, `--once`, explicit `--run-dir`, no process / wallet/network use, empty folder and ctrl-C cleanup.
- [ ] Implement `concentration_monitor.py` using Rich Live alternate screen, 0.5s refresh, 1–5/q keyboard, explicit stale badge on read error.
- [ ] Test green and document terminal invocation for a new `configured_v3_*` run.

### Task 4. Regression and release evidence
- [ ] Add tests to the existing GitHub Actions targeted workflow and compile checks; verify full suite, both simulator and historical 500-block gates.
- [ ] Verify old buying-block reader and original `live_dashboard.py` still work.
- [ ] Produce exact run commands and explain missing live fields (no real receipt/quote yet), without starting a trade.
