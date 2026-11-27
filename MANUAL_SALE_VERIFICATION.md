# Manual sale verification

27 November 2026. Baseline source `12ae5070a8dea42ceda189ac6463763997b94028`.

- Targeted manual sale tests, 10 passed.
- Complete repository unittest discovery with aiohttp 3.13.3, eth-account 0.13.7, eth-abi 5.2.0, eth-utils 5.3.1 and rich installed, 393 passed in 12.328 seconds.
- Independent review identified payout-pending status loss, credit without verified debit, per-fill FX reuse and slow upfront venue discovery. Corrected these and added regression tests for payout state, verified credit and per-fill FX. Historical venue discovery now runs per token with a 20-second bound; sale processing has a 60-second bound. The existing journal preserves unresolved signed transactions.
- Current scope uses the operator's 2p gross quote cutoff. Separate fee profitability or aggregate gas budgets were removed following the user's clarification.
- Provider wallet coverage, router deployment bytecode, current wallet identity, actual quotes and actual transactions have not been verified live in this workspace. No mainnet transactions were submitted.
- Explicit limitations are documented in MANUAL_SALE_README.md, including unavailable native traces, unverifiable index watermarks, provider refusal of historical v4 pool discovery and simulation failures after a successful full-size quote.
