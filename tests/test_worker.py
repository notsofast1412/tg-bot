import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from paintwatch.domain import Snapshot, parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.worker import Worker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_records_price_and_respects_restart_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            store.configure({"monitoring": 1})
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
