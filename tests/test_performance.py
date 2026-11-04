from contextlib import contextmanager
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from paintwatch.bridge import BrowserBridge
from paintwatch.domain import parse_ref
from paintwatch.store import Store
from paintwatch.ui import Window, sync_rows


class Tree:
    def __init__(self):
        self.ids, self.values, self.calls = [], {}, []

    def get_children(self):
        return tuple(self.ids)

    def insert(self, parent, index, iid, values):
        self.calls.append(("insert", iid))
        self.ids.insert(index, iid)
        self.values[iid] = values

    def item(self, iid, *, values):
        self.calls.append(("update", iid))
        self.values[iid] = values

    def move(self, iid, parent, index):
        self.calls.append(("move", iid))
        self.ids.remove(iid)
        self.ids.insert(index, iid)

    def delete(self, *ids):
        self.calls.append(("delete", ids))
        for iid in ids:
            self.ids.remove(iid)
            del self.values[iid]


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_621_unchanged_rows_do_not_rebuild_and_only_changed_rows_update(self):
        tree, cache = Tree(), {}
        rows = [(str(n), (n, "Проверено", 27800)) for n in range(621)]
        sync_rows(tree, rows, cache)
        tree.calls.clear()
        sync_rows(tree, rows, cache)
        self.assertEqual(tree.calls, [])
        rows[3] = ("3", (3, "Проверено", 25000))
        sync_rows(tree, rows, cache)
        self.assertEqual(tree.calls, [("update", "3")])
        filtered = [rows[3], rows[1]]
        sync_rows(tree, filtered, cache)
        self.assertEqual(tree.get_children(), ("3", "1"))
        sync_rows(tree, rows, cache)
        self.assertEqual(tree.get_children(), tuple(iid for iid, _ in rows))

    def test_hidden_window_skips_periodic_refresh_but_keeps_poll_scheduled(self):
        window = Window.__new__(Window)
        window.closing = False
        window.root = Mock()
        window.root.state.return_value = "withdrawn"
        window.refresh = Mock()
        window.periodic_refresh()
        window.refresh.assert_not_called()
        window.root.after.assert_called_once_with(5000, window.periodic_refresh)
        window.root.state.return_value = "normal"
        window.periodic_refresh()
        window.refresh.assert_called_once()

    def test_completed_notification_history_is_excluded_from_delivery_queries(self):
        self.store.add([parse_ref("1", "ozon")], "", "#arton")
        pid = self.store.products()[0]["id"]
        with self.store.db() as db:
            db.executemany(
                "INSERT INTO outbox(product_id,generation,price,threshold,created_at,state) VALUES(?,?,?,?,?,?)",
                [(pid, n, 27800, 40000, n, "sent") for n in range(1000)]
                + [
                    (pid, 1000, 27800, 40000, 1000, "pending"),
                    (pid, 1001, 27800, 40000, 1001, "blocked"),
                ],
            )
        self.assertEqual(len(self.store.queue()), 1002)
        self.assertEqual(len(self.store.queue(states=("pending", "blocked"))), 2)
        self.assertEqual(
            [e["generation"] for e in self.store.queue(limit=2)], [1001, 1000]
        )

    def test_bulk_settings_and_unchanged_configuration_avoid_repeated_writes(self):
        original = self.store.db
        changes = []

        @contextmanager
        def counted():
            with original() as db:
                yield db
                changes.append(db.total_changes)

        self.store.db = counted
        self.store.configure({"interval": 500})
        self.store.configure({"interval": 500})
        self.assertEqual(changes, [1, 0])
        self.assertEqual(
            self.store.settings({"interval": "600", "parallelism": "6"}),
            {"interval": "500", "parallelism": "6"},
        )

    def test_extension_heartbeat_is_throttled_without_delaying_results(self):
        bridge = BrowserBridge(self.store, "fixture")
        self.store.configure = Mock(wraps=self.store.configure)
        with (
            patch("paintwatch.bridge.time.monotonic", side_effect=[10, 11, 19, 20]),
            patch("paintwatch.bridge.time.time", side_effect=[100, 110]),
        ):
            for _ in range(4):
                bridge.touch()
        self.assertEqual(self.store.configure.call_count, 2)
        self.assertEqual(self.store.setting("extension_seen"), "110")

    def result(self, bridge, job, **payload):
        bridge.result(
            {"id": job["id"], "lease": job["lease"], "url": job["url"], **payload}
        )

    def test_all_621_products_are_visited_fairly_with_six_slots_and_zero_interval(self):
        self.store.add(
            [parse_ref(str(1000 + n), "ozon") for n in range(621)], "", "#arton"
        )
        self.store.configure({"monitoring": 1, "interval": 0, "parallelism": 6})
        bridge = BrowserBridge(self.store, "fixture")
        seen = set()
        with patch("paintwatch.bridge.time.time", return_value=int(time.time())):
            while len(seen) < 621:
                jobs = []
                for slot in range(6):
                    if len(seen) + len(jobs) == 621:
                        break
                    job = bridge.job("fixture", slot)
                    self.assertIsNotNone(job)
                    self.assertNotIn(job["id"], seen)
                    jobs.append(job)
                if len(jobs) == 6:
                    self.assertEqual(bridge.status()["ready"], 0)
                for job in jobs:
                    seen.add(job["id"])
                    self.result(
                        bridge,
                        job,
                        kind="visible",
                        name="Краска",
                        price=70000,
                        price_kind="ozon_card",
                        currency="RUB",
                    )
        status = bridge.status()
        self.assertEqual(status["performance"]["unchecked"], 0)
        self.assertEqual(status["performance"]["successful"], 621)
        self.assertEqual(status["active"], 0)

    def test_ready_respects_market_pause_and_still_exposes_expired_leases(self):
        self.store.add([parse_ref("1", "ozon")], "", "#arton")
        self.store.configure({"monitoring": 1, "check_timeout": 1})
        bridge = BrowserBridge(self.store, "fixture")
        stamp = int(time.time())
        with patch("paintwatch.bridge.time.time", return_value=stamp):
            bridge.job()
        with patch("paintwatch.bridge.time.time", return_value=stamp + 17):
            self.assertEqual(bridge.status()["ready"], 1)
            self.assertIsNone(bridge.job())
            self.assertEqual(bridge.status()["ready"], 0)
        self.store.request_scan()
        self.store.cooldown("ozon", stamp + 1200, "rate_limit")
        self.assertEqual(bridge.status()["ready"], 0)

    def test_performance_distinguishes_confirmed_results_errors_and_stale_samples(self):
        self.store.add([parse_ref(str(n), "ozon") for n in range(1, 4)], "", "#arton")
        self.store.configure({"monitoring": 1, "parallelism": 3})
        bridge = BrowserBridge(self.store, "fixture")
        bridge.started = 1000
        with patch("paintwatch.bridge.time.time", return_value=1000):
            jobs = [bridge.job("fixture", n) for n in range(3)]
        with patch("paintwatch.bridge.time.time", return_value=1003):
            self.result(
                bridge,
                jobs[0],
                kind="visible",
                name="Краска",
                price=70000,
                price_kind="ozon_card",
                currency="RUB",
            )
        with patch("paintwatch.bridge.time.time", return_value=1008):
            self.result(bridge, jobs[1], error="Нет подтверждённой цены")
        with patch("paintwatch.bridge.time.time", return_value=1010):
            self.result(
                bridge,
                jobs[2],
                kind="sold_out",
                availability="out_of_stock",
                evidence="product_sold_out_heading",
            )
            stats = bridge.status()["performance"]
        self.assertEqual(
            (stats["completed"], stats["successful"], stats["average_seconds"]),
            (3, 2, 7),
        )
        self.assertEqual((stats["unchecked"], stats["oldest_success_age"]), (1, 7))
        with patch("paintwatch.bridge.time.time", return_value=1311):
            stats = bridge.status()["performance"]
        self.assertEqual(stats["completed"], 0)
        self.assertIsNone(stats["average_seconds"])
