#!/usr/bin/env python3
"""Fresh Robinhood Chain £0.50 peak tracker. Read-only: never signs or sends transactions.

Uses the unchanged detector's durable event_log SQLite outbox and actual onchain
Quoter eth_calls / Pons V2 curve pricing. Quoter evidence is NOT a filled sale.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import json
import os
from pathlib import Path
import sqlite3
import time

import aiohttp

CHAIN_ID = 4663
NATIVE = '0x' + '0' * 40
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
BASES = {NATIVE, WETH, USDG}
# Known ETH↔USDG conversion pools; always verify each leg by live onchain Quoter.
# V3 WETH/USDG 0.05% and V4 native ETH/USDG 0.05%, spacing 10, no hook.
# A reverting, missing, or illiquid bridge is NEVER a valid quote.
USDG_BRIDGES = (
    dict(id='weth-usdg-v3-500', source='uniswapV3', token0=min(WETH, USDG),
         token1=max(WETH, USDG), fee=500),
    dict(id='eth-usdg-v4-500-10-nohook', source='uniswapV4', token0=NATIVE,
         token1=USDG, fee=500, spacing=10, hooks=NATIVE),
)
V3_QUOTER = '0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7'
V4_QUOTER = '0x8dc178efb8111bb0973dd9d722ebeff267c98f94'
V3_SELECTOR = 'c6a5026a'
V4_SELECTOR = 'aa9d21cb'
PONS_RESERVES = '0902f1ac'
PONS_FEE = '24a9d853'
PONS_TAX = 'c1bb8901'
PONS_SNIPE = 'd7e1ef39'
PONS_SELLABLE = '808bcddc'
PONS_READY = 'c68360a5'
ENTRY_DELAY = 5.0
ENTRY_TOLERANCE = 3.0
ENTRY_RETRY_DELAY = 0.3
MAX_ENTRY_ATTEMPTS = 3
WARM_INTERVAL = 15.0
HOT_INTERVAL = 3.0
COLD_INTERVAL = 60.0
COLD_AFTER = 180.0
ORDINARY_RETIRE = 600.0
BLACK_SWAN_RETIRE = 7200.0


def validateAddress(value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        raise ValueError('Expected a 20-byte wallet/token address')
    int(value[2:], 16)
    return value.lower()


def word(value: int) -> str:
    if not 0 <= value < 2 ** 256:
        raise ValueError('Bad ABI integer')
    return f'{value:064x}'


def utc(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(timespec='milliseconds')


def publicKeystoreAddress(path: Path) -> str:
    """Read ONLY the plaintext public address field from an existing V3 wallet keystore.

    Never decrypt, sign, or take a private key. An explicit --wallet-address may
    be used instead; the tracker itself remains entirely read-only.
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        address = data['address']
        if not isinstance(address, str):
            raise ValueError('Keystore public address must be a string')
        return validateAddress(address if address.startswith('0x') else '0x' + address)
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f'Cannot read public wallet address from {path}: {type(error).__name__}') from None


def loadSettings(path: Path, walletAddress: str | None = None,
                 walletFile: Path | None = None) -> tuple[str, str]:
    values = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[7:]
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values.setdefault(key.strip(), value.strip().strip("\"'"))
    rpc = values.get('ROBINHOOD_ALCHEMY_HTTP_URL') or values.get('BSRH_RPC_HTTP')
    if not rpc or not rpc.startswith('https://'):
        raise ValueError('Missing Robinhood Alchemy HTTPS RPC URL')
    # Explicit choice wins; otherwise use the already-configured public wallet.
    # Explicit public-wallet selection overrides any stale wallet variable in .env.
    wallet = walletAddress or (publicKeystoreAddress(walletFile) if walletFile is not None else None)
    if not wallet:
        wallet = values.get('BSRH_WALLET_ADDRESS') or values.get('ROBINHOOD_WALLET_ADDRESS')
    if not wallet:
        # The original wallet-create command wrote a V3 keystore containing the
        # public address. Accommodate its common old runtime locations without
        # importing wallet/signing code or requiring the user to copy secrets.
        candidates = (Path.home() / 'Desktop/wallet.json',
                      path.parent / 'runtime/wallet.json',
                      path.parent / 'app/runtime/wallet.json',
                      Path.cwd() / 'runtime/wallet.json',
                      Path.cwd() / 'app/runtime/wallet.json')
        addresses = {publicKeystoreAddress(p) for p in candidates if p.is_file()}
        if len(addresses) > 1:
            raise ValueError('Multiple wallet addresses found; specify --wallet-address to select yours')
        wallet = next(iter(addresses), None)
    if not wallet:
        raise ValueError('Public wallet address not found; specify --wallet-address 0x... '
                         'or --wallet-file PATH/TO/wallet.json (no private key)')
    return rpc, validateAddress(wallet)


def launchVenues(event: dict) -> list[tuple[str, dict]]:
    """A single pool can introduce two novel assets; never assume one is the launch."""
    if event.get('chainId') != CHAIN_ID:
        return []
    d, source = event['details'], event['source']
    if source == 'ponsV2':
        token = validateAddress(d['token'])
        v = dict(id=validateAddress(d['curve']), source=source, token0=token,
                 token1=validateAddress(d['pairToken']), emitter=validateAddress(d['curve']))
        return [(token, v)]
    if source == 'uniswapV3':
        t0, t1 = validateAddress(d['token0']), validateAddress(d['token1'])
        v = dict(id=validateAddress(d['pool']), source=source, token0=t0,
                 token1=t1, fee=int(d['fee']))
    elif source == 'uniswapV4':
        t0, t1 = validateAddress(d['currency0']), validateAddress(d['currency1'])
        v = dict(id=d['poolId'].lower(), source=source, token0=t0,
                 token1=t1, fee=int(d['fee']), spacing=int(d['tickSpacing']),
                 hooks=validateAddress(d['hooks']))
    else:
        return []
    if t0 == t1:
        return []
    candidates = [x for x in (t0, t1) if x not in BASES]
    return [(token, v) for token in candidates]


def ponsBuy(ethIn: int, quoteReserve: int, tokenReserve: int, sellable: int,
            fee: int, tax: int, snipe: int) -> tuple[int, int]:
    if min(ethIn, tokenReserve, sellable) <= 0 or quoteReserve < 0 or min(fee, tax, snipe) < 0:
        raise ValueError('No Pons buy liquidity')
    if fee + tax >= 10000:
        raise ValueError('Bad Pons fee')
    snipe = min(snipe, max(0, 10000 - fee - tax - 100))
    net = ethIn - (ethIn * fee // 10000) - (ethIn * tax // 10000) - (ethIn * snipe // 10000)
    if net <= 0:
        raise ValueError('Buy taxes exhaust input')
    amount = (net * tokenReserve) // (quoteReserve + net)
    spent = ethIn
    if amount > sellable:
        amount = sellable
        if sellable >= tokenReserve:
            raise ValueError('Invalid sellable reserve')
        needed = sellable * quoteReserve // (tokenReserve - sellable) + 1
        spent = min(ethIn, (needed * 10000 + (10000 - fee - tax - snipe) - 1) // (10000 - fee - tax - snipe))
    if amount <= 0:
        raise ValueError('Zero tokens bought')
    return amount, spent


def ponsSell(tokenIn: int, quoteReserve: int, tokenReserve: int,
             fee: int, tax: int, ready: bool) -> int:
    if ready or tokenIn <= 0 or tokenReserve <= 0 or quoteReserve < 0 or not 0 <= fee + tax < 10000:
        raise ValueError('Pons sell unavailable')
    gross = tokenIn * quoteReserve // (tokenReserve + tokenIn)
    return gross - gross * fee // 10000 - gross * tax // 10000


class Rpc:
    def __init__(self, session: aiohttp.ClientSession, url: str, wallet: str):
        self.session, self.url, self.wallet = session, url, wallet
        self.requestNumber = 0
        # In-flight only: never reuse a quote or chain head after its RPC completes.
        self.blockFlight = None
        self.quoteFlights = {}
        self.methodCounts = {}
        self.estimatedCu = 0

    async def call(self, method: str, params: list) -> object:
        self.requestNumber += 1
        self.methodCounts[method] = self.methodCounts.get(method, 0) + 1
        self.estimatedCu += {'eth_call': 26, 'eth_blockNumber': 10}.get(method, 0)
        try:
            async with self.session.post(self.url, json=dict(jsonrpc='2.0', id=self.requestNumber,
                                                                method=method, params=params),
                                         timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status != 200:
                    raise RuntimeError(f'RPC_HTTP_{response.status}')
                payload = await response.json()
            if 'error' in payload:
                raise RuntimeError('RPC_REVERT_OR_REJECTED')
            return payload['result']
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise RuntimeError(type(error).__name__) from None

    async def ethCall(self, contract: str, data: str, block: str) -> str:
        # Share only identical in-flight requests pinned to the same exact block.
        # Different quantities, blocks or contracts are NEVER cached together.
        key = (contract, data, block)
        task = self.quoteFlights.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self.call('eth_call', [
                {'to': contract, 'from': self.wallet, 'data': data}, block]))
            self.quoteFlights[key] = task

            def clear(done, requestKey=key):
                if self.quoteFlights.get(requestKey) is done:
                    self.quoteFlights.pop(requestKey, None)
                if not done.cancelled():
                    done.exception()  # Consume failures even if waiters cancel.

            task.add_done_callback(clear)
        result = await asyncio.shield(task)
        if not isinstance(result, str) or not result.startswith('0x'):
            raise ValueError('Malformed eth_call result')
        return result

    async def block(self) -> str:
        # Concurrent quote jobs can share a fresh head request, not a cached head.
        task = self.blockFlight
        if task is None or task.done():
            task = asyncio.create_task(self.call('eth_blockNumber', []))
            self.blockFlight = task

            def clear(done):
                if self.blockFlight is done:
                    self.blockFlight = None
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(clear)
        return str(await asyncio.shield(task))


class Quotes:
    def __init__(self, rpc: Rpc, exporter=None):
        self.rpc = rpc
        self.exporter = exporter
        self.context = ContextVar('earlyQuoteContext', default=None)

    def exportEvidence(self, venue, kind, fields):
        context = self.context.get()
        if self.exporter is None or context is None:
            return
        token = context['token']
        if token not in (venue['token0'], venue['token1']):
            return
        record = dict(kind=kind, venueId=venue['id'], phase=context['phase'],
                      route=venue['id'], legRole='tokenPool', selected=False, **fields)
        if kind == 'quote':
            context['quotes'].append(record)
        self.exporter.offer(token, record)

    async def ponsState(self, venue: dict, block: str, buy: bool) -> dict:
        curve = venue['emitter']
        async def read(selector: str) -> int:
            requestedAt = time.time()
            value = await self.rpc.ethCall(curve, '0x' + selector, block)
            name = {PONS_FEE: 'fee', PONS_TAX: 'tax', PONS_SELLABLE: 'sellable',
                    PONS_READY: 'ready'}.get(selector, 'snipe')
            self.exportEvidence(venue, 'state', dict(requestAt=requestedAt, completedAt=time.time(),
                block=block, recipient=self.rpc.wallet, protocolSemanticsUnverified=True,
                **{name: str(int(value, 16))}))
            return int(value, 16)
        requestedAt = time.time()
        reserves = await self.rpc.ethCall(curve, '0x' + PONS_RESERVES, block)
        if len(reserves) < 130:
            raise ValueError('Missing Pons reserves')
        self.exportEvidence(venue, 'state', dict(quote=str(int(reserves[2:66], 16)),
            tokenReserve=str(int(reserves[66:130], 16)), block=block, requestAt=requestedAt,
            completedAt=time.time(), recipient=self.rpc.wallet, protocolSemanticsUnverified=True))
        state = dict(quote=int(reserves[2:66], 16), token=int(reserves[66:130], 16),
                     fee=await read(PONS_FEE), tax=await read(PONS_TAX))
        if buy:
            state['sellable'] = await read(PONS_SELLABLE)
            state['snipe'] = await read(PONS_SNIPE + word(int(self.rpc.wallet, 16)))
        else:
            state['ready'] = bool(await read(PONS_READY))
        return state

    async def quote(self, venue: dict, tokenIn: str, amount: int, block: str) -> dict:
        if amount <= 0:
            raise ValueError('Quote amount must be positive')
        if venue['source'] == 'uniswapV4' and amount >= 2 ** 128:
            raise ValueError('Quote amount exceeds v4 uint128')
        requestedAt = time.time()
        tokenIn = validateAddress(tokenIn)
        t0, t1 = venue['token0'], venue['token1']
        if tokenIn not in (t0, t1):
            raise ValueError('Input not in pool')
        if venue['source'] == 'ponsV2':
            state = await self.ponsState(venue, block, tokenIn != t0)
            if tokenIn == t0:
                output = ponsSell(amount, state['quote'], state['token'], state['fee'],
                                  state['tax'], state['ready'])
                spent = amount
            else:
                output, spent = ponsBuy(amount, state['quote'], state['token'],
                                        state['sellable'], state['fee'], state['tax'], state['snipe'])
            basis = 'PONS_CURVE_MATH'
        elif venue['source'] == 'uniswapV3':
            target = V3_QUOTER
            data = '0x' + V3_SELECTOR + ''.join(map(word, (int(tokenIn, 16),
                    int(t1 if tokenIn == t0 else t0, 16), amount, int(venue['fee']), 0)))
            result = await self.rpc.ethCall(target, data, block)
            if len(result) < 66:
                raise ValueError('Incomplete v3 quote')
            output, spent, basis = int(result[2:66], 16), amount, 'V3_QUOTER'
        elif venue['source'] == 'uniswapV4':
            words = [32, int(t0, 16), int(t1, 16), int(venue['fee']),
                     int(venue['spacing']) % (2 ** 256), int(venue['hooks'], 16),
                     int(tokenIn == t0), amount, 256, 0]
            result = await self.rpc.ethCall(V4_QUOTER, '0x' + V4_SELECTOR + ''.join(map(word, words)), block)
            if len(result) < 130:
                raise ValueError('Incomplete v4 quote')
            output, spent, basis = int(result[2:66], 16), amount, 'V4_QUOTER'
        else:
            raise ValueError('Unsupported venue')
        if self.exporter is not None:
            details = dict(requestAt=requestedAt, completedAt=time.time(), block=block,
                           tokenIn=tokenIn, quantity=str(amount), output=str(output), basis=basis)
            if venue['source'] == 'uniswapV3':
                details['rawResponse'] = result
                details['ticksCrossed'] = int(result[130:194], 16) if len(result) >= 194 else None
            self.exportEvidence(venue, 'quote', details)
        if output <= 0:
            raise ValueError('Quote returned zero')
        return dict(out=output, spent=spent, basis=basis)


class Fx:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.ethGbp = None
        self.updated = 0.0

    async def get(self) -> Decimal:
        if self.ethGbp and time.time() - self.updated < 60:
            return self.ethGbp
        try:
            async with self.session.get('https://api.kraken.com/0/public/Ticker?pair=ETHGBP',
                                        timeout=aiohttp.ClientTimeout(total=5)) as response:
                data = await response.json()
            if data.get('error'):
                raise ValueError('FX API error')
            rate = Decimal(str(next(iter(data['result'].values()))['c'][0]))
            if rate <= 0:
                raise ValueError('Invalid ETH/GBP')
            self.ethGbp, self.updated = rate, time.time()
            return rate
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, StopIteration):
            if self.ethGbp and time.time() - self.updated < 600:
                return self.ethGbp
            raise RuntimeError('ETHGBP_UNAVAILABLE') from None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(
          token TEXT PRIMARY KEY, detected REAL NOT NULL, entry_due REAL NOT NULL,
          entry_status TEXT NOT NULL, quantity TEXT, spent_wei TEXT,
          entry_at REAL, entry_lag REAL, entry_venue TEXT,
          last_growth REAL, best_wei TEXT, best_gbp TEXT, best_multiple TEXT,
          best_at REAL, best_venue TEXT, next_due REAL, state TEXT NOT NULL,
          last_check REAL, errors INTEGER NOT NULL DEFAULT 0,
          recovered INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS venues(
          token TEXT NOT NULL, venue_id TEXT NOT NULL, data TEXT NOT NULL,
          active INTEGER NOT NULL, PRIMARY KEY(token,venue_id));
        CREATE TABLE IF NOT EXISTS observations(
          id INTEGER PRIMARY KEY, token TEXT NOT NULL, phase TEXT NOT NULL,
          at REAL NOT NULL, block TEXT, venue_id TEXT, status TEXT NOT NULL,
          quantity TEXT, out_wei TEXT, gbp TEXT, multiple TEXT,
          basis TEXT, reason TEXT);
        CREATE INDEX IF NOT EXISTS obs_token ON observations(token,at);
        CREATE INDEX IF NOT EXISTS tokens_entry_due ON tokens(entry_status,entry_due);
        CREATE INDEX IF NOT EXISTS tokens_sell_due ON tokens(state,next_due);
        ''')

    def meta(self, key: str, default: str = '0') -> str:
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def saveMeta(self, key: str, value: str) -> None:
        self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def ingest(self, event: dict) -> None:
        # The detector recorded receipt time is authoritative. Replaying an old
        # journal must NEVER manufacture a fresh 5-second entry opportunity.
        try:
            detected = datetime.fromisoformat(event['observedAtUtc'].replace('Z', '+00:00')).timestamp()
        except (KeyError, ValueError, TypeError):
            raise ValueError('Detector event has no valid observedAtUtc') from None
        now = time.time()
        for token, venue in launchVenues(event):
            key = venue['id']
            removed = bool(event['removed'])
            with self.db:
                self.db.execute('''INSERT INTO venues VALUES(?,?,?,?) ON CONFLICT(token,venue_id)
                  DO UPDATE SET data=excluded.data,active=excluded.active''',
                  (token, key, json.dumps(venue), int(not removed)))
                if not removed:
                    self.db.execute('''INSERT OR IGNORE INTO tokens
                      (token,detected,entry_due,entry_status,next_due,state,recovered)
                      VALUES(?,?,?,'PENDING',?,'AWAITING_ENTRY',?)''',
                      (token, detected, detected + ENTRY_DELAY, detected + ENTRY_DELAY, int(bool(event.get('recovered')))))
                    # New routes can revive retired holdings without resetting their entry quantity.
                    # New routes stay available to the token but must not
                    # silently reset the user-specified retirement rule.

    def venues(self, token: str) -> list[dict]:
        return [json.loads(row['data']) for row in self.db.execute(
            'SELECT data FROM venues WHERE token=? AND active=1', (token,))]

    def observe(self, **values) -> None:
        self.db.execute('''INSERT INTO observations
           (token,phase,at,block,venue_id,status,quantity,out_wei,gbp,multiple,basis,reason)
           VALUES(:token,:phase,:at,:block,:venue_id,:status,:quantity,:out_wei,:gbp,:multiple,:basis,:reason)''', values)

    def totals(self) -> dict:
        states = {r['state']: r['count'] for r in self.db.execute(
            'SELECT state,COUNT(*) AS count FROM tokens GROUP BY state')}
        entries = {r['entry_status']: r['count'] for r in self.db.execute(
            'SELECT entry_status,COUNT(*) AS count FROM tokens GROUP BY entry_status')}
        observed = {r['status']: r['count'] for r in self.db.execute(
            'SELECT status,COUNT(*) AS count FROM observations GROUP BY status')}
        top = [dict(row) for row in self.db.execute('''SELECT token,best_multiple,best_gbp,best_venue,state
            FROM tokens WHERE best_multiple IS NOT NULL ORDER BY CAST(best_multiple AS REAL) DESC LIMIT 8''')]
        return dict(cursor=int(self.meta('cursor')), states=states, entries=entries,
                    observations=observed, tokens=sum(states.values()), top=top)


def interval(bestMultiple: Decimal, lastGrowth: float, now: float) -> tuple[str, float]:
    elapsed = max(0, now - lastGrowth)
    if elapsed >= (BLACK_SWAN_RETIRE if bestMultiple >= 25 else ORDINARY_RETIRE):
        return 'RETIRED', 0.0
    if elapsed >= COLD_AFTER:
        return 'COLD', COLD_INTERVAL
    if bestMultiple >= 2:
        return 'HOT', HOT_INTERVAL
    return 'WARM', WARM_INTERVAL


def isOutOfScopeEntry(token: str, venues: list[dict]) -> bool:
    """Exclude only pools with no ETH/WETH/USDG counterpart at entry time.

    Missing venues, malformed pool metadata, or a failed SUPPORTED quote are
    never silently called out-of-scope. Evaluate at +5s, not at detection,
    because a supported pool may appear before the entry decision.
    """
    if not venues:
        return False
    for venue in venues:
        if token == venue.get('token0'):
            counterpart = venue.get('token1')
        elif token == venue.get('token1'):
            counterpart = venue.get('token0')
        else:
            return False
        if not counterpart or counterpart == token or counterpart in BASES:
            return False
    return True


class Tracker:
    def __init__(self, store: Store, detectorPath: Path, rpc: Rpc, fx: Fx, exporter=None):
        self.store, self.detectorPath, self.rpc, self.fx = store, detectorPath, rpc, fx
        self.exporter = exporter
        self.quotes = Quotes(rpc, exporter)
        if exporter is not None:
            for row in store.db.execute('SELECT token,detected FROM tokens WHERE detected>?', (time.time()-45,)):
                exporter.register(row['token'], row['detected'])
        self.active: set[str] = set()
        self.entrySlots = asyncio.Semaphore(1)
        self.sellSlots = asyncio.Semaphore(2)
        self.tasks: set[asyncio.Task] = set()
        self.detector = sqlite3.connect(f'file:{detectorPath.resolve()}?mode=ro', uri=True, timeout=10)
        self.detector.row_factory = sqlite3.Row

    def pull(self) -> int:
        after = int(self.store.meta('cursor'))
        rows = self.detector.execute('SELECT seq,payload FROM event_log WHERE seq>? ORDER BY seq LIMIT 250', (after,)).fetchall()
        for row in rows:
            # A failed parse does not advance the durable cursor.
            event = json.loads(row['payload'])
            self.store.ingest(event)
            if self.exporter is not None:
                detectedAt = datetime.fromisoformat(event['observedAtUtc'].replace('Z', '+00:00')).timestamp()
                for token, _ in launchVenues(event):
                    self.exporter.register(token, detectedAt)
            self.store.saveMeta('cursor', str(row['seq']))
        return len(rows)

    async def best(self, token: str, amount: int, buying: bool) -> dict:
        """Compare complete ETH/WETH or USDG->ETH paths at ONE block.

        All SELL paths take the exact, complete position quantity. Every output
        compared is token quantity (entry) or ETH-equivalent wei (sell).
        Quoter output proves neither an actual router fill nor token approval.
        """
        if self.exporter is not None:
            self.quotes.context.set(dict(token=token, phase='entry' if buying else 'sell', quotes=[]))
        block = await self.rpc.block()
        contenders = []
        failures = []
        usdGBuyBridges = None  # cache one bridge comparison for all USDG entry pools
        researchCandidates = {}

        def rememberCandidate():
            if self.exporter is not None:
                quotes = self.quotes.context.get()['quotes']
                if quotes:
                    researchCandidates[id(contenders[-1])] = quotes[-1]

        async def conversions(inputToken: str, inputAmount: int) -> tuple[list[dict], list[str]]:
            results, problems = [], []
            for bridge in USDG_BRIDGES:
                # v3 deals in WETH; v4 deals in native ETH. Native<->WETH
                # wrapping is 1:1 before gas, which is explicitly excluded.
                actualInput = (WETH if bridge['source'] == 'uniswapV3' else NATIVE)
                if inputToken == USDG:
                    actualInput = USDG
                try:
                    quoted = await self.quotes.quote(bridge, actualInput, inputAmount, block)
                    results.append(dict(out=quoted['out'], basis=quoted['basis'],
                                        venue=bridge['id']))
                except (ValueError, RuntimeError) as error:
                    problems.append(f"{bridge['id']}:{str(error)[:45]}")
            return results, problems

        for venue in self.store.venues(token):
            counterpart = venue['token1'] if token == venue['token0'] else venue['token0']
            if counterpart in (NATIVE, WETH):
                try:
                    sampledAt = time.time()
                    quoted = await self.quotes.quote(
                        venue, counterpart if buying else token, amount, block)
                    contenders.append(dict(venue=venue['id'], source=venue['source'],
                                           out=quoted['out'], spent=quoted['spent'],
                                           basis=quoted['basis'], sampledAt=sampledAt))
                    rememberCandidate()
                except (ValueError, RuntimeError) as error:
                    failures.append(f"{venue['id'][:12]}:{str(error)[:55]}")
            elif counterpart == USDG:
                if buying:
                    if usdGBuyBridges is None:
                        usdGBuyBridges = await conversions('ETH', amount)
                    bridges, problems = usdGBuyBridges
                    failures.extend(problems)
                    for bridge in bridges:
                        try:
                            sampledAt = time.time()
                            quoted = await self.quotes.quote(venue, USDG, bridge['out'], block)
                            contenders.append(dict(
                                venue=bridge['venue'] + '>' + venue['id'],
                                source=venue['source'], out=quoted['out'], spent=amount,
                                basis=bridge['basis'] + '>' + quoted['basis'],
                                sampledAt=sampledAt))
                            rememberCandidate()
                        except (ValueError, RuntimeError) as error:
                            failures.append(f"{venue['id'][:12]}:{str(error)[:55]}")
                else:
                    try:
                        # Entire ORIGINAL position first becomes USDG, then ETH.
                        sampledAt = time.time()
                        quoted = await self.quotes.quote(venue, token, amount, block)
                        bridges, problems = await conversions(USDG, quoted['out'])
                        failures.extend(problems)
                        for bridge in bridges:
                            contenders.append(dict(
                                venue=venue['id'] + '>' + bridge['venue'],
                                source=venue['source'], out=bridge['out'], spent=amount,
                                basis=quoted['basis'] + '>' + bridge['basis'],
                                sampledAt=sampledAt))
                            rememberCandidate()
                    except (ValueError, RuntimeError) as error:
                        failures.append(f"{venue['id'][:12]}:{str(error)[:55]}")
            else:
                # Pool initialisation is not proof of a funded ETH entry path.
                failures.append('NO_ETH_CONVERSION:' + counterpart)
        if not contenders:
            raise ValueError('NO_EXECUTABLE_QUOTE:' + ','.join(failures[:3]))
        chosen = max(contenders, key=lambda x: x['out'])
        chosen['block'] = block
        chosen['routesChecked'] = len(contenders)
        if self.exporter is not None:
            selectedEvidence = researchCandidates.get(id(chosen))
            chosen['_researchQuotes'] = [selectedEvidence] if selectedEvidence is not None else []
        return chosen

    async def entry(self, token: str) -> None:
        async with self.entrySlots:
            row = self.store.db.execute('SELECT * FROM tokens WHERE token=?', (token,)).fetchone()
            if not row or row['entry_status'] != 'PENDING':
                return
            started = time.time()
            lag = started - row['entry_due']
            if isOutOfScopeEntry(token, self.store.venues(token)):
                # Intentionally excluded token/token pool, not a failed quote.
                # No RPC, FX fetch, or futile entry retries. Keep the event
                # and venue records; do not claim a simulated £0.50 position.
                self.store.db.execute("""UPDATE tokens SET entry_status='OUT_OF_SCOPE',
                   state='OUT_OF_SCOPE', entry_at=?,entry_lag=?,next_due=NULL WHERE token=?""",
                   (started, lag, token))
                self.store.observe(token=token, phase='ENTRY', at=started, block=None,
                                   venue_id=None, status='OUT_OF_SCOPE', quantity=None,
                                   out_wei=None, gbp=None, multiple=None, basis=None,
                                   reason='NONBASE_TOKEN_PAIR_NO_KNOWN_ETH_ANCHOR')
                return
            if lag > ENTRY_TOLERANCE:
                self.store.db.execute('''UPDATE tokens SET entry_status='MISSED_DEADLINE',
                   state='UNRESOLVED',entry_lag=?,next_due=NULL WHERE token=?''', (lag, token))
                self.store.observe(token=token, phase='ENTRY', at=started, block=None,
                                   venue_id=None, status='MISSED_DEADLINE', quantity=None, out_wei=None,
                                   gbp=None, multiple=None, basis=None, reason='Entry started too late')
                return
            block = None
            try:
                rate = await self.fx.get()
                amount = int((Decimal('0.50') / rate * 10**18).to_integral_value(rounding=ROUND_DOWN))
                result = await self.best(token, amount, True)
                block = result['block']
                # The position must be actionable when the whole route decision
                # is RECEIVED, not merely when an eth_call is first dispatched.
                completedAt = time.time()
                lag = completedAt - row['entry_due']
                if lag > ENTRY_TOLERANCE:
                    self.store.db.execute('''UPDATE tokens SET entry_status='MISSED_DEADLINE',
                      state='UNRESOLVED',entry_lag=?,next_due=NULL WHERE token=?''', (lag, token))
                    self.store.observe(token=token, phase='ENTRY', at=completedAt, block=block,
                                       venue_id=result['venue'], status='MISSED_DEADLINE', quantity=None,
                                       out_wei=None, gbp=None, multiple=None, basis=result['basis'],
                                       reason='Quote acquired after entry deadline; no position assumed')
                    return
                status = 'QUOTED' if abs(lag) <= 0.5 else 'LATE_ENTRY'
                finished = time.time()
                if self.exporter is not None:
                    poolIds = result['venue'].split('>')
                    for evidence in result.get('_researchQuotes', []):
                        if evidence['venueId'] in poolIds:
                            selected = dict(evidence, selected=True, route=result['venue'],
                                            selectedAt=completedAt, phase='entry')
                            self.exporter.offer(token, selected)
                self.store.db.execute('''UPDATE tokens SET entry_status=?,quantity=?,spent_wei=?,
                  entry_at=?,entry_lag=?,entry_venue=?,last_growth=?,state='WARM',next_due=? WHERE token=?''',
                  (status, str(result['out']), str(result['spent']), finished, lag, result['venue'],
                   finished, finished, token))
                self.store.observe(token=token, phase='ENTRY', at=started, block=block,
                                   venue_id=result['venue'], status=status, quantity=str(result['out']),
                                   out_wei=None, gbp='0.50', multiple=None,
                                   basis=result['basis'], reason='FX ETHGBP=' + str(rate) +
                                   '; inputWei=' + str(amount) + '; spentWei=' + str(result['spent']))
            except (ValueError, RuntimeError) as error:
                finished = time.time()
                elapsed = finished - row['entry_due']
                previousRetries = self.store.db.execute('''SELECT COUNT(*) FROM observations
                   WHERE token=? AND phase='ENTRY' AND status='RETRYABLE_UNAVAILABLE' ''',
                   (token,)).fetchone()[0]
                retry = (elapsed + ENTRY_RETRY_DELAY <= ENTRY_TOLERANCE
                         and previousRetries < MAX_ENTRY_ATTEMPTS - 1)
                if retry:
                    # A temporary missing route, RPC rejection or empty pool must
                    # not immediately and permanently exclude a fresh token.
                    self.store.db.execute('''UPDATE tokens SET next_due=? WHERE token=?''',
                                          (finished + ENTRY_RETRY_DELAY, token))
                    status = 'RETRYABLE_UNAVAILABLE'
                else:
                    status = 'MISSED_DEADLINE' if elapsed > ENTRY_TOLERANCE else 'UNAVAILABLE'
                    self.store.db.execute('''UPDATE tokens SET entry_status=?,state='UNRESOLVED',
                       entry_at=?,entry_lag=?,next_due=NULL WHERE token=?''',
                       (status, finished, elapsed, token))
                self.store.observe(token=token, phase='ENTRY', at=finished, block=block,
                                   venue_id=None, status=status, quantity=None, out_wei=None,
                                   gbp=None, multiple=None, basis=None, reason=str(error)[:200])

    async def sell(self, token: str) -> None:
        async with self.sellSlots:
            row = self.store.db.execute('SELECT * FROM tokens WHERE token=?', (token,)).fetchone()
            if not row or row['quantity'] is None or row['state'] == 'RETIRED':
                return
            now = time.time()
            elapsed = now - (row['last_growth'] or row['entry_at'] or now)
            bestMultiple = Decimal(row['best_multiple'] or '0')
            state, seconds = interval(bestMultiple, row['last_growth'] or now, now)
            if state == 'RETIRED':
                self.store.db.execute("UPDATE tokens SET state='RETIRED',next_due=NULL WHERE token=?", (token,))
                return
            try:
                # Token quantity is fixed by the purchase quote. Every sell quotes ALL tokens.
                result = await self.best(token, int(row['quantity']), False)
                rate = await self.fx.get()
                value = Decimal(result['out']) * rate / Decimal(10**18)
                multiple = value / Decimal('0.50')
                if self.exporter is not None:
                    completedAt = time.time()
                    fxAt = getattr(self.fx, 'updated', None)
                    self.exporter.offer(token, dict(kind='quote', phase='sell', selected=True,
                        fullOriginalQuantity=True, quantity=row['quantity'], grossGbp=str(value),
                        route=result['venue'], block=result['block'], requestAt=now,
                        completedAt=completedAt, fx=str(rate), fxAt=fxAt,
                        fxAge=completedAt-fxAt if fxAt else None,
                        qualification='quoteNotFillBeforeGas'))
                # The reported peak is GBP output, not ETH wei. ETH/GBP can move.
                isGrowth = row['best_gbp'] is None or value > Decimal(row['best_gbp'])
                if isGrowth:
                    state, seconds = interval(multiple, now, now)
                    self.store.db.execute('''UPDATE tokens SET best_wei=?,best_gbp=?,best_multiple=?,
                       best_at=?,best_venue=?,last_growth=? WHERE token=?''',
                       (str(result['out']), str(value), str(multiple), now, result['venue'], now, token))
                else:
                    state, seconds = interval(bestMultiple, row['last_growth'] or now, now)
                self.store.db.execute('''UPDATE tokens SET state=?,next_due=?,last_check=? WHERE token=?''',
                    (state, now + seconds if state != 'RETIRED' else None, now, token))
                self.store.observe(token=token, phase='SELL', at=now, block=result['block'],
                                   venue_id=result['venue'], status='ONCHAIN_QUOTE_ONLY',
                                   quantity=row['quantity'], out_wei=str(result['out']),
                                   gbp=str(value), multiple=str(multiple), basis=result['basis'], reason=None)
            except (RuntimeError, ValueError) as error:
                state, seconds = interval(bestMultiple, row['last_growth'] or now, now)
                self.store.db.execute('''UPDATE tokens SET state=?,next_due=?,last_check=?,errors=errors+1
                  WHERE token=?''', (state, now + seconds if state != 'RETIRED' else None, now, token))
                self.store.observe(token=token, phase='SELL', at=now, block=None,
                                   venue_id=None, status='UNAVAILABLE', quantity=row['quantity'],
                                   out_wei=None, gbp=None, multiple=None, basis=None, reason=str(error)[:200])

    async def runOne(self, token: str, entry: bool) -> None:
        try:
            if entry:
                await self.entry(token)
            else:
                await self.sell(token)
        finally:
            self.active.discard(token)

    async def run(self) -> None:
        chain = int(await self.rpc.call('eth_chainId', []), 16)
        if chain != CHAIN_ID:
            raise RuntimeError('RPC is not Robinhood Chain 4663')
        print(f'PEAK TRACKER LIVE | wallet={self.rpc.wallet} | simulated £0.50 | no transactions', flush=True)
        lastReport = 0.0
        while True:
            self.pull()
            now = time.time()
            # New entries first; don't let the routine sell queue block them.
            dueEntries = self.store.db.execute('''SELECT token FROM tokens WHERE entry_status='PENDING'
                       AND entry_due<=? AND next_due<=? ORDER BY entry_due LIMIT 20''',
                       (now, now)).fetchall()
            for row in dueEntries:
                token = row['token']
                if token not in self.active:
                    self.active.add(token)
                    task = asyncio.create_task(self.runOne(token, True))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            sells = self.store.db.execute('''SELECT token,best_multiple,last_growth,detected
              FROM tokens WHERE state IN ('WARM','HOT','COLD') AND next_due<=?
              ORDER BY CASE WHEN CAST(COALESCE(best_multiple,'0') AS REAL)>=2 THEN 0 ELSE 1 END,
                       next_due LIMIT 20''', (now,)).fetchall()
            for row in sells:
                token = row['token']
                if token not in self.active:
                    self.active.add(token)
                    task = asyncio.create_task(self.runOne(token, False))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            if now - lastReport >= 30:
                stats = self.store.totals()
                print(f"HEALTH {utc()} tokens={stats['tokens']} states={stats['states']} "
                      f"entries={stats['entries']} quoteStatus={stats['observations']} cursor={stats['cursor']} "
                      f"rpcCuEstimate={self.rpc.estimatedCu} "
                      f"rpcCalls={self.rpc.methodCounts}", flush=True)
                for x in stats['top'][:3]:
                    print(f"  BEST {x['token']} {x['best_multiple']}x £{x['best_gbp']} {x['state']}", flush=True)
                lastReport = now
            await asyncio.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description='Fresh read-only Robinhood peak tracker')
    parser.add_argument('command', choices=['run', 'status'])
    parser.add_argument('--detector-db', type=Path, default=Path('data/detector.sqlite3'))
    parser.add_argument('--db', type=Path, default=Path('data/peak.sqlite3'))
    parser.add_argument('--env', type=Path, default=Path.home() / 'blackswanRH_guarded/.env')
    parser.add_argument('--feature-export', type=Path, help='Optional early research JSONL handoff')
    parser.add_argument('--wallet-address', help='Public 0x... wallet address; never a private key')
    parser.add_argument('--wallet-file', type=Path, help='Existing encrypted wallet.json keystore (reads public address only)')
    args = parser.parse_args()
    if args.command == 'status':
        if not args.db.exists():
            raise SystemExit(f'Peak database missing: {args.db}')
        s = Store(args.db)
        print(json.dumps(s.totals(), indent=2))
        return
    if not args.detector_db.exists():
        raise SystemExit('Detector DB missing; start the unchanged detector first')
    url, wallet = loadSettings(args.env, args.wallet_address, args.wallet_file)
    import fcntl
    lockPath = args.db.with_suffix(args.db.suffix + '.lock')
    lock = lockPath.open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('Peak tracker already running on this database') from None
    store = Store(args.db)
    exporter = None
    if args.feature_export is not None:
        from research.quote_export import QuoteExporter
        exporter = QuoteExporter(args.feature_export)
    async def runner():
        async with aiohttp.ClientSession() as session:
            tracker = Tracker(store, args.detector_db, Rpc(session, url, wallet), Fx(session), exporter)
            try:
                await tracker.run()
            finally:
                for task in tracker.tasks:
                    task.cancel()
                if tracker.tasks:
                    await asyncio.gather(*tracker.tasks, return_exceptions=True)
                tracker.detector.close()
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print('Peak tracker stopped; data retained')
    finally:
        if exporter is not None:
            exporter.close()
        store.db.close()
        lock.close()


if __name__ == '__main__':
    main()