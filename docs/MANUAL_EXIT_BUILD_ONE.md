# Manual Exit Build One

**Standalone operator interface. No keys, extra signer, RPC endpoints, or wallet.** The existing campaign executor alone signs and reconciles trades. The console only reads the identified campaign journal or, after explicit operator action, writes a request.

## Running campaigns — no upgrade required for existing protocol

The previously deployed executor already supports percentage of remaining main holdings. Run the new console in a **separate checkout**, pointing explicitly to the running campaign directory and using the existing Python environment. Do not checkout/update files in a live installation, stop its executor, or migrate its database. The console never creates or alters schema. List/status are read-only.

Example commands (replace the run path with the actual full campaign directory):
```bash
git clone --branch manual-exit/build-one --single-branch https://github.com/Ld0g22/Blackswan.git ~/blackswan-manual-exit
```
```bash
~/blackswan-concentration-audit/.venv/bin/python ~/blackswan-manual-exit/manual_console.py list --data-dir ~/blackswan-concentration-audit/data
```
```bash
~/blackswan-concentration-audit/.venv/bin/python ~/blackswan-manual-exit/manual_console.py menu --data-dir ~/blackswan-concentration-audit/data --legacy
```

Discovery accepts exactly one compatible, recently heartbeating configured campaign with remaining main positions. Otherwise specify `--run-dir` exactly. Menu shows full token address and original/remaining quantity, numbered positions, historical full-size peak, and errors. Select all, reserve, or integer percentage of remaining; preview is fresh, then enter minimum GBP (0 explicitly permitted) and confirm y to submit. Quit does not stop executor or cancel requests. The preview price is not a fill or a guarantee; exact quantity is re-evaluated at execution. Price floor is gross output at quote-time FX; gas is additional. New purchases/automatic exits can change holdings between preview and submission. Full means main balance only, never sacrifice or unrelated wallet holdings.

**Legacy limitation:** `--legacy` explicitly opts in to the existing protocol; queued requests can survive restart indefinitely and cannot be safely cancelled by this console. Reserve command works only if the entire remaining holding is the reserve; otherwise use percent/all or upgrade between campaigns. No upgrade is necessary to sell all of the final 10% once the two automated stages are complete. If the request is pending, inspect status and do not resubmit.

## Enhanced executor — only on a deliberate separate installation/new campaign

The enhanced executor adds the optional manual_request_controls table at its own initialization, without editing existing manifests, requests, fills, or historical trade policy. No hot upgrade. Existing legacy requests preserve their original semantics; new control records are opt-in and capability checked against the live executor PID/start time and heartbeat. Do not downgrade while controlled requests are outstanding: old executors do not enforce the new expiry/reserve controls. Reconcile all signed intents and resolve or cancel outstanding requests first. Keep prior checkout and backups; never discard execution journals.

Use the normal launcher and monitor from the enhanced checkout only for a new campaign (or explicitly validated safe resume). The console's menu in that checkout needs no --legacy. Additional commands: list, status, preview, sell, cancel. The direct preview/sell commands require --selection-id, sell additionally --min-gbp; --percent defaults 100 of remaining, --reserve requests exact remaining configured reserve. Default --ttl 60 seconds (5–600); --slippage-bps defaults 1000 (0–1000). A controlled sale expires before signing, including during signer simulation; no deadline cancels a previously signed transaction. Cancel is only for queued unsent controlled requests and requires --request-id. No automatic retries with new nonces.

Manual work has priority over new intake, but never overtakes an in-flight wallet transaction. Other automatic positions retain normal policy; an uncertain sale blocks further sales of that position. The original full-size peak quote semantics/cadence remain in force, including reserve-only positions in upgraded executor. Listing never triggers RPC; use preview for fresh exact-size sell quote.

## Verification and release gate

Offline suite: python -m unittest -v test_manual_console test_manual_sale test_configured_manual test_configured_exits ; then python -m unittest discover -p 'test_*.py' -q. CI uses controlled fixtures and offline signing, not actual funds. No live compatibility guarantee until installed source, wallet/chain, executable routes and active writer are verified. Never start another executor merely to access the console.
