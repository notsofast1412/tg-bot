import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from paintwatch.domain import Snapshot, parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.telegram import Telegram, TelegramError, deliver, notification


TOKEN = "123456789:" + "A" * 35


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def api(self, handler):
        return Telegram(
            TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    async def test_lost_send_response_is_uncertain_and_redacted(self):
        def handler(request):
            raise httpx.ReadTimeout("sensitive " + TOKEN)

        api = self.api(handler)
        with self.assertRaises(TelegramError) as error:
            await api.send("-1001", "Test")
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn(TOKEN, str(error.exception))
        with self.assertRaises(TelegramError) as error:
            await api.call("getMe")
        self.assertFalse(error.exception.uncertain)
        await api.close()

    async def test_wb_notification_labels_the_wallet_payment_type(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("708052242", "wb")
            store.add([ref], "Paint", "#arton")
            store.record(store.products()[0]["id"], Snapshot(ref, "Paint", 26100), 0)
            text = notification(store.queue()[0])
            self.assertIn("Wildberries — по WB Карте (с WB Кошельком)", text)
            self.assertIn("261.00", text)
            self.assertNotIn("без WB Кошелька", text)

    async def test_private_reply_keyboard_is_serialized_without_changing_group_sends(
        self,
    ):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

        api = self.api(handler)
        keyboard = {"keyboard": [[{"text": "📊 Статус"}]], "resize_keyboard": True}
        await api.send(123, "Panel", reply_markup=keyboard)
        await api.send("-1001", "Price")
        self.assertEqual(requests[0]["reply_markup"], keyboard)
        self.assertNotIn("reply_markup", requests[1])
        await api.close()

    async def test_further_price_drops_notify_at_15_rubles_from_actual_sent_price(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            store.configure({"chat_id": "-1001", "monitoring": 1})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            sent = []

            def handler(request):
                if request.url.path.endswith("/sendMessage"):
                    sent.append(json.loads(request.content)["text"])
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            def record(price):
                store.record(pid, Snapshot(ref, "Paint", price), 0)

            with (
                patch(
                    "paintwatch.telegram.Telegram",
                    side_effect=lambda token: self.api(handler),
                ),
                patch("asyncio.sleep", new=AsyncMock()),
            ):
                record(35000)
                record(34000)
                await deliver(store, vault)
                self.assertEqual(store.product(pid)["alert_price"], 34000)
                for price in (33900, 33000, 32501):
                    record(price)
                    await deliver(store, vault)
                self.assertEqual(len(sent), 1)
                record(32500)
                await deliver(store, vault)
                self.assertEqual(len(sent), 2)
                self.assertIn("Цена снизилась ещё", sent[1])
                self.assertIn("Ранее: 340.00", sent[1])
                self.assertIn("Снижение: 15.00", sent[1])
                for price in (32500, 34000, 32500, 31001):
                    record(price)
                    await deliver(store, vault)
                self.assertEqual(len(sent), 2)
                record(31000)
                await deliver(store, vault)
                self.assertEqual(len(sent), 3)
                self.assertEqual(store.product(pid)["alert_price"], 31000)

    async def test_rejected_or_uncertain_repeat_does_not_advance_baseline_or_duplicate_events(
        self,
    ):
        import time

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            store.configure({"chat_id": "-1001", "monitoring": 1})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            mode = "success"
            attempts = []

            def handler(request):
                if request.url.path.endswith("/sendMessage"):
                    attempts.append(json.loads(request.content)["text"])
                    if mode == "rejected":
                        return httpx.Response(
                            429,
                            json={
                                "ok": False,
                                "error_code": 429,
                                "parameters": {"retry_after": 73},
                            },
                        )
                    if mode == "uncertain":
                        raise httpx.ReadTimeout("Lost reply")
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            now = int(time.time())
            with (
                patch(
                    "paintwatch.telegram.Telegram",
                    side_effect=lambda token: self.api(handler),
                ),
                patch("asyncio.sleep", new=AsyncMock()),
                patch("time.time", return_value=now),
            ):
                store.record(pid, Snapshot(ref, "Paint", 35000), 0)
                await deliver(store, vault)
                mode = "rejected"
                store.record(pid, Snapshot(ref, "Paint", 33500), 0)
                await deliver(store, vault)
                store.record(pid, Snapshot(ref, "Paint", 32000), 0)
                self.assertEqual(store.product(pid)["alert_price"], 35000)
                self.assertEqual(len(store.queue()), 2)
            mode = "success"
            with (
                patch(
                    "paintwatch.telegram.Telegram",
                    side_effect=lambda token: self.api(handler),
                ),
                patch("asyncio.sleep", new=AsyncMock()),
                patch("time.time", return_value=now + 74),
            ):
                await deliver(store, vault)
                self.assertEqual(store.product(pid)["alert_price"], 32000)
                mode = "uncertain"
                store.record(pid, Snapshot(ref, "Paint", 30500), 0)
                await deliver(store, vault)
                store.record(pid, Snapshot(ref, "Paint", 29000), 0)
                await deliver(store, vault)
                self.assertEqual(store.product(pid)["alert_price"], 32000)
                self.assertEqual(store.queue()[0]["state"], "uncertain")
                self.assertEqual(len(store.queue()), 3)
                self.assertEqual(len(attempts), 4)

    async def test_retry_after_is_definite_rejection(self):
        api = self.api(
            lambda request: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 73},
                },
            )
        )
        with self.assertRaises(TelegramError) as error:
            await api.send("-1001", "Test")
        self.assertEqual(error.exception.delay, 73)
        self.assertFalse(error.exception.uncertain)
        await api.close()

    async def test_notification_delivery_durable_and_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            store.record(pid, Snapshot(ref, "Paint", 35000), 600)
            store.configure({"chat_id": "-1001"})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            sent = []

            def handler(request):
                method = request.url.path.rsplit("/", 1)[-1]
                if method == "sendMessage":
                    sent.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            def factory(token):
                return self.api(handler)

            with patch("paintwatch.telegram.Telegram", side_effect=factory):
                await deliver(store, vault)
                await deliver(store, vault)
            self.assertEqual(len(sent), 1)
            self.assertIn("350.00", sent[0]["text"])
            self.assertEqual(store.queue()[0]["state"], "sent")

    async def test_continuous_checks_do_not_starve_price_notifications(self):
        from paintwatch.bridge import BrowserBridge

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            store.configure({"chat_id": "-1001", "monitoring": 1, "interval": 0})
            pid = store.products()[0]["id"]
            store.record(pid, Snapshot(ref, "Paint", 35000), 0)
            self.assertIsNotNone(BrowserBridge(store, "fixture").job())
            vault = SecretStore(directory)
            vault.save(TOKEN)
            sends = []

            def handler(request):
                if request.url.path.endswith("/sendMessage"):
                    sends.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            with patch(
                "paintwatch.telegram.Telegram",
                side_effect=lambda token: self.api(handler),
            ):
                await deliver(store, vault)
            self.assertEqual(len(sends), 1)
            self.assertEqual(store.queue()[0]["state"], "sent")

    async def test_stale_or_failed_latest_check_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            store.record(pid, Snapshot(ref, "Paint", 35000), 600, 1000)
            store.configure({"chat_id": "-1001"})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            with patch(
                "paintwatch.telegram.Telegram",
                side_effect=lambda token: self.api(
                    lambda r: self.fail("Stale data must not be sent")
                ),
            ):
                await deliver(store, vault)
            self.assertEqual(store.queue()[0]["state"], "pending")

    async def test_parallel_price_change_during_telegram_preflight_cancels_send(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            store.configure({"chat_id": "-1001", "monitoring": 1})
            store.record(pid, Snapshot(ref, "Paint", 35000), 0)
            vault = SecretStore(directory)
            vault.save(TOKEN)

            def handler(request):
                if request.url.path.endswith("/sendMessage"):
                    self.fail("The price rose while getMe was in flight")
                store.record(pid, Snapshot(ref, "Paint", 50000), 0)
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            with patch(
                "paintwatch.telegram.Telegram",
                side_effect=lambda token: self.api(handler),
            ):
                await deliver(store, vault)
            self.assertEqual(store.queue()[0]["state"], "cancelled")

    async def test_same_second_failed_check_does_not_look_fresh(self):
        from paintwatch.domain import PriceError

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            pid = store.products()[0]["id"]
            now = int(__import__("time").time())
            store.record(pid, Snapshot(ref, "Paint", 35000), 600, now)
            store.failure(pid, PriceError("No access"), 600, now)
            store.configure({"chat_id": "-1001"})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            with patch(
                "paintwatch.telegram.Telegram",
                side_effect=lambda token: self.api(
                    lambda r: self.fail("Failed check must prevent delivery")
                ),
            ):
                await deliver(store, vault)
            self.assertEqual(store.queue()[0]["state"], "pending")

    async def test_uncertain_send_survives_restart_without_blind_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            ref = parse_ref("196525150", "ozon")
            store.add([ref], "Paint", "#arton")
            store.record(store.products()[0]["id"], Snapshot(ref, "Paint", 35000), 600)
            store.configure({"chat_id": "-1001"})
            vault = SecretStore(directory)
            vault.save(TOKEN)
            sends = []

            def handler(request):
                if request.url.path.endswith("/sendMessage"):
                    sends.append(request)
                    raise httpx.ReadTimeout("Lost response")
                return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

            with patch(
                "paintwatch.telegram.Telegram",
                side_effect=lambda token: self.api(handler),
            ):
                await deliver(store, vault)
                store.recover()
                await deliver(store, vault)
            self.assertEqual(len(sends), 1)
            self.assertEqual(store.queue()[0]["state"], "uncertain")


if __name__ == "__main__":
    unittest.main()
