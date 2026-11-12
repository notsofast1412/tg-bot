"""Private Telegram commands and grouped operational alerts for the configured owner."""

import asyncio
import hashlib
import re
import time

from .add_dialog import AddDialog, CANCEL
from .domain import money, rub
from .mentions import ALLOWED_UPDATES, GROUP_COMMANDS, GroupMentions
from .telegram import Telegram, TelegramError


BUTTONS = {
    "📊 Статус": "/status",
    "🔎 Проверить все": "/scan",
    "▶️ Запустить": "/run",
    "⏸ Остановить": "/stop",
    "📦 Товары": "/products",
    "🏷 Теги": "/tags",
    "➕ Добавить товар": "/add",
    "➕ Добавить тег": "/tag",
    "🗑 Удалить товар": "/delete",
    "❓ Помощь": "/help",
}
KEYBOARD = {
    "keyboard": [
        [{"text": name} for name in list(BUTTONS)[i : i + 2]]
        for i in range(0, len(BUTTONS), 2)
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Выберите действие или введите команду",
}
MENU_VERSION = "0.4.11"
COMMANDS = [
    {"command": command, "description": description}
    for command, description in (
        ("start", "Обновить кнопки управления"),
        ("status", "Состояние мониторинга"),
        ("scan", "Проверить все цены сейчас"),
        ("run", "Включить расписание"),
        ("stop", "Остановить мониторинг и разовый обход"),
        ("products", "Список товаров"),
        ("tags", "Список тегов и порогов"),
        ("add", "Добавить товары: ссылки или артикулы"),
        ("cancel", "Отменить добавление товара"),
        ("tag", "Создать тег с порогом"),
        ("delete", "Удалить товар по ID с подтверждением"),
        ("threshold", "Изменить порог товара"),
        ("pause", "Поставить товар на паузу"),
        ("resume", "Включить товар"),
        ("interval", "Изменить интервал в секундах"),
        ("parallel", "Число параллельных проверок"),
        ("check", "Запросить проверку по интервалу"),
        ("verified", "Повторить после ручной проверки доступа"),
        ("errors", "Текущие проблемы"),
        ("tagall", "Упомянуть участников настроенной группы"),
        ("tagall_status", "Состояние упоминаний в группе"),
        ("tagall_cancel", "Остановить упоминания в группе"),
        ("id", "Мой Telegram ID"),
        ("help", "Команды и правила уведомлений"),
    )
]


HELP = """PaintWatch — управление
/id — ваш Telegram ID
/status — состояние и нагрузка
/run /stop — включить / остановить мониторинг
/products [страница] — список товаров (ID из первого столбца)
/add #тег ссылка [ссылка…] — добавить товары
/add #тег артикул — бот спросит площадку
Можно прислать только артикул: бот спросит площадку и тег.
/cancel — отменить добавление
/delete ID confirm — удалить товар и его историю
/pause ID /resume ID — выключить / включить товар
/check [ID|all] — запросить проверки по текущему интервалу
/scan — разово проверить все включённые товары сейчас
/tag #название РУБЛИ — создать тег с порогом
/tags — список тегов и порогов
/verified ID — повторить после ручной проверки доступа
/interval СЕКУНДЫ — интервал, 0 = непрерывный обход
/parallel ЧИСЛО — 1–32 одновременные проверки
/threshold ID РУБЛИ — индивидуальный порог
/errors — последние проблемы
/tagall — упомянуть известных участников по username или ID
/tagall_status /tagall_cancel — состояние / остановка упоминаний
В группе /tagall доступна админам; /here добавляет участника в список.
/help — эта справка

Повторное ценовое уведомление — при снижении ещё минимум на 15 ₽ от последнего отправленного.

Команды работают, пока PaintWatch запущен. Chrome с расширением нужен для сбора цен."""


class TelegramControl:
    def __init__(self, store, vault, bridge=None):
        self.store, self.vault, self.bridge = store, vault, bridge
        self.started = time.time()
        self.menu_retry_at = 0
        self.group = None

    def group_mentions(self, fingerprint):
        chat = GroupMentions.parse_chat(self.store.setting("chat_id"))
        if (
            self.group is None
            or self.group.bot != fingerprint
            or self.group.chat_id() != chat
        ):
            self.group = GroupMentions(self.store, fingerprint)
        return self.group

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
        for event in reversed(
            self.store.queue(states=("pending", "blocked", "uncertain"))
        ):
            if event["state"] in ("blocked", "uncertain") or (
                event["state"] == "pending" and event["error"]
            ):
                issues.append(f"Уведомление №{event['id']}: {event['error'][:200]}")
        return issues

    def add_dialog(self, fingerprint=""):
        return AddDialog(self.store, self.owner(), fingerprint)

    def execute(self, text, fingerprint=""):
        text = BUTTONS.get(text.strip(), text)
        text = text.strip()
        dialog = self.add_dialog(fingerprint)
        if text.casefold() in ("/cancel", CANCEL.casefold()):
            dialog.cancel()
            return "Добавление отменено"
        if re.fullmatch(r"[0-9]+(?:\s+[0-9]+)*", text):
            return dialog.begin(text)
        parts = text.split()
        if not parts:
            return HELP
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if command == "/cancel":
            dialog.cancel()
            return "Добавление отменено"
        if not command.startswith("/"):
            answer = dialog.answer(text)
            if answer is not None:
                return answer
        if command in ("/start", "/help"):
            if command == "/start":
                dialog.cancel()
            return HELP
        if command == "/status":
            products = self.store.products()
            status = self.bridge.status() if self.bridge else {"active": 0, "due": 0}
            performance = status.get("performance", {})
            average = performance.get("average_seconds")
            cycle = performance.get("estimated_cycle_seconds")
            speed = (
                f"\nЗа {performance['window_seconds']} с: успешно {performance['successful']} из {performance['completed']} проверок"
                f"\nСредняя проверка: {average if average is not None else '—'} с · темп: {performance['per_minute']} товаров/мин"
                f"\nРасчёт обхода без пауз: {cycle if cycle is not None else '—'} с"
                f"\nЕщё не проверено: {performance['unchecked']} · возраст старейшего результата: {performance['oldest_success_age'] if performance['oldest_success_age'] is not None else '—'} с"
                if performance
                else ""
            )
            return (
                f"Мониторинг: {'включён' if self.store.setting('monitoring', '0') == '1' else 'остановлен'}\n"
                f"Товаров: {len(products)}, включено: {sum(p['enabled'] for p in products)}\n"
                f"Проверяется: {status['active']}, ожидают: {status['due']}\n"
                f"Разовый обход: осталось {status.get('manual', 0)}\n"
                f"Интервал: {self.store.interval()} с (0 = непрерывно)\n"
                f"Параллельно: {self.store.parallelism()}, лимит: {self.store.check_timeout()} с\n"
                f"Проблем: {len(self.problems())}{speed}"
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
        if command == "/tags":
            return (
                "Теги и пороги:\n"
                + "\n".join(
                    f"{tag['name']}: {rub(tag['threshold'])}"
                    for tag in self.store.tags()
                )
                + "\n\n/tag #название 400 — создать тег"
            )
        if command == "/add" and not args:
            dialog.cancel()
            return "Пришлите артикул — я спрошу площадку и тег.\n\nИли укажите тег заранее:\n/add #belazo 1797187018\n\nС полной ссылкой площадка определяется автоматически:\n/add #arton https://www.wildberries.ru/catalog/708052242/detail.aspx\n\n/tags — доступные теги\n/cancel — отменить добавление"
        if command == "/tag" and not args:
            return "Создание тега:\n/tag #название порог_в_рублях\n\nПример: /tag #montana 400"
        if command == "/delete" and not args:
            return "Сначала найдите ID товара: /products.\n/delete 12 — показать подтверждение удаления товара №12"
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
        if command == "/add" and args:
            tag = args[0].casefold() if args[0].startswith("#") else None
            items = args[1:] if tag else args
            if not items:
                raise ValueError("После тега укажите артикул или полную ссылку")
            return dialog.begin("\n".join(items), tag)
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
                    "allowed_updates": ALLOWED_UPDATES,
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
                "allowed_updates": ALLOWED_UPDATES,
            },
        )
        for update in updates:
            if update["update_id"] < int(self.store.setting(key, "0")):
                continue
            # Persist before applying commands: a restart never repeats a deletion/addition.
            self.store.configure({key: update["update_id"] + 1})
            group = self.group_mentions(fingerprint)
            group.observe(update)
            message = update.get("message", {})
            if await group.handle(api, message):
                continue
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
                answer = self.execute(text, fingerprint)
            except (ValueError, OverflowError) as error:
                answer = "Ошибка: " + str(error)[:500]
            await api.send(
                uid,
                answer,
                reply_markup=self.add_dialog(fingerprint).markup() or KEYBOARD,
            )

    async def refresh_menu(self, api, fingerprint):
        owner = self.owner()
        if not owner:
            return
        key = "telegram_menu_" + fingerprint
        signature = f"{MENU_VERSION}:{owner}"
        if self.store.setting(key) == signature:
            return
        scope = {"type": "chat", "chat_id": owner}
        # Override both Telegram's fallback and Russian-localized command lists.
        for language in ("", "ru"):
            await api.call(
                "setMyCommands",
                {"commands": COMMANDS, "scope": scope, "language_code": language},
            )
        await api.call(
            "setChatMenuButton", {"chat_id": owner, "menu_button": {"type": "commands"}}
        )
        # Claim before sending: a lost reply must not duplicate the panel on each restart.
        self.store.configure({key: signature})
        dialog = self.add_dialog(fingerprint)
        await api.send(
            owner,
            dialog.prompt()
            or "Панель PaintWatch обновлена. Пришлите артикул — я спрошу площадку и тег. «Проверить все» запускает разовый обход. /help — все команды",
            reply_markup=dialog.markup() or KEYBOARD,
        )

    async def refresh_group_menu(self, api, fingerprint):
        chat = self.group_mentions(fingerprint).chat_id()
        if chat is None:
            return
        key = "telegram_group_menu_" + fingerprint
        signature = f"{MENU_VERSION}:{chat}"
        if self.store.setting(key) == signature:
            return
        for language in ("", "ru"):
            await api.call(
                "setMyCommands",
                {
                    "commands": GROUP_COMMANDS,
                    "scope": {"type": "chat", "chat_id": chat},
                    "language_code": language,
                },
            )
        self.store.configure({key: signature})

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
                        api = Telegram(token, store=self.store) if token else None
                        fingerprint = current
                    if api:
                        await self.poll(api, fingerprint)
                        await self.alert(api)
                        await self.group_mentions(fingerprint).deliver(api)
                        if time.time() >= self.menu_retry_at:
                            try:
                                await self.refresh_menu(api, fingerprint)
                                await self.refresh_group_menu(api, fingerprint)
                            except TelegramError as error:
                                self.menu_retry_at = time.time() + error.delay
                                self.store.configure(
                                    {
                                        "telegram_menu_error": f"Не удалось обновить меню: {error}"
                                    }
                                )
                            else:
                                self.store.configure({"telegram_menu_error": ""})
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
