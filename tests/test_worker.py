import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from paintwatch.domain import Snapshot, parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.worker import Worker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_mode_does_not_launch_controlled_browser(self):
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add([parse_ref("196525150", "ozon")], "Paint", "#arton")
            store.configure({"monitoring": 1})
            bridge = Mock()
            collector = AsyncMock()
            worker = Worker(store, SecretStore(directory), bridge=bridge)

            async def finish(*args):
                worker.stop()

            with (
                patch("paintwatch.worker.BrowserCollector", return_value=collector),
                patch("paintwatch.worker.deliver", side_effect=finish),
            ):
                await worker.run()
            collector.fetch.assert_not_awaited()
            collector.open.assert_not_awaited()
            bridge.start.assert_called_once()
            bridge.close.assert_called_once()

    async def test_legacy_wb_collector_cannot_publish_an_ordinary_price_as_wallet_price(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("708052242", "wb")
            store.add([ref], "Paint", "#arton")
            store.configure({"monitoring": 1, "collector": "playwright"})
            collector = AsyncMock()
            collector.fetch.return_value = Snapshot(ref, "Paint", 27000)
            worker = Worker(store, SecretStore(directory))

            async def finish(*args):
                worker.stop()

            with (
                patch("paintwatch.worker.BrowserCollector", return_value=collector),
                patch("paintwatch.worker.deliver", side_effect=finish),
            ):
                await worker.run()
            collector.fetch.assert_not_awaited()
            self.assertIsNone(store.products()[0]["price"])
            self.assertEqual(store.queue(), [])
            self.assertIn("WB Кошельком", store.products()[0]["issue"])

    async def test_scheduler_records_price_and_respects_restart_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            store.configure({"monitoring": 1, "collector": "playwright"})
            vault = SecretStore(directory)
            collector = AsyncMock()
            collector.fetch.return_value = Snapshot(ref, "Paint", 35000)
            worker = Worker(store, vault)

            async def sent(*args):
                worker.stop()

            with (
                patch("paintwatch.worker.BrowserCollector", return_value=collector),
                patch("paintwatch.worker.deliver", side_effect=sent),
            ):
                await worker.run()
            self.assertEqual(store.products()[0]["price"], 35000)
            self.assertEqual(len(store.queue()), 1)
            collector.close.assert_awaited_once()
            collector.fetch.reset_mock()
            worker = Worker(store, vault)
            with (
                patch("paintwatch.worker.BrowserCollector", return_value=collector),
                patch("paintwatch.worker.deliver", side_effect=sent),
            ):
                await worker.run()
            collector.fetch.assert_not_awaited()
            self.assertEqual(len(store.queue()), 1)
