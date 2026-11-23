#!/usr/bin/env python3
"""Launch one fresh detector + read-only £0.50 peak experiment from one terminal.

No detector modifications, wallet decryption, signing, or real trades.
Ctrl+C stops both workers; each experiment's databases and logs are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid


def newExperimentId() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]


def validateWalletFile(path: Path) -> str:
    """Only read the public address; encrypted key material stays untouched."""
    from peak_tracker import publicKeystoreAddress
    if not path.is_file():
        raise ValueError(f'Wallet not found: {path}')
    return publicKeystoreAddress(path)


def detectorReady(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True, timeout=0.2) as db:
            return db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='event_log'"
            ).fetchone()[0] == 1
    except sqlite3.Error:
        return False


def readLines(name: str, stream, messages: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, ''):
            messages.put((name, line.rstrip('\n')))
    finally:
        stream.close()


def startWorker(name: str, args: list[str], messages: queue.Queue) -> subprocess.Popen:
    worker = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )
    threading.Thread(target=readLines, args=(name, worker.stdout, messages),
                     daemon=True).start()
    return worker


def stopWorkers(workers: list[subprocess.Popen]) -> None:
    """Stop children even when the parent receives Ctrl+C."""
    for worker in workers:
        if worker.poll() is None:
            try:
                os.killpg(worker.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
    for worker in workers:
        if worker.poll() is None:
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(worker.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait()


def drain(messages: queue.Queue) -> None:
    while True:
        try:
            name, line = messages.get_nowait()
        except queue.Empty:
            break
        print(f'[{name}] {line}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='One-command NEW Robinhood detector and £0.50 peak tracker experiment')
    parser.add_argument('--record-features', action='store_true', help='Enable independent early feature recorder')
    parser.add_argument('--wallet-file', type=Path,
                        default=Path.home() / 'Desktop/wallet.json')
    parser.add_argument('--env', type=Path,
                        default=Path.home() / 'blackswanRH_guarded/.env')
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    python = root / '.venv/bin/python'
    if not python.is_file():
        parser.error(f'Existing project Python not found: {python}')
    walletFile, envFile = args.wallet_file.expanduser().resolve(), args.env.expanduser().resolve()
    try:
        wallet = validateWalletFile(walletFile)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if not envFile.is_file():
        parser.error(f'Detector environment file not found: {envFile}')

    experiment = newExperimentId()
    dataDir = args.data_dir.expanduser().resolve() / ('fresh_peaks_' + experiment)
    dataDir.mkdir(parents=True, exist_ok=False)
    detectorDb, peakDb = dataDir / 'detector.sqlite3', dataDir / 'peak.sqlite3'
    metadata = {'experimentId': experiment, 'wallet': wallet,
                'detectorDatabase': str(detectorDb), 'trackerDatabase': str(peakDb),
                'startedUtc': datetime.now(timezone.utc).isoformat(),
                'evidence': 'ONCHAIN_QUOTE_ONLY_NOT_FILLED',
                'detectorEstimatedCu': 'FRESH_LOCAL_ROLLING_COUNTER_NOT_PROVIDER_RESET'}
    (dataDir / 'experiment.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'FRESH EXPERIMENT {experiment}', flush=True)
    print(f'Wallet: {wallet} (read-only public address from Desktop wallet.json)', flush=True)
    print(f'Data: {dataDir}', flush=True)
    print('New detector local CU counter. Alchemy account usage itself is NOT reset.', flush=True)
    print('Ctrl+C stops both workers; data remains saved. No trades are sent.', flush=True)

    detectorArgs = [str(python), '-u', str(root / 'detector.py'), 'run',
                    '--env', str(envFile), '--db', str(detectorDb)]
    trackerArgs = [str(python), '-u', str(root / 'peak_tracker.py'), 'run',
                   '--env', str(envFile), '--wallet-file', str(walletFile),
                   '--detector-db', str(detectorDb), '--db', str(peakDb)]
    messages = queue.Queue()
    workers = []
    try:
        detector = startWorker('DETECTOR', detectorArgs, messages)
        workers.append(detector)
        # Wait only for the durable SQLite outbox; not for unrelated backfill or
        # an initial launch. This minimizes latency before monitoring starts.
        deadline = time.monotonic() + 20
        while not detectorReady(detectorDb):
            drain(messages)
            if detector.poll() is not None:
                print('ERROR: detector exited before its database was ready.', flush=True)
                return 1
            if time.monotonic() >= deadline:
                print('ERROR: detector did not initialise its database within 20s.', flush=True)
                return 1
            time.sleep(0.1)
        recorder = None
        recorderReported = False
        if args.record_features:
            featureDb = dataDir / 'research.sqlite3'
            quoteFile = dataDir / 'early_quote_evidence.jsonl'
            recorderArgs = [str(python), '-u', '-m', 'research.recorder', 'run',
                            '--env', str(envFile), '--detector-db', str(detectorDb),
                            '--db', str(featureDb), '--quote-file', str(quoteFile)]
            # Use absolute script location so this command also works outside the project cwd.
            recorderArgs = [str(python), '-u', '-c',
                            'import sys; sys.path.insert(0, ' + repr(str(root)) + '); '
                            'from research.recorder import main; main()', *recorderArgs[4:]]
            recorder = startWorker('RESEARCH', recorderArgs, messages)
            workers.append(recorder)
            trackerArgs.extend(['--feature-export', str(quoteFile)])
        tracker = startWorker('PEAK', trackerArgs, messages)
        workers.append(tracker)
        while True:
            drain(messages)
            if recorder is not None and recorder.poll() is not None and not recorderReported:
                recorderReported = True
                print(f'ERROR: RESEARCH exited with status {recorder.returncode}; detector and tracker continue', flush=True)
            if any(worker.poll() is not None for worker in (detector, tracker)):
                drain(messages)
                for name, worker in [('DETECTOR', detector), ('PEAK', tracker)]:
                    if worker.poll() is not None:
                        print(f'ERROR: {name} exited with status {worker.returncode}', flush=True)
                return 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        print('\nStopping detector and peak tracker...', flush=True)
        return 0
    finally:
        stopWorkers(workers)
        drain(messages)
        print(f'Experiment saved in {dataDir}', flush=True)


if __name__ == '__main__':
    sys.exit(main())