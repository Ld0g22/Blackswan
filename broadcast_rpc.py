"""Single-purpose Robinhood Chain signed-transaction broadcaster.

Keeps the actual JSON-RPC rejection code/message instead of converting every
response to RPC_REVERT_OR_REJECTED. Never prints raw transactions or RPC URLs.
"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp


class BroadcastError(RuntimeError):
    def __init__(self, kind, detail, definite=False):
        self.kind = kind
        self.detail = str(detail)[:260].replace('\n', ' ').replace('\r', ' ')
        self.definite = bool(definite)
        super().__init__(f'{kind}: {self.detail}')


def definitiveRejection(code, message):
    """Classify clear RPC pre-admission rejections, without clearing nonce holds.

    Even a rejected signed intent stays locked until separately reconciled;
    nonce-too-low and already-known results are always ambiguous here.
    """
    message = str(message).lower()
    if any(part in message for part in (
            'already known', 'already imported', 'nonce too low',
            'known transaction', 'replacement transaction',
            'execution reverted', 'timeout', 'temporarily',
            'internal error', 'context deadline')):
        return False
    if code in (-32600, -32601, -32602):
        return True
    return any(part in message for part in (
        'insufficient funds', 'gas limit too high',
        'intrinsic gas too low', 'invalid sender', 'invalid signature',
        'unsupported transaction type', 'transaction type not supported',
        'wrong chain', 'invalid chain id',
        'max fee per gas less than', 'fee cap less than',
        'fee cap too low', 'gas price too low',
        'transaction underpriced', 'max gas limit'))


async def sendRaw(rpc, rawHex):
    """Submit identical signed bytes through the configured Alchemy RPC.

    Does not decrypt keys, mutate transactions, choose alternative nonces,
    or fall back to a different endpoint. Calling this CAN broadcast money.
    """
    if not isinstance(rawHex, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', rawHex):
        raise ValueError('Malformed signed transaction hex')
    if not hasattr(rpc, 'session') or not hasattr(rpc, 'url'):
        # Unit-test RPCs explicitly implement call; production uses the
        # detailed transport below, preserving compatibility with old fakes.
        return await rpc.call('eth_sendRawTransaction', [rawHex])
    rpc.requestNumber += 1
    # Include direct submission in the existing per-method and CU telemetry.
    if hasattr(rpc, 'methodCounts'):
        rpc.methodCounts['eth_sendRawTransaction'] = rpc.methodCounts.get('eth_sendRawTransaction', 0) + 1
    if hasattr(rpc, 'estimatedCu'):
        rpc.estimatedCu += 40  # Alchemy Robinhood Chain endpoint documentation
    requestId = rpc.requestNumber
    try:
        async with rpc.session.post(
            rpc.url,
            json={'jsonrpc': '2.0', 'id': requestId,
                  'method': 'eth_sendRawTransaction', 'params': [rawHex]},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as response:
            status = response.status
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise BroadcastError('HTTP_UNPARSEABLE', f'status={status}', False) from None
    except BroadcastError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise BroadcastError('TRANSPORT_UNCERTAIN', type(exc).__name__, False) from None
    if not isinstance(payload, dict):
        raise BroadcastError('MALFORMED_RPC', f'status={status}', False)
    error = payload.get('error')
    if error:
        if not isinstance(error, dict):
            raise BroadcastError('RPC_ERROR_UNPARSEABLE', f'status={status}', False)
        code = error.get('code', 'unknown')
        message = error.get('message', '(no message)')
        definite = definitiveRejection(code, message)
        # Provider rate limiting/authentication never proves onchain inclusion.
        # Only the explicitly classified RPC pre-admission errors are final.
        raise BroadcastError('RPC_REJECTED' if definite else 'RPC_UNCERTAIN',
                             f'code={code} message={message}', definite)
    if status != 200:
        # Never interpret an HTTP failure as a successful submission.
        raise BroadcastError('HTTP_UNCERTAIN', f'status={status}', False)
    txHash = payload.get('result')
    if not isinstance(txHash, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', txHash):
        raise BroadcastError('MISSING_OR_BAD_TX_HASH', f'status={status}', False)
    return txHash
