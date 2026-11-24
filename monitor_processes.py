"""Read-only Linux /proc checks for the *exact* BlackSwan campaign workers.

Never infer 'alive' from a journal's old ACTIVE flag. Do not log any argv,
credentials or environment. Never signal/kill processes.
"""
from __future__ import annotations

from pathlib import Path


def findRunWorkers(directory,procRoot=Path("/proc")):
    directory=Path(directory).expanduser().resolve()
    root=Path(procRoot)
    result={"executor":None,"selector":None,"detector":None,"available":root.is_dir()}
    if not result["available"]:
        return result
    expected={
        "executor":("live_executor.py",directory/"execution.sqlite3"),
        "selector":("concentration_selector.py",directory/"selector.sqlite3"),
        "detector":("detector.py",directory/"detector.sqlite3")
    }
    found={name:[] for name in expected}
    try:
        children=root.iterdir()
        for proc in children:
            if not proc.name.isdecimal():
                continue
            try:
                with (proc/"cmdline").open("rb") as f:
                    raw=f.read(65536)
                parts=[p.decode("utf-8","surrogateescape") for p in raw.split(b"\0") if p]
            except (PermissionError,FileNotFoundError,ProcessLookupError,OSError):
                continue
            if not parts:
                continue
            scriptNames={Path(p).name for p in parts[:4]}
            location=None
            for i,argument in enumerate(parts):
                if argument=="--db" and i+1<len(parts):
                    location=parts[i+1]
                    break
                if argument.startswith("--db="):
                    location=argument[len("--db="):]
                    break
            if not location:
                continue
            path=Path(location)
            if not path.is_absolute():
                continue  # Cannot prove the target's original working dir.
            resolved=path.resolve()
            for name,(script,db) in expected.items():
                if script in scriptNames and resolved==db:
                    found[name].append(int(proc.name))
    except (PermissionError,OSError):
        result["available"]=False
        return result
    for name,pids in found.items():
        # Ambiguous duplicates are a critical condition; don't pick one PID.
        result[name]=(pids[0] if len(pids)==1 else "MULTIPLE_MATCHING_PIDS"
                      if len(pids)>1 else None)
    return result
