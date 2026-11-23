"""Offline regression tests for honest 100%-position ETH/USDG routing."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from peak_tracker import CHAIN_ID, NATIVE, USDG, WETH, Store, Tracker, USDG_BRIDGES

TOKEN = '0x' + '11' * 20
OTHER = '0x' + '22' * 20
POOL = '0x' + '33' * 32


def event(base=USDG, token=TOKEN):
    return {'schemaVersion': 1, 'chainId': CHAIN_ID, 'source': 'uniswapV4',
            'details': {'currency0': NATIVE if base == NATIVE else min(base, token),
                        'currency1': token if base == NATIVE else max(base, token),
                        'poolId': POOL, 'fee': 500, 'tickSpacing': 10, 'hooks': NATIVE},
            'removed': False, 'recovered': False,
            'observedAtUtc': datetime.fromtimestamp(1000, timezone.utc).isoformat()}


class Rpc:
    wallet = '0x'+'ab'*20
    async def block(self):
        return '0xaaaa'


class Fx:
    async def get(self):
        return Decimal('2000')


class Routes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.detector = self.path / 'detector.sqlite3'
        with sqlite3.connect(self.detector) as db:
            db.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,payload TEXT)')
        self.store = Store(self.path / 'peak.sqlite3')
        self.tracker = Tracker(self.store, self.detector, Rpc(), Fx())
        self.store.ingest(event())
        self.calls = []
        self.failV3 = False
        self.failV4 = False

        async def quote(venue, tokenIn, amount, block):
            self.calls.append((venue['id'], tokenIn, amount, block))
            if venue['id'] == USDG_BRIDGES[0]['id']:
                if self.failV3:
                    raise RuntimeError('QUOTE_REVERT')
                if tokenIn == WETH:
                    return {'out': 600_000, 'spent': amount, 'basis': 'V3_QUOTER'}
                self.assertEqual(tokenIn, USDG)
                return {'out': 10**15, 'spent': amount, 'basis': 'V3_QUOTER'}
            if venue['id'] == USDG_BRIDGES[1]['id']:
                if self.failV4:
                    raise RuntimeError('QUOTE_REVERT')
                if tokenIn == NATIVE:
                    return {'out': 610_000, 'spent': amount, 'basis': 'V4_QUOTER'}
                self.assertEqual(tokenIn, USDG)
                return {'out': 2*10**15, 'spent': amount, 'basis': 'V4_QUOTER'}
            self.assertEqual(venue['id'], POOL)
            if tokenIn == USDG:
                # Entry uses the full USDG conversion output with native 6 decimal units.
                return {'out': amount*50, 'spent': amount, 'basis': 'V4_QUOTER'}
            self.assertEqual(tokenIn, TOKEN)
            return {'out': 900_000, 'spent': amount, 'basis': 'V4_QUOTER'}

        self.tracker.quotes.quote = quote

    async def asyncTearDown(self):
        self.tracker.detector.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_entry_quotes_full_fifty_pence_through_best_usdg_bridge(self):
        result = await self.tracker.best(TOKEN, 10**15, True)
        self.assertEqual(result['out'], 610_000*50)
        self.assertEqual(result['spent'], 10**15)
        self.assertIn('eth-usdg-v4-500-10-nohook', result['venue'])
        self.assertIn((POOL, USDG, 610_000, '0xaaaa'), self.calls)
        self.assertTrue(all(call[3] == '0xaaaa' for call in self.calls))

    async def test_sell_converts_exact_full_quantity_to_eth_at_same_block(self):
        quantity = 123_456_789
        result = await self.tracker.best(TOKEN, quantity, False)
        self.assertEqual(result['out'], 2*10**15)
        self.assertEqual(result['spent'], quantity)
        self.assertEqual(self.calls[0], (POOL, TOKEN, quantity, '0xaaaa'))
        self.assertIn((USDG_BRIDGES[0]['id'], USDG, 900_000, '0xaaaa'), self.calls)
        self.assertIn((USDG_BRIDGES[1]['id'], USDG, 900_000, '0xaaaa'), self.calls)

    async def test_fallback_v3_when_v4_bridge_reverts(self):
        self.failV4 = True
        result = await self.tracker.best(TOKEN, 1234, False)
        self.assertEqual(result['out'], 10**15)
        self.assertIn('weth-usdg-v3-500', result['venue'])

    async def test_no_conversion_does_not_invent_entry(self):
        self.failV3 = self.failV4 = True
        with self.assertRaisesRegex(ValueError, 'NO_EXECUTABLE_QUOTE'):
            await self.tracker.best(TOKEN, 10**15, True)
        self.assertEqual(self.store.db.execute('SELECT entry_status FROM tokens').fetchone()[0], 'PENDING')

    async def test_unknown_to_unknown_pool_without_anchor_remains_unresolved(self):
        self.store.db.execute('DELETE FROM venues')
        self.store.ingest(event(base=OTHER))
        with self.assertRaisesRegex(ValueError, 'NO_ETH_CONVERSION'):
            await self.tracker.best(TOKEN, 10**15, True)

    async def test_entire_position_recorded_after_quoted_usdg_entry_and_sell(self):
        # Entry is five seconds after detector observed the token at t=1000.
        with patch('peak_tracker.time.time', return_value=1005.01):
            await self.tracker.entry(TOKEN)
        row = self.store.db.execute('SELECT quantity,entry_status FROM tokens WHERE token=?',
                                    (TOKEN,)).fetchone()
        self.assertEqual(row['entry_status'], 'QUOTED')
        self.assertEqual(row['quantity'], str(610_000*50))
        self.calls.clear()
        with patch('peak_tracker.time.time', return_value=1007):
            await self.tracker.sell(TOKEN)
        self.assertEqual(self.calls[0], (POOL, TOKEN, 610_000*50, '0xaaaa'))
        peak = self.store.db.execute('SELECT best_wei,best_multiple FROM tokens WHERE token=?',
                                     (TOKEN,)).fetchone()
        self.assertEqual(peak['best_wei'], str(2*10**15))
        self.assertEqual(Decimal(peak['best_multiple']), Decimal('8'))

    async def test_late_quote_response_is_not_mistaken_for_on_time_entry(self):
        now = {'value': 1005.01}
        async def slowQuote(venue, tokenIn, amount, block):
            if venue['id'] in (bridge['id'] for bridge in USDG_BRIDGES):
                return {'out': 600_000, 'spent': amount, 'basis': 'V3_QUOTER'}
            now['value'] = 1009.0  # route returns after the +3s tolerance
            return {'out': 1000, 'spent': amount, 'basis': 'V4_QUOTER'}
        self.tracker.quotes.quote = slowQuote
        with patch('peak_tracker.time.time', side_effect=lambda: now['value']):
            await self.tracker.entry(TOKEN)
        row = self.store.db.execute('SELECT entry_status,quantity,entry_lag FROM tokens').fetchone()
        self.assertEqual(row['entry_status'], 'MISSED_DEADLINE')
        self.assertIsNone(row['quantity'])
        self.assertGreater(row['entry_lag'], 3.0)

    async def test_transient_quote_failure_retry_then_success_in_window(self):
        now = {'value': 1005.05}
        self.failV3 = self.failV4 = True
        with patch('peak_tracker.time.time', side_effect=lambda: now['value']):
            await self.tracker.entry(TOKEN)
        row = self.store.db.execute('SELECT entry_status,quantity,next_due FROM tokens').fetchone()
        self.assertEqual(row['entry_status'], 'PENDING')
        self.assertIsNone(row['quantity'])
        self.assertGreater(row['next_due'], now['value'])
        self.assertEqual(self.store.db.execute('SELECT status FROM observations ORDER BY id DESC LIMIT 1').fetchone()[0],
                         'RETRYABLE_UNAVAILABLE')
        now['value'] += 0.6
        self.failV3 = self.failV4 = False
        with patch('peak_tracker.time.time', side_effect=lambda: now['value']):
            await self.tracker.entry(TOKEN)
        row = self.store.db.execute('SELECT entry_status,quantity FROM tokens').fetchone()
        self.assertEqual(row['entry_status'], 'LATE_ENTRY')
        self.assertEqual(row['quantity'], str(610_000*50))

    async def test_failed_retries_do_not_continue_unbounded(self):
        self.failV3 = self.failV4 = True
        now = {'value': 1005.05}
        for _ in range(5):
            with patch('peak_tracker.time.time', side_effect=lambda: now['value']):
                await self.tracker.entry(TOKEN)
            now['value'] += 0.6
        row = self.store.db.execute('SELECT entry_status,quantity FROM tokens').fetchone()
        self.assertEqual(row['entry_status'], 'UNAVAILABLE')
        self.assertIsNone(row['quantity'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations WHERE status='RETRYABLE_UNAVAILABLE'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)