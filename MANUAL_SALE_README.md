# BlackSwan manual wallet sale

Run between campaigns. The command reads the configured main keystore's public address, inventories its ERC-20 holdings, retains ETH/WETH/USDG, and uses the existing Robinhood v3/v4 swap and signing modules to sell holdings with a fresh gross quote of at least £0.02. Smaller holdings show their estimated GBP proceeds and stay in the wallet. The 2p cutoff is the sole economic filter. It cannot guarantee that a failed transaction will recover its gas.

## Install once

Clone the `manual-wallet-sale/build-two` Git branch into `~/blackswan-manual-sale-v2`, then run these commands independently.

```bash
cd ~/blackswan-manual-sale-v2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements_live.txt
```

The defaults use `~/Desktop/wallet.json` and `~/.config/blackswan/concentration-new-alchemy.env`. Override `--wallet-file` or `--env` if your current main wallet uses different paths. The local keystore password is entered on your own computer.

The existing run journal remains under the same per-wallet state directory. A fresh checkout resumes an interrupted run automatically. Null/malformed index responses preserve partial inventory and fall back to the other index. A failed closing scan saves a coverage failure and retains the active run for retry. Native receipt evidence that is unavailable stays accounting-pending. Pool discovery retains successful v3 results even when v4 history fails, and reports unavailable discovery sources.

## Preview

```bash
~/blackswan-manual-sale-v2/.venv/bin/python ~/blackswan-manual-sale-v2/manual_wallet_sale.py preview --data-dir ~/blackswan-concentration-audit/data
```

## Sell

Stop campaigns using their ordinary controls, then run:

```bash
~/blackswan-manual-sale-v2/.venv/bin/python ~/blackswan-manual-sale-v2/manual_wallet_sale.py execute --data-dir ~/blackswan-concentration-audit/data
```

The same command can be reused whenever you want. It resumes its outstanding run before creating another. A completed run followed by another invocation reads fresh balances. The full main wallet address appears in the report. Point `--data-dir` at the historical campaign data you actually have; the tool reads these databases without changing them. It also checks indexed wallet balances and discovers standard v3 pools and targeted v4 initialization logs.

## Reopen report

```bash
~/blackswan-manual-sale-v2/.venv/bin/python ~/blackswan-manual-sale-v2/manual_wallet_sale.py report --data-dir ~/blackswan-concentration-audit/data
```

Each run saves `report.json` and its durable execution journal under `~/.local/state/blackswan/manual-sales/WALLET/RUN/`. Keep these files. The journal contains signed transaction bytes and is private operator data. Use the same Linux user each time.

## Behaviour and evidence

- Fresh ETH/GBP rates are required for the 2p decision. A rate older than 60 seconds cannot authorize a sale. Slippage is 3% against a fresh exact-input quote, using the existing verified router encoders. Output settles to retained ETH or WETH, including supported USDG bridge routes.
- Transactions are sequential. A token has at most nine quote sizes, eight successful chunks and 60 seconds of processing per pass. Unknown-pool discovery has a 20-second cap. A pending signed transaction pauses the run, retaining its exact intent and nonce. Restart reconciles it before any new sale.
- Missing or illiquid routes, failed approvals, failed simulations, residual holdings and provider errors remain visible by full token address. Partial amounts are tried when the whole-size quote fails. A whole-size quote that later fails simulation is deferred for a future invocation.
- Proceeds use confirmed receipt transfers for WETH. Native ETH proceeds need the provider's transaction call trace. If that trace is unavailable, the token debit and hash remain recorded with accounting pending. Repeating the execution command retries accounting and rereads holdings.
- Gross GBP is valued using each sale attempt's timestamped ETH/GBP rate. Net GBP is labelled an estimate because the raw transaction fees are converted using the last available run rate. Exact raw proceeds and gas are always retained. Existing base balances are excluded from sale proceeds.
- The command reports indexed inventory coverage conservatively. A provider can refuse historical v4 log ranges or lag the chain head. A token whose pool cannot be discovered is reported as `NO_KNOWN_POOL`. The tool never certifies an indexed empty result as a completely clear wallet without a verified indexing watermark.
- Old campaigns whose holdings have been manually sold require reconciliation before resuming. Start new campaigns with fresh campaign records.

## Verification

The archive is built from BlackSwan commit `12ae5070a8dea42ceda189ac6463763997b94028`, with a standalone manual script and targeted tests. Offline tests verify the exact 2p boundary, no signing below threshold, bounded quote failures, stale FX rejection, native payout attribution, and unknown payout handling. Read-only mainnet inventory and a bounded live sale remain to be checked on the operator's computer. No private wallet or mainnet sale was accessed during the build.

```bash
.venv/bin/python -m unittest test_manual_wallet_sale -v
```
