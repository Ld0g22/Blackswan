#!/usr/bin/env python3
"""Create a protected Robinhood Chain 4663 RPC environment from NEW Alchemy.

Default source: ~/blackswan-modular/newalchemy.txt (the separately verified
new account). Verifies HTTP chain identity and WebSocket subscription *before*
persisting a new env. Never logs or passes API keys to a subprocess.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

import aiohttp

EXPECTED_HOST = "robinhood-mainnet.g.alchemy.com"
EXPECTED_CHAIN_ID = 4663
SOURCE_DEFAULT = Path.home() / "blackswan-modular" / "newalchemy.txt"
ENV_DEFAULT = Path.home() / ".config" / "blackswan" / "concentration-new-alchemy.env"
HTTP_KEY = "ROBINHOOD_ALCHEMY_HTTP_URL"
WS_KEY = "ROBINHOOD_ALCHEMY_WSS_URL"

URL_PATTERN = re.compile(r'(?:https|wss)://[A-Za-z0-9._-]+/v2/[A-Za-z0-9_-]+')


def _token(url: str,scheme: str) -> str:
    if type(url) is not str:
        raise ValueError("INVALID_ALCHEMY_ENDPOINT")
    parts = urlsplit(url)
    if (parts.scheme != scheme or parts.hostname != EXPECTED_HOST or
            parts.port is not None or parts.username is not None or
            parts.password is not None or parts.query or parts.fragment or
            not re.fullmatch(r"/v2/[A-Za-z0-9_-]+",parts.path) or
            parts.netloc != EXPECTED_HOST):
        raise ValueError("NOT_ROBINHOOD_MAINNET_ALCHEMY_"+scheme.upper())
    return parts.path[4:]


def validateAlchemyUrls(httpUrl: str, websocketUrl: str) -> tuple[str,str]:
    h=_token(httpUrl,"https")
    w=_token(websocketUrl,"wss")
    if not secrets.compare_digest(h,w):
        raise ValueError("NEW_ALCHEMY_HTTP_WSS_KEY_MISMATCH")
    return httpUrl,websocketUrl


def parseCredentials(text: str) -> tuple[str,str]:
    """Accept URL lines, named .env pairs, simple JSON, or a named API key."""
    if type(text) is not str:
        raise ValueError("INVALID_NEW_ALCHEMY_SOURCE")
    values={}
    try:
        obj=json.loads(text)
        if isinstance(obj,dict):
            values.update({str(k):v for k,v in obj.items() if isinstance(v,str)})
    except (ValueError,TypeError):
        pass
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line=line[7:].strip()
        if "=" in line:
            name,value=line.split("=",1)
            name=name.strip()
            value=value.strip().strip("'\"")
            values[name]=value
        elif ":" in line and not line.startswith(("http:", "https:", "wss:")):
            # The original 20-Nov "newalchemy.txt" used descriptive labels,
            # e.g. "New Alchemy API key: ..." and "HTTP endpoint: ...".
            label,value=line.split(":",1)
            normalized=re.sub(r"[^a-z0-9]","",label.lower())
            if normalized in ("apikey","alchemyapikey","newalchemyapikey",
                              "robinhoodalchemyapikey"):
                values["ALCHEMY_API_KEY"]=value.strip().strip("'\\\"")

    def chosen(keys):
        candidates={values[k].strip() for k in keys if isinstance(values.get(k),str)
                    and values[k].strip()}
        if len(candidates)>1:
            raise ValueError("AMBIGUOUS_NEW_ALCHEMY_SOURCE")
        return next(iter(candidates),None)
    httpUrl=chosen((HTTP_KEY,"BSRH_RPC_HTTP","httpUrl","http","rpcHttpUrl"))
    websocketUrl=chosen((WS_KEY,"BSRH_RPC_WS","websocketUrl","wsUrl","wss","rpcWebsocketUrl"))
    urls=URL_PATTERN.findall(text)
    detectedHttp={u for u in urls if u.startswith("https://")}
    detectedWs={u for u in urls if u.startswith("wss://")}
    if httpUrl is None:
        if len(detectedHttp)>1:raise ValueError("AMBIGUOUS_HTTP_URL")
        httpUrl=next(iter(detectedHttp),None)
    if websocketUrl is None:
        if len(detectedWs)>1:raise ValueError("AMBIGUOUS_WSS_URL")
        websocketUrl=next(iter(detectedWs),None)
    key=chosen(("ALCHEMY_API_KEY","ROBINHOOD_ALCHEMY_API_KEY","apiKey","api_key"))
    if key is not None and not re.fullmatch(r"[A-Za-z0-9_-]+",key):
        raise ValueError("INVALID_ALCHEMY_API_KEY_FORMAT")
    # Previous successful installers accepted only the new HTTP endpoint and
    # API key, deriving wss:// for the SAME Alchemy host and key.
    if httpUrl:
        urlKey=_token(httpUrl,"https")
        if key and not secrets.compare_digest(key,urlKey):
            raise ValueError("NEW_ALCHEMY_API_KEY_ENDPOINT_MISMATCH")
        if not websocketUrl:
            websocketUrl=f"wss://{EXPECTED_HOST}/v2/{urlKey}"
    elif websocketUrl:
        urlKey=_token(websocketUrl,"wss")
        if key and not secrets.compare_digest(key,urlKey):
            raise ValueError("NEW_ALCHEMY_API_KEY_ENDPOINT_MISMATCH")
        httpUrl=f"https://{EXPECTED_HOST}/v2/{urlKey}"
    elif key:
        httpUrl=f"https://{EXPECTED_HOST}/v2/{key}"
        websocketUrl=f"wss://{EXPECTED_HOST}/v2/{key}"
    else:
        raise ValueError("NEW_ALCHEMY_HTTP_ENDPOINT_OR_KEY_MISSING")
    return validateAlchemyUrls(httpUrl,websocketUrl)


def requireSameNewCredentials(env: Path, source: Path) -> bool:
    """Fail closed when a runner points at the old/incorrect .env file."""
    env=Path(env).expanduser().resolve()
    source=Path(source).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("NEW_ALCHEMY_CREDENTIALS_SOURCE_MISSING")
    if not env.is_file():
        raise RuntimeError("PREPARED_NEW_ALCHEMY_ENV_MISSING")
    try:
        expected=parseCredentials(source.read_text(encoding="utf-8"))
        actual=parseCredentials(env.read_text(encoding="utf-8"))
    except (ValueError,TypeError,UnicodeError,OSError) as exc:
        raise RuntimeError("INVALID_NEW_ALCHEMY_CONFIG") from None
    # No URL/API key values are reported in the exception or caller logs.
    if not all(secrets.compare_digest(a,b) for a,b in zip(expected,actual)):
        raise RuntimeError("NEW_ALCHEMY_CREDENTIALS_MISMATCH")
    return True


def prepareEnvFile(destination: Path,httpUrl: str,websocketUrl: str):
    """Idempotent, 0600, never overwrite different credentials."""
    validateAlchemyUrls(httpUrl,websocketUrl)
    destination=Path(destination).expanduser()
    if destination.is_symlink():
        raise RuntimeError("NEW_ALCHEMY_DESTINATION_SYMLINK_REFUSED")
    body=f"{HTTP_KEY}={httpUrl}\n{WS_KEY}={websocketUrl}\n"
    if destination.exists():
        if not destination.is_file() or destination.read_text(encoding="utf-8")!=body:
            raise RuntimeError("NEW_ALCHEMY_ENV_EXISTS_DIFFERENT_REFUSE_OVERWRITE")
        destination.chmod(0o600)
        return
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    destination.parent.chmod(0o700)
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as output:
            output.write(body)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def verifyProvider(httpUrl: str,websocketUrl: str):
    """Explicit provider chain and WS-subscription verification; zero writes."""
    validateAlchemyUrls(httpUrl,websocketUrl)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        try:
            async with session.post(httpUrl,json={
                "jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]
            }) as response:
                if response.status!=200:
                    raise RuntimeError("NEW_ALCHEMY_HTTP_FAILED")
                payload=await response.json()
            if int(payload["result"],16)!=EXPECTED_CHAIN_ID:
                raise RuntimeError("NEW_ALCHEMY_WRONG_CHAIN_HTTP")
        except RuntimeError:
            raise
        except (aiohttp.ClientError,asyncio.TimeoutError,KeyError,ValueError,
                TypeError,UnicodeError) as exc:
            raise RuntimeError("NEW_ALCHEMY_HTTP_FAILED") from None
        try:
            async with session.ws_connect(websocketUrl,heartbeat=10,timeout=10) as ws:
                await ws.send_json({
                    "jsonrpc":"2.0","id":2,"method":"eth_chainId","params":[]})
                message=await ws.receive_json(timeout=10)
                if int(message["result"],16)!=EXPECTED_CHAIN_ID:
                    raise RuntimeError("NEW_ALCHEMY_WRONG_CHAIN_WS")
                await ws.send_json({
                    "jsonrpc":"2.0","id":3,"method":"eth_subscribe",
                    "params":["newHeads"]})
                subscription=None
                for _ in range(5):
                    response=await ws.receive_json(timeout=10)
                    if response.get("id")==3:
                        subscription=response.get("result")
                        break
                if not isinstance(subscription,str) or not subscription:
                    raise RuntimeError("NEW_ALCHEMY_WS_SUBSCRIPTION_FAILED")
                await ws.send_json({
                    "jsonrpc":"2.0","id":4,"method":"eth_unsubscribe",
                    "params":[subscription]})
                # Do not wait for a head; subscription acceptance proves WS access.
        except RuntimeError:
            raise
        except (aiohttp.ClientError,asyncio.TimeoutError,KeyError,ValueError,
                TypeError,UnicodeError) as exc:
            raise RuntimeError("NEW_ALCHEMY_WS_FAILED") from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials",type=Path,default=SOURCE_DEFAULT,
                        help="Local NEW Alchemy source (default: ~/blackswan-modular/newalchemy.txt)")
    parser.add_argument("--output",type=Path,default=ENV_DEFAULT,
                        help="New restricted environment file outside the git repository")
    parser.add_argument("--check-only",action="store_true",
                        help="Verify HTTP+WSS without writing an env file")
    args=parser.parse_args()
    if not args.credentials.expanduser().is_file():
        parser.error("NEW_ALCHEMY_CREDENTIALS_SOURCE_MISSING")
    try:
        httpUrl,websocketUrl=parseCredentials(
            args.credentials.expanduser().read_text(encoding="utf-8"))
        asyncio.run(verifyProvider(httpUrl,websocketUrl))
        if not args.check_only:
            prepareEnvFile(args.output,httpUrl,websocketUrl)
            requireSameNewCredentials(args.output,args.credentials)
            print("PASS_NEW_ALCHEMY_ENV_CONFIGURED; HTTP_AND_WS_CHAIN_4663")
            print("ENV_FILE",args.output.expanduser())
        else:
            print("PASS_NEW_ALCHEMY_HTTP_AND_WS_CHAIN_4663_CHECK_ONLY")
    except (ValueError,RuntimeError,OSError,UnicodeError) as exc:
        # Exception messages are deliberately fixed strings: never print URLs.
        parser.error(str(exc))


if __name__=="__main__":
    main()
