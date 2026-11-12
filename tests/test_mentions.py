import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from paintwatch.control import TelegramControl
from paintwatch.mentions import ALLOWED_UPDATES, GROUP_COMMANDS, GroupMentions
from paintwatch.platform import SecretStore
from paintwatch.store import Store
from paintwatch.telegram import Telegram, TelegramError


def user(uid, username="", bot=False):
    return {"id": uid, "is_bot": bot, "username": username}


def message(text, uid=123, username="Owner", chat=-1001, kind="supergroup", **extra):
    return {
        "chat": {"id": chat, "type": kind},
        "from": user(uid, username),
        "text": text,
        **extra,
    }


class MentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.configure(
            {"owner_id": 123, "chat_id": -1001, "telegram_offset_fixture": 1}
        )
        self.members = {}
        self.updates = []
        self.bot_admin = True
        self.api = AsyncMock()
        self.api.call.side_effect = self.call
        self.control = TelegramControl(self.store, SecretStore(self.temp.name))
        self.group = self.control.group_mentions("fixture")

    def tearDown(self):
        self.temp.cleanup()

    async def call(self, method, params=None):
        if method == "getUpdates":
            return self.updates
        if method == "getMe":
            return user(999, "PriceBot", True)
        if method == "getChatMember":
            uid = params["user_id"]
            if uid == 999:
                return {
                    "user": user(999, "PriceBot", True),
                    "status": "administrator" if self.bot_admin else "member",
                }
            return self.members.get(uid, {"user": user(uid), "status": "left"})
        if method == "getChatAdministrators":
            return [
                member
                for member in self.members.values()
                if member["status"] in ("administrator", "creator")
            ]
        if method in ("setMyCommands", "setChatMenuButton"):
            return True
        raise AssertionError(method)

    def add(self, uid, name, status="member", **extra):
        item = {"user": user(uid, name), "status": status, **extra}
        self.members[uid] = item
        self.group.remember(item["user"], self.group.active(item))

    async def start(self, **kwargs):
        self.assertTrue(
            await self.group.handle(
                self.api, message("/tagall", chat=123, kind="private", **kwargs)
            )
        )

    def sent(self):
        return [
            call.args[1]
            for call in self.api.send.call_args_list
            if call.args[1].startswith("📣")
        ]

    async def tick(self, stamp=2000000000):
        with patch("paintwatch.mentions.time.time", return_value=stamp):
            await self.group.deliver(self.api)

    async def test_poll_collects_only_configured_group_humans_without_private_message_control(
        self,
    ):
        self.updates = [
            {"update_id": 1, "message": message("Привет", uid=1, username="First")},
            {"update_id": 2, "message": message("/run", uid=2, username="Second")},
            {
                "update_id": 3,
                "message": message("Привет", uid=3, username="Other", chat=-1002),
            },
            {
                "update_id": 4,
                "message": message(
                    "Привет", uid=4, username="Private", chat=4, kind="private"
                ),
            },
            {
                "update_id": 5,
                "message": message(
                    "Привет",
                    uid=5,
                    username="Bot",
                    **{"from": user(5, "OtherBot", True)},
                ),
            },
        ]
        await self.control.poll(self.api, "fixture")
        self.assertEqual(self.store.setting("monitoring", "0"), "0")
        self.api.send.assert_not_awaited()
        with self.store.db() as db:
            rows = [
                tuple(row)
                for row in db.execute(
                    "SELECT user_id,username FROM telegram_members ORDER BY user_id"
                )
            ]
        self.assertEqual(rows, [(1, "First"), (2, "Second")])
        self.assertEqual(
            self.api.call.call_args.args[1]["allowed_updates"], ALLOWED_UPDATES
        )

    async def test_joins_leaves_restrictions_and_username_changes_survive_restart(self):
        self.group.observe(
            {
                "message": message(
                    "", new_chat_members=[user(1, "First"), user(999, "Bot", True)]
                )
            }
        )
        self.group.observe(
            {
                "chat_member": {
                    "chat": {"id": -1001},
                    "new_chat_member": {
                        "user": user(1, "Renamed"),
                        "status": "restricted",
                        "is_member": True,
                    },
                }
            }
        )
        self.group.observe(
            {"message": message("", left_chat_member=user(1, "Renamed"))}
        )
        restored = GroupMentions(Store(self.temp.name), "fixture")
        with self.store.db() as db:
            row = db.execute(
                "SELECT username,active FROM telegram_members WHERE user_id=1"
            ).fetchone()
        self.assertEqual(tuple(row), ("Renamed", 0))
        restored.observe(
            {
                "chat_member": {
                    "chat": {"id": -1001},
                    "new_chat_member": {"user": user(1), "status": "member"},
                }
            }
        )
        with self.store.db() as db:
            row = db.execute(
                "SELECT username,active FROM telegram_members WHERE user_id=1"
            ).fetchone()
        self.assertEqual(tuple(row), ("", 1))

    async def test_here_registers_members_without_exposing_private_controls(self):
        self.updates = [
            {"update_id": 1, "message": message("/here", uid=1, username="Member")},
            {"update_id": 2, "message": message("/here", uid=2, username="")},
        ]
        await self.control.poll(self.api, "fixture")
        texts = [call.args[1] for call in self.api.send.call_args_list]
        self.assertIn("Добавлен @Member", texts)
        self.assertTrue(any("нет username" in text for text in texts))
        self.assertEqual(self.store.setting("monitoring", "0"), "0")

    async def test_admin_command_snapshots_members_and_batches_current_usernames_without_bots_or_leavers(
        self,
    ):
        for uid in range(1, 13):
            self.add(uid, f"Member{uid}")
        self.add(20, "Left", "left")
        self.group.remember(user(999, "Bot", True))
        await self.start()
        self.members[1]["user"]["username"] = "Changed"
        self.members[2]["status"] = "left"
        self.members[3]["user"]["username"] = ""
        await self.tick()
        self.assertIn("@Changed", self.sent()[0])
        self.assertNotIn("@Member1 ", self.sent()[0] + " ")
        self.assertNotIn("@Member2", " ".join(self.sent()))
        self.group = GroupMentions(Store(self.temp.name), "fixture")
        await self.tick(2000000005)
        await self.tick(2000000010)
        output = " ".join(self.sent())
        self.assertEqual(len(self.sent()), 3)
        self.assertEqual(output.count("@"), 10)
        self.assertEqual(self.group.job()["sent"], 10)
        self.assertEqual(self.group.job()["state"], "completed")
        self.assertNotIn("@Left", output)
        self.assertNotIn("@Bot", output)
        self.assertTrue(all(len(text.split("@")) - 1 <= 5 for text in self.sent()))

    async def test_regular_members_other_groups_forwarded_commands_and_other_bot_suffix_are_not_authorized(
        self,
    ):
        self.add(1, "Member")
        for command in [
            message("/tagall", uid=1),
            message("/tagall", chat=-1002),
            message("/tagall@SomeoneElse"),
            message("/tagall", forward_origin={"type": "user"}),
            message("/tagall", sender_chat={"id": -1002}),
        ]:
            await self.group.handle(self.api, command)
        self.assertIsNone(self.group.job())
        self.assertEqual(len(self.sent()), 0)

    async def test_group_admin_and_anonymous_own_group_admin_are_allowed_but_private_nonowner_is_not(
        self,
    ):
        self.add(1, "Admin", "administrator")
        await self.group.handle(self.api, message("/tagall@PriceBot", uid=1))
        self.assertEqual(self.group.job()["requester"], 1)
        await self.group.handle(self.api, message("/tagall_cancel", uid=1))
        await self.group.handle(
            self.api, message("/tagall", uid=999, sender_chat={"id": -1001})
        )
        self.assertEqual(self.group.job()["requester"], 0)
        self.group.state("cancelled")
        await self.group.handle(
            self.api, message("/tagall", uid=1, chat=1, kind="private")
        )
        self.assertEqual(self.group.job()["state"], "cancelled")

    async def test_owner_can_start_in_group_and_updates_do_not_replay(self):
        self.add(1, "Member")
        self.updates = [{"update_id": 1, "message": message("/tagall")}]
        await self.control.poll(self.api, "fixture")
        await self.control.poll(self.api, "fixture")
        self.api.send.assert_awaited_once()
        self.assertEqual(self.group.job()["requester"], 123)
        await self.tick()
        self.assertEqual(
            self.sent()[0].count("@"), 1
        )  # Owner without membership in the fixture is skipped.

    async def test_existing_job_is_not_duplicated_and_cancel_stops_remaining_parts(
        self,
    ):
        for uid in range(1, 8):
            self.add(uid, f"Member{uid}")
        await self.start()
        await self.start()
        await self.tick()
        await self.group.handle(self.api, message("/tagall_cancel"))
        await self.tick(2000000010)
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.group.job()["state"], "cancelled")

    async def test_bot_admin_rights_are_required_and_revocation_cancels_job(self):
        self.add(1, "Member")
        self.bot_admin = False
        await self.start()
        self.assertIsNone(self.group.job())
        self.assertIn("администратором", self.api.send.call_args.args[1])
        self.bot_admin = True
        await self.start()
        self.bot_admin = False
        await self.tick()
        self.assertEqual(self.group.job()["state"], "cancelled")
        self.assertEqual(self.sent(), [])

    async def test_lost_admin_authority_or_changed_owner_cancels_pending_mentions(self):
        self.add(1, "Member")
        await self.start()
        self.store.configure({"owner_id": 456})
        self.members[123] = {"user": user(123), "status": "member"}
        await self.tick()
        self.assertEqual(self.group.job()["state"], "cancelled")
        self.assertEqual(self.sent(), [])

    async def test_membership_lookup_failure_retries_after_delay_without_partial_send(
        self,
    ):
        self.add(1, "First")
        self.add(2, "Second")
        await self.start()
        fail = True

        async def call(method, params=None):
            if fail and method == "getChatMember" and params["user_id"] == 2:
                raise TelegramError("Rate limit", delay=17)
            return await self.call(method, params)

        self.api.call.side_effect = call
        await self.tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.group.job()["next_try"], 2000000017)
        fail = False
        await self.tick(2000000016)
        self.assertEqual(self.sent(), [])
        await self.tick(2000000017)
        self.assertEqual(len(self.sent()), 1)

    async def test_rejected_send_can_retry_but_uncertain_send_and_restart_never_repeat(
        self,
    ):
        self.add(1, "Member")
        await self.start()
        self.api.send.side_effect = TelegramError("Rejected", delay=7)
        await self.tick()
        self.assertEqual(self.group.job()["state"], "active")
        self.assertEqual(self.group.job()["next_try"], 2000000007)
        self.api.send.side_effect = TelegramError("Lost response", uncertain=True)
        await self.tick(2000000007)
        count = self.api.send.await_count
        self.assertEqual(self.group.job()["state"], "uncertain")
        self.group = GroupMentions(Store(self.temp.name), "fixture")
        await self.tick(2000000100)
        self.assertEqual(self.api.send.await_count, count)
        self.assertEqual(self.group.job()["sent"], 0)

    async def test_crash_during_send_is_uncertain_on_restart(self):
        self.add(1, "Member")
        await self.start()
        self.group.state("sending")
        self.group = GroupMentions(Store(self.temp.name), "fixture")
        count = self.api.send.await_count
        await self.tick()
        self.assertEqual(self.api.send.await_count, count)
        self.assertEqual(self.group.job()["state"], "uncertain")

    async def test_new_bot_and_new_chat_do_not_inherit_recipients_or_send_old_job(self):
        self.add(1, "Member")
        await self.start()
        other = GroupMentions(self.store, "other_bot")
        self.assertIsNone(other.job())
        self.store.configure({"chat_id": -1002})
        await self.tick()
        changed = GroupMentions(self.store, "fixture")
        self.assertIsNone(changed.job())
        with self.store.db() as db:
            state = db.execute(
                "SELECT state FROM telegram_mention_jobs WHERE chat_id=-1001"
            ).fetchone()[0]
        self.assertEqual(state, "cancelled")
        self.assertEqual(self.sent(), [])

    async def test_group_change_during_lookup_does_not_write_recipients_into_the_new_group(
        self,
    ):
        self.add(1, "Member")
        await self.start()

        async def call(method, params=None):
            if method == "getChatMember" and params["user_id"] == 1:
                self.store.configure({"chat_id": -1002})
            return await self.call(method, params)

        self.api.call.side_effect = call
        await self.tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.group.job()["state"], "cancelled")
        with self.store.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM telegram_members WHERE chat_id=-1002"
                ).fetchone()[0],
                0,
            )

    async def test_wrong_member_identity_and_bot_removal_do_not_send_mentions(self):
        self.add(1, "Member")
        await self.start()

        async def call(method, params=None):
            if method == "getChatMember" and params["user_id"] == 1:
                return {"user": user(8, "Outsider"), "status": "member"}
            return await self.call(method, params)

        self.api.call.side_effect = call
        await self.tick()
        self.assertEqual(self.sent(), [])
        self.group.observe(
            {
                "my_chat_member": {
                    "chat": {"id": -1001},
                    "new_chat_member": {
                        "user": user(999, "PriceBot", True),
                        "status": "kicked",
                    },
                }
            }
        )
        self.assertEqual(self.group.job()["state"], "cancelled")

    async def test_incomplete_committed_send_is_not_retried_in_the_same_process(self):
        self.add(1, "Member")
        await self.start()
        self.group.state("sending")
        count = self.api.send.await_count
        await self.tick()
        self.assertEqual(self.group.job()["state"], "uncertain")
        self.assertEqual(self.api.send.await_count, count)

    async def test_malformed_names_are_not_interpreted_as_mentions_and_unknown_members_are_explained(
        self,
    ):
        self.add(1, "name\n@outsider")
        await self.start()
        self.assertIsNone(self.group.job())
        self.assertIn("/here", self.api.send.call_args.args[1])
        await self.group.handle(self.api, message("/tagall_status"))
        self.assertIn("полный список", self.api.send.call_args.args[1])

    async def test_group_menu_contains_only_group_commands_and_is_published_once(self):
        await self.control.refresh_group_menu(self.api, "fixture")
        await self.control.refresh_group_menu(self.api, "fixture")
        self.assertEqual(self.api.call.await_count, 2)
        for call in self.api.call.call_args_list:
            self.assertEqual(call.args[0], "setMyCommands")
            self.assertEqual(call.args[1]["scope"], {"type": "chat", "chat_id": -1001})
            self.assertEqual(call.args[1]["commands"], GROUP_COMMANDS)

    async def test_private_commands_still_work_without_configured_group(self):
        self.store.configure({"chat_id": ""})
        self.updates = [
            {"update_id": 1, "message": message("/status", chat=123, kind="private")}
        ]
        await self.control.poll(self.api, "fixture")
        self.assertIn("Мониторинг", self.api.send.call_args.args[1])

    async def test_all_group_sends_share_one_persistent_rate_limit_but_private_messages_do_not_wait(
        self,
    ):
        token = "123456789:" + "A" * 35

        def response(request):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        first = Telegram(
            token,
            httpx.AsyncClient(transport=httpx.MockTransport(response)),
            store=self.store,
        )
        second = Telegram(
            token,
            httpx.AsyncClient(transport=httpx.MockTransport(response)),
            store=self.store,
        )
        try:
            with (
                patch("paintwatch.telegram.time.time", return_value=1000),
                patch(
                    "paintwatch.telegram.asyncio.sleep", new_callable=AsyncMock
                ) as sleep,
            ):
                await first.send(-1001, "Price")
                await second.send(-1001, "@Member")
                await first.send(123, "Private status")
                await second.send(-1001, "@AnotherMember")
                await second.send(-1002, "Other group")
            self.assertEqual(sleep.await_count, 2)
            self.assertAlmostEqual(sleep.await_args_list[0].args[0], 3.2)
            self.assertAlmostEqual(sleep.await_args_list[1].args[0], 6.4)
        finally:
            await first.close()
            await second.close()
