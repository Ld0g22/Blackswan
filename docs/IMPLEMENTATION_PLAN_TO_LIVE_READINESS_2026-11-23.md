# Implementation plan — reliable, configurable BlackSwan pipeline

Date: 2026-11-23
Baseline: `Ld0g22/Blackswan` main `28f3c7709296d26f525c8dd538ae8da6fb4b2e69`.
Status: PLAN ONLY, not an instruction to launch or alter existing campaigns.

## Objective and definition of this stage

Produce one functioning Robinhood Chain (4663) chain-event → selectable policy → real sacrifice → survival → main buy → executable peak valuation → threshold sale → receipt-verified net GBP accounting pipeline. The detector, original quote-route selection, transaction journal and swap machinery are the preserved baseline. Each campaign can select its policy and set position count/stake, sacrifice, exit ladder and operational limits without editing code. Sacrifice is ON for new campaigns unless the user explicitly requests OFF. Do not claim profitability before repeated fully reconciled campaigns.

**Scope at this stage:** functionality, safe configuration, reproducible integration and live readiness. Developing the predictive buying-block/concentration selection algorithms is separate; allow a named minimal/test policy for integration. Never run an indiscriminate selector with real money absent a separate, explicit, capped authorisation.

## Verified starting position

- Build One: source-shaped detector emits durable `event_log` records; recovery and reorg tests. Unmodified in this plan.
- Build Two: historical `peak_tracker.py`, `swap_adapters.py`, `execution_peak_routes.py`, `live_executor.py`, `wallet_transactions.py`, `sacrificial_lamb.py`, `probe_buying_blocks.py`, campaign launcher/config/finance and monitor restored. Source and mock integrations run at D1, not proven D4.
- Selector compatibility proof is mocked policy-labelled PASS rows, **not** an actually switchable launcher or implemented concentration selector.
- `run_live_campaign.py` hardcodes `five_second_selector.py` and has no-sacrifice default. `ExecutionStore(sacrifice=False)` defaults OFF.
- Historical sacrifice value is hard-coded `£0.01`; modern block gate accepts `500` only (older legacy branch waits 10 seconds). The 500-block gate also imposes **a separate post-sacrifice ≤7 buying-block activity condition** in qualifying historical modes.
- Original exits are fixed policy presets / stage-specific DB columns, not arbitrary multipliers and allocations.
- A confirmed bug has a failing regression: a transient RPC 429 during the post-sacrifice quote transitions `PROBE_CONFIRMED -> PROBE_UNKNOWN`; scheduler scans only `PROBE_CONFIRMED` and loses the eligible buy. Requirement regression is in `audit/sacrifice-live-path-20261123`, with GitHub Actions run 45283889687. Another regression proves sacrifice defaults OFF.
- Main buy and initial sacrifice have a three-second lag cap. This may be too short with serial network checks/signing; measure before setting any new threshold.
- Proceeds accounting is not uniformly verified for every entry/exit mode; original probe tokens and all fees must be reconciled.

## Non-negotiable boundaries

1. No rewriting the detector, original quotation mathematics, route builders, or signed intent/broadcast semantics unless a reproduced defect demands the narrowest change.
2. The selector only makes and journals policy decisions, never quotes/signs/funds/buys/sells. Exactly one named selector per campaign. It must publish the unchanged `decisions` and `revocations` SQLite contract and full V4 venue identity for eligible launches.
3. Sacrifice belongs in the execution engine. Changing selector must not turn sacrifice OFF or modify buy/sell semantics.
4. New campaigns have immutable, versioned configuration. Historic campaign DBs and their rules remain untouched; resume validates the original manifest.
5. Unknown RPC outcomes never silently become permanent rejection; uncertain signed transactions cannot be replaced with fresh nonces or duplicate orders without reconciliation. Stop new entries safely while preserving exits, held positions and signed intents.
6. Preserve one wallet / the existing signer and wallet lock initially. No new wallet, treasury, supervisor, research collector, paid provider or background daemon purely for modularity.
7. Never arm, sign, broadcast, or spend real funds without distinct explicit authorisation. No secret bytes in source, tests or logs.

## Ordered changes, each isolated and gated

### Phase 0 — Freeze truth and define pass/fail

- Capture current main SHA, historical imported file/blob SHAs, dependencies, default CLI arguments, detector and selector schemas, campaign DB schema, and the D1 test command/results. Keep copies of the existing launch examples; never modify live campaign data.
- Move/retain the actual failing-first sacrifice regression and the upstream 500-block tests in the future fix branch. Add failures for selector handoff, arbitrary exits, restart and no-spend default.
- Deliverable: immutable baseline + reproducible fail-first evidence + no secrets.

**Pass:** existing suite green on baseline; two known defects remain intentionally red on the separate audit suite; explicit gap inventory. **Fail:** tests prove only mock outcomes but are described as live success.

### Phase 1 — One campaign contract; sacrifice ON

- Introduce a single typed, immutable, versioned `campaign.json` validated before workers start. New-launch defaults: `sacrifice.enabled=true`, historic starter `sacrifice.gbp=0.01`, `wait={unit:blocks,value:500}`, historic activity filter explicitly named/configurable as an **execution survival rule**. Leave all historic campaigns/resume logic intact.
- Independently configure selector name/parameters/version, target confirmed main buys, main GBP stake, total campaign spend/exposure cap (including probes, gas and pending transactions), sacrifice value and time/blocks wait, sacrifice survival rules, ordered exit stages (positive multiple and fraction), optional manual reserve, quote deadlines/slippage, RPC settings (nonsecret) and relevant monitor cadence.
- Avoid generic unbounded/unsafe values: validate bounds, percentages, decimals, token/asset units, unsupported gate combinations and position-limit rules. Configure policy only at campaign start; immutable after arming; reject unknown fields and mismatched resumes rather than guessing. Permit a deliberate `--no-sacrifice` only if explicitly chosen by user.
- Keep one CLI or one simple config loader; do not add another process/service.

**Pass:** new run created with no sacrifice flag has sacrifice ENABLED; `--no-sacrifice` is explicit, schema/version and resume identity verified; arbitrary valid stake/count/exit/sacrifice configurations parse correctly; malformed values fail *before* wallet/RPC/worker access. Existing DB tests remain green.

### Phase 2 — Fix sacrifice state machine and entry timeliness

- Use explicit durable states: `LAMB_QUEUED -> PROBE_PENDING -> PROBE_CONFIRMED -> WAITING/VERIFYING -> QUEUED -> BUY_PENDING -> BOUGHT`, plus terminal rejection, safe retryable unknown and unresolved signed states. Avoid introducing unnecessary status duplication if existing schema can encode these transitions.
- Fix the reproduced `PROBE_UNKNOWN` loss: transient 429, stale quote, temporary missing route, RPC timeout or canonicality uncertainty should retain/requeue a check with bounded, rate-aware backoff; definitely invalid or rug-risk assets fail closed with reason. On recovery, recheck all surviving pending sacrifices. Preserve exact receipt-anchored block wait/reorg checks.
- Ensure amount comes from the manifest, not `PROBE_GBP`, and time/block survival checking respects the configured unit; preserve original receipt confirmation, spender/route preflights and one in-flight transaction invariant.
- Test the real measured decision-to-probe signed/broadcast latency, probe-receipt-to-ready latency and ready-to-main signed/broadcast latency. Avoid extending the current three-second deadlines without a measured and strategy-consistent justification. Existing exits must preempt new purchases.
- Check sacrifice disposal: probe token balance is distinct from main stake and must remain accounted for; do not report its retained token value as realised cash or accidentally sell it as a main tranche.

**Pass:** transient 429 regression red→green, canonical reorg and definitely unsellable gates still block; no double probe or main buy on restart; signed-but-uncertain outcomes remain recoverable; all testable paths have durable recorded outcomes. Measured p50/p95/p99 and worst lag, with sample counts and deadline-miss reasons.

### Phase 3 — Selector adapter and launcher switching

- The existing SQLite selector boundary is sufficient. Replace the hardcoded `five_second_selector.py` process choice with a config-selected module/entrypoint that publishes exactly the existing `SelectorStore` PASS/revocation payload. No selector-specific imports or strategy checks inside the executor.
- Provide a minimal deterministic eligible-V4 policy for **offline/replay and read-only validation**, explicitly marked `integrationOnly`; ensure that its operation and trading permissions are not conflated. The original buying-block selector remains selectable. The concentration policy can be added later via the same interface without revisiting execution.
- Document V4 eligible venue identity, Pons exclusions, duplicate candidate handling and reorg semantics, with an explicit reason for selection, no buy, rejection or terminal uncertainty.

**Pass:** swap between original buying-block and integration-only selectors by a single campaign setting; repeat same Build One detector replay and verify identical executor behaviour downstream from a PASS. Check restart, duplicate event, reorg removal, wrong pool/base, stale decision and selector crash.

### Phase 4 — Generalise exits without changing route mechanics

- Replace reliance on fixed `trigger3/5/10/1000`, `soldQty3/5/...` branching for *new* cohorts with a versioned per-position, per-stage obligation/receipt record; let legacy modes retain their old schema and behaviour.
- Keep the original executable full-position sell quote and latched threshold definition. At crossing, latch a persisted obligation once; at execution, quote the actual available tranche, preserve signer/journal/nonce, verify receipt token debit and proceeds, and handle partial fills, dust, fees, approvals and unavailable quote fail-closed.
- Independently configure any practical positive threshold ladder and allocations totaling ≤100%, with explicit reserve remainder or terminal sell policy. Do not silently remap 1.5×→3× legacy slots in new config.
- Calculate verified GBP proceeds and **net** campaign P&L including main stakes, sacrifices, gas, realised sells and any still-held assets as separately disclosed exposure; do not use prospective peaks as cash profit. Support ETH/WETH/USDG denominated receipts only when the relevant actual transfer and exchange-rate evidence can be reconciled.

**Pass:** multi-stage synthetic scenario (e.g. 2×/5×/50×) sells only the intended token fractions, no duplicate sale across restart, same-block reversible route tests still pass, quote outages never erase triggered obligations, and book/monitor/net cash agree with receipts.

### Phase 5 — Single reliable runtime and operator monitor

- Keep the existing launcher and processes where possible. Guarantee detector→selector readiness ordering, unchanged event ordering, no selection lost on launch or reconnect, worker restart, intake stop at **confirmed main purchase count** with pending funding exposure protected, and guardian/peak/sell worker continues while new intake is closed.
- Surface full token/pool IDs, policy/rejection reasons, 1p-or-configured sacrifice hash/receipt/probe quantity/wait progress/verdict, main buy receipt and quantity, current/best executable quote and timestamp, stage triggers/confirmed sales, exposure, true realised net GBP, unconfirmed transaction states, RPC 429/lag and detector gap/recovery. Reuse old monitor layout.
- Prevent monitoring, old-history recovery and low-priority post-probe activity collection from crowding out urgent sales and new entry critical path.

**Pass:** replay with simultaneous fresh events, active peaks, 429, disconnects, launch/stop/resume, and max-position cap; no duplicate spend or stranded exit. Include p50/p95/p99 and discrepancy report relative to historical code.

### Phase 6 — Prove integrated operation before live money

- **D1:** full unit and regression suite including all new defects and config permutations; CI on the exact review SHA, no failures/skips concealed.
- **D2:** captured detector V4 events → each configured selector → actual sacrifice engine/journal → receipt fixtures → 500-or-configured-block survival with reorg/429 → main tx intent/receipt → full-size sell quotes → stage latches → sale receipt/net GBP. Test cancellation/restart at every boundary and nonce uncertainty.
- **D3:** real read-only Robinhood Chain 4663 provider, chain identity and contract code, v3/v4 routes, ETH/WETH/USDG decimals, live two-way buy/sell simulations, fee/slippage/min-out and latency under load. No signing or money movement. Explicitly report excluded/unsupported cases.
- An independent adversarial review signs off exact error handling, speed, security, cap/risk and purchase/sale ABI. If D2/D3 fail, do not provide a live launch command.

**Pass:** all eligible staged paths reliably reach either reconciled fill and sale or durable truthful reason, under fault and restart testing; monitor/ledger/journal consistency and performance within measured critical-path budget.

### Phase 7 — Controlled real-money commissioning (separate user approval)

- After D3 evidence, request explicit user authority for a tiny, strictly capped **one-position** campaign. The integration-only selector must not accidentally admit a broad set into real-money mode. Funding, wallet, password and account parameters stay in user custody.
- D4 evidence: actual selector decision, confirmed sacrifice buy, proven wait and outcome, main buy receipt and real token delta, actual quote-led sale trigger, sale receipt, token debit and proceeds, outstanding probe balances and net GBP after all costs. If no threshold is reached, report that as open exposure rather than pretending a successful round trip.
- Only after D4 pursue a small repeated cohort (D5); evaluate realised net GBP after costs, entry success, sacrificed-but-unbought rate, missed deadlines, exit capture and extreme winner retention. Profitability is an observed outcome, never a build-time guarantee.

## Implementation sequence and PR boundaries

Make independently reviewable PRs: (1) configuration/defaults and failing-first regressions, (2) sacrifice reliability and configurable gates, (3) selector launcher adapter, (4) generic exit stages/accounting, (5) launcher/monitor/latency fault hardening, (6) read-only network commissioning. Validate adjacent modules on every PR, *not* just a newly added unit test. Preserve old DBs, old live services and keys; use new campaign folders. No blanket stop/restart or funded deployment without separate user instruction.

## Definition of done for this stage

A deterministic replay and real read-only network checks prove the complete modular pipeline; new campaigns express all desired control parameters through one manifest; sacrifice is ON without a special flag; changing selector or exit ladder does not edit other modules; risk/failure states, receipt-backed sales and net GBP accounting survive restart; no historical working code degraded without a recorded reason. This is **D3 ready to request a D4 micro-campaign**, not proof of profitability. Profitability is the D5 next goal.
