# Build One — release evidence and handoff (23 November 2026)

**Component:** read-only Robinhood Chain launch detector. **Release tier:** **D1 (local tested)**. **NOT LIVE VERIFIED.** The code is not authorised to trade and contains no signer, selector, executor or funded-wallet interface.

## Provenance

- Upstream frozen repository `Ld0g22/BlackswanNovember`, revision `108f4d5abfd45edc80051a55c9beb72412a79ceb` (2026-11-06), originally extracted as unchanged detector-only implementation.
- Initial imported blobs `detector.py`, `test_detector.py`, `requirements.txt`, `DETECTOR_README.md` and `.gitignore` all matched exact upstream Git blob SHA.
- This Build One intentionally changes **only detector transaction atomicity and recovery-health reporting**, plus supplementary tests, documentation, CI and ignore rules. Details below.

## What we reproduced, changed and validated

1. **Confirmed lost-event bug:** in the original SQLite autocommit connection, if `event_state` updated but `event_log` insertion failed, state remained committed and a retry was silently dropped. Two database-trigger failure tests initially failed on the unmodified source. Fixed by using a transactional SQLite connection (`DEFERRED`), so a failed write rolls back both records.
2. **Confirmed missing health evidence:** an interrupted `eth_getLogs` recovery was logged as a gap, but `stats()` exposed no recovery outcome; `status=LIVE` only denoted WebSocket transport. Added machine-readable `recoveryStatus` (`UNKNOWN`, `IN_PROGRESS`, `COMPLETE`, `INCOMPLETE`) and `lastBackfilledHead` to health, and print recovery status in periodic health output. Two new tests exercise failed and completed backfill.
3. **Preserved:** exact contract/topic subscriptions, Pons/V3/V4 ABI decoders, `schemaVersion:1` event contract and JSON outbox, single-writer locking, recovery batch size/overlap, reorg logging, CLI and no-trading operation. No selector or execution added.
4. **CI:** GitHub Actions, Python 3.12, pinned aiohttp install, source compilation, **16/16 original tests passed** and **19/19 project/regression tests passed**, including all four new detector regression tests. Final verified Build One branch commit: `75f6620bf16f07519046d7c50f7736f348fef2da`. Workflow run `45278459642`: success.

## Adversarial limitations — do not obscure these

- **Read-only chain completeness unverified:** we have not connected an actual funded/user-specific Alchemy WSS+HTTP account here, measured head-to-event delay, sampled independent same-block logs, or validated actual provider rate limits. D3 remains **FAILED / NOT YET EVIDENCED**, rather than a guessed pass.
- **Limited recovery:** the original bounded fast recovery is **600 blocks**, overlapping **100 blocks**. Outages older than the window create persistent recorded gaps; Build One now surfaces incomplete recovery, but does **not** automatically fill all old gaps. `recoveryStatus=COMPLETE` means the latest *bounded recovery pass* completed, not that all historical coverage is complete. Consumers must check `reportedGaps` and original log evidence.
- **Provider/indexer discrepancies:** synthetic tests cannot establish that a stale indexing result cannot cause an incorrect reorg removal. Check against independent source before D3.
- **Historical CU estimator retained from the original:** it is a *local estimate*, not provider truth, and introduces SQLite usage writes. It does not account for other processes sharing the API key. The user previously preferred no internal CU accounting; replace or remove only under a separately measured, regression-tested performance change rather than silently changing the recovered runtime now.
- **Latency not yet benchmarked on Robinhood:** 20–50 ms outbox polling is a suggested consumer cadence, not a measured end-to-end detection latency. No claim about 5-second purchase deadlines.

## Safe next verification — no trading

Use a separately validated RPC configuration, start a **read-only** detector with a fresh data path under explicit operator control, observe actual events for all relevant sources, and compare event identities/ordering with independently fetched matching logs over the same block window. Force a recoverable disconnection, observe durable replay and no unexplained gaps; measure p50/p95/p99 event delay and provider errors. Do not attach a selector/executor or run a live-money campaign to perform this acceptance.

## Rollback

- Original immutable source: `Ld0g22/BlackswanNovember@108f4d5abfd45edc80051a55c9beb72412a79ceb`.
- Build One branch and upstream snapshots keep full provenance.
- Rollback must not overwrite running database files or change a running process behind the operator. No services or wallets were touched during this release.
