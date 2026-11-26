# Manual Exit Build One — implementation plan

Base: 0f030cc8c4b50a043592939ddf07b7fb6acfddbc (optional-sacrifice-caps concentration build). Isolated branch only. No existing branch, installation, campaign file, signer or wallet is modified in place. No live sale.
Approved scope: reuse manual_sale.py and ManualExitEngine; add an operator console for numbered full-address positions, exact remaining/reserve/partial exits, preview, explicit minimum and confirmation, status and refresh. Selector independent.
 
## Interfaces and invariants
- Console uses existing request/fill journal and campaign identity validation, no signer, no RPC credentials, no automatic trade on startup. A legacy running executor can accept the existing percent-of-remaining protocol; explicit warning/opt-in is required because legacy requests have no expiry/cancel protection. No live schema migrations by console.
- New executor adds an optional manual_request_controls table and capability marker at initialization only, preserving existing schema/requests. New requests expire before signing after operator-specified deadline (default 60 seconds); signed or uncertain intent is never cancelled or duplicated. Queued cancellation is atomic and only supported on new executor. Existing unbounded requests remain unchanged. Reserve is remaining original allocation after manual sales consume it first. Reserve-only sale must use exact capped quantity, not rounded percentage.
- New engine protects reserve and excludes sacrifice; no-sacrifice supported; manual exits in progress get scheduling priority over new admissions, not over already submitted transactions. Do not block all admissions indefinitely on BLOCKED/UNVERIFIED requests. Keep original full-size peak quote semantics; reserve-only tracking continues with existing cadence.
- No hot upgrade: old executor remains sole signer; new console can be run in a separate checkout with explicit old run path; upgraded executor only on deliberately installed/resumed campaign. No strategy/exit/selector changes.

## Task 1 — RED
Add offline test_manual_console.py for discovery, exact reserve quantities, legacy compatibility/identity, expiry and cancellation, no-sacrifice quote and reserve preservation. Trigger existing full CI via draft PR (never merge).
## Task 2 — GREEN
Implement manual_console.py (stdlib, existing manual_sale helpers), additive controls in manual_exit_engine.py, minimal scheduling change in live_executor.py. Existing CLI and historical requests continue to work. Test transaction guard and preview/requote; no new dependencies.
## Task 3 — release
Run entire CI and review diff against base. Write operator docs with exact commands, capability limits, rollback, live preflight and no guarantees of liquidity. Report all unverified paths. No independent reviewer available; self-review disclosed.
