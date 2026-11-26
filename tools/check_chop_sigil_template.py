#!/usr/bin/env python3
"""Recover CHOP/SIGIL initialization evidence from local campaign DBs, offline."""
import argparse
import json
import os
import sqlite3
from pathlib import Path
from eth_utils import keccak

POOLS={
    '0x930f9db5500bd697f2c8a415606501dd72d642472bdcb45f1960d9bb110330db':('CHOP','0x1c8004f2003af04582877fde5cf46765338a0ec7'),
    '0x0539bdfb93806341f800d1f60d62260dbc44cdfdfd1c27dda6236243b599704e':('SIGIL','0xc4c8b397546d145b8979e64e91b8d26093fad7fc'),
}
TOPIC='0x'+keccak(text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex()
MANAGER='0x8366a39cc670b4001a1121b8f6a443a643e40951'
ZERO='0x'+'0'*40

def decode(v):
    if not isinstance(v,dict):return None
    t=v.get('topics')
    if not isinstance(t,list) or len(t)!=4 or str(t[0]).lower()!=TOPIC or str(v.get('address')).lower()!=MANAGER:return None
    pool=t[1].lower()
    if pool not in POOLS:return None
    b=bytes.fromhex(v['data'][2:])
    if len(b)!=160:raise ValueError('incorrect initialization data length')
    w=[b[i:i+32] for i in range(0,160,32)]
    c0,c1=['0x'+x[-40:].lower() for x in t[2:]]
    fee=int.from_bytes(w[0],'big');spacing=int.from_bytes(w[1],'big',signed=True)
    hooks='0x'+w[2][-20:].hex();sqrt=int.from_bytes(w[3],'big');tick=int.from_bytes(w[4],'big',signed=True)
    if {c0,c1}!={ZERO,POOLS[pool][1]}:raise ValueError('unexpected pool currencies')
    return dict(name=POOLS[pool][0],poolId=pool,currency0=c0,currency1=c1,fee=fee,tickSpacing=spacing,hooks=hooks,initialTick=tick,sqrtPriceX96=str(sqrt),transactionHash=v.get('transactionHash'),blockHash=v.get('blockHash'),blockNumber=v.get('blockNumber'),excluded=(fee==100 and spacing==1 and hooks==ZERO and tick==184216))

def visit(v):
    if isinstance(v,dict):
        d=decode(v)
        if d:yield d
        for value in v.values():yield from visit(value)
    elif isinstance(v,list):
        for value in v:yield from visit(value)
    elif isinstance(v,str) and v.lstrip().startswith(('{','[')) and any(p in v.lower() for p in POOLS):
        try:x=json.loads(v)
        except ValueError:return
        yield from visit(x)

def ident(s):return '"'+s.replace('"','""')+'"'

def scan(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if not any(s in table.lower() for s in ('event','launch','decision','selection','detect','watch','venue')):continue
            for col in db.execute('PRAGMA table_info('+ident(table)+')'):
                name=col[1]
                if any(s in name.lower() for s in ('secret','private','password','credential','signature','signed')):continue
                if col[2].upper() not in ('TEXT','JSON','VARCHAR',''):continue
                q='SELECT '+ident(name)+' FROM '+ident(table)+' WHERE lower(CAST('+ident(name)+' AS TEXT)) LIKE ? OR lower(CAST('+ident(name)+' AS TEXT)) LIKE ?'
                for (value,) in db.execute(q,tuple('%'+p+'%' for p in POOLS)):
                    if not isinstance(value,str):continue
                    for result in visit(value):yield result
    finally:db.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('roots',nargs='*',type=Path)
    a=p.parse_args()
    roots=a.roots or [x for x in Path.home().iterdir() if x.is_dir() and 'blackswan' in x.name.lower()]
    found={};errors=[];seen=set()
    for root in roots:
        for directory,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if d not in ('.git','.venv','venv','node_modules','__pycache__')]
            for f in files:
                if not f.lower().endswith(('.sqlite3','.sqlite','.db')):continue
                path=Path(directory)/f
                if path.resolve() in seen:continue
                seen.add(path.resolve())
                try:
                    for item in scan(path):
                        key=(item['poolId'],item['transactionHash'],item['blockHash'],item['initialTick'])
                        if key not in found:
                            found[key]=item;print(json.dumps(dict(source=str(path),**item)),flush=True)
                except (sqlite3.Error,ValueError,OSError) as e:errors.append(dict(path=str(path),error=type(e).__name__))
    for pool,(name,_) in POOLS.items():
        matches=[d for d in found.values() if d['poolId']==pool]
        if not matches:print(name,'UNKNOWN: initialization log not recovered')
        elif len({(d['initialTick'],d['transactionHash'],d['blockHash']) for d in matches})!=1:print(name,'UNKNOWN: conflicting initialization records')
        else:print(name,'EXCLUDE' if matches[0]['excluded'] else 'RETAIN','initial tick',matches[0]['initialTick'])
    print('Databases inspected:',len(seen),'read/decode errors:',len(errors))
    for e in errors:print(json.dumps(e))

if __name__=='__main__':main()
