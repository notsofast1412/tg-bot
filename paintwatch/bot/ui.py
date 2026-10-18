import asyncio
import json
import logging
import secrets
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)

from paintwatch.diagnostics import group_access, test_group
from paintwatch.domain import MARKET_NAMES, FetchError, Snapshot, kopecks, rub
from paintwatch.links import article_ref
from paintwatch.services import BatchService

logger = logging.getLogger(__name__)

MENU = [
    ["➕ Добавить товар", "📋 Список товаров"],
    ["🏷 Теги и пороги", "📈 История цен"],
    ["🔌 Диагностика", "📨 Проверить отправку в группу"],
    ["⚙️ Настройки", "❓ Помощь"],
]
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=t) for t in row] for row in MENU],
    resize_keyboard=True,
)


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=value)
                for label, value in row
            ]
            for row in rows
        ]
    )


CANCEL = buttons([[("Отмена", "cancel")]])
HELP = (
    "<b>PaintWatch — WB и Ozon</b>\nДобавляйте только нужные карточки по прямым ссылкам или числовым ID. "
    "Поиска аналогов нет. WB и Ozon с одинаковыми ID — разные товары.\n\n"
    "/start /help /cancel\n/list — товары и фильтры\n/tags — пороги тегов\n/tagitems — фильтр по тегу\n"
    "/status — диагностика\n/check — проверить товары, для которых наступил срок\n"
    "/setgroup -100… — группа уведомлений\n/settings — настройки\n"
    "/history ID — последние изменения и ошибки\n/queue — спорные отправки\n"
    "/retry ID — сначала подтверждение повторной доставки\n\n"
    "Цена сравнивается строго: 400 ₽ при пороге 400 ₽ не вызывает уведомление. "
    "WB: обычная цена без WB Кошелька. Ozon: цена по карте; обычная — только справочно. "
    "Неизвестная цена не заменяется нулём. Изменение порога пересчитывает состояние без рассылки.\n"
    "Управление — только владельцу в личном чате. Группа получает только уведомления."
)


class OwnerGate(BaseMiddleware):
    def __init__(self, owner_id, store):
        self.owner_id, self.store = owner_id, store
        self.lock = asyncio.Lock()

    async def __call__(self, handler, event: Update, data):
        obj = event.message or event.callback_query
        message = obj.message if isinstance(obj, CallbackQuery) else obj
        user = obj.from_user if obj else None
        if (
            not message
            or str(message.chat.type) != "private"
            or not user
            or user.id != self.owner_id
        ):
            return
        async with self.lock:
            if self.store.processed(event.update_id):
                return
            try:
                result = await handler(event, data)
            except Exception:
                logger.exception("Ошибка обработчика Telegram")
                await data["bot"].send_message(
                    self.owner_id,
                    "Не удалось выполнить операцию. Состояние сохранено; попробуйте /cancel или повторите. Подробности в журнале.",
                )
                return
            self.store.mark_processed(event.update_id)
            return result


class UI:
    def __init__(self, store, adapters, config, monitor, bot):
        self.store, self.adapters, self.config, self.monitor, self.bot = (
            store,
            adapters,
            config,
            monitor,
            bot,
        )
        self.batch = BatchService(store, adapters, config)
        self.router = Router()
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)

    def attach(self, dispatcher: Dispatcher):
        dispatcher.update.outer_middleware(OwnerGate(self.config.owner_id, self.store))
        dispatcher.include_router(self.router)

    def dt(self, at):
        return (
            datetime.fromtimestamp(at, ZoneInfo(self.config.display_timezone)).strftime(
                "%d.%m.%Y %H:%M:%S"
            )
            if at
            else "ещё не было"
        )

    def save(self, data):
        self.store.save_draft(self.config.owner_id, data, self.config.scenario_timeout)

    async def say(self, text, keyboard=None):
        # Сохраняем HTML целым: выводим ограниченные по длине блоки отдельно.
        await self.bot.send_message(
            self.config.owner_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    async def cancel(self):
        self.store.clear_draft(self.config.owner_id)
        await self.say("Сценарий отменён. Несохранённые товары не добавлены.", KEYBOARD)

    async def message(self, message: Message):
        text = (message.text or "").strip()
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lower()
        aliases = {
            "➕ Добавить товар": "/add",
            "📋 Список товаров": "/list",
            "🏷 Теги и пороги": "/tags",
            "📈 История цен": "/history",
            "🔌 Диагностика": "/status",
            "🔌 Проверить связь и доступ": "/status",
            "📨 Проверить отправку в группу": "/testgroup",
            "⚙️ Настройки": "/settings",
            "❓ Помощь": "/help",
        }
        command = aliases.get(text, command)
        if command == "/cancel" or text == "Отмена":
            return await self.cancel()
        if command in ("/start", "/help"):
            if command == "/start":
                self.store.clear_draft(self.config.owner_id)
            return await self.say(HELP, KEYBOARD)
        if command == "/add":
            self.save({"step": "method"})
            return await self.say(
                "Как добавить товары?",
                buttons(
                    [
                        [("🔗 Добавить по ссылке", "add:link")],
                        [("🔢 Добавить по артикулу", "add:article")],
                        [("Отмена", "cancel")],
                    ]
                ),
            )
        if command == "/list":
            self.store.set_setting("list_filter", "{}")
            return await self.show_list()
        if command in ("/tags", "/tagitems"):
            return await self.show_tags(filtering=command == "/tagitems")
        if command == "/status":
            return await self.status()
        if command == "/testgroup":
            return await self.say(
                await test_group(self.bot, self.store.setting("group_id"))
            )
        if command == "/settings":
            return await self.settings()
        if command == "/setgroup":
            if argument:
                return await self.set_group(argument)
            self.save({"step": "group"})
            return await self.say(
                "Добавьте бота в группу и разрешите отправку. Введите отрицательный ID группы (обычно -100…).",
                CANCEL,
            )
        if command == "/check":
            await self.say("Запрошена проверка с учётом интервалов и пауз площадок.")
            count = await self.monitor.check_due()
            return await self.say(
                "Проверка уже идёт."
                if count is None
                else f"Обработано товаров: {count}. Для остальных срок ещё не наступил. Результаты: /status"
            )
        if command == "/history":
            if argument.isdigit():
                return await self.history(int(argument))
            return await self.show_list(history=True)
        if command == "/queue":
            items = self.store.uncertain_notifications()
            text = "Отправки с неизвестным результатом:\n" + "\n".join(
                f"#{n['id']} — товар #{n['product_id']}, {self.dt(n['created_at'])}"
                for n in items
            )
            return await self.say(
                text
                + "\nПроверьте группу по номеру события. Для повтора: /retry ID (может дублировать уже доставленное сообщение)."
            )
        if command == "/retry":
            if not argument.isdigit():
                return await self.say("Используйте /retry ID из /queue")
            self.save(
                {"step": "retry", "id": int(argument), "nonce": secrets.token_hex(4)}
            )
            return await self.say(
                "Ответ Telegram был потерян, сообщение могло дойти. Проверьте группу. Повтор может создать дубликат.",
                buttons(
                    [
                        [
                            (
                                "Повторить отправку",
                                f"retry:{self.store.draft(self.config.owner_id)['nonce']}",
                            )
                        ],
                        [("Отмена", "cancel")],
                    ]
                ),
            )
        draft = self.store.draft(self.config.owner_id)
        if draft and draft.get("expired"):
            return await self.say(
                "Время сценария истекло. Несохранённые данные сброшены; начните заново.",
                KEYBOARD,
            )
        if not draft:
            return await self.say("Выберите действие в меню или /help", KEYBOARD)
        await self.handle_step(text, draft)

    async def handle_step(self, text, d):
        step = d["step"]
        if step == "links":
            await self.say(
                "Проверяю ссылки. Остальные сообщения можно отправить следом."
            )
            snapshots, errors, skipped = await self.batch.collect(
                text, d.get("snapshots", [])
            )
            d["snapshots"] = snapshots
            d["errors"] = d.get("errors", []) + errors
            d["skipped"] = d.get("skipped", 0) + skipped
            self.save(d)
            for error in errors[: self.config.max_batch]:
                await self.say("⚠️ " + escape(error[:600]))
            await self.say(
                f"В пакете: {len(snapshots)}. Дубликатов пропущено: {d['skipped']}. Ошибок: {len(d['errors'])}."
            )
            return await self.preview_batch(d)
        if step == "article":
            try:
                ref = article_ref(d["market"], text)
                if self.store.exists(ref):
                    return await self.say(
                        "Эта карточка уже отслеживается. Введите другой ID или /cancel"
                    )
                snapshot = await self.adapters[d["market"]].fetch(ref)
                if snapshot.ref != ref:
                    raise FetchError("identity_mismatch", "Получена другая карточка")
            except FetchError as e:
                return await self.say(
                    escape(str(e)) + "\nПовторите ID или /cancel и добавьте по ссылке."
                )
            d.update(
                step="article_confirm",
                snapshots=[snapshot.to_dict()],
                errors=[],
                skipped=0,
            )
            self.save(d)
            return await self.say(
                self.snapshot_text(snapshot) + "\nЭто нужная карточка?",
                buttons(
                    [
                        [("Да, продолжить", "article:yes")],
                        [("Ввести другой артикул", "article:again")],
                        [("Отмена", "cancel")],
                    ]
                ),
            )
        if step == "name":
            if not 1 <= len(text) <= 200 or text.startswith("/"):
                return await self.say("Введите название пакета от 1 до 200 символов.")
            d.update(name=text, step="tag")
            self.save(d)
            return await self.show_tags(adding=True)
        if step in ("threshold", "edit_tag", "product_threshold"):
            try:
                value = (
                    None
                    if step == "threshold" and text == "/default"
                    else kopecks(text)
                )
            except FetchError:
                return await self.say(
                    "Введите положительную сумму в рублях, например 399,50. Для добавления /default — порог тега."
                )
            if step == "edit_tag":
                self.store.update_tag(d["tag_id"], value)
                self.store.clear_draft(self.config.owner_id)
                return await self.say(
                    "Порог тега сохранён. Состояние пересчитано без уведомлений; индивидуальные пороги товаров не изменены."
                )
            if step == "product_threshold":
                self.store.update_product_threshold(d["product_id"], value)
                self.store.clear_draft(self.config.owner_id)
                return await self.say(
                    "Индивидуальный порог сохранён, состояние пересчитано без уведомлений."
                )
            d.update(threshold=value, step="metadata")
            self.save(d)
            return await self.metadata_prompt(d)
        if step == "metadata_edit":
            parts = [p.strip() for p in text.split("|")]
            if (
                len(parts) != 3
                or (
                    parts[2] != "?"
                    and (not parts[2].isdigit() or not 0 < int(parts[2]) < 10000)
                )
                or any(len(p) > 100 for p in parts)
            ):
                return await self.say(
                    "Формат: цвет | объём | количество. Например: чёрный | 400 мл | 6. Неизвестное поле: ?"
                )
            s = d["snapshots"][d["metadata_index"]]
            for key, val in zip(("Цвет", "Объём", "Количество в упаковке"), parts):
                if val != "?":
                    s["characteristics"][f"Владелец: {key}"] = val
            d["metadata_index"] += 1
            self.save(d)
            if d["metadata_index"] < len(d["snapshots"]):
                return await self.say(
                    f"Товар {d['metadata_index'] + 1}: {escape(d['snapshots'][d['metadata_index']]['name'][:200])}\nЦвет | объём | количество (или ? | ? | ?)",
                    CANCEL,
                )
            return await self.confirm(d)
        if step == "group":
            return await self.set_group(text)
        await self.say("Продолжите кнопками под последним сообщением или /cancel.")

    def snapshot_text(self, s):
        props = (
            "; ".join(f"{k}: {v}" for k, v in list(s.characteristics.items())[:8])
            or "нет подтверждённых характеристик"
        )
        alternate = "\n".join(
            f"{escape(k)} (справочно): {rub(v)}" for k, v in s.alternate_prices.items()
        )
        return (
            f"<b>{escape(s.name[:250])}</b>\n{MARKET_NAMES[s.ref.market]}, ID {escape(s.ref.article)}\n"
            f"Цена: {rub(s.price)}\nЦвет: {escape(s.color or 'неизвестен')}; объём: {escape(s.volume or 'неизвестен')}; "
            f"упаковка: {s.pack_count if s.pack_count is not None else 'неизвестна'}\n"
            f"{escape(props[:800])}\n{escape(s.issue or s.price_kind)}\n"
            f"{alternate}\n"
            f'<a href="{escape(s.ref.url, quote=True)}">Карточка</a>'
        )

    async def preview_batch(self, d):
        for item in d["snapshots"][-self.config.max_batch :]:
            await self.say(self.snapshot_text(Snapshot.from_dict(item)))
        await self.say(
            "Можно прислать ещё ссылки. Когда пакет собран, нажмите «Далее».",
            buttons(
                [[("Далее — название пакета", "batch:next")], [("Отмена", "cancel")]]
            ),
        )

    async def show_tags(self, adding=False, filtering=False):
        prefix = "choose" if adding else "filtertag" if filtering else "edittag"
        rows = [
            [(f"{t['name']} — < {rub(t['threshold'])}", f"{prefix}:{t['id']}")]
            for t in self.store.tags()
        ]
        if adding:
            rows.append([("Отмена", "cancel")])
        await self.say(
            "Выберите тег для всего пакета:"
            if adding
            else "Теги: строгие пороги в RUB. Нажмите для выбора фильтра."
            if filtering
            else "Нажмите тег для изменения порога (без немедленной рассылки).",
            buttons(rows),
        )

    async def metadata_prompt(self, d):
        await self.say(
            "Цвет, объём и количество берутся только из карточки. Если данные отсутствуют или неоднозначны, уточните их вручную либо явно оставьте неизвестными. Количество из названия тега не вычисляется.",
            buttons(
                [
                    [("Уточнить комплектацию по каждому товару", "meta:edit")],
                    [("Оставить доступные данные, остальное неизвестно", "meta:skip")],
                    [("Отмена", "cancel")],
                ]
            ),
        )

    async def confirm(self, d):
        d.update(step="confirm", nonce=secrets.token_hex(4))
        self.save(d)
        tag = next(t for t in self.store.tags() if t["id"] == d["tag_id"])
        threshold = (
            d.get("threshold") if d.get("threshold") is not None else tag["threshold"]
        )
        await self.say(
            f"<b>Подтверждение пакета</b>\nНазвание: {escape(d['name'])}\nТоваров: {len(d['snapshots'])}\n"
            f"Тег: {escape(tag['name'])}; порог: строго меньше {rub(threshold)}\n"
            f"Дубликатов: {d.get('skipped', 0)}; ошибок: {len(d.get('errors', []))}\n"
            "Карточки без цены сохранятся в ожидании. Если подтверждённая цена уже ниже порога, будет создано уведомление.",
            buttons([[("Сохранить", f"save:{d['nonce']}")], [("Отмена", "cancel")]]),
        )

    async def callback(self, callback: CallbackQuery):
        await callback.answer()
        data = callback.data or ""
        if data == "cancel":
            return await self.cancel()
        parts = data.split(":")
        action = parts[0]
        d = self.store.draft(self.config.owner_id)
        if action in (
            "add",
            "market",
            "batch",
            "article",
            "choose",
            "meta",
            "save",
            "retry",
        ):
            if not d or d.get("expired"):
                return await self.say(
                    "Сценарий истёк или уже завершён. Начните заново."
                )
        if action == "add" and d["step"] == "method":
            if parts[1] == "link":
                self.save(
                    {"step": "links", "snapshots": [], "errors": [], "skipped": 0}
                )
                return await self.say(
                    "Пришлите одну или несколько прямых ссылок WB/Ozon. Можно несколькими сообщениями и без пробелов между URL.",
                    CANCEL,
                )
            self.save({"step": "market"})
            return await self.say(
                "Выберите площадку:",
                buttons(
                    [
                        [("Wildberries", "market:wb"), ("Ozon", "market:ozon")],
                        [("Отмена", "cancel")],
                    ]
                ),
            )
        if action == "market" and d["step"] == "market" and parts[1] in MARKET_NAMES:
            self.save({"step": "article", "market": parts[1]})
            return await self.say(
                "Введите числовой ID карточки (WB nmID или Ozon SKU). Артикул продавца может быть неоднозначным — тогда используйте ссылку.",
                CANCEL,
            )
        if action == "article" and d["step"] == "article_confirm":
            if parts[1] == "again":
                d["step"] = "article"
                self.save(d)
                return await self.say("Введите другой ID", CANCEL)
            d["step"] = "name"
            self.save(d)
            return await self.say(
                "Введите пользовательское название для товара.", CANCEL
            )
        if action == "batch" and d["step"] == "links":
            if not d.get("snapshots"):
                return await self.say(
                    "В пакете нет подтверждённых карточек. Пришлите корректные ссылки или /cancel."
                )
            d["step"] = "name"
            self.save(d)
            return await self.say(
                "Введите общее пользовательское название для пакета.", CANCEL
            )
        if action == "choose" and d["step"] == "tag":
            tag = next((t for t in self.store.tags() if str(t["id"]) == parts[1]), None)
            if not tag:
                return
            d.update(step="threshold", tag_id=tag["id"])
            self.save(d)
            return await self.say(
                f"{escape(tag['name'])}: строго меньше {rub(tag['threshold'])}.\nВведите /default для порога тега или свою сумму в RUB для всех товаров пакета.",
                CANCEL,
            )
        if action == "meta" and d["step"] == "metadata":
            if parts[1] == "edit":
                d.update(step="metadata_edit", metadata_index=0)
                self.save(d)
                return await self.say(
                    f"Товар 1: {escape(d['snapshots'][0]['name'][:200])}\nЦвет | объём | количество (или ? | ? | ?)",
                    CANCEL,
                )
            return await self.confirm(d)
        if action == "save" and d["step"] == "confirm" and parts[1] == d["nonce"]:
            await self.say(
                "Сохраняю пакет; при необходимости обновляю устаревшие цены."
            )
            snapshots, errors = await self.batch.refresh(d["snapshots"])
            added, skipped = self.store.add_batch(
                snapshots,
                d["name"],
                d["tag_id"],
                d.get("threshold"),
                {m: self.config.interval(m) for m in MARKET_NAMES},
            )
            self.store.clear_draft(self.config.owner_id)
            return await self.say(
                f"Добавлено: {len(added)}. Пропущено дубликатов: {skipped + d.get('skipped', 0)}. Ошибок ссылок: {len(d.get('errors', []))}. Ошибок обновления цены: {len(errors)}.\nБез достоверной цены товар остаётся в ожидании; причина видна в карточке.",
                KEYBOARD,
            )
        if action == "retry" and d["step"] == "retry" and parts[1] == d["nonce"]:
            ok = self.store.retry_uncertain(d["id"])
            self.store.clear_draft(self.config.owner_id)
            return await self.say(
                "Событие возвращено в очередь."
                if ok
                else "Событие не найдено или уже обработано."
            )
        if (
            action in ("edittag", "filtertag")
            and len(parts) == 2
            and parts[1].isdigit()
        ):
            tag_id = int(parts[1])
            if action == "filtertag":
                filters = json.loads(self.store.setting("list_filter", "{}"))
                filters["tag_id"] = tag_id
                self.store.set_setting("list_filter", json.dumps(filters))
                return await self.show_list()
            self.save({"step": "edit_tag", "tag_id": tag_id})
            return await self.say(
                "Введите новый порог тега в RUB. У товаров с порогом тега состояние будет пересчитано без рассылки; индивидуальные пороги сохранятся.",
                CANCEL,
            )
        if action == "filters":
            return await self.say(
                "Фильтр списка:",
                buttons(
                    [
                        [("WB", "filter:market:wb"), ("Ozon", "filter:market:ozon")],
                        [
                            ("Активные", "filter:status:active"),
                            ("С ошибками", "filter:status:error"),
                            ("Пауза", "filter:status:paused"),
                        ],
                        [
                            ("По тегу", "filter:tags:all"),
                            ("Сбросить", "filter:reset:all"),
                        ],
                    ]
                ),
            )
        if action == "filter" and len(parts) == 3:
            if parts[1] == "tags":
                return await self.show_tags(filtering=True)
            filters = json.loads(self.store.setting("list_filter", "{}"))
            if parts[1] == "reset":
                filters = {}
            elif parts[1] == "market" and parts[2] in MARKET_NAMES:
                filters["market"] = parts[2]
            elif parts[1] == "status" and parts[2] in ("active", "error", "paused"):
                filters["status"] = parts[2]
            self.store.set_setting("list_filter", json.dumps(filters))
            return await self.show_list()
        if action == "page" and len(parts) == 2 and parts[1].isdigit():
            return await self.show_list(int(parts[1]))
        if (
            action in ("product", "history", "toggle", "productthreshold")
            and len(parts) == 2
            and parts[1].isdigit()
        ):
            product_id = int(parts[1])
            if not self.store.get_product(product_id):
                return await self.say("Товар не найден")
            if action == "history":
                return await self.history(product_id)
            if action == "toggle":
                self.store.toggle(product_id)
            if action == "productthreshold":
                self.save({"step": "product_threshold", "product_id": product_id})
                return await self.say(
                    "Введите индивидуальный порог товара в RUB.", CANCEL
                )
            return await self.show_product(product_id)
        await self.say(
            "Кнопка устарела или сценарий изменился. Используйте актуальное меню."
        )

    async def show_list(self, page=0, history=False):
        filters = json.loads(self.store.setting("list_filter", "{}"))
        items, count = self.store.products(page=page, **filters)
        await self.say(
            f"Товаров по фильтру: {count}. Страница {page + 1}. Фильтр: {escape(str(filters))}",
            buttons([[("Фильтры", "filters")]]),
        )
        for p in items:
            await self.say(
                self.product_text(p),
                buttons(
                    [
                        [
                            (
                                "История" if history else "Открыть управление",
                                f"{'history' if history else 'product'}:{p['id']}",
                            )
                        ]
                    ]
                ),
            )
        rows = []
        if page:
            rows.append(("← Назад", f"page:{page - 1}"))
        if (page + 1) * 5 < count:
            rows.append(("Далее →", f"page:{page + 1}"))
        if rows:
            await self.say("Навигация:", buttons([rows]))

    def product_text(self, p):
        active = "активен" if p["active"] else "пауза"
        price_kind = escape(p["details"].get("price_kind", "Тип цены не подтверждён"))
        alternate = "; ".join(
            f"{k} (справочно): {rub(v)}"
            for k, v in p["details"].get("alternate_prices", {}).items()
        )
        return (
            f"<b>#{p['id']} {escape(p['custom_name'][:200])}</b>\n{escape(p['name'][:300])}\n"
            f"{MARKET_NAMES[p['market']]} · ID {escape(p['article'])} · вариант {escape(p['variant'] or 'ID карточки')}\n"
            f"Последняя достоверная цена: {rub(p['last_price'])}\n{price_kind}\n{escape(alternate)}\n{escape(p['tag'])} · порог &lt; {rub(p['threshold'])}"
            f"{' (индивидуальный)' if p['custom_threshold'] else ''}\nУспешная проверка: {self.dt(p['last_success'])}\n"
            f"Попытка: {self.dt(p['last_attempt'])}; следующая: {self.dt(p['next_check'])}\n"
            f"Статус: {active}, {escape(p['status'])}\n{escape((p['last_error'] or '')[:500])}\n"
            f'<a href="{escape(p["url"], quote=True)}">Открыть карточку</a>'
        )

    async def show_product(self, product_id):
        p = self.store.get_product(product_id)
        props = "; ".join(
            f"{k}: {v}" for k, v in p["details"].get("characteristics", {}).items()
        )
        await self.say(
            self.product_text(p) + "\n" + escape(props[:1200]),
            buttons(
                [
                    [
                        (
                            "Возобновить" if not p["active"] else "Приостановить",
                            f"toggle:{product_id}",
                        )
                    ],
                    [
                        ("История", f"history:{product_id}"),
                        ("Изменить порог товара", f"productthreshold:{product_id}"),
                    ],
                ]
            ),
        )

    async def history(self, product_id):
        p = self.store.get_product(product_id)
        if not p:
            return await self.say("Товар не найден")
        prices, errors = self.store.history(product_id)
        lines = [
            f"<b>История #{product_id}: {escape(p['custom_name'][:200])}</b>",
            "Изменения достоверной цены (20 последних):",
        ]
        lines += [f"{self.dt(v['at'])} — {rub(v['price'])}" for v in prices] or [
            "Изменений пока нет"
        ]
        lines += ["\nОшибки проверок отдельно (10 последних):"]
        lines += [
            f"{self.dt(v['at'])} — {escape(v['status'])}: {escape((v['error'] or '')[:100])}"
            for v in errors
        ] or ["Ошибок нет"]
        await self.say("\n".join(lines))

    async def set_group(self, raw):
        try:
            group_id = int(raw)
            if group_id >= 0:
                raise ValueError
        except ValueError:
            return await self.say("Нужен отрицательный числовой ID группы.")
        try:
            chat = await self.bot.get_chat(group_id)
            if str(chat.type) not in ("group", "supergroup"):
                return await self.say("Этот ID не является группой.")
        except TelegramAPIError as e:
            return await self.say(
                f"Не удалось подтвердить группу: {type(e).__name__}. Добавьте бота, проверьте ID и сеть."
            )
        self.store.set_group(group_id)
        self.store.clear_draft(self.config.owner_id)
        await self.say(
            f"Группа {group_id} сохранена. Проверьте права через диагностику и выполните тестовую отправку."
        )

    async def settings(self):
        intervals = "; ".join(
            f"{MARKET_NAMES[m]}: ≥{self.config.interval(m) // 60} мин"
            for m in MARKET_NAMES
        )
        await self.say(
            f"Группа: {self.store.setting('group_id', 'не настроена')}\n{intervals}\nПауза между запросами площадки: {self.config.request_gap} с. После ошибок — увеличение интервала до суток.\nЧасовой пояс: {self.config.display_timezone}\nСценарий истекает через {self.config.scenario_timeout // 60} мин.\nРегион WB dest={escape(self.config.wb_dest)}; для Ozon регион из неперсонализированной сессии.\nИнтервалы/регион/браузер задаются в .env, затем restart службы. /setgroup — настроить группу; /tags — пороги; /queue — спорные отправки."
        )

    async def status(self):
        lines = await group_access(self.bot, self.store.setting("group_id"))
        stats = self.store.statistics()
        lines += [
            f"Мониторинг: {'работает' if self.monitor.running else 'не запущен'}; heartbeat {self.dt(self.monitor.heartbeat)}",
            f"Последняя успешная проверка: {self.dt(stats['last_success'])}",
            f"Последняя попытка: {self.dt(stats['last_attempt'])}",
            f"Активных: {stats['active']}; с ошибками/ожидают: {stats['errors']}",
            f"Очередь: {stats['queue']}; доставка неизвестна: {stats['uncertain']}",
        ]
        for market, states in stats["markets"].items():
            lines.append(
                f"{MARKET_NAMES[market]}: {states or 'проверок активных товаров ещё нет'}; интервал ≥{self.config.interval(market) // 60} мин"
            )
        lines.append(
            "Статус площадок основан на реальных попытках товаров, отдельный запрос к площадкам здесь не выполняется."
        )
        await self.say(escape("\n".join(lines)))
