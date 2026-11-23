"""Regression tests for detector v0.1. No credentials, external calls or trades."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aiohttp import web, ClientSession

import detector


def abiAddress(address: str) -> str:
    return address.lower()[2:].rjust(64, "0")


def uint(value: int) -> str:
    return hex(value & ((1 << 256) - 1))[2:].rjust(64, "0")


def sint(value: int, bits: int) -> str:
    return uint(value if value >= 0 else (1 << 256) + value)


def logTemplate(source: str, txNumber: int = 1, logNumber: int = 0) -> dict:
    event = detector.SOURCES[source]
    log = {
        "address": event["address"],
        "blockNumber": "0x3e8",
        "blockHash": "0x" + "a1" * 32,
        "transactionHash": "0x" + (hex(txNumber)[2:].rjust(64, "0")),
        "logIndex": hex(logNumber),
        "removed": False,
    }
    if source == "ponsV2":
        log["topics"] = [event["topic"], "0x" + abiAddress("0x" + "11" * 20),
                         "0x" + abiAddress("0x" + "22" * 20), "0x" + abiAddress("0x" + "33" * 20)]
        log["data"] = "0x" + abiAddress("0x" + "00" * 20) + uint(12) + uint(500_000)
    elif source == "uniswapV3":
        log["topics"] = [event["topic"], "0x" + abiAddress("0x" + "11" * 20),
                         "0x" + abiAddress("0x" + "22" * 20), "0x" + uint(3000)]
        log["data"] = "0x" + sint(60, 24) + abiAddress("0x" + "44" * 20)
    else:
        log["topics"] = [event["topic"], "0x" + "55" * 32,
                         "0x" + abiAddress("0x" + "00" * 20),
                         "0x" + abiAddress("0x" + "11" * 20)]
        log["data"] = "0x" + uint(0) + sint(-10, 24) + abiAddress("0x" + "66" * 20) + uint(2 ** 96) + sint(-123, 24)
    return log


class DecodeTests(unittest.TestCase):
    def testAllThreeABIs(self):
        a = detector.decodeLog(logTemplate("ponsV2"))
        self.assertEqual(a["details"]["token"], "0x" + "11" * 20)
        self.assertEqual(a["details"]["curve"], "0x" + "22" * 20)
        self.assertEqual(a["details"]["pairToken"], "0x" + "00" * 20)
        self.assertEqual(a["details"]["graduationThreshold"], "500000")
        b = detector.decodeLog(logTemplate("uniswapV3"))
        self.assertEqual(b["details"]["fee"], 3000)
        self.assertEqual(b["details"]["tickSpacing"], 60)
        self.assertEqual(b["details"]["pool"], "0x" + "44" * 20)
        c = detector.decodeLog(logTemplate("uniswapV4"))
        self.assertEqual(c["details"]["poolId"], "0x" + "55" * 32)
        self.assertEqual(c["details"]["tickSpacing"], -10)
        self.assertEqual(c["details"]["tick"], -123)
        self.assertEqual(c["details"]["sqrtPriceX96"], str(2 ** 96))

    def testUnknownContractAndMalformedAbiRejected(self):
        log = logTemplate("ponsV2")
        log["address"] = "0x" + "aa" * 20
        with self.assertRaises(ValueError):
            detector.decodeLog(log)
        log = logTemplate("uniswapV4")
        log["data"] = log["data"][:-64]
        with self.assertRaises(ValueError):
            detector.decodeLog(log)

    def testUnexpectedTopicTypeIsRejectedCleanly(self):
        log = logTemplate("ponsV2")
        log["topics"][0] = None
        with self.assertRaises(ValueError):
            detector.decodeLog(log)

    def testSameTokenDifferentPoolRemainsSeparate(self):
        a = logTemplate("uniswapV3", txNumber=1)
        b = logTemplate("uniswapV3", txNumber=2)
        b["data"] = "0x" + sint(60, 24) + abiAddress("0x" + "77" * 20)
        decodedA, decodedB = detector.decodeLog(a), detector.decodeLog(b)
        self.assertEqual(decodedA["details"]["token0"], decodedB["details"]["token0"])
        self.assertNotEqual(decodedA["eventKey"], decodedB["eventKey"])

    def testMissingCredentialsAreNotPrinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / ".env"
            file.write_text("ROBINHOOD_ALCHEMY_HTTP_URL=https://private.example/v2/key\n"
                            "ROBINHOOD_ALCHEMY_WSS_URL=wss://private.example/v2/key\n")
            parsed = detector.loadEnv(file)
            self.assertIn("ROBINHOOD_ALCHEMY_WSS_URL", parsed)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.sqlite3"
        self.journal = detector.EventJournal(self.path)

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def testDurabilityIdempotencyRemovalAndReplay(self):
        event = detector.decodeLog(logTemplate("ponsV2"))
        self.assertEqual(self.journal.append(event), 1)
        self.assertIsNone(self.journal.append(event))
        eventRemoved = dict(event, removed=True)
        self.assertEqual(self.journal.append(eventRemoved), 2)
        self.assertIsNone(self.journal.append(eventRemoved))
        self.assertEqual(self.journal.append(event), 3)
        self.assertEqual(self.journal.stats()["events"], 3)
        self.journal.close()
        self.journal = detector.EventJournal(self.path)
        self.assertIsNone(self.journal.append(event))
        records = self.journal.db.execute("SELECT seq,payload FROM event_log ORDER BY seq").fetchall()
        self.assertEqual([row[0] for row in records], [1, 2, 3])
        self.assertEqual([json.loads(row[1])["removed"] for row in records], [False, True, False])

    def testDecodedEvidenceIsPreserved(self):
        raw = logTemplate("uniswapV4")
        event = detector.decodeLog(raw)
        self.journal.append(event)
        row = self.journal.db.execute("SELECT payload FROM event_log").fetchone()
        self.assertEqual(json.loads(row[0])["rawLog"], raw)

    def testDecodeFailuresPersistForInspection(self):
        raw = logTemplate("uniswapV4")
        raw["data"] = "0x1234"
        self.journal.saveDecodeFailure(raw, "ValueError")
        self.assertEqual(self.journal.stats()["decodeFailures"], 1)

    def testGapsAndBudgetPersistence(self):
        self.journal.addGap(100, 110, "test")
        self.assertEqual(self.journal.stats()["reportedGaps"], 1)
        budget = detector.LocalBudget(self.journal, dailyMax=5)
        budget.record(3, "test")
        with self.assertRaises(detector.BudgetExceeded):
            budget.record(3, "test")
        self.assertEqual(round(self.journal.recentCu()), 3)


class LivePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = detector.EventJournal(Path(self.tmp.name) / "test.sqlite3")
        self.received = []
        self.connectionCount = 0
        self.reconnectScenario = False
        self.log1 = logTemplate("ponsV2", 11)
        self.log2 = logTemplate("uniswapV3", 12)
        self.log3 = logTemplate("uniswapV4", 13)
        self.backfillLogs = [self.log1]
        self.failLogRequests = 0
        self.app = web.Application()
        self.app.router.add_post("/rpc", self.rpc)
        self.app.router.add_get("/ws", self.websocket)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.config = {"ROBINHOOD_ALCHEMY_HTTP_URL": f"http://127.0.0.1:{port}/rpc",
                       "ROBINHOOD_ALCHEMY_WSS_URL": f"ws://127.0.0.1:{port}/ws"}

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.db.close()
        self.tmp.cleanup()

    async def rpc(self, request):
        data = await request.json()
        method = data["method"]
        if method == "eth_chainId":
            result = hex(detector.CHAIN_ID)
        elif method == "eth_blockNumber":
            result = "0x3e8"
        elif method == "eth_getLogs":
            self.received.append(data)
            if self.failLogRequests:
                self.failLogRequests -= 1
                return web.Response(status=503)
            # Real JSON-RPC log filters support OR arrays for addresses and topics[0].
            filt = data["params"][0]
            addresses = filt["address"] if isinstance(filt["address"], list) else [filt["address"]]
            topics0 = filt["topics"][0]
            topics = topics0 if isinstance(topics0, list) else [topics0]
            result = [entry for entry in self.backfillLogs
                      if entry["address"] in addresses and entry["topics"][0] in topics]
        else:
            return web.json_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}})
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": result})

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connectionCount += 1
        for _ in range(3):
            entry = await ws.receive_json(timeout=5)
            await ws.send_json({"jsonrpc": "2.0", "id": entry["id"], "result": f"subscription{entry['id']}"})
        if self.reconnectScenario:
            emits = [(self.log1, 1)] if self.connectionCount == 1 else [(self.log2, 2), (self.log3, 3)]
        else:
            emits = [(self.log1, 1), (self.log2, 2), (self.log3, 3), (self.log1, 1)]
        for raw, source in emits:
            await ws.send_json({"jsonrpc": "2.0", "method": "eth_subscription",
                                "params": {"subscription": f"subscription{source}", "result": raw}})
        if self.reconnectScenario and self.connectionCount == 1:
            await ws.close()
            return ws
        while not ws.closed:
            try:
                await ws.receive(timeout=2)
            except asyncio.TimeoutError:
                continue
        return ws

    async def testRecoveryRemovesOrphanedEvent(self):
        # Old v3 event is in the journal, but canonical backfill returns no v3 log.
        old = detector.decodeLog(logTemplate("uniswapV3", 98))
        self.db.append(old)
        self.db.setMeta("lastConnectedHead", "999")
        recovered = detector.Detector(self.config, self.db, recoveryBlocks=25)
        async with ClientSession() as session:
            # RPC mock head=1000; block 1000 itself is inside the 10-block margin,
            # so use explicit target 1012: 1000 is now old enough for reorg repair.
            await recovered.recover(session, 1012)
        logs = [json.loads(x[0]) for x in self.db.db.execute("SELECT payload FROM event_log ORDER BY seq")]
        orphan = [x for x in logs if x["eventKey"] == old["eventKey"]]
        self.assertEqual(len(orphan), 2)
        self.assertFalse(orphan[0]["removed"])
        self.assertTrue(orphan[1]["removed"])
        self.assertTrue(orphan[1]["recovered"])

    async def testDisconnectedWebsocketReconnectsAndResumes(self):
        self.reconnectScenario = True
        live = detector.Detector(self.config, self.db, recoveryBlocks=2)
        task = asyncio.create_task(live.run())
        try:
            for _ in range(150):
                if self.connectionCount >= 2 and self.db.stats()["events"] >= 3:
                    break
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(self.connectionCount, 2, "WebSocket never reconnected")
            self.assertEqual(self.db.stats()["bySource"], {"ponsV2": 1, "uniswapV3": 1, "uniswapV4": 1})
            self.assertEqual(self.db.stats()["events"], 3, "Reconnection duplicated a launch event")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def testRecoveryMarksUnrecoverableOldGap(self):
        self.db.setMeta("lastConnectedHead", "100")
        recovered = detector.Detector(self.config, self.db, recoveryBlocks=2)
        async with ClientSession() as session:
            await recovered.recover(session, 1000)
        gap = self.db.db.execute("SELECT start_block,end_block FROM gaps").fetchone()
        self.assertEqual(gap, (0, 998))
        self.assertEqual(self.db.getMeta("lastBackfilledHead"), "1000")

    async def testRecoveryRetriesOneTransientProviderError(self):
        self.failLogRequests = 1
        recovered = detector.Detector(self.config, self.db, recoveryBlocks=2)
        async with ClientSession() as session:
            await recovered.recover(session, 1000)
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.db.stats()["reportedGaps"], 0)
        self.assertEqual(self.db.stats()["bySource"]["ponsV2"], 1)

    async def testCombinedFilterIgnoresCrossPairEvents(self):
        bogus = logTemplate("uniswapV3", txNumber=44)
        bogus["topics"][0] = detector.SOURCES["uniswapV4"]["topic"]
        self.backfillLogs = [self.log1, self.log2, self.log3, bogus]
        recovered = detector.Detector(self.config, self.db, recoveryBlocks=2)
        async with ClientSession() as session:
            await recovered.recover(session, 1000)
        self.assertEqual(self.db.stats()["events"], 3)
        self.assertEqual(self.db.stats()["decodeFailures"], 0)

    async def testRecoveryUsesOneCombinedFilterPerTenBlockRange(self):
        self.backfillLogs = [self.log1, self.log2, self.log3]
        recovered = detector.Detector(self.config, self.db, recoveryBlocks=2)
        async with ClientSession() as session:
            await recovered.recover(session, 1000)
        self.assertEqual(len(self.received), 1,
                         "Three requests for three venues waste two paid calls per range")
        charged = self.db.db.execute("SELECT SUM(estimate_cu) FROM usage WHERE reason='eth_getLogs'").fetchone()[0]
        self.assertEqual(charged, 60.0)
        request = self.received[0]
        expectedAddresses = {item["address"] for item in detector.SOURCES.values()}
        expectedTopics = {item["topic"] for item in detector.SOURCES.values()}
        self.assertEqual(set(request["params"][0]["address"]), expectedAddresses)
        self.assertEqual(set(request["params"][0]["topics"][0]), expectedTopics)
        self.assertEqual(self.db.stats()["bySource"], {"ponsV2": 1, "uniswapV3": 1, "uniswapV4": 1})
        self.assertEqual(self.db.stats()["decodeFailures"], 0)

    async def testWebsocketBackfillAndSqliteInterface(self):
        live = detector.Detector(self.config, self.db, recoveryBlocks=2)
        async with ClientSession() as session:
            task = asyncio.create_task(live.connectOnce(session))
            try:
                for _ in range(100):
                    if self.db.stats()["events"] >= 3 and len(self.received) >= 3:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(self.db.stats()["events"], 3)
                self.assertTrue(self.received)
                self.assertEqual(self.db.stats()["bySource"], {"ponsV2": 1, "uniswapV3": 1, "uniswapV4": 1})
                filters = {address for r in self.received
                           for address in r["params"][0]["address"]}
                self.assertEqual(filters, {spec["address"] for spec in detector.SOURCES.values()})
                self.assertTrue(all(set(r["params"][0]["topics"][0]) ==
                                    {spec["topic"] for spec in detector.SOURCES.values()}
                                    for r in self.received))
                rows = self.db.db.execute("SELECT payload FROM event_log ORDER BY seq").fetchall()
                self.assertEqual(len(rows), 3)
                self.assertEqual([json.loads(row[0])["schemaVersion"] for row in rows], [1] * 3)
                subscriptionCu = self.db.db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(estimate_cu), 0) FROM usage "
                    "WHERE reason='eth_subscribe'").fetchone()
                self.assertEqual(subscriptionCu, (3, 30.0))
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
