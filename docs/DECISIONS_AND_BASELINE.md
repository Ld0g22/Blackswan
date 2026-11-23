# Decisions, invariants and baseline discovery register

**Orientation state:** 2026-11-23. This file records user's stated strategic intentions, not unverified technical facts. Only subsequent user decisions can supersede a locked direction. Amend with date, proposed change, evidence, impact and decision; do not silently overwrite past entries.

| ID | Direction | Status | Evidence required before implementation |
| --- | --- | --- | --- |
| DIR-01 | Focus on **net realised profitability**, asymmetry and executable convexity | Orientation locked | Define comparison metrics and per-trade cash reconciliation |
| DIR-02 | **Original working detector** is candidate baseline | Baseline identity pending | Commit, run log, gap and timeliness evidence |
| DIR-03 | **Earlier peak tracker + execution** running with buying-block filter for >1 week is candidate baseline | User-reported, not independently reverified | Specific source/hashes, original campaign configs, receipts and observations |
| DIR-04 | Future **buying-block and concentration selectors** interchangeable | Orientation locked; no implementation authorised | Interface test with unchanged executor, same replay |
| DIR-05 | **Sacrifice is part of execution**, not selector | Orientation locked | Execution state machine, receipt/guard tests |
| DIR-06 | Original flexible **campaign size and stake values** maintained | Orientation locked | Recover historical CLI/config and defaults before designing changes |
| DIR-07 | Monitor modelled on effective **old monitor** | Orientation locked | Original monitor file/screenshot, operator feedback, data field accuracy |
| DIR-08 | No new deployment, trading or architecture changes from this standards task | Locked now | No source changes, branch changes, or wallet transactions |
| DIR-09 | Never send interactive-terminal `set -e`/shell-exiting equivalents | Hard invariant | Automatic scanner + manual review of every user shell command |
| DIR-10 | Engineering partner must **reason, challenge, solve and verify**, not defer or claim premature success | Hard invariant | Review/report evidence on every material change |

## Future baseline discovery card (complete when authorised)

- Repository name / URL: **unknown here**.
- Immutable working commit for detector: **unknown**.
- Immutable working commit for peak tracking/execution: **unknown**.
- Exact buying-block filtering code/version: **unknown**.
- Historical campaign IDs, date ranges and run evidence: **to retrieve**.
- Monitor version/file and display reference: **to retrieve**.
- Tested RPC/network assumptions and dependencies: **to retrieve**.
- Actual source-defined campaign knobs and defaults: **to retrieve**.
- Last observed successful real buy and sell with hashes: **to verify from evidence**.

Do not fill missing fields using guesses or later failing branches. Once proven, freeze a copy and produce a baseline regression report. Only then make the selector modularisation change, if requested.

## Change decision entry format

**YYYY-MM-DD | ID | Proposed change | Why it advances realised profit, reliability or speed | Evidence and alternative | Scope impact | User decision | Tested commit**.

Changes inside an authorised engineering task need not be individually escalated if they preserve all locked strategy and operational invariants; record technical decisions in the change report. Ask before changing investment policy, stakes, exits, universe or wallet authority.