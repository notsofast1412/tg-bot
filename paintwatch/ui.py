from datetime import datetime
import time
from queue import Empty, Queue
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .clipboard import ClipboardActions
from .bridge import BrowserBridge
from .domain import display_time, money, parse_batch, rub
from .platform import SecretStore, autostart, extension_directory
from .worker import Worker


MARKETS = {"Все площадки": "", "Ozon": "ozon", "Wildberries": "wb"}
STATES = {
    "pending": "Ожидает",
    "sending": "Отправляется",
    "sent": "Отправлено",
    "uncertain": "Результат неизвестен",
    "blocked": "Отложено",
    "cancelled": "Отменено",
}


def when(value):
    return display_time(value)


class Window:
    def __init__(self, store, vault, smoke=False):
        self.store, self.vault = store, vault
        self.root = tk.Tk()
        self.root.title(f"PaintWatch {__version__} — мониторинг цен")
        self.root.geometry("1280x820")
        self.root.minsize(1020, 650)
        self.root.configure(background="#f1f5f9")
        self.actions = Queue()
        self.pending = []
        self.tray = None
        self.closing = False
        self.smoke = smoke
        import secrets

        pairing = SecretStore(store.directory, "extension.dpapi")
        key = pairing.read()
        if not key:
            key = secrets.token_urlsafe(32)
            pairing.save(key)
        self.bridge = BrowserBridge(store, key)
        self.worker = Worker(store, vault, bridge=self.bridge)
        self.status = tk.StringVar(value="Мониторинг остановлен")
        self.search = tk.StringVar()
        self.market = tk.StringVar(value="Все площадки")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f1f5f9")
        style.configure("TLabel", background="#f1f5f9", foreground="#0f172a")
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("Muted.TLabel", foreground="#475569")
        style.configure("TButton", padding=(12, 7))
        style.configure("Primary.TButton", background="#2563eb", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1d4ed8")])
        style.configure(
            "Treeview", rowheight=32, background="white", fieldbackground="white"
        )
        style.configure("Treeview.Heading", padding=8, font=("Segoe UI", 10, "bold"))
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="PaintWatch", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="WB с Кошельком · Ozon по карте\nУведомления в Telegram-группу",
            style="Muted.TLabel",
        ).pack(side="left", padx=24)
        self.monitor_button = ttk.Button(
            header, text="▶ Запустить", style="Primary.TButton", command=self.toggle
        )
        self.monitor_button.pack(side="right")
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.products_tab = ttk.Frame(self.notebook, padding=12)
        self.tags_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_page = ttk.Frame(self.notebook)
        canvas = tk.Canvas(
            self.settings_page, highlightthickness=0, background="#f1f5f9"
        )
        scroll = ttk.Scrollbar(
            self.settings_page, orient="vertical", command=canvas.yview
        )
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.settings_tab = ttk.Frame(canvas, padding=16)
        content = canvas.create_window((0, 0), window=self.settings_tab, anchor="nw")
        self.settings_tab.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                content, width=max(event.width, self.settings_tab.winfo_reqwidth())
            ),
        )
        self.queue_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=12)
        for tab, title in (
            (self.products_tab, "Товары"),
            (self.tags_tab, "Теги и пороги"),
            (self.settings_page, "Настройки"),
            (self.queue_tab, "Уведомления"),
            (self.log_tab, "События"),
        ):
            self.notebook.add(tab, text=title)
        self._products()
        self._tags()
        self._settings()
        self._queue()
        self.log = tk.Text(
            self.log_tab,
            state="disabled",
            wrap="word",
            font=("Consolas", 10),
            relief="flat",
        )
        self.log.pack(fill="both", expand=True)
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(
            side="left"
        )
        ttk.Button(footer, text="Резервная копия", command=self.backup).pack(
            side="right"
        )
        ttk.Button(footer, text="Выход", command=self.exit).pack(side="right", padx=8)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.refresh()

    def _products(self):
        bar = ttk.Frame(self.products_tab)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(
            bar, text="+ Добавить", style="Primary.TButton", command=self.add
        ).pack(side="left")
        for title, action in (
            ("Изменить", self.edit),
            ("Пауза / продолжить", self.pause),
            ("Удалить", self.delete),
            ("История", self.history),
        ):
            ttk.Button(bar, text=title, command=action).pack(side="left", padx=(6, 0))
        scan_bar = ttk.Frame(self.products_tab)
        scan_bar.pack(fill="x", pady=(0, 10))
        ttk.Button(
            scan_bar,
            text="Проверить все сейчас",
            style="Primary.TButton",
            command=self.scan_all,
        ).pack(side="left")
        self.scan_status = tk.StringVar()
        ttk.Label(scan_bar, textvariable=self.scan_status, style="Muted.TLabel").pack(
            side="left", padx=12
        )
        filters = ttk.Frame(self.products_tab)
        filters.pack(fill="x", pady=(0, 10))
        ttk.Combobox(
            filters,
            textvariable=self.market,
            values=list(MARKETS),
            state="readonly",
            width=18,
        ).pack(side="left")
        entry = ttk.Entry(filters, textvariable=self.search, width=35)
        ClipboardActions(entry)
        entry.pack(side="left", padx=8)
        ttk.Label(
            filters, text="Поиск по названию, артикулу или тегу", style="Muted.TLabel"
        ).pack(side="left")
        self.search.trace_add("write", lambda *_: self.refresh())
        self.market.trace_add("write", lambda *_: self.refresh())
        self.tree = self._table(
            self.products_tab,
            [
                ("id", "№", 42),
                ("market", "Площадка", 92),
                ("name", "Товар", 260),
                ("price", "Цена", 95),
                ("threshold", "Порог", 95),
                ("tag", "Тег", 100),
                ("status", "Статус", 155),
                ("checked", "Проверено", 100),
            ],
        )
        self.tree.bind("<Double-1>", lambda _: self.edit())
        buttons = ttk.Frame(self.products_tab)
        buttons.pack(fill="x", pady=(12, 0))
        for title, action in (
            ("Проверить по расписанию", self.check),
            ("Открыть браузер", lambda: self.browser_command("open")),
            ("Я прошёл проверку", lambda: self.browser_command("resume")),
        ):
            ttk.Button(buttons, text=title, command=action).pack(
                side="left", padx=(0, 8)
            )
        ttk.Label(
            self.products_tab,
            text="«Проверить все сейчас» запускает разовый обход включённых товаров вне расписания. Паузы площадок сохраняются.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        self.details = tk.StringVar(
            value="Выберите товар, чтобы увидеть причину ошибки и характеристики"
        )
        ttk.Label(
            self.products_tab,
            textvariable=self.details,
            wraplength=1120,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=8)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_details())

    def _table(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=[c[0] for c in columns], show="headings", selectmode="browse"
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=40, stretch=key in ("name", "error"))
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _tags(self):
        ttk.Label(
            self.tags_tab,
            text="Уведомление — при цене ниже порога; повторное — при снижении ещё минимум на 15 ₽ от последнего отправленного. Изменение порога само по себе не вызывает рассылку.",
            wraplength=1050,
        ).pack(anchor="w", pady=(0, 12))
        self.tags_tree = self._table(
            self.tags_tab, [("name", "Тег", 220), ("threshold", "Порог", 200)]
        )
        bar = ttk.Frame(self.tags_tab)
        bar.pack(anchor="w", pady=10)
        ttk.Button(bar, text="+ Добавить тег", command=self.add_tag).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(bar, text="Изменить выбранный порог", command=self.edit_tag).pack(
            side="left"
        )

    def _settings(self):
        self.token = tk.StringVar()
        self.chat = tk.StringVar(value=self.store.setting("chat_id"))
        self.owner_id = tk.StringVar(value=self.store.setting("owner_id"))
        self.interval = tk.StringVar(value=str(self.store.interval()))
        self.parallelism = tk.StringVar(value=str(self.store.parallelism()))
        self.check_timeout = tk.StringVar(value=str(self.store.check_timeout()))
        selected = self.store.setting("browser", "chrome")
        self.browser = tk.StringVar(
            value={
                "msedge": "Microsoft Edge",
                "chrome": "Google Chrome",
            }.get(selected, "Google Chrome")
        )
        self.dest = tk.StringVar(value=self.store.setting("wb_dest", "-1257786"))
        self.startup = tk.BooleanVar(value=self.store.setting("autostart", "0") == "1")
        form = ttk.Frame(self.settings_tab)
        form.pack(anchor="nw", fill="x")
        rows = [
            ("Токен Telegram-бота", self.token),
            ("ID группы (отрицательное число)", self.chat),
            ("Интервал, секунд (0 — непрерывно)", self.interval),
            ("Регион WB: dest", self.dest),
        ]
        for row, (label, variable) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=8)
            parent = form
            if variable is self.chat:
                parent = ttk.Frame(form)
                parent.grid(row=row, column=1, sticky="w", padx=16)
            entry = ttk.Entry(
                parent,
                textvariable=variable,
                width=20 if variable is self.chat else 58,
                show="•" if variable is self.token else "",
            )
            ClipboardActions(entry)
            if variable is self.chat:
                entry.pack(side="left")
                ttk.Label(parent, text="Ваш ID для ЛС:").pack(side="left", padx=8)
                owner_entry = ttk.Entry(parent, textvariable=self.owner_id, width=20)
                ClipboardActions(owner_entry)
                owner_entry.pack(side="left")
            else:
                entry.grid(row=row, column=1, sticky="w", padx=16)
        ttk.Label(form, text="Параллельно / лимит времени").grid(
            row=4, column=0, sticky="w", pady=8
        )
        pool = ttk.Frame(form)
        pool.grid(row=4, column=1, sticky="w", padx=16)
        for label, variable in (
            ("карточек (1–32)", self.parallelism),
            ("секунд (1–120)", self.check_timeout),
        ):
            entry = ttk.Entry(pool, textvariable=variable, width=7)
            ClipboardActions(entry)
            entry.pack(side="left")
            ttk.Label(pool, text=label).pack(side="left", padx=(4, 12))
        ttk.Label(form, text="Обычный браузер с расширением").grid(
            row=5, column=0, sticky="w", pady=8
        )
        ttk.Combobox(
            form,
            textvariable=self.browser,
            values=["Google Chrome", "Microsoft Edge"],
            state="readonly",
            width=36,
        ).grid(row=5, column=1, sticky="w", padx=16)
        ttk.Checkbutton(
            form, text="Запускать при входе в Windows", variable=self.startup
        ).grid(row=6, column=1, sticky="w", pady=12, padx=16)
        ttk.Label(form, text="Код подключения расширения").grid(
            row=7, column=0, sticky="w", pady=8
        )
        self.pairing_display = tk.StringVar(value=self.bridge.key)
        ttk.Entry(
            form,
            textvariable=self.pairing_display,
            state="readonly",
            show="•",
            width=45,
        ).grid(row=7, column=1, sticky="w", padx=16)
        ttk.Button(form, text="Скопировать код", command=self.copy_pairing).grid(
            row=7, column=2, padx=4
        )
        ttk.Button(
            form, text="Открыть папку расширения", command=self.open_extension
        ).grid(row=8, column=1, sticky="w", padx=16, pady=6)
        self.extension_status = tk.StringVar(value="Расширение ещё не подключено")
        ttk.Label(
            self.settings_tab, textvariable=self.extension_status, style="Muted.TLabel"
        ).pack(anchor="w", pady=4)
        try:
            saved = bool(self.vault.read())
        except RuntimeError:
            saved = False
        self.telegram_status = tk.StringVar()
        ttk.Label(
            self.settings_tab,
            textvariable=self.telegram_status,
            style="Muted.TLabel",
            wraplength=1060,
        ).pack(anchor="w")
        self.token_status = tk.StringVar(
            value="Токен сохранён" if saved else "Токен не задан"
        )
        ttk.Label(
            self.settings_tab, textvariable=self.token_status, style="Muted.TLabel"
        ).pack(anchor="w", pady=8)
        bar = ttk.Frame(self.settings_tab)
        bar.pack(anchor="w", pady=12)
        for label, action in (
            ("Сохранить", self.save_settings),
            ("Проверить доступ Telegram", lambda: self.submit("diagnose")),
            ("Отправить тест в группу", lambda: self.submit("test")),
            ("Удалить токен", self.clear_token),
        ):
            ttk.Button(bar, text=label, command=action).pack(side="left", padx=(0, 8))
        ttk.Label(
            self.settings_tab,
            text="Установите папку расширения через chrome://extensions → Режим разработчика → Загрузить распакованное расширение.\nВ его окне вставьте код подключения. Держите обычный Chrome и PaintWatch открытыми.\nПустое поле токена сохраняет прежний токен. В Windows токен защищён DPAPI.\nСон и выключение останавливают мониторинг. CAPTCHA проходится вручную в обычном браузере.",
            wraplength=1060,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=16)

    def _queue(self):
        ttk.Label(
            self.queue_tab,
            text="При неизвестном результате отправки сначала проверьте группу по номеру события. Повтор может создать дубликат.",
            wraplength=1050,
        ).pack(anchor="w", pady=(0, 12))
        self.queue_tree = self._table(
            self.queue_tab,
            [
                ("id", "Событие", 65),
                ("name", "Товар", 240),
                ("price", "Цена события", 120),
                ("state", "Доставка", 160),
                ("error", "Причина", 400),
            ],
        )
        ttk.Button(
            self.queue_tab, text="Повторить выбранное событие", command=self.retry
        ).pack(anchor="w", pady=10)

    def selected(self):
        selection = self.tree.selection()
        return self.store.product(int(selection[0])) if selection else None

    def refresh(self):
        selection = self.tree.selection()
        selected_tag = self.tags_tree.selection()
        selected_event = self.queue_tree.selection()
        query = self.search.get().casefold()
        market = MARKETS[self.market.get()]
        self.tree.delete(*self.tree.get_children())
        for p in self.store.products():
            if market and p["market"] != market:
                continue
            if (
                query
                and query not in f"{p['name']} {p['article']} {p['tag']}".casefold()
            ):
                continue
            self.tree.insert(
                "",
                "end",
                iid=str(p["id"]),
                values=(
                    p["id"],
                    "Ozon" if p["market"] == "ozon" else "WB",
                    p["name"],
                    rub(p["price"]),
                    rub(p["threshold"]),
                    p["tag"],
                    p["status"] if p["enabled"] else "Пауза",
                    when(p["last_success"]),
                ),
            )
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection[0])
        self.tags_tree.delete(*self.tags_tree.get_children())
        for tag in self.store.tags():
            self.tags_tree.insert(
                "", "end", iid=tag["name"], values=(tag["name"], rub(tag["threshold"]))
            )
        self.queue_tree.delete(*self.queue_tree.get_children())
        for e in self.store.queue()[:500]:
            self.queue_tree.insert(
                "",
                "end",
                iid=str(e["id"]),
                values=(
                    e["id"],
                    e["name"],
                    rub(e["price"]),
                    STATES[e["state"]],
                    e["error"],
                ),
            )
        if selected_tag and self.tags_tree.exists(selected_tag[0]):
            self.tags_tree.selection_set(selected_tag[0])
        if selected_event and self.queue_tree.exists(selected_event[0]):
            self.queue_tree.selection_set(selected_event[0])
        active = self.store.setting("monitoring", "0") == "1"
        import time

        connected = time.time() - int(self.store.setting("extension_seen", "0")) < 120
        load = self.bridge.status()
        if self.notebook.select() != str(self.settings_page):
            self.interval.set(str(self.store.interval()))
            self.parallelism.set(str(self.store.parallelism()))
            self.check_timeout.set(str(self.store.check_timeout()))
        self.extension_status.set(
            f"Расширение подключено · активных: {load['active']} · ожидают: {load['due']}"
            if connected
            else "Расширение не подключено — откройте обычный Chrome с расширением"
        )
        self.telegram_status.set(
            self.store.setting("telegram_control_error")
            or self.store.setting("telegram_menu_error")
            or "ЛС: сохраните токен, отправьте боту /id, укажите Ваш ID. Команды: /help"
        )
        self.scan_status.set(
            f"Разовая проверка: осталось {load['manual']}"
            if load["manual"]
            else "Разовый обход не запущен"
        )
        self.monitor_button.configure(
            text="⏸ Остановить" if active or load["manual"] else "▶ Запустить"
        )
        self.show_details()

    def show_details(self):
        p = self.selected()
        if not p:
            self.details.set(
                "Выберите товар, чтобы увидеть причину ошибки и характеристики"
            )
        if p:
            self.details.set(
                f"{p['url']}\n{p['issue'] or p['status']} · Следующая проверка: {when(p['next_check'])}\nЦвет: {p['color'] or 'неизвестен'} · Объём: {p['volume'] or 'неизвестен'} · Упаковка: {p['pack'] or 'неизвестна'} · Продавец: {p['seller'] or 'не подтверждён'}"
            )

    def toggle(self):
        active = self.bridge.status()["monitoring"]
        if active:
            self.store.stop_monitoring()
        else:
            self.store.configure({"monitoring": 1})
        self.status.set("Мониторинг остановлен" if active else "Мониторинг включён")
        self.refresh()

    def scan_all(self):
        count = self.store.request_scan()
        self.status.set(
            f"Разовая проверка: {count} товаров. Откройте Chrome с расширением"
        )
        self.refresh()

    def add_tag(self):
        from tkinter.simpledialog import askstring

        name = askstring(
            "Новый тег", "Название тега, например #montana:", parent=self.root
        )
        if name is None:
            return
        value = askstring("Порог тега", "Порог цены в рублях:", parent=self.root)
        if value is None:
            return
        try:
            tag = self.store.add_tag(name, money(value))
            self.refresh()
            self.tags_tree.selection_set(tag)
            self.status.set(f"Добавлен тег {tag}")
        except ValueError as error:
            messagebox.showerror("Тег", str(error), parent=self.root)

    def add(self):
        ProductDialog(self)

    def edit(self):
        if p := self.selected():
            ProductDialog(self, p)

    def pause(self):
        if p := self.selected():
            self.store.enable(p["id"], not p["enabled"])
            self.refresh()

    def delete(self):
        if (p := self.selected()) and messagebox.askyesno(
            "Удалить товар",
            f"Удалить «{p['name']}» вместе с историей и очередью?",
            parent=self.root,
        ):
            self.store.delete(p["id"])
            self.refresh()

    def check(self):
        p = self.selected()
        self.store.request_check(p["id"] if p else None)
        self.status.set(
            "Проверка запрошена с учётом интервала и пауз площадки. Включите мониторинг, если он остановлен"
        )
        self.refresh()

    def browser_command(self, command):
        if p := self.selected():
            self.submit(command, p["id"])
        else:
            messagebox.showinfo(
                "Выбор товара", "Сначала выберите товар", parent=self.root
            )

    def submit(self, command, value=None):
        if self.smoke:
            return
        self.status.set("Выполняется…")
        self.pending.append(self.worker.submit(command, value))

    def edit_tag(self):
        if not self.tags_tree.selection():
            return
        name = self.tags_tree.selection()[0]
        from tkinter.simpledialog import askstring

        tag = next(t for t in self.store.tags() if t["name"] == name)
        value = askstring(
            "Порог тега",
            f"Новый порог {name}, ₽:",
            initialvalue=f"{tag['threshold'] / 100:.2f}",
            parent=self.root,
        )
        if value is not None:
            try:
                self.store.edit_tag(name, money(value))
                self.refresh()
            except ValueError as error:
                messagebox.showerror("Порог", str(error), parent=self.root)

    def history(self):
        if not (p := self.selected()):
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"История — {p['name']}")
        dialog.geometry("820x500")
        table = self._table(
            dialog,
            [("time", "Время", 150), ("price", "Цена", 130), ("name", "Источник", 450)],
        )
        for row in self.store.history(p["id"]):
            table.insert(
                "",
                "end",
                values=(when(row["observed_at"]), rub(row["price"]), row["source"]),
            )

    def save_settings(self):
        try:
            chat = self.chat.get().strip()
            if chat and (not chat.lstrip("-").isdigit() or int(chat) >= 0):
                raise ValueError("ID группы должен быть отрицательным целым числом")
            owner = self.owner_id.get().strip()
            if owner and (not owner.isdigit() or int(owner) <= 0):
                raise ValueError(
                    "Ваш Telegram ID должен быть положительным числом. Узнать: /id в личном чате с ботом"
                )
            seconds = int(self.interval.get())
            if not 0 <= seconds <= 2**63 - 1 - int(time.time()):
                raise ValueError(
                    "Интервал должен быть неотрицательным целым числом секунд в пределах SQLite"
                )
            parallelism = int(self.parallelism.get())
            timeout = int(self.check_timeout.get())
            if not 1 <= parallelism <= 32 or not 1 <= timeout <= 120:
                raise ValueError(
                    "Параллельно: 1–32 карточки. Лимит времени: 1–120 секунд"
                )
            dest = self.dest.get().strip()
            if not dest.lstrip("-").isdigit():
                raise ValueError("Регион WB должен быть числовым dest")
            token = self.token.get().strip()
            if token:
                import re

                if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{25,}", token):
                    raise ValueError("Неверный формат токена")
                self.vault.save(token)
                self.token.set("")
                self.token_status.set(
                    "Токен сохранён"
                    if sys.platform == "win32"
                    else "Токен хранится только до закрытия приложения (не Windows)"
                )
            selected = {
                "Microsoft Edge": "msedge",
                "Google Chrome": "chrome",
                "Chromium": "chromium",
            }[self.browser.get()]
            changed_browser = selected != self.store.setting("browser", "chrome")
            if sys.platform == "win32":
                autostart(self.startup.get())
            elif self.startup.get():
                raise ValueError("Автозапуск доступен только в Windows")
            changed_interval = seconds != self.store.interval()
            self.store.configure(
                {
                    "chat_id": chat,
                    "owner_id": owner,
                    "interval": seconds,
                    "parallelism": parallelism,
                    "check_timeout": timeout,
                    "browser": selected,
                    "wb_dest": dest,
                    "autostart": int(self.startup.get()),
                }
            )
            if changed_interval:
                self.store.request_check()
            self.status.set("Настройки сохранены")
            if changed_browser:
                self.submit("browser")
        except (ValueError, RuntimeError, OSError) as error:
            messagebox.showerror("Настройки", str(error), parent=self.root)

    def clear_token(self):
        self.vault.clear()
        self.token.set("")
        self.token_status.set("Токен удалён")

    def copy_pairing(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.bridge.key)
        self.status.set(
            "Код скопирован. Вставьте его в окно расширения PaintWatch в браузере"
        )

    def open_extension(self):
        import os

        folder = extension_directory()
        if sys.platform == "win32" and folder.is_dir():
            os.startfile(str(folder))
        else:
            messagebox.showinfo("Папка расширения", str(folder), parent=self.root)

    def retry(self):
        if not self.queue_tree.selection():
            return
        oid = int(self.queue_tree.selection()[0])
        event = next((e for e in self.store.queue() if e["id"] == oid), None)
        if not event or event["state"] not in ("uncertain", "blocked"):
            return
        if messagebox.askyesno(
            "Повтор отправки",
            f"Проверьте событие №{oid} в группе. Повтор может создать дубликат. Отправить повторно после свежей проверки цены?",
            parent=self.root,
        ):
            self.store.delivery(oid, "pending")
            self.refresh()

    def backup(self):
        target = filedialog.asksaveasfilename(
            parent=self.root,
            title="Сохранить копию базы",
            defaultextension=".sqlite3",
            filetypes=[("База SQLite", "*.sqlite3")],
        )
        if target:
            self.submit("backup", target)

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "101.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        while True:
            try:
                action = self.actions.get_nowait()
            except Empty:
                break
            if action == "show":
                self.root.deiconify()
                self.root.lift()
            elif action == "exit":
                self.exit()
        for future in list(self.pending):
            if future.done():
                self.pending.remove(future)
                try:
                    result = future.result()
                    self.status.set(result)
                    self.append_log(result)
                except Exception as error:
                    self.status.set(str(error))
                    self.append_log(str(error))
                    messagebox.showerror("PaintWatch", str(error), parent=self.root)
        while True:
            try:
                kind, text = self.worker.events.get_nowait()
            except Empty:
                break
            if kind == "refresh":
                self.refresh()
            else:
                self.status.set(text)
                self.append_log(text)
                if kind == "error":
                    self.store.configure({"monitoring": 0})
                    self.refresh()
        if not self.closing:
            self.root.after(250, self.poll)

    def periodic_refresh(self):
        if not self.closing:
            self.refresh()
            self.root.after(5000, self.periodic_refresh)

    def create_tray(self):
        if sys.platform != "win32":
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (64, 64), "#2563eb")
            ImageDraw.Draw(image).line(
                [(12, 18), (12, 46), (52, 46), (52, 18), (32, 37), (12, 18)],
                fill="white",
                width=5,
            )
            self.tray = pystray.Icon(
                "PaintWatch",
                image,
                "PaintWatch",
                menu=pystray.Menu(
                    pystray.MenuItem(
                        "Открыть", lambda *_: self.actions.put("show"), default=True
                    ),
                    pystray.MenuItem("Выход", lambda *_: self.actions.put("exit")),
                ),
            )
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.exit()

    def exit(self):
        if self.closing:
            return
        self.closing = True
        self.status.set("Завершение работы и сохранение профилей браузера…")
        self.worker.stop()

        def finish():
            if self.worker.thread.is_alive():
                self.root.after(200, finish)
                return
            if self.tray:
                self.tray.stop()
            self.root.destroy()

        finish()

    def run(self, minimized=False):
        self.create_tray()
        self.worker.start()
        if minimized and self.tray:
            self.root.withdraw()
        self.poll()
        self.periodic_refresh()
        self.root.mainloop()


class ProductDialog:
    def __init__(self, window, product=None):
        self.window, self.product = window, product
        self.root = tk.Toplevel(window.root)
        self.root.title("Изменить товар" if product else "Добавить товары")
        self.root.geometry("740x650")
        self.root.transient(window.root)
        self.root.grab_set()
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        self.name = tk.StringVar(value=product["name"] if product else "")
        self.market = tk.StringVar(value="Ozon")
        self.tag = tk.StringVar(
            value=product["tag"] if product else window.store.tags()[0]["name"]
        )
        self.threshold = tk.StringVar(
            value=f"{product['custom_threshold'] / 100:.2f}"
            if product and product["custom_threshold"] is not None
            else ""
        )
        self.color = tk.StringVar(value=product["color"] if product else "")
        self.volume = tk.StringVar(value=product["volume"] if product else "")
        self.pack = tk.StringVar(value=product["pack"] if product else "")
        if not product:
            ttk.Label(
                outer,
                text="Прямые ссылки или числовые артикулы — до 1000, по одному на строку",
            ).pack(anchor="w")
            self.urls = tk.Text(outer, height=7, wrap="word")
            self.urls.pack(fill="x", pady=8)
            self.clipboard = ClipboardActions(self.urls)
            self.paste_button = ttk.Button(
                outer, text="Вставить из буфера", command=self.clipboard.paste
            )
            self.paste_button.pack(anchor="w", pady=(0, 6))
            self.root.after_idle(self.urls.focus_set)
            ttk.Label(outer, text="Площадка для числовых артикулов:").pack(anchor="w")
            ttk.Combobox(
                outer,
                textvariable=self.market,
                values=["Ozon", "Wildberries"],
                state="readonly",
            ).pack(anchor="w", pady=6)
        else:
            ttk.Label(outer, text=product["url"], wraplength=680).pack(
                anchor="w", pady=8
            )
        fields = [
            ("Название / название пакета (можно оставить пустым)", self.name),
            ("Индивидуальный порог, ₽ (пусто — порог тега)", self.threshold),
        ]
        if product:
            fields += [
                ("Цвет (уточнение владельца)", self.color),
                ("Объём (уточнение владельца)", self.volume),
                ("Упаковка (уточнение владельца)", self.pack),
            ]
        for label, variable in fields:
            ttk.Label(outer, text=label).pack(anchor="w", pady=(8, 2))
            entry = ttk.Entry(outer, textvariable=variable, width=70)
            ClipboardActions(entry)
            entry.pack(fill="x")
        ttk.Label(outer, text="Тег").pack(anchor="w", pady=(10, 2))
        ttk.Combobox(
            outer,
            textvariable=self.tag,
            values=[t["name"] for t in window.store.tags()],
            state="readonly",
            width=30,
        ).pack(anchor="w")
        self.hint = tk.StringVar()
        self.tag.trace_add("write", lambda *_: self.tag_hint())
        self.tag_hint()
        ttk.Label(
            outer, textvariable=self.hint, wraplength=670, style="Muted.TLabel"
        ).pack(anchor="w", pady=8)
        ttk.Button(
            outer,
            text="Сохранить" if product else "Проверить список и добавить",
            style="Primary.TButton",
            command=self.save,
        ).pack(anchor="e", pady=12)

    def tag_hint(self):
        tag = next(t for t in self.window.store.tags() if t["name"] == self.tag.get())
        self.hint.set(
            f"Порог тега: {rub(tag['threshold'])}. Цена должна быть строго ниже. Количество и объём из тега не вычисляются."
        )

    def save(self):
        try:
            threshold = (
                money(self.threshold.get()) if self.threshold.get().strip() else None
            )
            if self.product:
                self.window.store.edit(
                    self.product["id"],
                    self.name.get().strip() or self.product["article"],
                    self.tag.get(),
                    threshold,
                    self.color.get().strip(),
                    self.volume.get().strip(),
                    self.pack.get().strip(),
                )
            else:
                refs = parse_batch(
                    self.urls.get("1.0", "end"), MARKETS[self.market.get()]
                )
                preview = "\n".join(
                    f"{r.market.upper()}: {r.article}" for r in refs[:15]
                )
                if len(refs) > 15:
                    preview += f"\n… и ещё {len(refs) - 15}"
                if not messagebox.askyesno(
                    "Подтвердить добавление",
                    f"Добавить {len(refs)} карточек?\n{preview}\n\nДубликаты пропускаются. Цены и характеристики будут проверены после запуска мониторинга.",
                    parent=self.root,
                ):
                    return
                count = self.window.store.add(
                    refs, self.name.get().strip(), self.tag.get(), threshold
                )
                self.window.status.set(
                    f"Добавлено {count} из {len(refs)} карточек; дубликаты пропущены"
                )
            self.window.refresh()
            self.root.destroy()
        except ValueError as error:
            messagebox.showerror("Товар", str(error), parent=self.root)
