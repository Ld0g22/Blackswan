# Manual sale verification

27 November 2026. Baseline source `12ae5070a8dea42ceda189ac6463763997b94028`.

- Targeted manual sale tests, 10 passed.
- Complete repository unittest discovery with aiohttp 3.13.3, eth-account 0.13.7, eth-abi 5.2.0, eth-utils 5.3.1 and rich installed, 393 passed in 12.328 seconds.
- Independent review identified payout-pending status loss, credit without verified debit, per-fill FX reuse and slow upfront venue discovery. Corrected these and added regression tests for payout state, verified credit and per-fill FX. Historical venue discovery now runs per token with a 20-second bound; sale processing has a 60-second bound. The existing journal preserves unresolved signed transactions.
- Current scope uses the operator's 2p gross quote cutoff. Separate fee profitability or aggregate gas budgets were removed following the user's clarification.
- Provider wallet coverage, router deployment bytecode, current wallet identity, actual quotes and actual transactions have not been verified live in this workspace. No mainnet transactions were submitted.
- Explicit limitations are documented in MANUAL_SALE_README.md, including unavailable native traces, unverifiable index watermarks, provider refusal of historical v4 pool discovery and simulation failures after a successful full-size quote.

## Build two repair

- Reproduced HTTP 429 with JSON null, null RPC results and malformed token rows. Added index response validation and explicit incomplete coverage.
- Added closing-inventory failure recovery with a full execute/resume regression. No transaction signing occurs in that fixture.
- Bounded concurrent pool lookup preserves discovered venues through failed v4 history. Empty ABI responses are local discovery failures.
- Null receipt evidence remains accounting-pending. Existing 2p cutoff and current Alchemy config are retained.
- Targeted manual tests pass, 20 tests. Full repository suite passes, 403 tests in 12.311 seconds. Live provider and wallet validation remain pending.

## Build three transport redesign

- Replaced direct manual RPC calls with one paced, serialized session shared by inventory, signing dependencies, quote reads and signed broadcasts. Campaign RPC modules remain their existing source.
- Regression coverage includes HTTP 429, JSON-RPC 429, null responses, 503 recovery, 20 concurrent reads, same-byte signed retransmission, deterministic revert without retry, bounded outage circuit, and no quantity reduction after throttling.
- Targeted tests pass, 28 tests. Full repository suite passes, 411 tests in 12.518 seconds.
- Live verification against the operator’s Alchemy account and liquid token remains pending.
