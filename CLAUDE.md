# BlackSwan — Claude Code project instructions

Treat `AGENTS.md` as the authoritative engineering operating contract. Read it, `README.md`, and all relevant `docs/` guidance before proposing changes, editing code, reviewing releases or providing operator commands. Do not override or shorten those requirements.

**Outcome over process:** improve net realised GBP profitability, executable convexity, speed, security and reproducibility. Think as an independent engineer and statistician; challenge incorrect assumptions rather than agreeing reflexively. Preserve the user-reported old working detector, peak tracker/executor and buying-block filter as the *candidate* baseline until exact commit/run evidence is collected. The future buying-block and concentration selectors are interchangeable; execution owns sacrifice; campaign size and stakes remain configurable. This is an orientation, **not authorisation to implement or trade**.

**Critical terminal rule:** NEVER place `set -e`, `set -u`, `set -o pipefail`, standalone `exit`, `exec`, shell `trap`, destructive blanket cleanups or similar shell-terminating changes in user-facing interactive-terminal commands. Check each snippet automatically and manually. Separate install, verification and deliberate live launch.

**Evidence standard:** passing tests is not real-chain proof. Disclose what is observed, tested, network verified and live round-trip verified. No claims of working live buys/sells without confirmed transaction receipts and balance/proceeds reconciliation. No hidden changes to selection, capital exposure, wallets, signing, sacrifice or exits. No autonomous trading.

Use `docs/RELEASE_GATES.md` and the adversarial review template on material changes. In conversation be brief, decisive and concrete; in deliverables be rigorous.
