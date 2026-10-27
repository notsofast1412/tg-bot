from contextlib import closing
import json
import tempfile
import time
import unittest
from unittest.mock import patch

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

    def test_bulk_import_200_cards_and_limit(self):
        text = "\n".join(f"https://www.ozon.ru/product/{n}/" for n in range(1, 201))
        self.assertEqual(len(parse_batch(text)), 200)
        with self.assertRaises(ValueError):
            parse_batch("\n".join(str(n) for n in range(1, 1002)), "ozon")

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

    def sent(self, now=1000):
        event = self.store.queue()[0]
        with patch("time.time", return_value=now):
            claimed = self.store.claim_notification(event["id"])
            self.assertIsNotNone(claimed)
            self.store.delivery(event["id"], "sent")
        return event["id"]

    def test_further_drop_uses_cumulative_15_rubles_and_includes_exact_boundary(self):
        self.record(35000)
        self.sent()
        for price in (34900, 34000, 33501):
            self.record(price, 1001)
            self.assertEqual(len(self.store.queue()), 1)
        self.record(33500, 1002)
        self.assertEqual(len(self.store.queue()), 2)
        self.assertEqual(self.store.queue()[0]["previous_price"], 35000)
        self.sent(1002)
        self.record(32100, 1003)
        self.assertEqual(len(self.store.queue()), 2)
        self.record(32000, 1004)
        self.record(32000, 1005)
        self.assertEqual(len(self.store.queue()), 3)
        self.assertEqual(self.store.queue()[0]["previous_price"], 33500)

    def test_new_threshold_crossing_still_alerts_and_unchanged_tag_does_not_reset_anchor(
        self,
    ):
        self.record(35000)
        self.sent()
        self.record(34000, 1001)
        self.store.edit_tag("#arton", 40000)
        self.assertEqual(self.store.product(self.pid)["alert_price"], 35000)
        self.record(33500, 1002)
        self.sent(1002)
        self.record(40000, 1003)
        self.assertIsNone(self.store.product(self.pid)["alert_price"])
        self.record(39999, 1004)
        self.assertIsNone(self.store.queue()[0]["previous_price"])
        self.assertEqual(self.store.queue()[0]["state"], "pending")

    def test_rise_below_threshold_does_not_reset_the_last_notified_price(self):
        self.record(35000)
        self.sent()
        self.record(33500, 1001)
        self.sent(1001)
        for price in (35000, 34900, 33500, 32001):
            self.record(price, 1002)
        self.assertEqual(len(self.store.queue()), 2)
        self.record(32000, 1003)
        self.assertEqual(len(self.store.queue()), 3)

    def test_unsent_drops_coalesce_and_use_the_actual_claimed_price(self):
        self.record(35000)
        for price in (33000, 31500):
            self.record(price, 1001)
        self.assertEqual(len(self.store.queue()), 1)
        self.assertIsNone(self.store.product(self.pid)["alert_price"])
        self.sent(1001)
        self.assertEqual(self.store.queue()[0]["price"], 31500)
        self.assertEqual(self.store.product(self.pid)["alert_price"], 31500)
        self.record(30100, 1002)
        self.assertEqual(len(self.store.queue()), 1)
        self.record(30000, 1003)
        self.assertEqual(len(self.store.queue()), 2)

    def test_unknown_and_failed_prices_do_not_advance_the_repeat_baseline(self):
        self.record(35000)
        self.sent()
        self.store.failure(self.pid, PriceError("No data"), 600, 1001)
        self.store.record(
            self.pid,
            Snapshot(self.ref, "Paint", None, seller="shop", issue="No price"),
            600,
            1002,
        )
        self.assertEqual(self.store.product(self.pid)["alert_price"], 35000)
        self.record(33500, 1003)
        self.assertEqual(len(self.store.queue()), 2)

    def test_pending_repeat_is_cancelled_if_price_recovers_before_send(self):
        self.record(35000)
        self.sent()
        self.record(33500, 1001)
        repeat = self.store.queue()[0]
        self.record(33501, 1002)
        self.assertEqual(self.store.queue()[0]["state"], "cancelled")
        with patch("time.time", return_value=1002):
            self.assertIsNone(self.store.claim_notification(repeat["id"]))
        self.assertEqual(self.store.product(self.pid)["alert_price"], 35000)
        self.record(33500, 1003)
        self.assertEqual(self.store.queue()[0]["state"], "pending")

    def test_further_drop_during_send_is_planned_after_successful_delivery(self):
        self.record(35000)
        self.sent()
        self.record(33500, 1001)
        repeat = self.store.queue()[0]
        with patch("time.time", return_value=1001):
            self.assertIsNotNone(self.store.claim_notification(repeat["id"]))
            self.record(32000, 1001)
            self.assertEqual(len(self.store.queue()), 2)
            self.store.delivery(repeat["id"], "sent")
        self.assertEqual(len(self.store.queue()), 3)
        self.assertEqual(self.store.queue()[0]["previous_price"], 33500)

    def test_rebased_rule_is_not_overwritten_by_an_old_send_in_flight(self):
        self.record(37000)
        event = self.store.queue()[0]
        with patch("time.time", return_value=1000):
            self.store.claim_notification(event["id"])
            self.record(35000)
            self.store.edit_tag("#arton", 50000)
            self.store.delivery(event["id"], "sent")
        self.assertEqual(self.store.product(self.pid)["alert_price"], 35000)
        self.assertEqual(len(self.store.queue()), 1)
        self.record(33500, 1001)
        self.assertEqual(len(self.store.queue()), 2)

    def test_repeat_baseline_survives_restart(self):
        self.record(35000)
        self.sent()
        self.store = Store(self.temp.name)
        self.record(33500, 1001)
        self.assertEqual(len(self.store.queue()), 2)
        self.assertEqual(self.store.queue()[0]["previous_price"], 35000)

    def test_v3_migration_adds_quiet_baseline_without_replaying_legacy_events(self):
        self.record(35000)
        self.sent()
        self.record(34000, 1001)
        with self.store.db() as db:
            db.execute("ALTER TABLE products DROP COLUMN alert_price")
            db.execute("ALTER TABLE outbox DROP COLUMN previous_price")
            db.execute("PRAGMA user_version=3")
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.product(self.pid)["alert_price"], 34000)
        self.assertEqual(len(self.store.queue()), 1)
        self.record(32501, 1002)
        self.assertEqual(len(self.store.queue()), 1)
        self.record(32500, 1003)
        self.assertEqual(len(self.store.queue()), 2)
        self.assertTrue(list((self.store.directory / "backups").glob("*.sqlite3")))

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
        self.store.cooldown("ozon", 0, "")
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

    def test_v1_migration_backs_up_and_preserves_products_history_and_settings(self):
        import sqlite3

        self.record(35000)
        self.store.configure({"interval": 1200, "owner_id": 123})
        with self.store.db() as db:
            db.execute("DROP TABLE check_jobs")
            db.execute("DROP TABLE manual_checks")
            db.execute("ALTER TABLE products DROP COLUMN alert_price")
            db.execute("ALTER TABLE outbox DROP COLUMN previous_price")
            db.execute("PRAGMA user_version=1")
        migrated = Store(self.temp.name)
        self.assertEqual(migrated.product(self.pid)["price"], 35000)
        self.assertEqual(migrated.interval(), 1200)
        self.assertEqual(len(migrated.history(self.pid)), 1)
        self.assertEqual(len(migrated.queue()), 1)
        with migrated.db() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM check_jobs").fetchone()[0], 0
            )
        backups = list((migrated.directory / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1
            )

    def test_v2_migration_preserves_active_job_and_adds_manual_queue(self):
        from paintwatch.bridge import BrowserBridge

        self.store.configure({"monitoring": 1})
        job = BrowserBridge(self.store, "fixture").job()
        with self.store.db() as db:
            db.execute("DROP TABLE manual_checks")
            db.execute("ALTER TABLE products DROP COLUMN alert_price")
            db.execute("ALTER TABLE outbox DROP COLUMN previous_price")
            db.execute("PRAGMA user_version=2")
        migrated = Store(self.temp.name)
        with migrated.db() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                db.execute("SELECT lease FROM check_jobs").fetchone()[0], job["lease"]
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM manual_checks").fetchone()[0], 0
            )
        self.assertTrue(list((migrated.directory / "backups").glob("*.sqlite3")))

    def test_custom_tag_is_available_for_products_and_survives_restart(self):
        tag = self.store.add_tag("Montana-94", 50000)
        self.assertEqual(tag, "#montana-94")
        self.store.edit(self.pid, "Paint", tag, None, "", "", "")
        restored = Store(self.temp.name)
        self.assertEqual(restored.product(self.pid)["threshold"], 50000)
        with self.assertRaises(ValueError):
            restored.add_tag("montana-94", 60000)
        self.assertEqual(restored.product(self.pid)["threshold"], 50000)
        for name, value in (("#bad tag", 100), ("#", 100), ("#new", 0)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                restored.add_tag(name, value)

    def test_wb_payment_policy_change_resets_only_wb_price_series_and_cancels_old_events(
        self,
    ):
        wb = parse_ref("708052242", "wb")
        self.store.add([wb], "Paint", "#arton")
        pid = self.store.products()[0]["id"]
        self.store.record(pid, Snapshot(wb, "Paint", 27000), 600, 1000)
        self.record(35000)
        with self.store.db() as db:
            db.execute("DELETE FROM settings WHERE key='wb_price_kind'")
        restored = Store(self.temp.name)
        self.assertIsNone(restored.product(pid)["price"])
        self.assertIsNone(restored.product(pid)["alert_price"])
        self.assertEqual(restored.product(pid)["next_check"], 0)
        self.assertEqual(restored.product(pid)["failures"], 0)
        self.assertEqual(restored.product(self.pid)["price"], 35000)
        old_wb = next(e for e in restored.queue() if e["product_id"] == pid)
        self.assertEqual(old_wb["state"], "cancelled")
        self.assertEqual(len(restored.history(pid)), 1)
        restored.record(pid, Snapshot(wb, "Paint", 26100), 600, 1001)
        new_wb = restored.queue()[0]
        self.assertEqual(new_wb["price"], 26100)
        self.assertIsNone(new_wb["previous_price"])
        self.assertEqual(Store(self.temp.name).product(pid)["price"], 26100)
        self.assertEqual(
            len(list((restored.directory / "backups").glob("*.sqlite3"))), 1
        )

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
