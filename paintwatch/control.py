"""Private Telegram commands and grouped operational alerts for the configured owner."""

import asyncio
import hashlib
import time

from .domain import money, parse_batch, rub
from .telegram import Telegram, TelegramError


HELP = """PaintWatch — управление
/id — ваш Telegram ID
/status — состояние и нагрузка
/run /stop — включить / остановить мониторинг
/products [страница] — список товаров (ID из первого столбца)
/add #тег ссылка [ссылка…] — добавить товары
/delete ID confirm — удалить товар и его историю
/pause ID /resume ID — выключить / включить товар
/check [ID|all] — запросить проверки по текущему интервалу
/scan — разово проверить все включённые товары сейчас
/tag #название РУБЛИ — создать тег с порогом
/verified ID — повторить после ручной проверки доступа
/interval СЕКУНДЫ — интервал, 0 = непрерывный обход
/parallel ЧИСЛО — 1–32 одновременные проверки
/threshold ID РУБЛИ — индивидуальный порог
/errors — последние проблемы
/help — эта справка

Команды работают, пока PaintWatch запущен. Chrome с расширением нужен для сбора цен."""


class TelegramControl:
    def __init__(self, store, vault, bridge=None):
        self.store, self.vault, self.bridge = store, vault, bridge
        self.started = time.time()

    def owner(self):
        value = self.store.setting("owner_id")
        return int(value) if value.isdigit() and int(value) > 0 else None

    def problems(self):
        products = self.store.products()
        issues = [
            f"{p['market'].upper()} #{p['id']} {p['name'][:60]}: {p['issue'][:200]}"
            for p in reversed(products)
            if p["enabled"] and p["issue"]
        ]
        if (
            self.store.setting("monitoring", "0") == "1"
            or (self.bridge and self.bridge.status()["manual"])
        ) and any(p["enabled"] for p in products):
            if (
                self.store.setting("collector", "extension") == "extension"
                and time.time()
                - max(self.started, int(self.store.setting("extension_seen", "0")))
                > 90
            ):
                issues.insert(
                    0,
                    "Расширение Chrome не отвечает более 90 секунд. Проверьте браузер и подключение",
                )
        for event in reversed(self.store.queue()):
            if event["state"] in ("blocked", "uncertain") or (
                event["state"] == "pending" and event["error"]
            ):
                issues.append(f"Уведомление №{event['id']}: {event['error'][:200]}")
        return issues

    def execute(self, text):
        parts = text.split()
        if not parts:
            return HELP
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if command in ("/start", "/help"):
            return HELP
        if command == "/status":
            products = self.store.products()
            status = self.bridge.status() if self.bridge else {"active": 0, "due": 0}
            return (
                f"Мониторинг: {'включён' if self.store.setting('monitoring', '0') == '1' else 'остановлен'}\n"
                f"Товаров: {len(products)}, включено: {sum(p['enabled'] for p in products)}\n"
                f"Проверяется: {status['active']}, ожидают: {status['due']}\n"
                f"Разовый обход: осталось {status.get('manual', 0)}\n"
                f"Интервал: {self.store.interval()} с (0 = непрерывно)\n"
                f"Параллельно: {self.store.parallelism()}, лимит: {self.store.check_timeout()} с\n"
                f"Проблем: {len(self.problems())}"
            )
        if command in ("/run", "/stop"):
            if command == "/stop":
                self.store.stop_monitoring()
            else:
                self.store.configure({"monitoring": 1})
            return (
                "Мониторинг включён"
                if command == "/run"
                else "Мониторинг остановлен. Текущие результаты не будут приняты"
            )
        if command == "/scan":
            count = self.store.request_scan()
            return f"Разово проверить все цены: {count} включённых товаров. Расписание и паузы площадок сохраняются"
        if command == "/tag" and len(args) == 2:
            tag = self.store.add_tag(args[0], money(args[1]))
            return f"Создан тег {tag}, порог {rub(money(args[1]))}"
        if command == "/products":
            page = int(args[0]) if args else 1
            if page < 1:
                raise ValueError("Страница должна быть от 1")
            products = list(reversed(self.store.products()))
            rows = products[(page - 1) * 15 : page * 15]
            return (
                f"Товары · страница {page}/{max(1, (len(products) + 14) // 15)}\n"
                + "\n".join(
                    f"#{p['id']} {p['market'].upper()} {p['article']} {p['tag']} · {rub(p['price'])} · {'пауза' if not p['enabled'] else p['status']}"
                    for p in rows
                )
            )
        if command == "/errors":
            issues = self.problems()
            return (
                "Проблем не обнаружено"
                if not issues
                else f"Проблем: {len(issues)}\n" + "\n".join(issues[:10])
            )
        if command == "/interval" and len(args) == 1:
            interval = int(args[0])
            if not 0 <= interval <= 2**63 - 1 - int(time.time()):
                raise ValueError("Нужно неотрицательное число секунд в пределах SQLite")
            self.store.configure({"interval": interval})
            self.store.request_check()
            return (
                f"Интервал: {interval} с (0 = непрерывно). Паузы площадок сохраняются"
            )
        if command == "/parallel" and len(args) == 1:
            count = int(args[0])
            if not 1 <= count <= 32:
                raise ValueError("Допустимо 1–32 проверки")
            self.store.configure({"parallelism": count})
            return f"Параллельно: {count}. Уже начатые проверки завершатся"
        if command == "/add" and len(args) >= 2:
            tag = args[0]
            if tag not in {t["name"] for t in self.store.tags()}:
                raise ValueError(
                    "Неизвестный тег. Доступны: "
                    + " ".join(t["name"] for t in self.store.tags())
                )
            refs = parse_batch("\n".join(args[1:]))
            count = self.store.add(refs, "", tag)
            return f"Добавлено {count} из {len(refs)} карточек. Дубликаты пропущены"
        if command == "/check" and (not args or args == ["all"]):
            self.store.request_check()
            return "Запрошены проверки всех товаров по текущему интервалу. Паузы площадок сохраняются"
        if (
            command
            in ("/delete", "/pause", "/resume", "/check", "/verified", "/threshold")
            and args
        ):
            pid = int(args[0].lstrip("#"))
            p = self.store.product(pid)
            if not p:
                raise ValueError("Товар с таким ID не найден; используйте /products")
            if command == "/delete":
                if args[1:] != ["confirm"]:
                    return f"Будут удалены #{pid} {p['market'].upper()} {p['article']}, история и очередь. Для удаления: /delete {pid} confirm"
                self.store.delete(pid)
                return f"Товар #{pid} удалён"
            if command == "/threshold":
                if len(args) != 2:
                    raise ValueError("Пример: /threshold 12 400.50")
                self.store.edit(
                    pid,
                    p["name"],
                    p["tag"],
                    money(args[1]),
                    p["color"],
                    p["volume"],
                    p["pack"],
                )
                return f"Порог #{pid}: {rub(self.store.product(pid)['threshold'])}. Смена порога сама по себе не рассылает уведомления"
            if command in ("/pause", "/resume"):
                self.store.enable(pid, command == "/resume")
                return (
                    f"Товар #{pid}: {'включён' if command == '/resume' else 'на паузе'}"
                )
            if command == "/verified":
                self.store.resume_after_verification(pid)
            else:
                self.store.request_check(pid)
            return f"Запрошена проверка #{pid}. Активный HTTP 429 не отменяется"
        return "Команда не распознана. /help — список команд"

    async def poll(self, api, fingerprint):
        key = "telegram_offset_" + fingerprint
        offset = self.store.setting(key)
        if not offset:
            # Discard commands queued before first installation/token change.
            old = await api.call(
                "getUpdates",
                {
                    "offset": -1,
                    "limit": 1,
                    "timeout": 0,
                    "allowed_updates": ["message"],
                },
            )
            offset = str(max((u["update_id"] + 1 for u in old), default=0))
            self.store.configure({key: offset})
            return
        updates = await api.call(
            "getUpdates",
            {
                "offset": int(offset),
                "limit": 20,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        for update in updates:
            if update["update_id"] < int(self.store.setting(key, "0")):
                continue
            # Persist before applying commands: a restart never repeats a deletion/addition.
            self.store.configure({key: update["update_id"] + 1})
            message = update.get("message", {})
            sender, chat = message.get("from", {}), message.get("chat", {})
            uid = sender.get("id")
            if (
                type(uid) is not int
                or chat.get("type") != "private"
                or chat.get("id") != uid
                or sender.get("is_bot")
            ):
                continue
            text = message.get("text", "").strip()
            if text.split("@", 1)[0] == "/id":
                await api.send(
                    uid,
                    f"Ваш Telegram ID: {uid}. Укажите его в настройках PaintWatch → Ваш ID",
                )
                continue
            if uid != self.owner():
                continue
            try:
                answer = self.execute(text)
            except (ValueError, OverflowError) as error:
                answer = "Ошибка: " + str(error)[:500]
            await api.send(uid, answer)

    async def alert(self, api):
        owner = self.owner()
        if not owner:
            return
        issues = self.problems()
        if not issues:
            self.store.configure({"private_issue_hash": ""})
            return
        now = int(time.time())
        last = int(self.store.setting("private_issue_at", "0"))
        digest = hashlib.sha256(
            (str(owner) + "\n" + "\n".join(sorted(set(issues)))).encode()
        ).hexdigest()
        same = digest == self.store.setting("private_issue_hash")
        if now - last < (1800 if same else 60):
            return
        # Claim before send; a lost response does not cause a burst of duplicate alerts.
        self.store.configure({"private_issue_hash": digest, "private_issue_at": now})
        try:
            await api.send(
                owner,
                f"⚠️ PaintWatch: проблем {len(issues)}\n\n"
                + "\n\n".join(issues[:6])
                + "\n\n/errors — подробности. Проверьте приложение и вкладки Chrome",
            )
        except TelegramError as error:
            if not error.uncertain:
                self.store.configure(
                    {
                        "private_issue_hash": "",
                        "private_issue_at": now + max(60, error.delay) - 60,
                    }
                )
            raise

    async def run(self):
        api, fingerprint = None, None
        try:
            while True:
                delay = 2
                try:
                    token = self.vault.read()
                    current = (
                        hashlib.sha256(token.encode()).hexdigest()[:16]
                        if token
                        else None
                    )
                    if current != fingerprint:
                        if api:
                            await api.close()
                        api = Telegram(token) if token else None
                        fingerprint = current
                    if api:
                        await self.poll(api, fingerprint)
                        await self.alert(api)
                        self.store.configure({"telegram_control_error": ""})
                except (TelegramError, ValueError, RuntimeError) as error:
                    self.store.configure({"telegram_control_error": str(error)[:400]})
                    delay = error.delay if isinstance(error, TelegramError) else 30
                except Exception as error:
                    self.store.configure(
                        {
                            "telegram_control_error": f"Сбой управления ЛС ({type(error).__name__})"
                        }
                    )
                    delay = 30
                await asyncio.sleep(delay)
        finally:
            if api:
                await api.close()
