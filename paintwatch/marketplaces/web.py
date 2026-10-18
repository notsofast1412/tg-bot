import asyncio
import json
import os
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from paintwatch.domain import FetchError, timestamp


ALLOWED_HOSTS = {
    "www.wildberries.ru",
    "wildberries.ru",
    "card.wb.ru",
    "www.ozon.ru",
    "ozon.ru",
}


def safe_destination(url):
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname not in ALLOWED_HOSTS
        or p.username
        or p.password
        or p.port
    ):
        raise FetchError(
            "unsafe_redirect", "Переход на неподдерживаемый адрес запрещён"
        )


def detect_block(text: str):
    # Проверяем признаки страницы ограничения, а не произвольное слово в JSON.
    sample = text[:100_000].lower()
    if "/__wbaas/challenges/antibot/" in sample:
        raise FetchError(
            "access_blocked",
            "Wildberries вернул проверку WBAAS AntiBot вместо карточки товара; данные и цена не получены. Автоматический обход проверки не выполняется",
        )
    if any(
        marker in sample
        for marker in (
            "<title>доступ ограничен",
            "<title>access denied",
            "<title>robot or human",
            "<title>just a moment",
            "<title>проверка браузера",
            "captcha-container",
            'id="captcha"',
            "showcaptcha",
            "challenge-platform",
        )
    ):
        raise FetchError(
            "access_blocked",
            "Площадка требует проверку доступа/CAPTCHA; автоматический обход отключён",
        )


def access_error(status, text, headers, delay):
    # Один HTTP 403 не доказывает, на каком участке возник отказ.
    if any(
        headers.get(name)
        for name in ("x-denied-reason", "x-proxy-error", "x-mitmproxy-blocked-reason")
    ):
        return FetchError(
            "proxy_denied",
            f"Прокси сообщил об ограничении запроса (HTTP {status}); проверьте сетевую политику среды",
            retry_after=delay,
        )
    try:
        detect_block(text)
    except FetchError as error:
        error.retry_after = delay
        return error
    return FetchError(
        "access_blocked",
        f"Запрос ограничен (HTTP {status}); источник отказа — прокси или площадка — по этому ответу не установлен",
        retry_after=delay,
    )


def retry_delay(header, minimum):
    try:
        delay = (
            int(header)
            if header.isdigit()
            else math.ceil(
                (
                    parsedate_to_datetime(header) - datetime.now(timezone.utc)
                ).total_seconds()
            )
        )
        return max(minimum, min(delay, 365 * 86400))
    except (ValueError, TypeError, OverflowError, AttributeError):
        return minimum


class Web:
    def __init__(self, config, client=None, *, store=None):
        self.config = config
        self.store = store
        self.client = client or httpx.AsyncClient(
            timeout=config.request_timeout,
            headers={
                "User-Agent": "PaintWatch/0.1 (personal price monitoring)",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            follow_redirects=False,
        )
        self.client.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
        self.locks = {"wb": asyncio.Lock(), "ozon": asyncio.Lock()}
        self.last_request = {"wb": 0.0, "ozon": 0.0}
        self.blocked_until = {"wb": 0.0, "ozon": 0.0}

    def _restrict(self, market, header=None):
        delay = retry_delay(header, self.config.interval(market))
        self.blocked_until[market] = max(
            self.blocked_until[market], time.monotonic() + delay
        )
        if self.store:
            until = max(
                int(self.store.setting(f"cooldown_{market}", "0")), timestamp() + delay
            )
            self.store.set_setting(f"cooldown_{market}", until)
        return delay

    async def _pace(self, market):
        if self.store:
            remaining = int(self.store.setting(f"cooldown_{market}", "0")) - timestamp()
            if remaining > 0:
                raise FetchError(
                    "cooldown",
                    "Сохранённая пауза площадки после ограничения доступа",
                    retry_after=remaining,
                )
        if self.blocked_until[market] > time.monotonic():
            raise FetchError(
                "cooldown",
                "Пауза площадки после ограничения доступа; проверка будет повторена позже",
                retry_after=math.ceil(self.blocked_until[market] - time.monotonic()),
            )
        delay = self.config.request_gap - (time.monotonic() - self.last_request[market])
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_request[market] = time.monotonic()

    async def get(self, market: str, url: str) -> tuple[str, str]:
        safe_destination(url)
        async with self.locks[market]:
            for attempt in range(2):
                await self._pace(market)
                try:
                    current = url
                    for _ in range(4):
                        safe_destination(current)
                        response = await self.client.get(
                            current, follow_redirects=False
                        )
                        if response.status_code in (301, 302, 303, 307, 308):
                            target = urljoin(
                                current, response.headers.get("location", "")
                            )
                            safe_destination(target)
                            await self._pace(market)
                            current = target
                            continue
                        break
                    else:
                        raise FetchError(
                            "redirect_loop", "Слишком много перенаправлений"
                        )
                    if response.status_code in (401, 403, 429, 498):
                        delay = self._restrict(
                            market, response.headers.get("retry-after")
                        )
                        raise access_error(
                            response.status_code, response.text, response.headers, delay
                        )
                    if response.status_code in (404, 410):
                        raise FetchError("not_found", "Карточка не найдена")
                    response.raise_for_status()
                    age = response.headers.get("age", "0")
                    if (
                        age.isdigit() and int(age) > 0
                    ) or "stale" in response.headers.get("warning", "").lower():
                        raise FetchError(
                            "stale_data",
                            "Источник вернул кэшированные данные; актуальность цены не подтверждена",
                        )
                    if len(response.content) > 10_000_000:
                        raise FetchError(
                            "oversized", "Ответ площадки превышает допустимый размер"
                        )
                    detect_block(response.text)
                    return response.text, current
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ) as error:
                    if attempt:
                        raise FetchError(
                            "network", f"Ошибка соединения: {type(error).__name__}"
                        ) from None
                except httpx.ProxyError:
                    raise FetchError(
                        "proxy_denied",
                        "Прокси не разрешает запрос: проверьте доступ к домену и настройки сети",
                    ) from None
                except httpx.HTTPStatusError as error:
                    if error.response.status_code < 500 or attempt:
                        raise FetchError(
                            "http_error", f"HTTP {error.response.status_code}"
                        ) from None
                except FetchError as error:
                    if error.code == "access_blocked" and not error.retry_after:
                        error.retry_after = self._restrict(market)
                    raise
                await asyncio.sleep(2)
        raise FetchError("network", "Не удалось получить данные")

    async def render(self, market: str, url: str) -> tuple[str, str]:
        if not self.config.browser_enabled:
            raise FetchError(
                "browser_disabled",
                "Структурированных HTTP-данных нет; резервный Playwright выключен (BROWSER_ENABLED)",
            )
        from playwright.async_api import async_playwright, Error

        safe_destination(url)
        async with self.locks[market]:
            await self._pace(market)
            try:
                async with async_playwright() as pw:
                    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                    options = {"headless": True}
                    if self.config.chromium_path:
                        options["executable_path"] = self.config.chromium_path
                    if proxy_url:
                        options["proxy"] = {"server": proxy_url}
                    browser = await pw.chromium.launch(**options)
                    try:
                        context = await browser.new_context(
                            locale="ru-RU",
                            extra_http_headers={
                                "Cache-Control": "no-cache",
                                "Pragma": "no-cache",
                            },
                        )

                        async def route_request(route):
                            request = route.request
                            resource = urlsplit(request.url)
                            permitted_suffixes = (
                                ("ozon.ru", "ozone.ru", "ozonusercontent.com")
                                if market == "ozon"
                                else (
                                    "wildberries.ru",
                                    "wb.ru",
                                    "wbbasket.ru",
                                    "wbstatic.net",
                                )
                            )
                            host = resource.hostname or ""
                            if resource.scheme not in ("https", "data", "blob") or (
                                resource.scheme == "https"
                                and not any(
                                    host == suffix or host.endswith("." + suffix)
                                    for suffix in permitted_suffixes
                                )
                            ):
                                return await route.abort()
                            if (
                                request.is_navigation_request()
                                and request.frame == page.main_frame
                            ):
                                try:
                                    safe_destination(request.url)
                                except FetchError:
                                    return await route.abort()
                            if request.resource_type in ("image", "media", "font"):
                                return await route.abort()
                            await route.continue_()

                        page = await context.new_page()
                        await page.route("**/*", route_request)
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=int(self.config.request_timeout * 1000),
                        )
                        if response and response.status in (401, 403, 429, 498):
                            delay = self._restrict(
                                market,
                                (await response.all_headers()).get("retry-after"),
                            )
                            raise access_error(
                                response.status,
                                await page.content(),
                                await response.all_headers(),
                                delay,
                            )
                        if response and response.status >= 400:
                            raise FetchError(
                                "http_error", f"Браузер: HTTP {response.status}"
                            )
                        if (
                            response
                            and (await response.all_headers()).get("age", "0").isdigit()
                            and int((await response.all_headers()).get("age", "0")) > 0
                        ):
                            raise FetchError(
                                "stale_data",
                                "Браузер получил кэшированную страницу; цена не принята",
                            )
                        detect_block(await page.content())
                        try:
                            await page.wait_for_selector(
                                'script[type="application/ld+json"], [data-widget="webPrice"]',
                                state="attached",
                                timeout=5000,
                            )
                        except Error:
                            pass
                        safe_destination(page.url)
                        html = await page.content()
                        detect_block(html)
                        return html, page.url
                    finally:
                        await browser.close()
            except FetchError as error:
                if error.code == "access_blocked" and not error.retry_after:
                    error.retry_after = self._restrict(market)
                raise
            except Error as error:
                if "ERR_CERT_AUTHORITY_INVALID" in str(error):
                    raise FetchError(
                        "tls_untrusted",
                        "Chromium не смог проверить сертификат HTTPS-прокси/сайта. Проверьте доверенные CA и доступ к NSS-хранилищу браузера; проверка TLS не отключается",
                    ) from None
                raise FetchError(
                    "browser_error",
                    "Браузер не смог загрузить карточку; проверьте журнал и установку Chromium",
                ) from None

    async def close(self):
        await self.client.aclose()


def json_data(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise FetchError("schema_changed", "Ответ не содержит ожидаемый JSON") from None
