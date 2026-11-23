"""Small, read-only, in-process v3/v4 venue watcher for OUR confirmed holdings.

After five 10p buys, the broad detector can retire; the existing executor
continues quoting/selling and this WS listener notices later pool migrations.
HTTP getLogs is only used to backfill a bounded sequence (10 blocks/request),
not repeatedly poll the entire high-throughput Robinhood chain.
"""
import asyncio
import json
import time

import aiohttp
import detector
import peak_tracker as peak
from swap_adapters import PONS_HOOK


class HeldVenueWatcher:
    def __init__(self,executor):
        self.owner=executor
        self.store=executor.store
        self.rpc=executor.rpc
        self.wsUrl=None  # Set from the SAME verified .env by original live launcher.
        self.task=None

    def _held(self):
        return {r[0].lower() for r in self.store.db.execute('SELECT token FROM positions')}

    def _apply(self,raw):
        """Reuse canonical detector ABI and existing route catalogue, no fake pools."""
        if not isinstance(raw,dict):raise ValueError('Malformed pool log')
        specs=(detector.SOURCES['uniswapV3'],detector.SOURCES['uniswapV4'])
        addr=raw.get('address')
        topics=raw.get('topics')
        if (not isinstance(addr,str) or not isinstance(topics,list) or not topics
                or not any(addr.lower()==spec['address'] and
                           isinstance(topics[0],str) and
                           topics[0].lower()==spec['topic'] for spec in specs)):
            return
        event=detector.decodeLog(raw,recovered=True)
        held=self._held()
        for token,venue in peak.launchVenues(event):
            if token not in held:continue
            if venue['source']=='uniswapV4' and venue.get('hooks','').lower()==PONS_HOOK:
                continue
            self.store.setVenue(token,venue,not bool(event['removed']))
            print('HELD TOKEN VENUE',token,venue['source'],
                  'REMOVED' if event['removed'] else 'AVAILABLE',flush=True)

    def _cursor(self,head):
        raw=self.store.get('heldVenueCursor','')
        if raw:return max(0,int(raw)-20)
        row=self.owner.detector.execute(
            "SELECT value FROM meta WHERE key='lastConnectedHead'").fetchone()
        if not row or not row[0]:
            raise RuntimeError('DETECTOR_LAST_HEAD_NOT_AVAILABLE')
        anchor=int(row[0],16) if str(row[0]).startswith('0x') else int(row[0])
        if anchor>head+2:raise RuntimeError('DETECTOR_HEAD_AHEAD_OF_RPC')
        return max(0,anchor-20)

    async def _backfill(self,target):
        begin=self._cursor(target)+1
        if begin>target:return
        self.store.set('venueWatchState','RECOVERING')
        while begin<=target:
            end=min(begin+9,target)
            specs=[detector.SOURCES[x] for x in ('uniswapV3','uniswapV4')]
            rpcFilter=dict(fromBlock=hex(begin),toBlock=hex(end),
                 address=[x['address'] for x in specs],
                 topics=[[x['topic'] for x in specs]])
            logs=await self.rpc.call('eth_getLogs',[rpcFilter])
            if not isinstance(logs,list):
                raise RuntimeError('INVALID_HELD_VENUE_LOG_RESULT')
            for raw in logs:
                self._apply(raw)
            # Only acknowledge a block range after the ENTIRE batch decodes.
            self.store.set('heldVenueCursor',end)
            self.store.set('venueWatchHead',end)
            begin=end+1
            if begin<=target:await asyncio.sleep(0.35)

    async def _connect(self):
        if not self.wsUrl:raise RuntimeError('HELD_VENUE_WS_URL_NOT_CONFIGURED')
        self.store.set('venueWatchState','CONNECTING')
        specs=[detector.SOURCES[x] for x in ('uniswapV3','uniswapV4')]
        async with self.rpc.session.ws_connect(self.wsUrl,heartbeat=20,
                                                max_msg_size=2_000_000) as ws:
            pending={i+1:s for i,s in enumerate(specs)}
            for idx,spec in pending.items():
                await ws.send_json(dict(jsonrpc='2.0',id=idx,method='eth_subscribe',
                    params=['logs',dict(address=spec['address'],topics=[spec['topic']])]))
            while pending:
                message=await ws.receive(timeout=20)
                if message.type!=aiohttp.WSMsgType.TEXT:
                    raise RuntimeError('HELD_VENUE_WS_SUBSCRIPTION_CLOSED')
                data=json.loads(message.data)
                if data.get('id') in pending:
                    if not isinstance(data.get('result'),str):
                        raise RuntimeError('HELD_VENUE_SUBSCRIPTION_REFUSED')
                    pending.pop(data['id'])
                elif data.get('method')=='eth_subscription':
                    raw=data.get('params',{}).get('result')
                    if raw is not None:self._apply(raw)
            # Subscribe FIRST, backfill SECOND. No hole between the handoff.
            head=int(await self.rpc.block(),16)
            await self._backfill(head)
            last= int(self.store.get('venueWatchHead','0'))
            if last<head:raise RuntimeError('HELD_VENUE_RECOVERY_INCOMPLETE')
            self.store.set('venueWatchState','READY')
            self.store.set('venueWatchError','')
            self.store.set('venueWatchLastOk',time.time())
            async for message in ws:
                if message.type!=aiohttp.WSMsgType.TEXT:
                    raise RuntimeError('HELD_VENUE_SOCKET_CLOSED')
                data=json.loads(message.data)
                if data.get('method')=='eth_subscription':
                    raw=data.get('params',{}).get('result')
                    if raw is not None:self._apply(raw)
                    self.store.set('venueWatchLastOk',time.time())
            raise RuntimeError('HELD_VENUE_SOCKET_ENDED')

    async def _forever(self):
        while True:
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except (RuntimeError,ValueError,TypeError,KeyError,IndexError,
                    aiohttp.ClientError,asyncio.TimeoutError) as exc:
                self.store.set('venueWatchState','DEGRADED')
                self.store.set('venueWatchError',(type(exc).__name__+':'+str(exc))[:160])
                print('HELD VENUE WATCH RETRY',type(exc).__name__,str(exc)[:120],flush=True)
                await asyncio.sleep(2.0)

    async def scanOnce(self):
        """Idempotent start of the one background watcher; never signs."""
        if self.task is None or self.task.done():
            self.task=asyncio.create_task(self._forever())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
