from contextlib import closing
import json
import tempfile
import time
import unittest

from paintwatch.browser import check_response
from paintwatch.domain import PriceError, Snapshot, money, parse_batch, parse_ref
from paintwatch.parsers import block_page, parse_ozon, parse_wb
from paintwatch.platform import SecretStore, instance_lock
from paintwatch.store import Store


def ozon_fixture(card="655 ₽", regular="718 ₽"):
    price = {"price": regular, "isAvailable": True}
    if card is not None:
        price["cardPrice"] = card
    return {
        "widgetStates": {
            "webStickyProducts-1": json.dumps(
                {
                    "sku": "196525150",
                    "name": "Arton A108 Mr Nuts",
                    "seller": {
                        "link": "https://www.ozon.ru/seller/graffitimarket-38248/"
                    },
                }
            ),
            "webPrice-1": json.dumps(price),
            "webShortCharacteristics-1": json.dumps(
                {
                    "characteristics": [
                        {
                            "title": {"textRs": [{"content": "Цвет"}]},
                            "values": [{"text": "Коричневый"}],
                        }
                    ]
                }
            ),
        }
    }


class DomainTests(unittest.TestCase):
    def test_canonical_links_and_batch_dedup(self):
        a = parse_ref("https://www.ozon.ru/product/kraska-196525150/?at=private")
        self.assertEqual(a.url, "https://www.ozon.ru/product/196525150/")
        self.assertEqual(len(parse_batch(a.url + "\n" + a.url)), 1)
        self.assertEqual(parse_ref("29220564", "wb").article, "29220564")
        self.assertEqual(
            parse_ref(
                "https://wildberries.ru/catalog/29220564/detail.aspx?size=9"
            ).variant,
            "9",
        )

    def test_reject_unsafe_or_ambiguous_input(self):
        for url in (
            "https://ozon.ru.evil.test/product/1/",
            "https://user:pass@ozon.ru/product/1/",
            "https://ozon.ru:443/product/1/",
            "https://ozon.ru/search/?text=paint",
            "ARTON-1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_ref(url)
        with self.assertRaises(ValueError):
            parse_batch("мусор https://ozon.ru/product/1/")

    def test_money_without_rounding(self):
        self.assertEqual(money("1 234,56 ₽"), 123456)
        for value in ("0", "-2", "nan", "inf", "1.001", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)

    def test_ozon_card_only_and_exact_sku(self):
        ref = parse_ref("196525150", "ozon")
        snap = parse_ozon(ozon_fixture(), ref)
        self.assertEqual(
            (snap.price, snap.seller, snap.color), (65500, "38248", "Коричневый")
        )
        self.assertEqual(snap.volume, "")
        self.assertIsNone(parse_ozon(ozon_fixture(None), ref).price)
        self.assertIsNone(parse_ozon(ozon_fixture("0 ₽"), ref).price)
        with self.assertRaises(PriceError):
            parse_ozon(ozon_fixture(), parse_ref("123", "ozon"))

    def test_wb_size_stock_and_price_units(self):
        ref = parse_ref("29220564", "wb")
        item = {
            "id": 29220564,
            "name": "Краска",
            "supplierId": 7,
            "sizes": [
                {"optionId": 1, "price": {"product": 38000}, "stocks": [{"qty": 1}]}
            ],
        }
        self.assertEqual(parse_wb({"products": [item]}, ref).price, 38000)
        item["sizes"].append(
            {"optionId": 2, "price": {"product": 41000}, "stocks": [{"qty": 1}]}
        )
        self.assertIsNone(parse_wb({"products": [item]}, ref).price)
        selected = parse_ref(ref.url + "?size=2")
        self.assertEqual(parse_wb({"products": [item]}, selected).price, 41000)
        item["sizes"][1]["stocks"] = []
        self.assertIsNone(parse_wb({"products": [item]}, selected).price)
        with self.assertRaises(PriceError):
            parse_wb({"products": [item]}, parse_ref("123", "wb"))

    def test_access_challenges_and_rate_limit(self):
        for html in (
            "<html>Инцидент: fab_chlg_123</html>",
            '<html><script src="/__wbaas/challenges/antibot/a.js"></script></html>',
            "<html><title>Access denied</title></html>",
        ):
            with self.subTest(html=html), self.assertRaises(PriceError) as error:
                block_page(html)
            self.assertEqual(error.exception.code, "access")
        with self.assertRaises(PriceError) as error:
            check_response(429, "", {"retry-after": "1200"})
        self.assertEqual(
            (error.exception.code, error.exception.retry_after), ("rate_limit", 1200)
        )
        with self.assertRaises(PriceError):
            check_response(200, "{}", {"age": "1"})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.ref = parse_ref("196525150", "ozon")
        self.store.add([self.ref], "", "#arton")
        self.pid = self.store.products()[0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def record(self, price, now=1000, seller="shop"):
        self.store.record(
            self.pid,
            Snapshot(self.ref, "Краска", price, seller=seller, source="fixture"),
            600,
            now,
        )

    def test_seed_idempotence_and_duplicate_products(self):
        self.assertEqual(len(self.store.tags()), 16)
        self.store.edit_tag("#arton", 45000)
        reopened = Store(self.temp.name)
        self.assertEqual(
            next(t for t in reopened.tags() if t["name"] == "#arton")["threshold"],
            45000,
        )
        self.assertEqual(reopened.add([self.ref], "Другой пакет", "#arton"), 0)

    def test_threshold_crossing_and_history(self):
        self.record(40000)
        self.assertEqual(len(self.store.queue()), 0)
        self.record(39999, 1600)
        self.record(39999, 2200)
        self.assertEqual(len(self.store.queue()), 1)
        self.assertEqual(len(self.store.history(self.pid)), 2)
        self.record(45000, 2800)
        self.record(35000, 3400)
        self.assertEqual(len(self.store.queue()), 2)

    def test_first_below_alert_and_error_preserves_state(self):
        self.record(35000)
        self.store.failure(
            self.pid, PriceError("Access denied", code="access"), 600, 1600
        )
        self.record(34000, 2200)
        self.assertEqual(len(self.store.queue()), 1)
        self.assertEqual(self.store.product(self.pid)["price"], 34000)

    def test_unknown_price_preserves_last_good_price(self):
        self.record(35000)
        self.store.record(
            self.pid,
            Snapshot(
                self.ref, "Краска", None, seller="shop", issue="Нет цены по карте"
            ),
            600,
            1600,
        )
        p = self.store.product(self.pid)
        self.assertEqual(
            (p["price"], p["last_success"], p["last_attempt"]), (35000, 1000, 1600)
        )
        self.assertEqual(len(self.store.history(self.pid)), 1)

    def test_threshold_edits_rebase_without_new_alert(self):
        self.record(50000)
        self.store.edit_tag("#arton", 60000)
        self.record(50000, 1600)
        self.assertEqual(len(self.store.queue()), 0)
        self.record(65000, 2200)
        self.record(59000, 2800)
        self.assertEqual(len(self.store.queue()), 1)
        self.store.edit_tag("#arton", 58000)
        self.assertEqual(self.store.queue()[0]["state"], "cancelled")

    def test_custom_threshold_not_overwritten(self):
        self.store.edit(self.pid, "Краска", "#arton", 25000, "", "", "")
        self.store.edit_tag("#arton", 90000)
        self.assertEqual(self.store.product(self.pid)["threshold"], 25000)

    def test_wrong_card_seller_and_invalid_prices_rejected(self):
        self.record(35000)
        with self.assertRaises(PriceError):
            self.record(34000, seller="other-shop")
        for bad in (0, False, 12.5):
            with self.assertRaises(PriceError):
                self.record(bad)
        with self.assertRaises(PriceError):
            self.store.record(
                self.pid, Snapshot(parse_ref("1", "ozon"), "Other", 30000), 600
            )
        self.assertEqual(self.store.product(self.pid)["price"], 35000)

    def test_pause_and_delete_cancel_notifications(self):
        self.record(35000)
        self.store.enable(self.pid, False)
        self.assertEqual(self.store.queue()[0]["state"], "cancelled")
        self.store.delete(self.pid)
        self.assertEqual(self.store.queue(), [])
        self.assertEqual(self.store.history(self.pid), [])

    def test_rate_limit_and_manual_verification(self):
        self.store.cooldown("ozon", int(time.time()) + 1200, "rate_limit")
        with self.assertRaises(ValueError):
            self.store.resume_after_verification(self.pid)
        self.store.cooldown("ozon", int(time.time()) + 1200, "access")
        self.store.resume_after_verification(self.pid)
        self.assertEqual(self.store.setting("cooldown_ozon"), "0")
        self.record(35000, 1000)
        self.store.request_check(self.pid)
        self.assertEqual(self.store.product(self.pid)["next_check"], 1600)

    def test_backup_and_uncertain_recovery(self):
        self.record(35000)
        event = self.store.queue()[0]
        self.store.delivery(event["id"], "sending")
        self.store.recover()
        self.assertEqual(self.store.queue()[0]["state"], "uncertain")
        backup = self.store.backup()
        self.assertTrue(backup.is_file())
        import sqlite3

        # sqlite3's own context manager ends a transaction but does not close
        # the connection. Windows cannot remove the temp backup while it is open.
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(ValueError):
            self.store.backup(self.store.path)

    def test_single_instance(self):
        with instance_lock(self.temp.name):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.temp.name):
                    pass
        with instance_lock(self.temp.name):
            pass

    def test_secret_not_saved_as_plaintext(self):
        import sys

        vault = SecretStore(self.temp.name)
        token = "123456789:" + "A" * 35
        vault.save(token)
        self.assertEqual(vault.read(), token)
        if sys.platform == "win32":
            self.assertNotIn(token.encode(), vault.path.read_bytes())
            self.assertEqual(SecretStore(self.temp.name).read(), token)
        else:
            self.assertFalse(vault.path.exists())
        vault.clear()
        self.assertEqual(vault.read(), "")


if __name__ == "__main__":
    unittest.main()
