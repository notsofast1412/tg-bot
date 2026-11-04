"""Owner-scoped Telegram flow for adding product references without a marketplace."""

import json
import re
import time

from .domain import parse_batch


CANCEL = "Отмена"
NEXT = "Далее ▶️"
PREVIOUS = "◀️ Назад"
PAGE_SIZE = 12
MARKETS = {"ozon": "ozon", "озон": "ozon", "wildberries": "wb", "wb": "wb", "вб": "wb"}


class AddDialog:
    def __init__(self, store, owner, fingerprint=""):
        self.store, self.owner = store, owner
        self.key = f"telegram_add_{fingerprint}_{owner}"
        self.expired = False

    def state(self):
        raw = self.store.setting(self.key)
        if not raw:
            return None
        try:
            state = json.loads(raw)
            if (
                not isinstance(state, dict)
                or state.get("version") != 1
                or state.get("stage") not in ("market", "tag")
                or not isinstance(state.get("items"), str)
                or type(state.get("expires")) is not int
                or type(state.get("page")) is not int
                or state["page"] < 0
                or "tag" not in state
                or (state["tag"] is not None and not isinstance(state["tag"], str))
                or state.get("market") not in ("", "wb", "ozon")
            ):
                raise ValueError
            if state["expires"] > time.time():
                return state
        except (ValueError, TypeError):
            pass
        self.cancel()
        self.expired = True
        return None

    def save(self, state):
        if not self.owner:
            raise ValueError("Укажите ваш Telegram ID в настройках приложения")
        self.store.configure({self.key: json.dumps(state, ensure_ascii=False)})

    def cancel(self):
        self.store.configure({self.key: ""})

    def tags(self):
        return [tag["name"] for tag in self.store.tags()]

    def prompt(self, state=None):
        state = state or self.state()
        if not state:
            return ""
        if state["stage"] == "market":
            numbers = re.findall(r"(?<!\S)[0-9]+(?!\S)", state["items"])
            preview = ", ".join(numbers[:3]) + ("…" if len(numbers) > 3 else "")
            tag = f"\nТег: {state['tag']}" if state["tag"] else ""
            return f"На какой площадке искать артикул: {preview}?\nВыберите Ozon или Wildberries.{tag}\n/cancel — отменить добавление"
        pages = max(1, (len(self.tags()) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(state.get("page", 0), pages - 1)
        return f"Выберите тег для товара · страница {page + 1}/{pages}.\nМожно написать тег сообщением.\n/tag #название 400 — создать тег\n/cancel — отменить добавление"

    def markup(self):
        state = self.state()
        if not state:
            return None
        if state["stage"] == "market":
            rows = [["Ozon", "Wildberries"]]
        else:
            tags = self.tags()
            pages = max(1, (len(tags) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(state.get("page", 0), pages - 1)
            visible = tags[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
            rows = [visible[n : n + 2] for n in range(0, len(visible), 2)]
            navigation = ([PREVIOUS] if page else []) + (
                [NEXT] if page + 1 < pages else []
            )
            if navigation:
                rows.append(navigation)
        rows.append([CANCEL])
        return {
            "keyboard": [[{"text": text} for text in row] for row in rows],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Выберите площадку"
            if state["stage"] == "market"
            else "Выберите тег",
        }

    def begin(self, items, tag=None):
        if tag is not None and tag not in self.tags():
            raise ValueError("Неизвестный тег. /tags — доступные теги")
        # Validate the entire input before storing a question or adding anything.
        # The temporary marketplace is used only to validate numeric references.
        refs = parse_batch(items, "ozon")
        numbers = re.findall(r"(?<!\S)\d+(?!\S)", items)
        if any(not number.isascii() for number in numbers):
            raise ValueError("Для артикула используйте цифры от 0 до 9")
        needs_market = bool(numbers)
        if not needs_market and tag:
            count = self.store.add(refs, "", tag, clear_setting=self.key)
            return f"Добавлено {count} из {len(refs)} карточек. Дубликаты пропущены"
        state = {
            "version": 1,
            "items": items,
            "tag": tag,
            "market": "",
            "stage": "market" if needs_market else "tag",
            "page": 0,
            "expires": int(time.time()) + 900,
        }
        self.save(state)
        return self.prompt(state)

    def finish(self, state):
        if state["tag"] not in self.tags():
            state.update(tag=None, stage="tag", page=0)
            self.save(state)
            return "Тег больше недоступен. " + self.prompt(state)
        refs = parse_batch(state["items"], state["market"])
        count = self.store.add(refs, "", state["tag"], clear_setting=self.key)
        markets = ", ".join(
            sorted({"Ozon" if ref.market == "ozon" else "Wildberries" for ref in refs})
        )
        return f"Добавлено {count} из {len(refs)} карточек.\nПлощадка: {markets}\nТег: {state['tag']}\nДубликаты пропущены"

    def answer(self, text):
        state = self.state()
        if not state:
            return (
                "Запрос добавления устарел. Пришлите артикул заново."
                if self.expired
                else None
            )
        if state["stage"] == "market":
            market = MARKETS.get(text.casefold())
            if not market:
                return self.prompt(state)
            state["market"] = market
            if state["tag"]:
                return self.finish(state)
            state["stage"] = "tag"
        elif text in (NEXT, PREVIOUS):
            pages = max(1, (len(self.tags()) + PAGE_SIZE - 1) // PAGE_SIZE)
            state["page"] = max(
                0, min(pages - 1, state["page"] + (1 if text == NEXT else -1))
            )
        elif text.casefold() in self.tags():
            state["tag"] = text.casefold()
            return self.finish(state)
        else:
            return self.prompt(state)
        self.save(state)
        return self.prompt(state)
