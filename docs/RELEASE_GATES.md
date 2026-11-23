# Release gates — actual evidence, minimal ceremony

A check mark is not evidence. Each gate needs an observable pass/fail result, a specific command or source record, and (for material changes) an independent adversarial review. Report gaps concisely.

## Gate A — scope and baseline

- [ ] User's desired outcome, unchanged invariants and material decisions captured exactly.
- [ ] Correct historical baseline source/commit and configuration identified; rollback and data safety defined.
- [ ] Diff limited to requested work plus demonstrated necessary supporting fixes; no hidden policy/wallet/exit changes.
- [ ] Architecture respects detector → pluggable selector → stable execution (sacrifice inside execution) boundaries where applicable.

## Gate B — behaviour and failure resistance

- [ ] Regression tests fail on old bug, pass with fix; changed behaviour tests exist.
- [ ] Full test suite runs on the actual commit and exits successfully; failures/skips explicitly listed.
- [ ] Relevant integration replay with captured events and/or fork supports buy, sell, recovery, timeout, rate limits and every newly supported pool route.
- [ ] Actual quote/ABI values and contract assumptions checked against source and deployed chain; no test fixture masquerades as on-chain success.
- [ ] Performance difference measured under representative feed + open-position load, with timestamped misses and p95/p99.
- [ ] Monitor, ledger and reconciliation tell same story for closed, open and unknown intents.

## Gate C — security and operator experience

- [ ] Signer policy, nonce ownership, spend limits, approvals and restart behaviour reviewed.
- [ ] Secrets redacted; no new unnecessary wallet/token-funding obligations.
- [ ] Operator commands reviewed manually and with `tools/check_terminal_commands.py`: **no shell-exiting constructs**, broad destructive cleanup or accidental live arm.
- [ ] Installation, verification and authorisation to trade are separate operations.
- [ ] RPC and environment configuration validated early with actionable, nonsecret errors.

## Gate D — claiming operational readiness

Use exact tier; do not substitute one for another:

- **D0 — designed:** architecture/spec/code review only.
- **D1 — local tested:** targeted + full suite, static/ABI checks, controlled RPC mocks.
- **D2 — integration tested:** captured mainline event replay, local/fork simulation and operational fault tests.
- **D3 — read-only network verified:** real Robinhood RPC, deployed contract bytecode, live quote/estimate checks; no signed live round trip.
- **D4 — real-money round trip verified:** *explicitly authorised* tiny real buy, confirmed receipt/token delta, confirmed real sell and net proceeds, restart/monitor verified where relevant.
- **D5 — campaign validated:** agreed campaign outcome metrics, repeated trades, complete accounting and error/performance evidence.

A new route at D1/D2/D3 is **not proven for real-money trading**. An exact feature may be D4 while the whole multi-asset system remains D2; report per-feature evidence, not a single inflated status.

## Gate E — release decision

- [ ] Reviewer tried to falsify correctness, timing and financial safety, recording counterexamples.
- [ ] Every critical/high issue either fixed or explicitly prevents live readiness.
- [ ] Change report states actual verified tier, results, residual risk, whether funds are exposed, operator steps and rollback.
- [ ] User explicitly authorises any money movement or campaign launch; no authorization inferred from request to code/install.

## What to report, not another diagnostic dump

Use `templates/CHANGE_REPORT.md`: a few lines in chat with files/evidence linked; full logs and tests in an artifact or repository. Failures must state root cause if known and the **one** missing proof before the next risk-bearing action. Do not deliver a 100-line diagnostic instead of a fix.

## Automatic vs manual gates

- Automated tests can enforce config, interfaces, shell command safety, regression evidence and static constraints.
- Manual/independent review is needed for wallet/risk/ABI logic and interpretations of recorded evidence.
- Only on-chain receipts and balance/proceeds reconciliation can establish real round-trip execution.
- No single gate eliminates all risk; each gate narrows specific failure modes.
