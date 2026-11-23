# Robinhood Black Swan — detector v0.2

**Read-only.** Watches Pons V2 factory launches, canonical Uniswap v3 factory pool creations, and canonical Uniswap v4 PoolManager initializations on Robinhood Chain (4663). **No selector, wallet, purchases or post-launch monitoring.**

## Install on Ubuntu

Make the folder you planned (`~/blackswanNovember`) and put these individual files directly inside it:

- `detector.py`
- `test_detector.py`
- `requirements.txt`
- `README.md`
- `.gitignore` (keep this as a hidden file; prevents accidental commits of `.env` or database files)

Then run:

```bash
cd ~/blackswanNovember
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v test_detector
```

The tests are entirely local: fake JSON-RPC + WebSocket endpoints and a disposable SQLite journal. **A passing test is NOT live-chain acceptance.**

## Run live

Uses the already working, unmodified `~/blackswanRH_guarded/.env` from your previous Robinhood system. No key copying or new API account needed.

```bash
cd ~/blackswanNovember
.venv/bin/python detector.py run --env ~/blackswanRH_guarded/.env --db data/detector.sqlite3
```

The console prints every unique new event (including removal notices), reconnects, brief recovery and a 30-second health summary. Ctrl+C stops. Only one detector process can write the same SQLite database.

In another terminal:

```bash
cd ~/blackswanNovember
.venv/bin/python detector.py status --db data/detector.sqlite3
.venv/bin/python detector.py tail --db data/detector.sqlite3 --after 0 --follow
```

To see the **selector-facing JSON** rather than a summary:

```bash
.venv/bin/python detector.py tail --db data/detector.sqlite3 --after 0 --follow --json
```

## Selector interface

SQLite WAL database, table `event_log`, with fields:

- `seq`: strictly increasing delivery sequence; consumer records its own last processed `seq`.
- `payload`: JSON with `schemaVersion:1`, `eventKey`, `chainId`, `source`, `eventType`, `contract`, `blockNumber`, `blockHash`, `transactionHash`, `logIndex`, `observedAtUtc`, `recovered`, `removed`, source-specific `details` and the exact `rawLog`.

For a low-latency selector, poll the WAL outbox in short intervals (e.g. **20–50 ms**), committing its cursor only after applying each row. The human-facing `tail --follow` display uses a slower 500 ms refresh and should **not** be used as the trading event feed.

**Delivery semantics:** append-on-change; the same event seen twice is stored once. A reorganisation removal produces a new row (`removed:true`), and a re-add produces another row. Different pools involving the same token are **not** deduplicated. Do not delete old rows while a consumer may need to replay. A selector should read `seq > lastProcessedSeq` and only checkpoint after processing that row. `observedAtUtc` is local receipt time, **not the block timestamp**.

`ponsV2.details`: `token`, `curve`, `deployer`, `pairToken`, `launchConfigId`, `graduationThreshold`.

`uniswapV3.details`: `token0`, `token1`, `fee`, `tickSpacing`, `pool`.

`uniswapV4.details`: `poolId`, `currency0`, `currency1`, `fee`, `tickSpacing`, `hooks`, `sqrtPriceX96`, `tick`.

**v4** stores a pool ID, not a separate pool contract. Currency pairs may have native ETH (`0x000...000`); no token-classification assumptions are made. Pons V2 launches on bonding curves and can later create a v4 pool; both events are retained.

## Recovery and API budget

- One Alchemy WSS connection, three exact contract+topic subscriptions. No all-chain swap stream. Each subscription call is accounted at 10 CU in addition to incoming bytes.
- Subscribe **before** making a bounded recent-block backfill so the handoff doesn't miss events. Backfill runs concurrently with the live feed.
- On startup or reconnect, scans up to the most recent **600 blocks** (roughly one minute), with a **100-block reconnect overlap** to protect against delayed WebSocket delivery. Free-tier `eth_getLogs` is limited to **10 blocks per request**. Since v0.2, a **single combined address/topic OR-filter request per 10-block window** replaces three separate requests; exact contract/topic pairs are still validated locally. A failed backfill read is retried once. Old gaps outside this window are recorded under `gaps` and shown in `status`, **not silently presented as complete**.
- WSS reconnections are automatic. This is **quick recovery**, not exhaustive historical indexing or token lifecycle monitoring.
- Rate-limits backfill HTTP requests; uses a local estimated 1,000,000 CU/rolling 24-hour stop and 3,000 CU/rolling 10-second throughput gate. CU is **estimated**, not an Alchemy billed balance. Other programs on the same account are **not included**. Future selector/execution must share a separate account-wide controller; this standalone detector cannot guarantee a global hard cap.
- Detection throughput/latency, true costs, every-event completeness and reliable backfill **must be tested on your actual Alchemy key before operational acceptance**.

## Safety / known limitations

- Connection and subscription acceptance do **not** prove live event coverage. Validate against actual matching logs and an independent indexed source over identical block ranges.
- We cannot run your keyed Alchemy endpoint in this container. The provided regression tests demonstrate the *local code path*, not live credential/venue performance.
- Direct event subscriptions see logs after block inclusion, not pending transactions. A transaction's initial buy may precede subscriber notification. Selector manages Pons opening taxes and tradeability.
- `eth_blockNumber` cost is locally estimated; WebSocket metering estimated as 0.04 CU/byte. The local counter isn't an account-wide quota authority.
- Matching logs whose ABI cannot be decoded are preserved in the `decode_failures` table and shown by `status`; they are never silently discarded.
- Health `LIVE` means transport subscribed, not 100% coverage. An unplanned outage longer than the recovery window produces a visible gap.
- No systemd service or daemon installed; run in the terminal initially and observe until live acceptance passes.

## Verification gates before freeze

1. Run the complete unmodified detector test suite; verify ABI decoding, durability, reorgs, reconnect recovery, combined filters, retries, credit accounting, HTTP and WebSocket operation.
2. Real v2/v3/v4 events appear and source-specific addresses/pool IDs decode correctly.
3. Forced disconnect repairs events within the recovery window without duplicate outbox rows.
4. Independent Envio enumeration agrees over the same live block window (or discrepancies are explained).
5. Actual event delay, missed count, budget and service uptime are measured.

## v0.2 engineering audit

- 600-block recovery: **60 × 60 = 3,600 CU** (previously 180 × 60 = 10,800 CU) before errors/retries. A 100-block reconnect overlap is **600 CU** (previously 1,800 CU). These are estimates based on Alchemy's published fixed `eth_getLogs` CU price.
- Source-specific WebSocket filters and selector-facing schema **unchanged**. Tests use synthetic locally served RPC logs; no credentialed live-mainnet performance claim.
- **Known limits:** local-only estimated CU; no account-wide enforcement, untested actual complete chain event coverage and latency; HTTP/indexer can return stale data near chain head. No live trading should be predicated on detector completeness until the acceptance gates above pass.

## Sources

- Pons v2 ABI and deployment: https://docs.ponsfamily.com/v2
- Canonical Uniswap v3 factory ABI: https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/IUniswapV3Factory.sol
- Canonical Uniswap v4 PoolManager ABI: https://github.com/Uniswap/v4-core/blob/main/src/interfaces/IPoolManager.sol
- Robinhood deployed addresses: https://developers.uniswap.org/docs/protocols/v4/deployments
- Alchemy Robinhood 10-block `eth_getLogs` free limit: https://www.alchemy.com/docs/chains/robinhood-chain/robinhood-chain-api-endpoints/eth-get-logs
