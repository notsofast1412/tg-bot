import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from paintwatch.bridge import BrowserBridge
from paintwatch.control import TelegramControl
from paintwatch.domain import PriceError, Snapshot, parse_ref
from paintwatch.parsers import parse_ozon
from paintwatch.platform import SecretStore
from paintwatch.store import Store


REDIRECT = "https://www.ozon.ru/search/?deny_category_prediction=true&from_global=true&text=paint&product_id=5517253050"


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.ref = parse_ref("5517253050", "ozon")
        self.store.add([self.ref], "Краска", "#arton")
        self.pid = self.store.products()[0]["id"]
        self.store.configure({"monitoring": 1, "owner_id": 123})
        self.bridge = BrowserBridge(self.store, "fixture")

    def tearDown(self):
        self.temp.cleanup()

    def price(self, amount):
        self.store.record(
            self.pid, Snapshot(self.ref, "Краска", amount, seller="shop"), 600
        )

    def sold_out(self):
        self.store.record(
            self.pid, Snapshot(self.ref, "Краска", None, available=False), 600
        )

    def test_search_redirect_canonicalizes_only_one_explicit_product_id(self):
        self.assertEqual(parse_ref(REDIRECT), self.ref)
        for url in (
            "https://www.ozon.ru/search/?text=paint",
            REDIRECT + "&product_id=1",
            REDIRECT + "&product_id=",
            REDIRECT.replace("5517253050", "invalid"),
            REDIRECT.replace("www.ozon.ru", "www.ozon.ru.evil.test"),
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_ref(url)

    def test_sold_out_redirect_is_successful_quiet_and_releases_manual_job(self):
        self.assertTrue(self.bridge.status()["availability_status"])
        self.price(35000)
        event = self.store.queue()[0]
        self.store.failure(self.pid, PriceError("Старая ошибка"), 600)
        self.store.request_scan()
        job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": REDIRECT,
                "kind": "sold_out",
                "availability": "out_of_stock",
                "evidence": "ozon_sold_out_search",
            }
        )
        product = self.store.product(self.pid)
        self.assertEqual(product["status"], "Товар закончился")
        self.assertEqual(product["issue"], "")
        self.assertEqual(product["failures"], 0)
        self.assertIsNone(product["price"])
        self.assertEqual(product["seller"], "shop")
        self.assertTrue(product["enabled"])
        self.assertGreaterEqual(product["next_check"], int(time.time()) + 599)
        self.assertEqual(len(self.store.history(self.pid)), 1)
        self.assertEqual(self.store.queue()[0]["state"], "cancelled")
        self.assertIsNone(self.store.claim_notification(event["id"]))
        self.assertEqual(self.bridge.status()["manual"], 0)
        self.assertEqual(self.bridge.status()["active"], 0)
        self.assertEqual(self.store.setting("cooldown_ozon"), "")
        control = TelegramControl(self.store, SecretStore(self.temp.name), self.bridge)
        self.assertEqual(control.problems(), [])
        api = AsyncMock()
        asyncio.run(control.alert(api))
        api.send.assert_not_awaited()
        with self.store.db() as db:
            check = db.execute(
                "SELECT * FROM checks ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual((check["ok"], check["reason"]), (1, "Товар закончился"))
        restored = Store(self.temp.name)
        self.assertEqual(restored.product(self.pid)["status"], "Товар закончился")

    def test_return_to_stock_keeps_last_sent_price_and_the_15_ruble_rule(self):
        self.price(35000)
        event = self.store.queue()[0]
        self.assertIsNotNone(self.store.claim_notification(event["id"]))
        self.store.delivery(event["id"], "sent", "", 0)
        self.price(33000)
        repeat = self.store.queue()[0]
        self.sold_out()
        self.assertEqual(self.store.product(self.pid)["alert_price"], 35000)
        self.assertIsNone(self.store.claim_notification(repeat["id"]))
        self.price(35000)
        self.assertFalse(any(e["state"] == "pending" for e in self.store.queue()))
        self.price(33500)
        pending = [e for e in self.store.queue() if e["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["previous_price"], 35000)

    def test_unsent_initial_alert_can_be_created_again_only_after_return(self):
        self.price(35000)
        self.sold_out()
        self.assertFalse(any(e["state"] == "pending" for e in self.store.queue()))
        self.price(35000)
        self.assertEqual(
            len([e for e in self.store.queue() if e["state"] == "pending"]), 1
        )

    def test_wrong_sku_or_contradictory_availability_never_marks_sold_out(self):
        for changes in (
            {"url": REDIRECT.replace("5517253050", "1")},
            {"price": 35000},
            {"availability": "unknown"},
            {"evidence": "product_sold_out_heading"},
        ):
            with self.subTest(changes=changes):
                self.store.request_scan()
                job = self.bridge.job()
                self.bridge.result(
                    {
                        "id": job["id"],
                        "lease": job["lease"],
                        "url": REDIRECT,
                        "kind": "sold_out",
                        "availability": "out_of_stock",
                        "evidence": "ozon_sold_out_search",
                        **changes,
                    }
                )
                self.assertNotEqual(
                    self.store.product(self.pid)["status"], "Товар закончился"
                )
                self.assertEqual(self.store.queue(), [])

    def test_search_result_price_cannot_be_accepted_as_the_original_product(self):
        job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": REDIRECT,
                "kind": "visible",
                "price": 27800,
                "name": "Похожая краска",
                "currency": "RUB",
                "price_kind": "ozon_card",
            }
        )
        self.assertIsNone(self.store.product(self.pid)["price"])
        self.assertEqual(self.store.queue(), [])

    def test_json_unavailability_is_quiet_but_missing_card_price_is_unknown(self):
        data = {
            "widgetStates": {
                "webStickyProducts-1": {"sku": self.ref.article, "name": "Краска"},
                "webPrice-1": {"isAvailable": False, "cardPrice": "278 ₽"},
            }
        }
        snapshot = parse_ozon(data, self.ref)
        self.assertIs(snapshot.available, False)
        self.assertIsNone(snapshot.price)
        self.assertEqual(snapshot.issue, "")
        self.store.record(self.pid, snapshot, 600)
        self.assertEqual(self.store.product(self.pid)["status"], "Товар закончился")
        data["widgetStates"]["webPrice-1"] = {"isAvailable": True}
        snapshot = parse_ozon(data, self.ref)
        self.assertIsNone(snapshot.available)
        self.assertTrue(snapshot.issue)

    def test_sold_out_snapshot_with_a_price_is_rejected(self):
        with self.assertRaises(PriceError):
            self.store.record(
                self.pid, Snapshot(self.ref, "Краска", 27800, available=False), 600
            )
        self.assertEqual(self.store.history(self.pid), [])
