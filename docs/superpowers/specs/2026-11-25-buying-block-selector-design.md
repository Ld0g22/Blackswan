# Buying Block Build One — selector design

Date: 2026-11-25. Status: proposed design only; no implementation, tests run or live authorisation.
Branch: buying-block/build-one, forked from concentration/build-one at 3a69337f27a253c57ec22b815e554374a6d12a53.

## Intent and provenance

Restore the original inclusive three-to-ten buying-block selection, with a configurable observation window initially 30 seconds, in a standalone complete system. Never run it concurrently with the concentration system. No shared runtime services, databases or arbitration. The detector, executor (including sacrifices), signing, peak tracking, sale/receipt logic and all unrelated campaign settings must remain unchanged.

The historical five_second_selector.py blob e6d3156f71ed6d7b2506f7243a5331099595dadd is identical on this branch, BlackswanNovember at 5bd3c2b6914d85fc703a17190d5834a7f7cf8b45, and BlackswanNovember branch lamb-selector-20s-buyblocks-3to10-20261110. This proves source identity, not attribution of historic profit to an exact runtime configuration. The new window is a strategy variation; profitability is not established.

## Approaches

A. Extend the existing historical selector and campaign validator (preferred): preserves counting, observation and output semantics; fewest changes and strongest replay comparability.
B. Reimplement using the concentration collector: potentially better canonical coverage, but a different timestamp anchor and evidence cutoff would silently alter the historical feature and increase regression risk. Not selected.
C. Build an additional selector service: redundant, no benefit for one active system. Not selected.

## Exact policy and configuration

New buying-block campaign template: selector = {"name":"buyingBlocks","decisionSeconds":30,"maxBuyingBlocks":10}. Minimum remains 3; maximum 10 inclusive. Only the window needs to be configurable for this request. Proposed validated range: integer seconds 1–300 (existing generic campaign schema range); reject bool, floats, out-of-range values. Preserve historical 5/20-second and unbounded CLI compatibility, but the new default must be bounded. Explicitly freeze settings in campaign.json and selector database; reject mismatched resumes, never mutate a running campaign's window. Changing the window requires a new campaign, not a hot switch.

Preserve the historical observation definition:
- Anchor: launch block header timestamp (not first trade, detection timestamp, or sacrifice receipt). Deadline = anchor + decisionSeconds.
- A buying block is one distinct block hash with at least one decoded token buy in the same candidate launch pool, starting at the launch block. Multiple buys/transactions in one block count once; sells alone count zero; a buy plus sell still counts once. Keep historical venue decoder/base handling and executor's V4 admission exclusions unchanged.
- Historical availability cutoff is local receipt time <= deadline, including overlapping launch-to-head RPC backfill that arrived before deadline. This is not an exact canonical block-timestamp window; do not silently substitute that different feature. No later trades or data received after deadline can turn a selection into PASS. Record late/coverage mismatches in read-only replay validation.
- At deadline: complete evidence and 3–10 distinct blocks -> PASS; complete evidence and 0–2 or >=11 -> FAIL. Incomplete coverage -> UNKNOWN, never buy (historical rule also permits FAIL above 10 on observed evidence; preserve this unless contradicted by a reorg). No early PASS at the third buy. Keep the historical wait-to-deadline behavior initially, rather than adding an optimisation.
- Same overlapping WSS subscription and bounded 10-block getLogs backfill, existing 25 ms detector journal polling and existing deduplication. No new research collector or polling service. Missing launch header, subscription failure, interrupted observation, decoding failure and restart must never yield an unsupported PASS.
- Caveat: legacy completeness flags certify subscription/backfill operation, not mathematically gap-free canonical delivery. Legacy removal handling must be examined for late reorg/duplicate edge cases. Compare to canonical replay and explicitly report mismatches; do not claim full coverage based on flags alone. Any needed change to historical selection semantics must be documented separately.

## Integration

Adjust five_second_selector.py constructor/CLI decision window validation and campaign_v3.py buyingBlocks validation/default only. Existing run_configured_campaign.py selectorCommand already forwards decisionSeconds/maxBuyingBlocks to the historical process; no execution change. Review readiness: the buying-block selector currently creates its SQLite file before verifying chain ID, and launcher checks only file existence for this policy. A minimal selector readiness signal and launcher guard may be required to avoid falsely declaring startup ready; no trading code change. No concentration-specific collector starts in a buying-block campaign.

Keep SelectorStore and interfaceVersion=1 decisions/revocations schema, stable selectionId, detector event and venue identity unchanged. Existing executor reads PASS, de-duplicates tokens, applies its own supported venue rules, then queues sacrifice or direct main entry according to campaign. Sacrifice activity filter is independent; do not replace it with the pre-selection count. Maintain independently configured stake, count, sacrifice settings, limits, exits, wallet and campaign databases. Monitor must continue reporting policy/window, count and decision reason alongside existing financial fields; no redesign.

## Acceptance gates (not yet executed)

1. Test-driven configuration coverage: 1, 5, 20, 30, 45, 60, 300 seconds; reject 0, 301, bool/floats and bad config. Template emits 30 and 10. Existing configurations/resume behavior retained. Manifest and DB mismatch rejected.
2. Replay same captured historical event arrival trace at 20 seconds through old and adapted selector, compare decisions, buy hashes, cutoff and reasons. At 30 seconds compare against independently counted eligible evidence. Boundary cases 0,2,3,10,11; duplicate logs, distinct transactions in same block, mixed sells, other pools, before launch, exactly at cutoff, after cutoff, late backfill, disconnect, malformed data, restart, removed logs and wrong chain. Preserve unknowns. Verify actual historical eligibility including any runtime exclusions from captured configs (not yet supplied).
3. Run existing selector, launcher, campaign and full regression suites. Actual detector journal -> same selector outbox -> executor admission; confirm no duplicate selections or changed sacrifice/exits. Compare hashes for untouched modules and check no concentration process is spawned.
4. Read-only actual chain validation on 4663: compare received evidence to canonical swap logs, measure observation completeness, p50/p95/p99 decision lag after deadline and provider load under representative launches; no internal CU accounting. Decision must not wait behind historical work. Any extra entry delay is explicitly measured against historical baseline.
5. No live claim or campaign start. New 30-second policy may select a different population from historical 20-second policy. Report baseline replay results, unknowns and timing; live funding requires separate user authorisation.

## Scope note

The fork is specifically Concentration Build One, not the later concentration monitor/optional sacrifice-cap descendant. Those shared improvements are separate, not implicitly imported here. No new paid dependency or shared runtime introduced. The 1–300 second validation range is a proposed engineering choice, not an additional trading constraint authorised by the user.
