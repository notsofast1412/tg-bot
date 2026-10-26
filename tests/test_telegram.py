import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from paintwatch.domain import Snapshot, parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.telegram import Telegram, TelegramError, deliver


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
