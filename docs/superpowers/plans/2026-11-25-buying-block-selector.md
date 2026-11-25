# Buying-block selector Implementation Plan

> **For agentic workers:** use superpowers:executing-plans and test-driven-development. Steps use checkbox syntax.

**Goal:** Standalone buying-block campaign with configurable inclusive min/max distinct buying blocks and observation duration, default 3–10 in 30 seconds.

**Architecture:** Adapt the historical selector, preserving its availability cutoff and decision journal; use existing campaign launcher and executor. Do not import concentration-specific code into the buying-block path.

**Tech Stack:** Python 3.12, unittest, SQLite, aiohttp, existing RPC/WSS.

**Spec:** docs/superpowers/specs/2026-11-25-buying-block-selector-design.md

## Global constraints
- Baseline 3a69337f27a253c57ec22b815e554374a6d12a53; branch buying-block/build-one only.
- No changes to detector, execution, peak tracking, signer, sacrifice rules, fees or exits.
- New campaign explicit bounded integer min/max with 0 <= min <= max; 1–300 seconds. Preserve legacy sealed manifests and unbounded legacy CLI.
- No live trading; read-only validation first. No concurrency between systems.

## Review focus
- Old manifests missing minimum must keep digest and semantics; integrationOnly remains synthetic.
- Selector database with different policy identity or parameters must reject resume before mutation; chain mismatch cannot be marked ready.
- Complete window required for bounded PASS; no early pass at minimum; exact cutoff and duplicate blocks.
- Fresh readiness versus stale heartbeat; fail closed on startup.
- Monitor must expose actual decision count, rule and reason; no changes to financial data.

## Task 1: Regression tests
- [ ] Create test_buying_block_configurable.py: default values, arbitrary range and window, invalid type/bounds, legacy manifest digest, selector arguments, decision boundary and incomplete coverage, persistent policy mismatch, readiness, deduplication and time cutoff.
- [ ] Run targeted tests against unchanged production code; expect red for new behavior.

## Task 2: Configuration and selector
- [ ] In campaign_v3.py extend defaultCampaign and validateCampaign; preserve old input shape and digest and integrationOnly synthetic policy. New explicit min requires finite max.
- [ ] In five_second_selector.py extend decisionStatus and Selector constructor with minBuyingBlocks keyword, validate integer bounds, persist all policy fields and reject mismatches, include min/max/window in decision evidence; keep historical counting and transport.
- [ ] In run_configured_campaign.py forward min to selector, verify buyingBlocks readiness with chain-verified heartbeat and expected policy name; preserve concentration default selectorReady behavior.
- [ ] Test constructor/CLI, same decisions for 3–10 historical rule, exact cutoffs and no early PASS.

## Task 3: Monitor and acceptance
- [ ] In live_dashboard.py read policy metadata and latest selector decisions using existing read-only SQLite; show counts, reasons and full token addresses while preserving execution display.
- [ ] Run targeted module and full unittest suite via repository CI, including configured pipeline and original tests. Verify changed file list, no money modules touched. Compare historical source and report any untested chain/replay assumptions.
- [ ] Supply safe independent clone/install/template commands and monitor invocation. Live start remains explicitly armed separately.

## Execution note
The local runtime cannot resolve GitHub DNS; use GitHub connector on the isolated branch and CI for actual full-repository tests. A draft verification PR may be used solely to trigger existing pull-request workflows; never merge it. No independent subagent is available; review by author must be disclosed.
