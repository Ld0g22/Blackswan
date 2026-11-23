# BlackSwan change report — fill in for material engineering work

**Goal / acceptance condition:**

**Version and baseline:** old commit ___ → candidate commit ___; repository ___; clean/dirty state ___

**What changed:**

**What remains unchanged (detector, selector thresholds, execution, stake, exits, wallets, monitor):**

**Observed defect and root cause:**

**Evidence:**

| Check | Environment | Exact command / evidence reference | Result | Limit |
| --- | --- | --- | --- | --- |
| Regression test red → green | | | | |
| Full suite | | | | |
| Historical replay / integration | | | | |
| RPC/contract/quote | | | | |
| Live buy receipt + token delta (if authorised) | | | | |
| Live sell receipt + net GBP (if authorised) | | | | |
| Latency comparison | | | | |
| Operator terminal safety | | | | |

**Verified tier:** D0 / D1 / D2 / D3 / D4 / D5; separate tier by new route when different. Never call D1 fully working live.

**Financial exposure / security review:**

**Known failures, unknowns and hard blockers:**

**Operator steps:** inspect, install, verify and launch are separate. No shell-terminating commands. Quote only directly usable, verified paths; nothing invented.

**Rollback and recovery:**

**Adversarial reviewer finding and final decision:**
