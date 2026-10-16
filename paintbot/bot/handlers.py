import json
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from paintbot.bot.access import Flows
from paintbot.marketplaces.links import MarketError, from_article
from paintbot.services.batch import Batch, collect
from paintbot.services.diagnostics import diagnose, group_access
from paintbot.services.formatting import kopecks, money, stamp

MENU = ["➕ Добавить товар", "📋 Список товаров", "🏷 Теги и пороги", "📈 История цен",
        "🔌 Диагностика", "📨 Проверить отправку в группу", "⚙️ Настройки", "❓ Помощь"]
HELP = ("Управление доступно только владельцу в личном чате. Группа получает лишь уведомления.\n"
        "/start /help /cancel /list /tags /tagitems #тег /status /check /setgroup /settings\n"
        "/history ID — история, /pause ID и /resume ID — мониторинг товара, /queue — доставки.\n"
        "/list market=wb tag=#arton status=error — фильтры (wb/ozon/yandex; active/paused/error).\n"
        "Добавление: выберите способ, отправьте ссылки, нажмите «Готово», задайте название и тег, подтвердите. "
        "Можно отправлять ссылки несколькими сообщениями; максимум 50 ссылок в пакете. "
        "Черновик истекает через 15 минут бездействия (по умолчанию), после перезапуска сбрасывается.\n"
        "Порог строгий: цена, равная порогу, не вызывает уведомление. Изменение порога создаёт новую базовую "
        "линию без рассылки и отменяет ещё не отправленные сообщения этого тега.\n"
        "Цена без подтверждения не вызывает уведомления. Неизвестные характеристики не угадываются. "
        "WB: обычная цена для региона Москва, без скидки кошелька. Сравнивайте с теми же условиями покупки.")


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=callback) for label, callback in row] for row in rows])


def cancel_keys():
    return keyboard([[('Отмена', 'cancel')]])


def card_text(card, index=None):
    attrs = json.dumps(card.attributes, ensure_ascii=False) if card.attributes else "нет подтверждённых данных"
    return (f"{str(index) + '. ' if index else ''}<b>{escape(card.title)}</b>\n"
            f"{escape(card.marketplace)} · {escape(card.article)}\nЦена: {money(card.price)}\n"
            f"Характеристики: {escape(attrs[:1200])}\nЦвет/объём/количество: только если явно указаны выше.\n"
            f"{escape(card.warning or '')}\n{escape(card.url)}")


def make_router(store, adapters, monitor, bot, ttl=900):
    router, flows = Router(), Flows(ttl)
    build_keyboard = globals()["keyboard"]

    def keyboard(rows):
        nonce = flows.current.nonce if flows.current else None
        if nonce:
            rows = [[(label, action if action.startswith(("page:", "filtertag:", "edittag:")) or action == "cancel"
                      else action + "|" + nonce) for label, action in row] for row in rows]
        return build_keyboard(rows)
    reply_menu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=m)] for m in MENU], resize_keyboard=True)

    async def show_tags(message, prefix):
        await message.answer("Выберите тег и порог (строго меньше):", reply_markup=keyboard(
            [[(f"{tag.name}: {money(tag.threshold)}", f"{prefix}:{tag.id}")] for tag in store.tags()] + [[("Отмена", "cancel")]]))

    async def begin_add(message):
        flows.start("method")
        await message.answer("Как добавить товар?", reply_markup=keyboard(
            [[("🔗 Добавить по ссылке", "method:links")], [("🔢 Добавить по артикулу", "method:article")], [("Отмена", "cancel")]]))

    async def show_list(message, page=0, market=None, tag=None, status=None):
        rows, total = store.products(market, tag, status, offset=page * 5)
        lines = [f"Товары: {total}. Страница {page + 1}"]
        for p, t in rows:
            lines.append(f"\nID {p.id} · {escape(p.custom_name[:80])}\n{escape(p.title[:150])}\n{p.marketplace} · {escape(p.article)}"
                         f"\n{money(p.last_price)} · {escape(t.name)} · порог {money(t.threshold)}"
                         f"\nУспех: {stamp(p.last_success)} · {'активен' if p.active else 'пауза'}"
                         f"\n{escape(p.status[:200])}\nСледующая попытка: {stamp(p.next_check)}\n{escape(p.url)}")
        # Carry filters in state-free callbacks. Tag names are short and ASCII.
        suffix = f":{market or '-'}:{tag or '-'}:{status or '-'}"
        buttons = []
        if page:
            buttons.append(("←", f"page:{page-1}" + suffix))
        if (page + 1) * 5 < total:
            buttons.append(("→", f"page:{page+1}" + suffix))
        await message.answer("\n".join(lines), reply_markup=keyboard([buttons]) if buttons else None,
                             disable_web_page_preview=True)

    async def history(message, product_id):
        p = store.product(product_id)
        if not p:
            raise ValueError("Товар не найден")
        changes, errors = store.history(product_id)
        lines = [f"ID {p.id} · {escape(p.custom_name)}", "Изменения цены (последние 20):"]
        lines.extend(f"{stamp(h.checked_at)}: {money(h.price)}" for h in changes)
        lines.append("Ошибки проверок (отдельный журнал, последние 20):")
        lines.extend(f"{stamp(e.checked_at)}: {escape(e.detail[:100])}" for e in errors)
        await message.answer("\n".join(lines))

    async def summary(message, flow):
        batch = flow.data["batch"]
        tag = next(t for t in store.tags() if t.id == flow.data["tag_id"])
        flow.data["threshold"] = tag.threshold
        flow.step = "confirm"
        await message.answer(f"Название пакета: {escape(flow.data['name'])}\nТоваров: {len(batch.cards)}\n"
                             f"Тег: {escape(tag.name)}; порог: {money(tag.threshold)}\n"
                             f"Пропущено дубликатов: {batch.skipped}; ошибок: {len(batch.errors)}\n"
                             "Сохранить каждый товар отдельно?", reply_markup=keyboard(
                                 [[("Подтвердить сохранение", "save")], [("Отмена", "cancel")]]))

    @router.message(F.text)
    async def message_handler(message):
        text = message.text.strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        try:
            if command == "/cancel" or text == "Отмена":
                flows.cancel()
                await message.answer("Сценарий отменён", reply_markup=reply_menu)
                return
            if command in ("/start", "/help") or text == MENU[7]:
                flows.cancel()
                await message.answer(HELP, reply_markup=reply_menu)
            elif text == MENU[0]:
                await begin_add(message)
            elif command == "/list" or text == MENU[1] or command == "/tagitems":
                filters = {}
                if command == "/tagitems":
                    tag = text.split(maxsplit=1)[1] if len(text.split()) > 1 else None
                    if not tag:
                        await show_tags(message, "filtertag")
                        return
                    filters["tag"] = tag
                elif command == "/list":
                    for token in text.split()[1:]:
                        key, sep, value = token.partition("=")
                        if not sep or key not in ("market", "tag", "status"):
                            raise ValueError("Формат: /list market=wb tag=#arton status=error")
                        filters[key] = value
                await show_list(message, **filters)
            elif command == "/tags" or text == MENU[2]:
                await show_tags(message, "edittag")
            elif command == "/history" or text == MENU[3]:
                if len(text.split()) > 1:
                    await history(message, int(text.split()[1]))
                else:
                    flows.start("history")
                    await message.answer("Отправьте ID товара из /list", reply_markup=cancel_keys())
            elif command == "/status" or text == MENU[4] or text == "🔌 Проверить связь и доступ":
                await message.answer(await diagnose(bot, store, monitor))
            elif text == MENU[5]:
                group = store.setting("group_id")
                if not group:
                    await message.answer("Сначала настройте группу: /setgroup")
                else:
                    try:
                        await bot.send_message(int(group), "✅ Тестовая отправка Paint Monitor. Это не уведомление о цене.")
                        await message.answer("Тестовое сообщение отправлено: Telegram подтвердил отправку")
                    except TelegramAPIError as exc:
                        await message.answer(f"Отправка не подтверждена: {type(exc).__name__}. Проверьте ID группы, членство, права и связь. При сетевом сбое проверьте группу перед повтором")
            elif command == "/check":
                await message.answer("Запрошена проверка только товаров, которым уже подошёл срок; ограничения площадок сохраняются")
                count = await monitor.cycle()
                await message.answer(f"Обработано: {count}. Результаты: /list и /status")
            elif command == "/setgroup":
                flows.start("group")
                await message.answer("Пришлите отрицательный ID группы (обычно -100…). Добавьте бота в группу с правом отправки", reply_markup=cancel_keys())
            elif command == "/settings" or text == MENU[6]:
                await message.answer(f"Группа: {escape(store.setting('group_id') or 'не настроена')}\n"
                                     f"Интервал: {monitor.interval} с; тайм-аут сценария: {ttl} с\n"
                                     "/setgroup — группа; /tags — пороги; /pause ID, /resume ID; /queue — доставки")
            elif command in ("/pause", "/resume"):
                store.set_active(int(text.split()[1]), command == "/resume")
                await message.answer("Состояние мониторинга обновлено")
            elif command == "/queue":
                rows = store.queue()
                await message.answer("Очередь (до 30): /retry ID — повтор с подтверждением")
                if not rows:
                    await message.answer("Очередь пуста")
                for start in range(0, len(rows), 10):
                    await message.answer("\n".join(
                        f"{q.id}: {q.state}; попыток {q.attempts}; {escape((q.error or '')[:180])}" for q in rows[start:start+10]))
            elif command == "/retry":
                queue_id = int(text.split()[1])
                flows.start("retry", queue_id=queue_id)
                await message.answer("При неизвестной доставке повтор может создать дубликат в группе. Проверьте группу. Повторить?",
                    reply_markup=keyboard([[("Повторить отправку", "retryconfirm")], [("Отмена", "cancel")]]))
            else:
                flow = flows.get()
                if not flow:
                    await message.answer("Выберите действие в меню. /help", reply_markup=reply_menu)
                elif flow.step == "links":
                    await collect(text, flow.data["batch"], adapters, store)
                    batch = flow.data["batch"]
                    lines = [f"Распознано: {len(batch.cards)}; дубликатов: {batch.skipped}; ошибок: {len(batch.errors)}"]
                    lines.extend(escape(e) for e in batch.errors[-5:])
                    await message.answer("\n".join(lines)[:3900], reply_markup=keyboard(
                        [[("Готово, проверить пакет", "batchdone")], [("Отмена", "cancel")]]))
                elif flow.step == "article":
                    ref = from_article(flow.data["market"], text)
                    card = await adapters[ref.marketplace].fetch(ref)
                    if (card.marketplace, card.card_id) != (ref.marketplace, ref.card_id):
                        raise ValueError("Карточка не соответствует артикулу")
                    flow.data["batch"] = Batch(cards=[card], processed=1)
                    flow.step = "articleconfirm"
                    await message.answer(card_text(card) + "\nЭто правильный товар?", reply_markup=keyboard(
                        [[("Да, продолжить", "articleyes")], [("Повторить артикул", "articleretry")], [("Отмена", "cancel")]]))
                elif flow.step == "name":
                    if not 1 <= len(text) <= 120:
                        raise ValueError("Название: от 1 до 120 символов")
                    flow.data["name"] = text
                    flow.step = "details"
                    await message.answer("Проверьте цвет, объём и количество в карточках выше. Если данных не хватает, "
                        "уточните по номеру товара: «1: 2 баллона, 400 мл, чёрный». По одному товару на сообщение. "
                        "Это будет сохранено как уточнение владельца, без подмены данных площадки. "
                        "Можно продолжить с неизвестными характеристиками.", reply_markup=keyboard(
                            [[("Продолжить к тегам", "detailsdone")], [("Отмена", "cancel")]]))
                elif flow.step == "details":
                    number, sep, note = text.partition(":")
                    if not sep or not number.strip().isdigit() or not 1 <= int(number) <= len(flow.data["batch"].cards) or not 1 <= len(note.strip()) <= 500:
                        raise ValueError("Формат: 1: цвет, объём, количество (до 500 символов)")
                    flow.data["batch"].cards[int(number) - 1].attributes["Уточнение владельца"] = note.strip()
                    await message.answer("Уточнение сохранено в черновике", reply_markup=keyboard(
                        [[("Продолжить к тегам", "detailsdone")], [("Отмена", "cancel")]]))
                elif flow.step == "threshold":
                    store.edit_threshold(flow.data["tag"], kopecks(text))
                    flows.cancel()
                    await message.answer("Порог обновлён. Базовое состояние пересчитано без рассылки, ожидающие сообщения тега отменены")
                elif flow.step == "group":
                    group = int(text)
                    if group >= 0:
                        raise ValueError("ID группы должен быть отрицательным")
                    allowed, status = await group_access(bot, group)
                    if not allowed:
                        raise ValueError(f"Бот не может отправлять сообщения: {status}")
                    store.setting("group_id", group)
                    flows.cancel()
                    await message.answer("Группа проверена и сохранена. Для фактической отправки нажмите «📨 Проверить отправку в группу»")
                elif flow.step == "history":
                    await history(message, int(text))
                    flows.cancel()
                else:
                    await message.answer("Продолжите кнопками текущего сценария или /cancel")
        except (ValueError, IndexError, StopIteration) as exc:
            await message.answer(escape(str(exc))[:1000] or "Проверьте формат команды. /help")
        except TelegramAPIError as exc:
            await message.answer(f"Ошибка Telegram: {type(exc).__name__}. Проверьте сеть, ID группы и права")

    @router.callback_query()
    async def callback_handler(callback):
        await callback.answer()
        message, action = callback.message, callback.data or ""
        try:
            if "|" in action:
                action, nonce = action.rsplit("|", 1)
                current = flows.get()
                if not current or current.nonce != nonce:
                    raise ValueError("Кнопка относится к другому или завершённому сценарию")
            if action == "cancel":
                flows.cancel()
                await message.answer("Сценарий отменён", reply_markup=reply_menu)
                return
            if action.startswith("page:"):
                _, page, market, tag, status = action.split(":")
                await show_list(message, max(0, int(page)), *[None if x == "-" else x for x in (market, tag, status)])
                return
            if action.startswith("filtertag:"):
                tag = next(t for t in store.tags() if t.id == int(action.split(":")[1]))
                await show_list(message, tag=tag.name)
                return
            if action.startswith("edittag:"):
                tag = next(t for t in store.tags() if t.id == int(action.split(":")[1]))
                flows.start("threshold", tag=tag.name)
                await message.answer(f"{escape(tag.name)}: сейчас {money(tag.threshold)}. Введите новый порог в рублях. "
                    "Изменение применяется ко всем товарам этого тега без рассылки; ожидающие сообщения будут отменены", reply_markup=cancel_keys())
                return
            flow = flows.get()
            if not flow:
                raise ValueError("Черновик отсутствует или сброшен после перезапуска. Начните заново")
            if action == "method:links" and flow.step == "method":
                flow.step, flow.data["batch"] = "links", Batch()
                await message.answer("Отправляйте прямые ссылки. Затем нажмите «Готово»", reply_markup=keyboard(
                    [[("Готово, проверить пакет", "batchdone")], [("Отмена", "cancel")]]))
            elif action == "method:article" and flow.step == "method":
                flow.step = "market"
                await message.answer("Выберите площадку", reply_markup=keyboard([
                    [("Wildberries", "market:wb"), ("Ozon", "market:ozon")],
                    [("Яндекс Маркет", "market:yandex")], [("Отмена", "cancel")]]))
            elif action.startswith("market:") and flow.step == "market":
                flow.step, flow.data["market"] = "article", action.split(":")[1]
                await message.answer("Введите ID карточки (WB/Ozon: числовой; Яндекс: product ID либо offer ID). "
                    "Артикул продавца без однозначной карточки не подменяется поиском", reply_markup=cancel_keys())
            elif action == "articleretry" and flow.step == "articleconfirm":
                flow.step = "article"
                await message.answer("Введите артикул заново", reply_markup=cancel_keys())
            elif (action == "batchdone" and flow.step == "links") or (action == "articleyes" and flow.step == "articleconfirm"):
                if not flow.data["batch"].cards:
                    raise ValueError("Нет подтверждённых карточек. Исправьте ссылки или /cancel")
                for index, card in enumerate(flow.data["batch"].cards, 1):
                    await message.answer(card_text(card, index), disable_web_page_preview=True)
                flow.step = "name"
                await message.answer("Введите общее пользовательское название пакета", reply_markup=cancel_keys())
            elif action == "detailsdone" and flow.step == "details":
                flow.step = "tag"
                await show_tags(message, "selecttag")
            elif action.startswith("selecttag:") and flow.step == "tag":
                flow.data["tag_id"] = int(action.split(":")[1])
                await summary(message, flow)
            elif action == "save" and flow.step == "confirm":
                tag = next(t for t in store.tags() if t.id == flow.data["tag_id"])
                if tag.threshold != flow.data["threshold"]:
                    await summary(message, flow)
                    return
                batch = flow.data["batch"]
                ids, skipped = store.add_batch(batch.cards, flow.data["name"], tag.id)
                flows.cancel()
                await message.answer(f"Добавлено: {len(ids)}; пропущено: {batch.skipped + skipped}; ошибок: {len(batch.errors)}. "
                                     "Цены будут проверены заново; /list — результат", reply_markup=reply_menu)
            elif action == "retryconfirm" and flow.step == "retry":
                store.retry_notification(flow.data["queue_id"])
                flows.cancel()
                await message.answer("Уведомление возвращено в очередь")
            else:
                raise ValueError("Эта кнопка устарела. Продолжите текущий сценарий или /cancel")
        except (ValueError, StopIteration, IndexError) as exc:
            await message.answer(escape(str(exc))[:1000] or "Кнопка устарела; начните заново")

    return router
