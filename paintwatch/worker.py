import asyncio
from concurrent.futures import Future
from queue import Empty, Queue
import threading
import time

from .browser import BrowserCollector
from .domain import PriceError
from .platform import open_normal_browser
from .telegram import Telegram, TelegramError, deliver


class Worker:
    def __init__(self, store, vault, bridge=None):
        self.store = store
        self.vault = vault
        self.bridge = bridge
        self.commands = Queue()
        self.events = Queue()
        self.stop_requested = threading.Event()
        self.thread = threading.Thread(
            target=self._thread, name="PaintWatch worker", daemon=False
        )

    def start(self):
        self.thread.start()

    def submit(self, command, value=None):
        future = Future()
        self.commands.put((command, value, future))
        return future

    def stop(self):
        self.stop_requested.set()

    def _thread(self):
        try:
            asyncio.run(self.run())
        except Exception as error:
            self.events.put(
                (
                    "error",
                    f"Фоновая работа остановлена ({type(error).__name__}). Перезапустите приложение",
                )
            )

    async def command(self, collector, command, value):
        if command == "open":
            p = self.store.product(value)
            if p:
                if self.store.setting("collector", "extension") == "playwright":
                    await collector.open(p)
                else:
                    open_normal_browser(
                        p["url"], self.store.setting("browser", "chrome")
                    )
            return "Браузер открыт. После ручной проверки нажмите «Я прошёл проверку»"
        if command == "resume":
            self.store.resume_after_verification(value)
            return "Запрошена повторная проверка выбранного товара"
        if command == "browser":
            await collector.close()
            return "Настройка браузера сохранена"
        if command in ("diagnose", "test"):
            token = self.vault.read()
            chat_id = self.store.setting("chat_id")
            if not token or not chat_id:
                raise ValueError("Сохраните токен и ID группы в настройках")
            api = Telegram(token)
            try:
                result = await api.diagnose(chat_id)
                if command == "test":
                    await api.send(
                        chat_id,
                        "✅ PaintWatch: тестовая отправка из Windows-приложения. История цен и очередь не менялись.",
                    )
                    result = "Тестовое сообщение отправлено. Проверьте группу"
                return result
            finally:
                await api.close()
        if command == "backup":
            return f"Резервная копия: {self.store.backup(value)}"
        raise ValueError("Неизвестная команда")

    async def run(self):
        collector = BrowserCollector(self.store.directory, self.store)
        self.store.recover()
        try:
            if self.bridge:
                self.bridge.start()
            while not self.stop_requested.is_set():
                try:
                    command, value, future = self.commands.get_nowait()
                except Empty:
                    pass
                else:
                    try:
                        future.set_result(await self.command(collector, command, value))
                    except (
                        PriceError,
                        TelegramError,
                        ValueError,
                        RuntimeError,
                    ) as error:
                        future.set_exception(ValueError(str(error)))
                    except Exception as error:
                        future.set_exception(
                            ValueError(
                                f"Не удалось выполнить действие ({type(error).__name__})"
                            )
                        )
                interval = max(600, int(self.store.setting("interval", "600")))
                if (
                    self.store.setting("monitoring", "0") == "1"
                    and self.store.setting("collector", "extension") == "playwright"
                ):
                    for p in self.store.products():
                        if (
                            self.stop_requested.is_set()
                            or self.store.setting("monitoring", "0") != "1"
                        ):
                            break
                        now = time.time()
                        if (
                            not p["enabled"]
                            or p["next_check"] > now
                            or int(self.store.setting(f"cooldown_{p['market']}", "0"))
                            > now
                        ):
                            continue
                        self.events.put(("status", f"Проверка: {p['name']}"))
                        try:
                            snapshot = await collector.fetch(p)
                            self.store.record(p["id"], snapshot, interval)
                        except PriceError as error:
                            self.store.failure(p["id"], error, interval)
                            if error.code in ("access", "rate_limit"):
                                self.store.cooldown(
                                    p["market"],
                                    int(time.time()) + max(interval, error.retry_after),
                                    error.code,
                                )
                        except Exception as error:
                            self.store.failure(
                                p["id"],
                                PriceError(
                                    f"Не удалось обработать ответ ({type(error).__name__})"
                                ),
                                interval,
                            )
                        self.events.put(("refresh", ""))
                        # A queued user command takes precedence over the next product.
                        if not self.commands.empty():
                            break
                if (
                    not self.stop_requested.is_set()
                    and self.store.setting("monitoring", "0") == "1"
                ):
                    await deliver(self.store, self.vault)
                if time.time() - int(self.store.setting("last_backup", "0")) >= 86400:
                    self.store.backup()
                    self.store.configure({"last_backup": int(time.time())})
                await asyncio.sleep(0.5)
        finally:
            await collector.close()
            if self.bridge:
                self.bridge.close()
            while not self.commands.empty():
                _, _, future = self.commands.get_nowait()
                if not future.done():
                    future.set_exception(ValueError("Приложение закрыто"))
