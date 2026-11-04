import json
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from paintwatch.add_dialog import CANCEL, NEXT, PREVIOUS
from paintwatch.control import KEYBOARD, TelegramControl
from paintwatch.domain import parse_ref
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.telegram import TelegramError


class AddDialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.configure({"owner_id": 123, "telegram_offset_fixture": 1})
        self.control = TelegramControl(self.store, SecretStore(self.temp.name))
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

    async def message(self, n, text):
        self.api.call.return_value = [self.update(n, text)]
        await self.control.poll(self.api, "fixture")
        call = self.api.send.call_args
        return call.args[1], call.kwargs["reply_markup"]

    def labels(self, markup):
        return [button["text"] for row in markup["keyboard"] for button in row]

    async def test_tagged_article_asks_market_and_preserves_tag_without_guessing(self):
        answer, markup = await self.message(1, "/add #belazo 1797187018")
        self.assertIn("1797187018", answer)
        self.assertIn("#belazo", answer)
        self.assertEqual(self.labels(markup), ["Ozon", "Wildberries", CANCEL])
        self.assertEqual(self.store.products(), [])
        answer, markup = await self.message(2, "Ozon")
        self.assertIn("Добавлено 1", answer)
        self.assertEqual(markup, KEYBOARD)
        p = self.store.products()[0]
        self.assertEqual(
            (p["market"], p["article"], p["tag"]), ("ozon", "1797187018", "#belazo")
        )
        self.assertEqual(p["threshold"], 35000)
        self.assertEqual(self.store.setting("monitoring", "0"), "0")
        self.assertEqual(self.store.queue(), [])

    async def test_bare_article_asks_market_then_tag_and_restores_main_keyboard(self):
        await self.message(1, "708052242")
        answer, markup = await self.message(2, "Wildberries")
        self.assertIn("Выберите тег", answer)
        self.assertIn("#belazo", self.labels(markup))
        self.assertEqual(self.store.products(), [])
        answer, markup = await self.message(3, "#belazo")
        p = self.store.products()[0]
        self.assertEqual(
            (p["market"], p["article"], p["tag"]), ("wb", "708052242", "#belazo")
        )
        self.assertIn("Добавлено 1", answer)
        self.assertEqual(markup, KEYBOARD)

    async def test_batch_and_mixed_links_preserve_explicit_link_market(self):
        await self.message(1, "/add #arton 123 https://www.ozon.ru/product/123/")
        answer, _ = await self.message(2, "Wildberries")
        self.assertIn("Добавлено 2", answer)
        self.assertEqual({p["market"] for p in self.store.products()}, {"ozon", "wb"})
        self.control.execute(
            "/add #arton " + " ".join(str(n) for n in range(1000, 1200))
        )
        result = self.control.execute("Ozon")
        self.assertIn("Добавлено 200", result)
        self.assertEqual(len(self.store.products()), 202)

    async def test_cancel_start_and_new_article_do_not_add_old_pending_product(self):
        await self.message(1, "/add #belazo 1797187018")
        answer, markup = await self.message(2, CANCEL)
        self.assertIn("отменено", answer)
        self.assertEqual(markup, KEYBOARD)
        await self.message(3, "123")
        await self.message(4, "Ozon")
        answer, markup = await self.message(5, "/cancel@paintwatch")
        self.assertIn("отменено", answer)
        self.assertEqual(markup, KEYBOARD)
        await self.message(6, "123")
        await self.message(7, "/start")
        self.assertIsNone(self.control.add_dialog("fixture").state())
        await self.message(8, "/add #belazo 1797187018")
        await self.message(9, "708052242")
        await self.message(10, "Wildberries")
        await self.message(11, "#arton")
        p = self.store.products()[0]
        self.assertEqual((p["article"], p["tag"]), ("708052242", "#arton"))
        self.assertEqual(len(self.store.products()), 1)

    async def test_owner_only_private_replies_and_bot_scope(self):
        await self.message(1, "/add #belazo 1797187018")
        self.api.send.reset_mock()
        self.api.call.return_value = [
            self.update(2, "Ozon", uid=456),
            self.update(3, "Ozon", chat_type="supergroup", chat_id=-1001),
            self.update(4, "Ozon", chat_id=456),
        ]
        await self.control.poll(self.api, "fixture")
        self.api.send.assert_not_awaited()
        self.assertEqual(self.store.products(), [])
        self.assertEqual(self.control.add_dialog("fixture").state()["stage"], "market")
        self.assertIsNone(self.control.add_dialog("different-bot").state())
        self.store.configure({"owner_id": 456})
        self.assertIsNone(self.control.add_dialog("fixture").state())
        self.store.configure({"owner_id": 123})
        await self.message(5, "Ozon")
        self.assertEqual(len(self.store.products()), 1)

    async def test_dialog_survives_restart_and_menu_refresh_keeps_question(self):
        await self.message(1, "/add #belazo 1797187018")
        restored = Store(self.temp.name)
        self.control = TelegramControl(restored, SecretStore(self.temp.name))
        await self.control.refresh_menu(self.api, "fixture")
        call = self.api.send.call_args
        self.assertIn("Ozon", call.args[1])
        self.assertIn("Wildberries", self.labels(call.kwargs["reply_markup"]))
        await self.message(2, "Ozon")
        self.assertEqual(restored.products()[0]["tag"], "#belazo")

    async def test_expired_and_corrupt_dialogs_are_discarded_without_adding(self):
        with patch("time.time", return_value=1000):
            await self.message(1, "/add #belazo 1797187018")
        with patch("time.time", return_value=1901):
            answer, markup = await self.message(2, "Ozon")
        self.assertIn("устарел", answer)
        self.assertEqual(markup, KEYBOARD)
        key = self.control.add_dialog("fixture").key
        for raw in ("broken JSON", json.dumps({"stage": "tag"}), "[]"):
            self.store.configure({key: raw})
            self.assertIn("устарел", self.control.execute("Ozon", "fixture"))
        self.assertEqual(self.store.products(), [])

    async def test_other_commands_and_wrong_replies_do_not_consume_choice(self):
        await self.message(1, "/add #belazo 1797187018")
        await self.message(2, "/status")
        await self.message(3, "/stop")
        answer, markup = await self.message(4, "какая площадка?")
        self.assertIn("Выберите Ozon", answer)
        self.assertIn("Ozon", self.labels(markup))
        self.assertEqual(self.store.products(), [])
        await self.message(5, "озон")
        self.assertEqual(len(self.store.products()), 1)
        self.assertEqual(self.store.setting("monitoring", "0"), "0")

    async def test_tag_pages_are_bounded_and_custom_tag_can_be_selected(self):
        for n in range(30):
            self.store.add_tag(f"#custom{n}", 12345)
        await self.message(1, "123")
        await self.message(2, "Ozon")
        seen = set()
        for page in range(4):
            dialog = self.control.add_dialog("fixture")
            labels = self.labels(dialog.markup())
            self.assertLessEqual(len(labels), 15)
            seen.update(label for label in labels if label.startswith("#"))
            if NEXT in labels:
                await self.message(3 + page, NEXT)
        self.assertEqual(seen, {t["name"] for t in self.store.tags()})
        await self.message(7, PREVIOUS)
        answer, markup = await self.message(8, "#custom29")
        self.assertIn("Добавлено 1", answer)
        self.assertEqual(markup, KEYBOARD)
        self.assertEqual(self.store.products()[0]["threshold"], 12345)

    async def test_full_links_still_add_without_question_and_duplicates_are_ignored(
        self,
    ):
        answer, markup = await self.message(
            1, "/add #belazo https://www.ozon.ru/product/1797187018/"
        )
        self.assertIn("Добавлено 1", answer)
        self.assertEqual(markup, KEYBOARD)
        await self.message(2, "/add #belazo 1797187018")
        answer, markup = await self.message(3, "Ozon")
        self.assertIn("Добавлено 0", answer)
        self.assertEqual(len(self.store.products()), 1)
        self.assertEqual(markup, KEYBOARD)

    async def test_invalid_batch_or_tag_does_not_create_partial_products(self):
        for n, text in enumerate(
            (
                "/add #missing 123",
                "/add #belazo 123 garbage",
                "/add #belazo",
                "/add #belazo https://ozon.ru/t/short",
            ),
            1,
        ):
            answer, markup = await self.message(n, text)
            self.assertIn("Ошибка:", answer)
            self.assertEqual(markup, KEYBOARD)
            self.assertEqual(self.store.products(), [])
        with self.assertRaises(ValueError):
            self.control.execute("/add #belazo ١٢٣")
        self.assertEqual(self.store.products(), [])
        await self.message(5, "/add https://www.ozon.ru/product/123/")
        await self.message(6, "#belazo")
        self.assertEqual(self.store.products()[0]["market"], "ozon")

    async def test_missing_tag_is_asked_again_instead_of_silently_using_a_default(self):
        self.store.add_tag("#temporary", 12345)
        await self.message(1, "/add #temporary 123")
        with self.store.db() as db:
            db.execute("DELETE FROM tags WHERE name='#temporary'")
        answer, markup = await self.message(2, "Ozon")
        self.assertIn("Тег больше недоступен", answer)
        self.assertIn("#belazo", self.labels(markup))
        self.assertEqual(self.store.products(), [])
        await self.message(3, "#belazo")
        self.assertEqual(self.store.products()[0]["threshold"], 35000)

    async def test_failed_confirmation_reply_does_not_replay_or_add_twice(self):
        await self.message(1, "/add #belazo 1797187018")
        self.api.send.side_effect = TelegramError("Lost reply", uncertain=True)
        with self.assertRaises(TelegramError):
            await self.message(2, "Ozon")
        self.assertEqual(len(self.store.products()), 1)
        self.assertIsNone(self.control.add_dialog("fixture").state())
        self.api.send.side_effect = None
        self.api.send.reset_mock()
        await self.control.poll(self.api, "fixture")
        self.api.send.assert_not_awaited()
        self.assertEqual(len(self.store.products()), 1)

    async def test_failed_insert_rolls_back_and_does_not_consume_dialog(self):
        await self.message(1, "/add #belazo 1797187018")
        key = self.control.add_dialog("fixture").key
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add(
                [parse_ref("1797187018", "ozon")], "", "#missing", clear_setting=key
            )
        self.assertIsNotNone(self.control.add_dialog("fixture").state())
        self.assertEqual(self.store.products(), [])
        await self.message(2, "Ozon")
        self.assertIsNone(self.control.add_dialog("fixture").state())
        self.assertEqual(len(self.store.products()), 1)
