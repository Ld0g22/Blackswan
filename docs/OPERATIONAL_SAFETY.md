# Operational safety — protect funds and the user's terminal

The operator's time and wallet are production assets. Engineering conveniences may not impose needless work on either.

## Interactive terminal: hard prohibition

**Never issue a user-facing interactive command containing** shell-terminating or shell-state-altering constructs such as `set -e`, `set -u`, `set -o pipefail`, `errexit`, standalone `exit`, process-replacing `exec`, or shell `trap` instructions. The user has suffered an actual terminal closure because this prohibition was violated repeatedly. A script pattern from a generic template is not an excuse.

Do **not** bury a shell termination in a subshell, function, `bash -c` or copy-paste block. Make user commands independently executable and harmless to the calling interactive shell on failure. A failing command should print its error and return to the prompt.

Avoid broad `killall`/`pkill -f` and `rm -rf`, force resets, mutating dotfiles and chained privileged operations. Any necessary stop/cleanup must first list exact target PIDs/services, command lines and outstanding trade state; avoid stopping unrelated programs. Preserve receipts and recovery DBs before clean-up.

User commands should be short and separated by purpose: inspect → fetch/install → verify → dry run → deliberately authorised trade. No combined install-and-live-launch scripts. Do not run code as root unless a proven requirement exists. Show where to run the command, expected result, and how a failure leaves the machine and wallet.

`tools/check_terminal_commands.py` detects a useful subset of violations; review manually for destructive and process-side effects. If a command cannot be inspected or justified, do not provide it.

## Wallet and signer

- User should not need fresh wallets, token balances, approvals, passwords or third-party services solely because a particular implementation is easier. Any unavoidable requirement must be supported by actual protocol behaviour and a demonstrated reason.
- Keep signing keys offline or in the approved existing keystore boundary. Never request or publish private keys, API tokens, plaintext passwords or whole secrets-file contents.
- One active writer per wallet and chain. Local+remote or old+new campaign writers must never race on the same nonce. Before arming, detect other writers and outstanding intents; do not kill processes indiscriminately.
- Journal exact transaction intent, chain ID, nonce, expected input/output, slippage limits, destination and identifiers **before** signing/submission.
- Unknown RPC send outcome must be reconciled by transaction hash/nonce before retry. No blind rebroadcast with fresh nonce after timeout.
- Spend and token approvals must be minimal and bounded. If a router can atomically handle currency conversion, test it end to end rather than require manually prefunded intermediary currency. Avoid unsupported promises that native-only routes always exist.
- Do not conflate position token custody, router intermediate tokens and wallet-funded base currency.
- New-buy pause must not block safe exits/reconciliation; emergency liquidation needs explicit policy and execution evidence.

## Network and on-chain deployment

- Verify **chain ID 4663**, actual RPC URL format, HTTPS/WSS handshake, block/head, rate-limit behaviour, router/Quoter/Permit2 contract bytecode, token addresses, decimals and fees before using a route.
- Never use placeholder/redacted URLs as though production endpoints; detect invalid configuration and say precisely which setting is missing without exposing the secret.
- Read-only `eth_call` checks and gas simulation are valuable **preflight**, but not proof that a tx will be accepted, mined, deliver intended tokens or support the reverse sell after the entry.
- Quote viability is specific to token, exact pool/hook, direction, stake amount, timestamp and wallet. A route that works for 1p does not prove viability for 20p or £5.
- Chain reorganisations, volatile liquidity, hooks, fee-on-transfer tokens, temporary quote absence and contract incompatibilities are ordinary failure cases, not exceptional bugs.

## Real-money admission gates

Never start a new campaign because unit tests passed. Before **proposing** a live launch:

1. Confirm exact source commit and clean/known workspace, baseline diff, approved campaign config and risk/exposure envelope.
2. Verify RPC, chain, contract addresses and necessary tokens, valid route in both directions and safe signer state.
3. Check there is no competing wallet writer, unresolved transaction, orphaned open position or admission lock.
4. Complete read-only/fork/captured-event path tests, error and restart tests, monitor/receipt reconciliation, and latency comparison to baseline.
5. Where a *new on-chain execution path* is involved, require a separately authorised bounded live round trip (buy + sell) before treating it as proven. Report what is **NOT LIVE VERIFIED**. No implied permission to trade.
6. Use one explicit launch command only after the user authorises the campaign. No extra services or credentials introduced silently.

If the wallet has open positions and a deployment fails, preserve safe existing exits. Never automatically sweep/fund/reallocate assets beyond the expressly approved policy.

## Recovery / failure response

- Classify whether the failure is local shell, installer, detector, selector, quote, signer, broadcast, receipt, guardian/exit or monitor.
- Determine if **funds moved** before doing anything else. Read-only chain checks first where possible.
- Protect transaction journal and databases; avoid deleting locks until writer state is understood.
- Stop new admissions if ownership or state is unknown, but keep valid recovery processes available.
- Provide a single direct explanation with supporting trace and safe recovery action; never assert 'no trade occurred' without nonce/hash/balance evidence.
- Record a regression test for the root cause and inspect neighbouring failure modes before redeploying.
