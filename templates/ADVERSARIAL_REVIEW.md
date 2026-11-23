# Independent engineering / statistical review prompt

Read `AGENTS.md`, `docs/DIRECTION_AND_BOUNDARIES.md`, `docs/ENGINEERING_STANDARD.md`, `docs/STATISTICAL_STANDARD.md`, `docs/OPERATIONAL_SAFETY.md`, `docs/RELEASE_GATES.md` and the complete proposed diff/test outputs. Do **not** assume developer claims are true. Treat missing repo content, transaction receipts or logs as *missing*, not as evidence of success.

## Objective

Try to **invalidate** the proposed change on correctness, live-money safety, timeliness, statistical value and compatibility with the historical working baseline. Aim to prevent trading losses and false completion claims, not to add process overhead.

## Required attacks

1. Determine whether the chosen baseline is **the actual old working detector + peak tracker + executor with buying-block filter**, rather than a later rewrite. Are commit provenance and historical results evidenced?
2. Does the change silently alter detector selection/eligibility, timing, stake, wallet preparation, position caps, sacrifice behaviour, exit logic, peak tracker or monitoring?
3. Can changing between buying-block and concentration selectors alter the executor? Is sacrifice owned by execution and idempotent?
4. Can a position be double-bought, funds be sent unexpectedly, an approval remain dangerously broad, or a sale be lost under timeout, reorg, rate limit, RPC rejection, process crash or restart?
5. Does the new route work **both directions at the actual stake** on the specified chain and pool? Are ABI, selectors, hooks, intermediate currencies and receipt accounting correct? Distinguish mocked tests, read-only RPC and actual receipts.
6. Can a candidate die while waiting behind a quote, research call, monitoring loop or recovery operation? Compare to measured historical latency and quote coverage.
7. Is a paper multiple mistaken for collectible GBP? Are hidden fees, slippage, censoring, selection bias, overfitting or tail-sample uncertainty masking a losing strategy?
8. Does every proposed terminal command avoid shell exit/altered shell options, broad destructive operations, hidden live arm, bad paths and destructive git actions?
9. Do error messages identify actual root causes without leaking secrets? Is rollback usable and does it preserve outstanding positions?
10. What concrete, minimum test would falsify the strongest developer claims?

## Output — concise, falsifiable

**Verdict:** REJECT / MORE EVIDENCE / CONDITIONAL PASS; and readiness tier D0–D5 **per relevant feature**.

**Top blockers:** most dangerous first, each with file:line or an exact observed record, impact on money/timing/correctness, and a reproducible proof/falsification test.

**Unproven claims:** list in one section, distinguishing missing from contradicted evidence.

**Minimal change/test to pass:** specific acceptance checks, without redesigning unrelated parts.

Never approve a real-money launch solely because the full unit suite is green. Do not invent evidence or require unnecessary infrastructure or bureaucracy.
