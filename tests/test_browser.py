"""Real Chromium with intercepted fixtures; no external network or live prices."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from paintwatch.browser import BrowserCollector
from paintwatch.domain import PriceError, parse_ref
from paintwatch.store import Store


@unittest.skipUnless(
    os.getenv("PAINTWATCH_TEST_BROWSER"),
    "Set PAINTWATCH_TEST_BROWSER for Chromium fixture integration",
)
class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_visible_price_requires_explicit_payment_label(self):
        await self.page.set_content(
            '<h1>Краска</h1><div data-widget="webPrice"><div><span>655 ₽</span><span>с Ozon Картой</span></div><div>718 ₽ обычная цена</div></div>'
        )
        script = Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
        await self.page.add_script_tag(path=str(script))
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"ozon"})'
        )
        self.assertEqual(result["price"], 65500)
        self.assertEqual(result["price_kind"], "ozon_card")
        await self.page.set_content(
            '<h1>Краска</h1><div data-widget="webPrice">718 ₽ обычная цена</div>'
        )
        await self.page.add_script_tag(path=str(script))
        self.assertIsNone(
            await self.page.evaluate(
                'PaintWatchExtract.visible(document,{market:"ozon"})'
            )
        )
        await self.page.set_content(
            '<h1>Краска</h1><div class="price-block"><div>350 ₽ с WB Кошельком</div><div><span>380 ₽</span><span>без WB Кошелька</span></div></div>'
        )
        await self.page.add_script_tag(path=str(script))
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"wb"})'
        )
        self.assertEqual(result["price"], 38000)
        self.assertEqual(result["price_kind"], "wb_product")

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.pw = await async_playwright().start()
        # This container-only fixture browser never contacts external hosts.
        # Production uses headed Chromium with chromium_sandbox=True.
        self.browser = await self.pw.chromium.launch(
            executable_path=os.environ["PAINTWATCH_TEST_BROWSER"], headless=True
        )
        self.page = await self.browser.new_page()
        self.collector = BrowserCollector(Path(self.temp.name), self.store)
        self.collector._page = AsyncMock(return_value=self.page)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def test_ozon_same_browser_session_and_card_price(self):
        requests = []

        async def route(request_route):
            request = request_route.request
            requests.append(request.url)
            if "/api/composer-api.bx/" in request.url:
                self.assertEqual(
                    await self.page.evaluate('localStorage.getItem("fixture-session")'),
                    "verified",
                )
                data = {
                    "widgetStates": {
                        "webStickyProducts-1": json.dumps(
                            {"sku": "196525150", "name": "Краска"}
                        ),
                        "webPrice-1": json.dumps(
                            {"cardPrice": "655 ₽", "price": "718 ₽"}
                        ),
                    }
                }
                await request_route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(data)
                )
            else:
                await request_route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<html><script>localStorage.setItem("fixture-session","verified")</script><body>Product</body></html>',
                )

        await self.page.route("**/*", route)
        self.store.add([parse_ref("196525150", "ozon")], "", "#arton")
        result = await self.collector.fetch(self.store.products()[0])
        self.assertEqual(result.price, 65500)
        self.assertEqual(len(requests), 2)

    async def test_wb_uses_page_response_without_additional_api_request(self):
        api_calls = []

        async def route(request_route):
            if "/cards/v4/detail" in request_route.request.url:
                api_calls.append(request_route.request.url)
                data = {
                    "products": [
                        {
                            "id": 29220564,
                            "name": "Краска",
                            "sizes": [
                                {
                                    "optionId": 1,
                                    "price": {"product": 35000},
                                    "stocks": [{"qty": 2}],
                                }
                            ],
                        }
                    ]
                }
                await request_route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(data)
                )
            else:
                await request_route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<html><script>fetch("/__internal/u-card/cards/v4/detail?nm=29220564")</script><body>Product</body></html>',
                )

        await self.page.route("**/*", route)
        self.store.add([parse_ref("29220564", "wb")], "", "#arton")
        result = await self.collector.fetch(self.store.products()[0])
        self.assertEqual(result.price, 35000)
        self.assertEqual(len(api_calls), 1)

    async def test_challenge_stops_before_api_and_keeps_page_open(self):
        requests = []

        async def route(request_route):
            requests.append(request_route.request.url)
            await request_route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<html><title>Проверка браузера</title><body>Challenge</body></html>",
            )

        await self.page.route("**/*", route)
        self.store.add([parse_ref("196525150", "ozon")], "", "#arton")
        with self.assertRaises(PriceError) as error:
            await self.collector.fetch(self.store.products()[0])
        self.assertEqual(error.exception.code, "access")
        self.assertEqual(len(requests), 1)
        self.assertFalse(self.page.is_closed())

    async def test_wb_api_rejection_does_not_trigger_another_request(self):
        api_calls = []

        async def route(request_route):
            if "/cards/v4/detail" in request_route.request.url:
                api_calls.append(request_route.request.url)
                await request_route.fulfill(
                    status=429, headers={"retry-after": "1200"}, body=""
                )
            else:
                await request_route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<html><script>fetch("/__internal/u-card/cards/v4/detail?nm=29220564")</script></html>',
                )

        await self.page.route("**/*", route)
        self.store.add([parse_ref("29220564", "wb")], "", "#arton")
        with self.assertRaises(PriceError) as error:
            await self.collector.fetch(self.store.products()[0])
        self.assertEqual(error.exception.code, "rate_limit")
        self.assertEqual(error.exception.retry_after, 1200)
        self.assertEqual(len(api_calls), 1)
