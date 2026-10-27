import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from paintwatch.bridge import BrowserBridge
from paintwatch.control import BUTTONS, COMMANDS, KEYBOARD, TelegramControl
from paintwatch.domain import PriceError, parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.telegram import TelegramError


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.configure({"owner_id": 123, "telegram_offset_fixture": 1})
        self.bridge = BrowserBridge(self.store, "fixture")
        self.control = TelegramControl(
            self.store, SecretStore(self.temp.name), self.bridge
        )
        self.api = AsyncMock()

    def tearDown(self):
        self.temp.cleanup()

    def update(self, n, text, uid=123, chat_type="private", chat_id=None):
        return {
            "update_id": n,
            "message": {
                "from": {"id": uid},
                "chat": {"id": uid if chat_id is None else chat_id, "type": chat_type},
                "text": text,
            },
        }

    async def test_only_owner_private_messages_control_state_and_updates_are_not_replayed(
        self,
    ):
        self.api.call.return_value = [
            self.update(1, "/run", uid=456),
            self.update(2, "/run", chat_type="supergroup", chat_id=-1001),
            self.update(3, "/run", chat_id=456),
        ]
        await self.control.poll(self.api, "fixture")
        self.assertEqual(self.store.setting("monitoring", "0"), "0")
        self.api.send.assert_not_awaited()
        self.api.call.return_value = [self.update(4, "/run")]
        await self.control.poll(self.api, "fixture")
        self.assertEqual(self.store.setting("monitoring"), "1")
        self.api.send.assert_awaited_once()
        self.store.configure({"monitoring": 0})
        restored = TelegramControl(self.store, SecretStore(self.temp.name))
        await restored.poll(self.api, "fixture")
        self.assertEqual(self.store.setting("monitoring"), "0")
        self.assertEqual(self.api.send.await_count, 1)

    async def test_id_discovery_does_not_expose_data_or_grant_access(self):
        self.store.configure({"owner_id": ""})
        self.api.call.return_value = [
            self.update(1, "/id", uid=456),
            self.update(2, "/status", uid=456),
        ]
        await self.control.poll(self.api, "fixture")
        self.api.send.assert_awaited_once()
        self.assertEqual(self.api.send.call_args.args[0], 456)
        self.assertIn("456", self.api.send.call_args.args[1])
        self.assertEqual(self.store.setting("owner_id"), "")

    async def test_first_start_discards_backlog_and_send_failure_never_reexecutes_command(
        self,
    ):
        self.api.call.return_value = [self.update(50, "/run")]
        await self.control.poll(self.api, "newbot")
        self.assertEqual(self.store.setting("telegram_offset_newbot"), "51")
        self.assertEqual(self.store.setting("monitoring", "0"), "0")
        self.api.call.return_value = [self.update(51, "/run")]
        self.api.send.side_effect = TelegramError("Lost reply", uncertain=True)
        with self.assertRaises(TelegramError):
            await self.control.poll(self.api, "newbot")
        self.store.configure({"monitoring": 0})
        await self.control.poll(self.api, "newbot")
        self.assertEqual(self.store.setting("monitoring"), "0")
        self.assertEqual(self.api.send.await_count, 1)

    async def test_add_delete_confirmation_threshold_and_product_commands(self):
        result = self.control.execute(
            "/add #arton https://www.ozon.ru/product/196525150/ https://www.wildberries.ru/catalog/29220564/detail.aspx"
        )
        self.assertIn("Добавлено 2", result)
        p = self.store.products()[0]
        pid = p["id"]
        self.assertIn("ID", self.control.execute("/help"))
        self.assertIn(str(pid), self.control.execute("/products"))
        self.control.execute(f"/threshold {pid} 350.50")
        self.assertEqual(self.store.product(pid)["threshold"], 35050)
        self.control.execute(f"/pause {pid}")
        self.assertFalse(self.store.product(pid)["enabled"])
        self.control.execute(f"/resume {pid}")
        self.assertTrue(self.store.product(pid)["enabled"])
        self.assertIn("confirm", self.control.execute(f"/delete {pid}"))
        self.assertIsNotNone(self.store.product(pid))
        self.control.execute(f"/delete {pid} confirm")
        self.assertIsNone(self.store.product(pid))
        self.assertIn(
            "Добавлено 0",
            self.control.execute("/add #arton https://www.ozon.ru/product/196525150/"),
        )
        with self.assertRaises(ValueError):
            self.control.execute("/add #bad https://www.ozon.ru/product/1/")

    async def test_custom_tag_and_manual_scan_commands_do_not_start_periodic_monitoring(
        self,
    ):
        self.control.execute("/tag #testpaint 270")
        self.control.execute("/add #testpaint https://www.ozon.ru/product/1/")
        self.assertEqual(self.store.products()[0]["threshold"], 27000)
        self.assertIn("1", self.control.execute("/scan"))
        self.assertTrue(self.bridge.status()["monitoring"])
        self.assertEqual(self.store.setting("monitoring", "0"), "0")
        self.assertIn("осталось 1", self.control.execute("/status"))
        self.control.execute("/stop")
        self.assertFalse(self.bridge.status()["monitoring"])

    async def test_keyboard_matches_actions_and_only_owner_gets_it(self):
        self.api.call.return_value = [
            self.update(1, "🔎 Проверить все", uid=456),
            self.update(2, "📊 Статус"),
        ]
        await self.control.poll(self.api, "fixture")
        self.api.send.assert_awaited_once()
        self.assertEqual(self.api.send.call_args.args[0], 123)
        self.assertEqual(self.api.send.call_args.kwargs["reply_markup"], KEYBOARD)
        labels = [button["text"] for row in KEYBOARD["keyboard"] for button in row]
        self.assertEqual(labels, list(BUTTONS))
        self.assertIn("/tag", self.control.execute("➕ Добавить тег"))
        self.assertIn("/add", self.control.execute("➕ Добавить товар"))
        self.assertIn("/products", self.control.execute("🗑 Удалить товар"))
        self.assertIn("#arton", self.control.execute("🏷 Теги"))
        self.control.execute("▶️ Запустить")
        self.assertEqual(self.store.setting("monitoring"), "1")
        self.control.execute("⏸ Остановить")
        self.assertEqual(self.store.setting("monitoring"), "0")

    async def test_menu_updates_once_per_version_owner_and_bot_with_private_chat_scope(
        self,
    ):
        await self.control.refresh_menu(self.api, "fixture")
        await self.control.refresh_menu(self.api, "fixture")
        self.assertEqual(self.api.call.await_count, 3)
        calls = self.api.call.call_args_list
        for call in calls[:2]:
            self.assertEqual(call.args[0], "setMyCommands")
            self.assertEqual(call.args[1]["scope"], {"type": "chat", "chat_id": 123})
            self.assertEqual(call.args[1]["commands"], COMMANDS)
        self.assertIn("scan", {c["command"] for c in COMMANDS})
        self.assertIn("tag", {c["command"] for c in COMMANDS})
        self.assertEqual(calls[2].args[0], "setChatMenuButton")
        self.api.send.assert_awaited_once()
        self.assertEqual(self.api.send.call_args.kwargs["reply_markup"], KEYBOARD)
        self.store.configure({"owner_id": 456})
        await self.control.refresh_menu(self.api, "fixture")
        self.assertEqual(self.api.send.call_args.args[0], 456)

    async def test_menu_is_not_published_without_owner_and_failure_can_retry(self):
        self.store.configure({"owner_id": ""})
        await self.control.refresh_menu(self.api, "fixture")
        self.api.call.assert_not_awaited()
        self.store.configure({"owner_id": 123})
        self.api.call.side_effect = TelegramError("Rejected", delay=1)
        with self.assertRaises(TelegramError):
            await self.control.refresh_menu(self.api, "fixture")
        self.assertEqual(self.store.setting("telegram_menu_fixture"), "")
        self.api.call.side_effect = None
        await self.control.refresh_menu(self.api, "fixture")
        self.api.send.assert_awaited_once()

    async def test_optional_menu_failure_does_not_delay_commands_or_alerts(self):
        self.control.vault.save("123456789:" + "A" * 35)
        self.control.poll = AsyncMock()
        self.control.alert = AsyncMock()
        self.control.refresh_menu = AsyncMock(
            side_effect=TelegramError("Menu unavailable", delay=73)
        )
        pauses = []

        async def pause(delay):
            pauses.append(delay)
            if len(pauses) == 2:
                raise asyncio.CancelledError

        with (
            patch("paintwatch.control.Telegram", return_value=self.api),
            patch("asyncio.sleep", side_effect=pause),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.control.run()
        self.assertEqual(pauses, [2, 2])
        self.assertEqual(self.control.poll.await_count, 2)
        self.assertEqual(self.control.alert.await_count, 2)
        self.control.refresh_menu.assert_awaited_once()
        self.api.close.assert_awaited_once()

    async def test_interval_and_parallel_changes_keep_market_cooldown(self):
        self.control.execute("/add #arton https://www.ozon.ru/product/1/")
        self.control.execute("/run")
        self.control.execute("/interval 0")
        self.control.execute("/parallel 8")
        self.assertEqual(self.store.interval(), 0)
        self.assertEqual(self.store.parallelism(), 8)
        self.store.cooldown("ozon", int(time.time()) + 1200, "rate_limit")
        self.control.execute("/check all")
        self.assertIsNone(self.bridge.job())
        with self.assertRaises(ValueError):
            self.control.execute(f"/verified {self.store.products()[0]['id']}")
        self.assertIn("Параллельно: 8", self.control.execute("/status"))
        for command in ("/interval -1", "/parallel 0", "/parallel 33"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.control.execute(command)

    async def test_alerts_aggregate_200_errors_throttle_and_survive_restart(self):
        self.store.add([parse_ref(str(n), "ozon") for n in range(1, 201)], "", "#arton")
        for p in self.store.products():
            self.store.failure(p["id"], PriceError("Нет цены"), 0)
        with patch("time.time", return_value=2000000000):
            await self.control.alert(self.api)
            await TelegramControl(self.store, SecretStore(self.temp.name)).alert(
                self.api
            )
        self.api.send.assert_awaited_once()
        owner, text = self.api.send.call_args.args
        self.assertEqual(owner, 123)
        self.assertIn("200", text)
        self.assertLess(len(text), 4000)
        with patch("time.time", return_value=2000001801):
            await self.control.alert(self.api)
        self.assertEqual(self.api.send.await_count, 2)

    async def test_rejected_private_alert_retries_but_lost_response_is_not_blindly_retried(
        self,
    ):
        self.store.add([parse_ref("1", "ozon")], "", "#arton")
        self.store.failure(self.store.products()[0]["id"], PriceError("No price"), 0)
        self.api.send.side_effect = TelegramError("Rejected", delay=90)
        with patch("time.time", return_value=2000000000):
            with self.assertRaises(TelegramError):
                await self.control.alert(self.api)
        self.api.send.side_effect = None
        with patch("time.time", return_value=2000000089):
            await self.control.alert(self.api)
        self.assertEqual(self.api.send.await_count, 1)
        with patch("time.time", return_value=2000000090):
            await self.control.alert(self.api)
        self.assertEqual(self.api.send.await_count, 2)
        self.store.configure({"private_issue_hash": "", "private_issue_at": 0})
        self.api.send.side_effect = TelegramError("Lost", uncertain=True)
        with patch("time.time", return_value=2000000100):
            with self.assertRaises(TelegramError):
                await self.control.alert(self.api)
        with patch("time.time", return_value=2000000161):
            await self.control.alert(self.api)
        self.assertEqual(self.api.send.await_count, 3)

    async def test_extension_loss_alert_is_detected_without_product_failures(self):
        self.store.add([parse_ref("1", "ozon")], "", "#arton")
        self.store.configure({"monitoring": 1})
        with patch("time.time", return_value=self.control.started + 91):
            await self.control.alert(self.api)
        self.assertIn("Chrome", self.api.send.call_args.args[1])

    async def test_control_task_is_cancellable_during_a_pending_telegram_request(self):
        vault = self.control.vault
        vault.save("123456789:" + "A" * 35)
        entered = asyncio.Event()

        async def stalled(*args):
            entered.set()
            await asyncio.Event().wait()

        self.api.call.side_effect = stalled
        with patch("paintwatch.control.Telegram", return_value=self.api):
            task = asyncio.create_task(self.control.run())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.api.close.assert_awaited_once()
