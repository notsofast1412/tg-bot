import asyncio
from email.utils import parsedate_to_datetime
import os
import sys
import time
from urllib.parse import urlencode, urlsplit

from playwright.async_api import Error, async_playwright

from .domain import PriceError, ProductRef, parse_ref
from .parsers import as_json, block_page, parse_ozon, parse_wb


def retry_after(value):
    try:
        delay = (
            int(value)
            if str(value).isdigit()
            else int(parsedate_to_datetime(value).timestamp() - time.time())
        )
        return max(600, min(delay, 365 * 86400))
    except (ValueError, TypeError, OverflowError):
        return 600


def check_response(status, text, headers=None):
    headers = headers or {}
    if status == 429:
        raise PriceError(
            "Площадка просит уменьшить частоту запросов (HTTP 429)",
            code="rate_limit",
            retry_after=retry_after(headers.get("retry-after", "")),
        )
    if status in (401, 403, 498):
        raise PriceError(
            f"Доступ ограничен (HTTP {status}). Проверьте страницу вручную в браузере",
            code="access",
            retry_after=retry_after(headers.get("retry-after", "")),
        )
    if status >= 400:
        raise PriceError(f"Источник вернул HTTP {status}")
    if str(headers.get("age", "0")).isdigit() and int(headers.get("age", "0")) > 0:
        raise PriceError(
            "Источник вернул кэшированные данные; свежесть цены не подтверждена"
        )
    block_page(text)


class BrowserCollector:
    """All Playwright objects live on the application's single worker thread."""

    def __init__(self, directory, settings):
        self.directory = directory
        self.settings = settings
        self.playwright = None
        self.contexts = {}
        self.pages = {}
        self.last_request = {}

    async def _page(self, market):
        if market in self.contexts:
            context = self.contexts[market]
            page = self.pages.get(market)
            if page and not page.is_closed():
                return page
            try:
                page = await context.new_page()
                self.pages[market] = page
                return page
            except Error:
                self.contexts.pop(market, None)
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        selected = self.settings.setting(
            "browser", "msedge" if sys.platform == "win32" else "chromium"
        )
        options = {}
        if selected in ("msedge", "chrome"):
            options["channel"] = selected
        if path := os.getenv("PAINTWATCH_BROWSER_PATH"):
            options["executable_path"] = path
            options.pop("channel", None)
        if proxy := os.getenv("HTTPS_PROXY") or os.getenv("https_proxy"):
            options["proxy"] = {"server": proxy}
        profile = self.directory / "profiles" / market
        profile.mkdir(parents=True, exist_ok=True)
        context = await self.playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            chromium_sandbox=True,
            locale="ru-RU",
            viewport={"width": 1200, "height": 800},
            **options,
        )
        self.contexts[market] = context
        page = context.pages[0] if context.pages else await context.new_page()
        self.pages[market] = page
        return page

    async def open(self, product):
        page = await self._page(product["market"])
        await page.bring_to_front()
        # An explicit user action opens a browser; it neither records a price nor resets cooldown.
        if page.url != product["url"]:
            try:
                await page.goto(
                    product["url"], wait_until="domcontentloaded", timeout=30000
                )
            except Error:
                pass

    async def fetch(self, product):
        market = product["market"]
        elapsed = time.monotonic() - self.last_request.get(market, 0)
        if elapsed < 3:
            await asyncio.sleep(3 - elapsed)
        self.last_request[market] = time.monotonic()
        ref = ProductRef(market, product["article"], product["url"], product["variant"])
        try:
            page = await self._page(market)
            captured = []
            restrictions = []
            tasks = set()

            async def capture(response):
                parsed = urlsplit(response.url)
                if (
                    market != "wb"
                    or parsed.hostname
                    not in ("www.wildberries.ru", "wildberries.ru", "card.wb.ru")
                    or "/cards/" not in parsed.path
                    or "/detail" not in parsed.path
                ):
                    return
                try:
                    headers = await response.all_headers()
                    # Rejection status is authoritative even when Chromium exposes no body.
                    check_response(response.status, "", headers)
                    text = await asyncio.wait_for(response.text(), timeout=10)
                    check_response(response.status, text, headers)
                    data = as_json(text)
                    captured.append(parse_wb(data, ref))
                except PriceError as error:
                    if error.code in ("access", "rate_limit"):
                        restrictions.append(error)
                except (
                    Error,
                    ValueError,
                    AttributeError,
                    TypeError,
                    TimeoutError,
                ):
                    pass

            def on_response(response):
                task = asyncio.create_task(capture(response))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            page.on("response", on_response)
            try:
                response = await page.goto(
                    ref.url, wait_until="domcontentloaded", timeout=30000
                )
                html = await page.content()
                if response:
                    check_response(response.status, html, await response.all_headers())
                final_ref = parse_ref(page.url)
                if (final_ref.market, final_ref.article, final_ref.variant) != (
                    ref.market,
                    ref.article,
                    ref.variant,
                ):
                    raise PriceError("Браузер открыл другую карточку", code="identity")
                if market == "wb":
                    for _ in range(20):
                        if restrictions:
                            raise restrictions[0]
                        if captured:
                            return next(
                                (s for s in captured if s.price is not None),
                                captured[0],
                            )
                        await asyncio.sleep(0.25)
                    block_page(await page.content())
                # Fetch runs inside the same browser origin and session the user can verify.
                endpoint = (
                    "/api/composer-api.bx/page/json/v2?"
                    + urlencode({"url": f"/product/{ref.article}/"})
                    if market == "ozon"
                    else "/__internal/u-card/cards/v4/detail?"
                    + urlencode(
                        {
                            "appType": 1,
                            "curr": "rub",
                            "dest": self.settings.setting("wb_dest", "-1257786"),
                            "spp": 30,
                            "nm": ref.article,
                        }
                    )
                )
                result = await page.evaluate(
                    """async path => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 20000);
                    try {
                        const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
                        const text = await response.text();
                        return {status: response.status, url: response.url,
                            text: text.length <= 10000000 ? text : '',
                            oversized: text.length > 10000000,
                            headers: {'retry-after': response.headers.get('retry-after') || '', 'age': response.headers.get('age') || '0'}};
                    } finally { clearTimeout(timer); }
                }""",
                    endpoint,
                )
                expected_host = (
                    "www.ozon.ru" if market == "ozon" else "www.wildberries.ru"
                )
                target = urlsplit(result["url"])
                if target.scheme != "https" or target.hostname != expected_host:
                    raise PriceError(
                        "API перенаправил запрос за пределы площадки", code="identity"
                    )
                check_response(result["status"], result["text"], result["headers"])
                if result["oversized"]:
                    raise PriceError("Ответ слишком большой для безопасной обработки")
                data = as_json(result["text"])
                return (
                    parse_ozon(data, ref) if market == "ozon" else parse_wb(data, ref)
                )
            finally:
                page.remove_listener("response", on_response)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except PriceError:
            raise
        except Error as error:
            if "ERR_CERT" in str(error):
                raise PriceError(
                    "Браузер не доверяет сертификату сайта/прокси. Проверка TLS остаётся включённой",
                    code="tls",
                ) from None
            raise PriceError(
                "Браузер не смог получить данные. Проверьте его окно, установку выбранного браузера и отсутствие другого экземпляра с тем же профилем",
                code="browser",
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PriceError(
                "Структура ответа изменилась или карточка не подтверждена",
                code="schema",
            ) from None

    async def close(self):
        for context in list(self.contexts.values()):
            try:
                await context.close()
            except Error:
                pass
        self.contexts.clear()
        self.pages.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
