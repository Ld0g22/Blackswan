"""Fresh tracker offline regression tests: no credentials, network or wallet signing."""
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from peak_tracker import (CHAIN_ID, NATIVE, Store, Tracker, Quotes, word,
                          ponsBuy, ponsSell, interval, launchVenues,
                          ENTRY_DELAY, WETH, V3_QUOTER, V4_QUOTER,
                          loadSettings, publicKeystoreAddress)

TOKEN = '0x' + '11' * 20
WALLET = '0x' + 'ab' * 20
POOL = '0x' + '22' * 20
POOL2 = '0x' + '33' * 20

def event(source='ponsV2', epoch=100., pool=POOL, removed=False):
    details = ({'token': TOKEN, 'curve': pool, 'pairToken': NATIVE}
               if source == 'ponsV2' else
               {'token0': NATIVE, 'token1': TOKEN, 'pool': pool, 'fee': 3000}
               if source == 'uniswapV3' else
               {'currency0': NATIVE, 'currency1': TOKEN, 'poolId': '0x'+'44'*32,
                'fee': 3000, 'tickSpacing': 60, 'hooks': NATIVE})
    return {'schemaVersion': 1, 'chainId': CHAIN_ID, 'source': source,
            'details': details, 'removed': removed, 'recovered': False,
            'observedAtUtc': datetime.fromtimestamp(epoch,timezone.utc).isoformat()}

class FakeRpc:
    wallet = WALLET
    def __init__(self):
        self.calls=[]
        self.outputs={}
    async def block(self):
        return '0x101'
    async def ethCall(self, to, data, block):
        self.calls.append((to,data,block))
        return self.outputs.get((to,data[:10]), '0x'+word(700000000000000)+word(80000))

class FixedFx:
    async def get(self):
        return Decimal('2000')

class WalletSettingsTests(unittest.TestCase):
    def test_reuses_existing_public_wallet_keystore(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'runtime').mkdir()
            keystore = base / 'runtime/wallet.json'
            # V3 keystore address metadata is plaintext; no private key needed.
            keystore.write_text(json.dumps({'version': 3, 'address': WALLET[2:], 'crypto': {}}))
            (base / '.env').write_text('ROBINHOOD_ALCHEMY_HTTP_URL=https://rpc.example/v2/key\n')
            # Isolate both fallback locations, even when the real machine has wallets.
            with (patch.dict(os.environ, {}, clear=True),
                  patch('peak_tracker.Path.home', return_value=base),
                  patch('peak_tracker.Path.cwd', return_value=base)):
                url, wallet = loadSettings(base / '.env')
            self.assertEqual(url, 'https://rpc.example/v2/key')
            self.assertEqual(wallet, WALLET)

    def test_explicit_wallet_override_and_no_secret_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            keystore = base / 'custom-wallet.json'
            keystore.write_text(json.dumps({'address': TOKEN[2:], 'crypto': {'ciphertext': 'DO_NOT_USE'}}))
            (base / '.env').write_text('ROBINHOOD_ALCHEMY_HTTP_URL=https://rpc.example/v2/key\n')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(loadSettings(base / '.env', walletAddress=WALLET)[1], WALLET)
                self.assertEqual(loadSettings(base / '.env', walletFile=keystore)[1], TOKEN)
            self.assertEqual(publicKeystoreAddress(keystore), TOKEN)

    def test_missing_wallet_fails_without_silent_fabrication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('ROBINHOOD_ALCHEMY_HTTP_URL=https://rpc.example/v2/key\n')
            with (patch.dict(os.environ, {}, clear=True),
                  patch('peak_tracker.Path.home', return_value=Path(folder)),
                  patch('peak_tracker.Path.cwd', return_value=Path(folder))):
                with self.assertRaisesRegex(ValueError, '--wallet-address'):
                    loadSettings(path)

class MathsTests(unittest.TestCase):
    def test_full_position_sell_is_not_a_fraction(self):
        tokens = 1_000_000
        self.assertEqual(ponsSell(tokens, 1000_000, 2_000_000, 100, 200, False),
                         (tokens*1_000_000//3_000_000) -
                         (tokens*1_000_000//3_000_000)//100 -
                         (tokens*1_000_000//3_000_000)*2//100)
        with self.assertRaises(ValueError):
            ponsSell(tokens, 1000_000, 2_000_000, 100, 200, True)
    def test_pons_buy_restricted_sellable_and_tax(self):
        total, spent=ponsBuy(1000, 1000, 10000, 1000, 100, 100, 300)
        self.assertEqual(total, 1000)
        self.assertLess(spent, 1000)
    def test_no_false_tradeable_peaks(self):
        self.assertEqual(interval(Decimal('2.01'),1000,1030),('HOT',3))
        self.assertEqual(interval(Decimal('10'),1000,1190),('COLD',60))
        self.assertEqual(interval(Decimal('10'),1000,1600),('RETIRED',0))
        self.assertEqual(interval(Decimal('25'),1000,1600),('COLD',60))
        self.assertEqual(interval(Decimal('25'),1000,8200),('RETIRED',0))
    def test_all_sources_decode(self):
        self.assertEqual(launchVenues(event('ponsV2'))[0][0],TOKEN)
        self.assertEqual(launchVenues(event('uniswapV3'))[0][0],TOKEN)
        self.assertEqual(launchVenues(event('uniswapV4'))[0][0],TOKEN)

class QuoteEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_wallet_address_and_exact_amount(self):
        rpc=FakeRpc(); q=Quotes(rpc)
        venue=launchVenues(event('uniswapV3'))[0][1]
        value=123456789
        result=await q.quote(venue,TOKEN,value,'0x101')
        self.assertEqual(result['spent'],value)
        self.assertEqual(rpc.calls[0][0],V3_QUOTER)
        self.assertTrue(rpc.calls[0][1].endswith(word(0)))
        self.assertIn(word(value),rpc.calls[0][1])
    async def test_v4_exact_quantity(self):
        rpc=FakeRpc(); q=Quotes(rpc)
        venue=launchVenues(event('uniswapV4'))[0][1]
        result=await q.quote(venue,TOKEN,987654321,'0xabc')
        self.assertEqual(result['spent'],987654321)
        self.assertEqual(rpc.calls[0][0],V4_QUOTER)
        self.assertIn(word(987654321),rpc.calls[0][1])
        with self.assertRaises(ValueError):
            await q.quote(venue,TOKEN,2**128,'latest')

class DurableTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.trackerPath=Path(self.tmp.name)/'peak.sqlite3'
        self.detectorPath=Path(self.tmp.name)/'detector.sqlite3'
        self.detector=sqlite3.connect(self.detectorPath)
        self.detector.execute('CREATE TABLE event_log(seq INTEGER PRIMARY KEY,payload TEXT)')
        self.store=Store(self.trackerPath)
        self.rpc=FakeRpc()
        self.tracker=Tracker(self.store,self.detectorPath,self.rpc,FixedFx())
    async def asyncTearDown(self):
        self.tracker.detector.close()
        self.store.db.close()
        self.detector.close()
        self.tmp.cleanup()
    def addEvent(self,seq,evt):
        self.detector.execute('INSERT INTO event_log VALUES (?,?)',(seq,json.dumps(evt)))
        self.detector.commit()
    async def test_replay_does_not_invent_fresh_entry(self):
        self.addEvent(1,event(epoch=1000))
        with patch('peak_tracker.time.time',return_value=1500):
            self.assertEqual(self.tracker.pull(),1)
            row=self.store.db.execute('SELECT * FROM tokens WHERE token=?',(TOKEN,)).fetchone()
            self.assertEqual(row['entry_due'],1005)
            await self.tracker.entry(TOKEN)
        row=self.store.db.execute('SELECT entry_status,quantity FROM tokens').fetchone()
        self.assertEqual(row['entry_status'],'MISSED_DEADLINE')
        self.assertIsNone(row['quantity'])
        self.assertEqual(int(self.store.meta('cursor')),1)
    async def test_new_pool_does_not_reset_entry_or_quantity(self):
        self.addEvent(1,event(epoch=1000))
        self.addEvent(2,event(source='uniswapV3',epoch=1020,pool=POOL2))
        with patch('peak_tracker.time.time',return_value=1000):
            self.tracker.pull()
        self.store.db.execute("UPDATE tokens SET quantity='456',entry_status='QUOTED',state='WARM' WHERE token=?",(TOKEN,))
        self.assertEqual(len(self.store.venues(TOKEN)),2)
        self.assertEqual(self.store.db.execute('SELECT quantity FROM tokens').fetchone()[0],'456')
        self.addEvent(3,event(source='ponsV2',epoch=1030,removed=True))
        self.tracker.pull()
        self.assertEqual(len(self.store.venues(TOKEN)),1)
        self.assertEqual(self.store.db.execute('SELECT quantity FROM tokens').fetchone()[0],'456')
    async def test_full_size_sell_and_peak_durable(self):
        self.addEvent(1,event(epoch=1000))
        self.tracker.pull()
        with patch('peak_tracker.time.time',return_value=1010):
            self.store.db.execute('''UPDATE tokens SET quantity=?,entry_status='QUOTED',state='WARM',
                last_growth=?,entry_at=?,next_due=? WHERE token=?''', ('8000000',1005,1005,1005,TOKEN))
            self.tracker.quotes.quote=self.fakeQuote
            await self.tracker.sell(TOKEN)
        row=self.store.db.execute('SELECT * FROM tokens WHERE token=?',(TOKEN,)).fetchone()
        self.assertEqual(row['best_wei'],'2000000000000000')
        self.assertEqual(Decimal(row['best_multiple']),Decimal('8'))
        self.assertEqual(row['state'],'HOT')
        obs=self.store.db.execute('SELECT * FROM observations').fetchone()
        self.assertEqual(obs['quantity'],'8000000')
        self.assertEqual(obs['status'],'ONCHAIN_QUOTE_ONLY')
        self.assertEqual(self.expectedAmounts,[8000000])
        self.store.db.close();self.store=Store(self.trackerPath)
        self.assertEqual(self.store.db.execute('SELECT best_multiple FROM tokens').fetchone()[0],'8')
    async def fakeQuote(self,venue,tokenIn,amount,block):
        self.expectedAmounts=getattr(self,'expectedAmounts',[])+[amount]
        return {'out':2000000000000000,'spent':amount,'basis':'PONS_CURVE_MATH'}
    async def test_unsupported_conversion_visible_not_zero(self):
        self.store.ingest(event(epoch=1000))
        self.store.db.execute('UPDATE venues SET data=?', (json.dumps({
            'id':POOL,'source':'ponsV2','token0':TOKEN,'token1':'0x'+'55'*20,'emitter':POOL}),))
        with self.assertRaisesRegex(ValueError,'NO_EXECUTABLE_QUOTE'):
            await self.tracker.best(TOKEN,1000,False)
    async def test_bad_event_does_not_advance_cursor(self):
        self.detector.execute("INSERT INTO event_log VALUES(1,'not-json')")
        self.detector.commit()
        with self.assertRaises(json.JSONDecodeError):
            self.tracker.pull()
        self.assertEqual(self.store.meta('cursor'),'0')

if __name__=='__main__':
    unittest.main(verbosity=2)