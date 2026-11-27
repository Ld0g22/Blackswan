"""Shared paced transport for manual sweeps, including signed broadcasts."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time

import aiohttp
import peak_tracker as peak


class ProviderUnavailable(RuntimeError):
    """Pause the wallet run while preserving its durable transaction journal."""


def transientFailure(error):
    text=str(error)
    return any(code in text for code in ('PROVIDER_PAUSED', 'RPC_HTTP_429', 'RPC_HTTP_502', 'RPC_HTTP_503', 'RPC_HTTP_504', 'TimeoutError', 'ClientConnectorError', 'ServerDisconnectedError'))


class PacedSession:
    def __init__(self, session, interval=0.5, attempts=5, backoff=1.0):
        self.session=session
        self.interval=interval
        self.attempts=attempts
        self.backoff=backoff
        self.lock=asyncio.Lock()
        self.nextRequest=0.0
        self.paused=False
        self.retries=0
        self.requests=0

    def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    def retryAfter(self, headers):
        value=headers.get('Retry-After','0')
        try:
            return max(0,float(value))
        except (ValueError,TypeError):
            try:
                return max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError,TypeError,OverflowError):
                return 0

    @asynccontextmanager
    async def post(self, url, **kwargs):
        # Hold ownership through response decoding. All callers share cooldown.
        async with self.lock:
            if self.paused:
                raise ProviderUnavailable('PROVIDER_PAUSED; rerun to resume')
            for attempt in range(self.attempts):
                await asyncio.sleep(max(0,self.nextRequest-time.monotonic()))
                retryDelay=0
                handedOff=False
                try:
                    self.requests+=1
                    self.nextRequest=time.monotonic()+self.interval
                    async with self.session.post(url,**kwargs) as response:
                        try:
                            payload=await response.json(content_type=None)
                        except (ValueError,aiohttp.ContentTypeError):
                            payload=None
                        error=payload.get('error') if isinstance(payload,dict) else None
                        throttled=response.status==429 or (isinstance(error,dict) and error.get('code')==429)
                        transient=throttled or response.status in (408,500,502,503,504) or (response.status==200 and not isinstance(payload,dict))
                        if not transient:
                            handedOff=True
                            yield response
                            return
                        retryDelay=self.retryAfter(response.headers)
                        if throttled:
                            self.interval=min(4.0,max(self.interval*1.5,0.5))
                except (aiohttp.ClientError,asyncio.TimeoutError):
                    if handedOff:
                        raise
                if attempt==self.attempts-1:
                    self.paused=True
                    raise ProviderUnavailable('PROVIDER_PAUSED after bounded retries; rerun to resume')
                self.retries+=1
                delay=max(retryDelay,self.backoff*2**attempt)
                if delay>30:
                    self.paused=True
                    raise ProviderUnavailable('PROVIDER_PAUSED; provider cooldown exceeds 30 seconds')
                print('Provider cooling down · retry',attempt+1,'in',round(delay,1),'seconds',flush=True)
                self.nextRequest=max(self.nextRequest,time.monotonic()+delay)


class ManualRpc(peak.Rpc):
    async def call(self, method, params):
        self.requestNumber+=1
        self.methodCounts[method]=self.methodCounts.get(method,0)+1
        async with self.session.post(self.url,json={'jsonrpc':'2.0','id':self.requestNumber,'method':method,'params':params},timeout=aiohttp.ClientTimeout(total=12)) as response:
            payload=await response.json(content_type=None)
            if response.status!=200:
                raise RuntimeError('RPC_HTTP_'+str(response.status))
            if not isinstance(payload,dict):
                raise ProviderUnavailable('PROVIDER_PAUSED malformed RPC response')
            error=payload.get('error')
            if error is not None:
                raise RuntimeError('RPC_REVERT_OR_REJECTED')
            if 'result' not in payload:
                raise ProviderUnavailable('PROVIDER_PAUSED missing RPC result')
            return payload['result']
