"""Known group members and restart-safe, explicit username mention commands."""

import re
import time

from .telegram import TelegramError


GROUP_COMMANDS = [
    {"command": "tagall", "description": "Упомянуть известных участников по username"},
    {
        "command": "tagall_status",
        "description": "Список участников и состояние упоминаний",
    },
    {"command": "tagall_cancel", "description": "Остановить оставшиеся упоминания"},
    {"command": "here", "description": "Добавить себя в список упоминаний"},
]
ALLOWED_UPDATES = ["message", "chat_member", "my_chat_member"]


class GroupMentions:
    def __init__(self, store, fingerprint):
        self.store, self.bot = store, fingerprint
        self.chat = self.parse_chat(store.setting("chat_id"))
        self.me = None
        with store.db() as db:
            db.execute(
                "UPDATE telegram_mention_jobs SET state='uncertain',error='Программа завершилась во время отправки. Проверьте чат перед повтором' WHERE bot=? AND state='sending'",
                (self.bot,),
            )
            db.execute(
                "UPDATE telegram_mention_jobs SET state='cancelled',error='Группа в настройках изменена' WHERE bot=? AND (chat_id<>? OR ? IS NULL) AND state='active'",
                (self.bot, self.chat, self.chat),
            )

    @staticmethod
    def parse_chat(value):
        return int(value) if re.fullmatch(r"-[1-9]\d*", value) else None

    def chat_id(self):
        return self.chat

    def configured(self):
        return self.chat is not None and self.chat == self.parse_chat(
            self.store.setting("chat_id")
        )

    @staticmethod
    def username(user):
        name = user.get("username", "")
        return (
            name
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", name)
            else ""
        )

    def owner(self):
        value = self.store.setting("owner_id")
        return int(value) if value.isdigit() and int(value) > 0 else None

    @staticmethod
    def active(member):
        status = member.get("status")
        if status in ("creator", "administrator", "member"):
            return True
        if status in ("left", "kicked"):
            return False
        if status == "restricted" and type(member.get("is_member")) is bool:
            return member["is_member"]
        raise TelegramError("Telegram не подтвердил участие пользователя в группе")

    def remember(self, user, active=True):
        uid, chat = user.get("id"), self.chat_id()
        if type(uid) is not int or uid <= 0 or user.get("is_bot") or chat is None:
            return
        username = self.username(user)
        with self.store.db() as db:
            db.execute(
                "INSERT INTO telegram_members VALUES(?,?,?,?,?) ON CONFLICT(bot,chat_id,user_id) DO UPDATE SET username=excluded.username,active=excluded.active WHERE username<>excluded.username OR active<>excluded.active",
                (self.bot, chat, uid, username, int(active)),
            )

    def observe(self, update):
        if not self.configured():
            return
        for kind in ("chat_member", "my_chat_member"):
            event = update.get(kind, {})
            if event.get("chat", {}).get("id") == self.chat_id():
                member = event.get("new_chat_member", {})
                self.remember(member.get("user", {}), self.active(member))
                if kind == "my_chat_member" and not self.active(member):
                    self.state("cancelled", "Бот удалён из группы")
        message = update.get("message", {})
        chat = message.get("chat", {})
        if (
            chat.get("type") not in ("group", "supergroup")
            or chat.get("id") != self.chat_id()
        ):
            return
        if not message.get("sender_chat"):
            self.remember(message.get("from", {}))
        for user in message.get("new_chat_members", []):
            self.remember(user)
        if message.get("left_chat_member"):
            self.remember(message["left_chat_member"], False)

    async def identity(self, api):
        if self.me is None:
            self.me = await api.call("getMe")
        return self.me

    async def administrator(self, api, uid):
        if uid == self.owner():
            return True
        member = await api.call(
            "getChatMember", {"chat_id": self.chat_id(), "user_id": uid}
        )
        return member.get("status") in ("creator", "administrator")

    async def bot_is_admin(self, api):
        me = await self.identity(api)
        member = await api.call(
            "getChatMember", {"chat_id": self.chat_id(), "user_id": me["id"]}
        )
        return member.get("status") in ("creator", "administrator")

    def job(self):
        with self.store.db() as db:
            row = db.execute(
                "SELECT * FROM telegram_mention_jobs WHERE bot=? AND chat_id=?",
                (self.bot, self.chat_id()),
            ).fetchone()
            return dict(row) if row else None

    def state(self, state, error="", delay=0):
        with self.store.db() as db:
            db.execute(
                "UPDATE telegram_mention_jobs SET state=?,error=?,next_try=? WHERE bot=? AND chat_id=?",
                (state, error[:400], time.time() + delay, self.bot, self.chat_id()),
            )

    async def handle(self, api, message):
        parts = message.get("text", "").strip().split()
        if not parts:
            return False
        command, _, target = parts[0].lower().partition("@")
        if command not in {"/" + item["command"] for item in GROUP_COMMANDS}:
            return False
        chat, sender = message.get("chat", {}), message.get("from", {})
        private = (
            chat.get("type") == "private"
            and chat.get("id") == sender.get("id") == self.owner()
        )
        group = (
            self.configured()
            and chat.get("type") in ("group", "supergroup")
            and chat.get("id") == self.chat_id()
        )
        if (
            not (private or group)
            or message.get("forward_origin")
            or message.get("is_automatic_forward")
        ):
            return False
        anonymous = message.get("sender_chat", {}).get("id") == self.chat_id() and group
        if message.get("sender_chat") and not anonymous:
            return True
        uid = 0 if anonymous else sender.get("id")
        if not anonymous and (type(uid) is not int or uid <= 0 or sender.get("is_bot")):
            return True
        if target and target != (await self.identity(api)).get("username", "").lower():
            return True
        reply = chat["id"]
        if self.chat_id() is None:
            await api.send(
                reply, "Сохраните отрицательный ID группы в настройках PaintWatch"
            )
            return True
        if command == "/here":
            if private or anonymous:
                await api.send(reply, "Отправьте /here в группе от своего аккаунта")
            else:
                self.remember(sender)
                name = self.username(sender)
                await api.send(
                    reply,
                    f"Добавлен @{name}"
                    if name
                    else "У аккаунта нет username. Задайте его в Telegram и снова отправьте /here",
                )
            return True
        if not anonymous and not await self.administrator(api, uid):
            await api.send(
                reply, "Команда доступна владельцу PaintWatch и администраторам группы"
            )
            return True
        if command == "/tagall_cancel":
            self.state("cancelled")
            await api.send(reply, "Оставшиеся упоминания остановлены")
            return True
        if command == "/tagall_status":
            with self.store.db() as db:
                total, names = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(username<>''),0) FROM telegram_members WHERE bot=? AND chat_id=? AND active=1",
                    (self.bot, self.chat_id()),
                ).fetchone()
            job = self.job()
            states = {
                "active": "идёт",
                "sending": "отправляется",
                "completed": "завершено",
                "uncertain": "результат неизвестен",
                "cancelled": "остановлено",
            }
            await api.send(
                reply,
                f"Известно участников: {total}, с username: {names}.\nУпоминания: {states.get(job['state'], job['state']) if job else 'не запускались'}, отправлено имён: {job['sent'] if job else 0}.\n{job['error'] if job else ''}\nTelegram не отдаёт полный список группы. Молчаливым участникам нужно один раз отправить /here",
            )
            return True
        if len(parts) != 1:
            await api.send(reply, "Используйте /tagall без дополнительных аргументов")
            return True
        if not await self.bot_is_admin(api):
            await api.send(
                reply,
                "Сделайте бота администратором группы: это нужно для событий участников и проверки членства",
            )
            return True
        admins = await api.call("getChatAdministrators", {"chat_id": self.chat_id()})
        if not self.configured():
            return True
        for member in admins:
            self.remember(member.get("user", {}))
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT state FROM telegram_mention_jobs WHERE bot=? AND chat_id=?",
                (self.bot, self.chat_id()),
            ).fetchone()
            if old and old[0] in ("active", "sending"):
                answer = "Упоминания уже идут. /tagall_status — состояние, /tagall_cancel — остановить"
            else:
                count = db.execute(
                    "SELECT COUNT(*) FROM telegram_members WHERE bot=? AND chat_id=? AND active=1 AND username<>''",
                    (self.bot, self.chat_id()),
                ).fetchone()[0]
                if not count:
                    answer = "Пока нет известных участников с username. Участникам нужно один раз отправить /here в группе"
                else:
                    db.execute(
                        "DELETE FROM telegram_mention_jobs WHERE bot=? AND chat_id=?",
                        (self.bot, self.chat_id()),
                    )
                    db.execute(
                        "INSERT INTO telegram_mention_jobs(bot,chat_id,requester,state) VALUES(?,?,?,'active')",
                        (self.bot, self.chat_id(), uid),
                    )
                    db.execute(
                        "INSERT INTO telegram_mention_targets(bot,chat_id,user_id) SELECT bot,chat_id,user_id FROM telegram_members WHERE bot=? AND chat_id=? AND active=1 AND username<>''",
                        (self.bot, self.chat_id()),
                    )
                    answer = f"Упомяну {count} известных боту участников по username. Молчаливым участникам нужно один раз отправить /here"
        await api.send(reply, answer)
        return True

    async def deliver(self, api):
        if not self.configured():
            return
        job = self.job()
        if job and job["state"] == "sending":
            self.state(
                "uncertain",
                "Результат последней отправки не сохранён. Проверьте чат перед повтором",
            )
            return
        if not job or job["state"] != "active" or job["next_try"] > time.time():
            return
        chat = self.chat_id()
        try:
            if job["requester"] and not await self.administrator(api, job["requester"]):
                self.state("cancelled", "Автор команды больше не администратор")
                return
            if not await self.bot_is_admin(api):
                self.state("cancelled", "Боту нужны права администратора")
                return
            with self.store.db() as db:
                pending = [
                    r[0]
                    for r in db.execute(
                        "SELECT user_id FROM telegram_mention_targets WHERE bot=? AND chat_id=? AND state='pending' ORDER BY user_id LIMIT 5",
                        (self.bot, chat),
                    )
                ]
            if not pending:
                self.state("completed")
                return
            names = {}
            for uid in pending:
                member = await api.call(
                    "getChatMember", {"chat_id": chat, "user_id": uid}
                )
                if member.get("user", {}).get("id") != uid:
                    raise TelegramError("Telegram вернул другого участника")
                self.remember(member["user"], self.active(member))
                user = member["user"]
                name = self.username(user)
                if self.active(member) and not user.get("is_bot") and name:
                    names[name.casefold()] = name
            if not self.configured():
                self.state("cancelled", "Группа в настройках изменена")
                return
            if names:
                self.state("sending")
                try:
                    await api.send(
                        chat,
                        "📣 Участники чата:\n"
                        + " ".join(
                            "@" + name
                            for name in sorted(names.values(), key=str.casefold)
                        ),
                    )
                except TelegramError as error:
                    self.state(
                        "uncertain" if error.uncertain else "active",
                        str(error),
                        error.delay,
                    )
                    return
            with self.store.db() as db:
                db.executemany(
                    "UPDATE telegram_mention_targets SET state='done' WHERE bot=? AND chat_id=? AND user_id=?",
                    [(self.bot, chat, uid) for uid in pending],
                )
                remaining = db.execute(
                    "SELECT EXISTS(SELECT 1 FROM telegram_mention_targets WHERE bot=? AND chat_id=? AND state='pending')",
                    (self.bot, chat),
                ).fetchone()[0]
                db.execute(
                    "UPDATE telegram_mention_jobs SET state=?,sent=sent+?,next_try=?,error='' WHERE bot=? AND chat_id=?",
                    (
                        "active" if remaining else "completed",
                        len(names),
                        time.time() + 4,
                        self.bot,
                        chat,
                    ),
                )
        except TelegramError as error:
            self.state("active", str(error), error.delay)
