# Build One — Robinhood launch detector

**Source of truth:** `Ld0g22/BlackswanNovember`, detector-only snapshot `108f4d5abfd45edc80051a55c9beb72412a79ceb` dated 2026-11-06.
**Source file:** `detector.py`, with original regression suite `test_detector.py`, pinned `requirements.txt`, `DETECTOR_README.md` and `.gitignore`. Compare Git blob SHA to original before changing any source.
**Branch:** `build/one-detector`. On 2026-11-23, original code was imported with matching Git hashes, then intentionally amended for proven detector defects: SQLite outbox atomicity and explicit recovery-health reporting. Original upstream remains unchanged and recoverable. No selector, signer, sacrifice, peak tracker or execution code. **No funds move through this module.**

## Intended behaviour

One read-only launch-event service for Robinhood Chain 4663: Pons V2 factory `TokenLaunched`, Uniswap v3 factory `PoolCreated`, and Uniswap v4 PoolManager `Initialize`. It uses three exact WebSocket subscriptions, concurrent HTTP `eth_getLogs` fast recovery and persistent SQLite WAL outbox `event_log`. It records decode failures and gaps, supports reorganisation removal/readdition, and exports JSON `schemaVersion:1` including full raw log provenance.

The detector is a **producer**, not a selector, peak tracker or transaction execution engine. Consumers own their persistent cursor; consume `event_log` in increasing `seq` and checkpoint after applying a row. They must handle `removed=true` and duplicated token identities across pools. Do not consume the human `tail --follow` console as a time-critical feed.

## Validation levels

- **D0:** imported with provenance and source identity.
- **D1:** original tests and project tests pass on Build One's exact commit. Regression evidence: injected SQLite outbox write errors originally left event_state committed without an output row, and a failed backfill originally had no machine-readable recovery status. Added failing tests, then made both tests pass. See `tests/test_detector_journal_atomicity.py` and `tests/test_detector_recovery_health.py`.
- **D2:** integration replay, interruption/429/reconnect/resume, reorg and outbox consumer tests pass.
- **D3:** read-only actual Robinhood RPC verification, recent matching-chain-log comparison and latency/coverage measurements, plus verified source deployments.
- **D4/D5:** not applicable to detector alone; end-to-end trading requires separate permissions and components.

Do not declare “flawless” or D3 from unit tests. CI is local mock validation only.

## Important audit targets

1. **Incomplete recovery while live:** `status=LIVE` means WebSocket transport only. A failed or cancelled backfill now exposes `recoveryStatus=INCOMPLETE`; `recoveryStatus=COMPLETE` means **the latest bounded recovery pass finished**, not that all historical gaps are healed. Check `reportedGaps` and `lastBackfilledHead` separately.
2. **Resume cursor versus observed head:** do not mistake a periodic head observation for the highest block actually reconciled.
3. **Long outages:** 600-block lookback only, so old gaps must be explicit; does not promise full historical completeness.
4. **Canonicality near the head:** avoid orphaning valid logs due to an indexing delay; test provider stale responses.
5. **Health and latency:** active journal counts are not unique token counts; health must expose head lag, age of last event and confirmed coverage.
6. **Resource/policy:** historical estimated CU accounting is preserved in source for provenance, but may not be accurate, account-wide or aligned with future architecture; do not use it as proof of remaining provider budget.

## Safe local testing (no RPC, no credentials, no live traffic)

```bash
python3 -m pip install -r requirements.txt
```

```bash
python3 -m unittest -v test_detector
```

```bash
python3 -m unittest discover -s tests -v
```

The dependency installation is a local environment action; prefer a dedicated virtual environment. Any read-only *live detector run* requires separate provider/config verification and express operator action; do not automatically launch from install or tests. Run files in the repository root, with `--env` and `--db` paths explicitly reviewed.

## Rollback and separation

The upstream immutable detector source remains available at the recorded commit. Reverting Build One affects only the proposed branch. Existing data, wallets, old experiments and running processes are untouched. Keep `data/`, `.env`, WAL and keystores out of Git.
