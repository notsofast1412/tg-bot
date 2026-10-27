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
    async def test_wb_payment_label_can_be_split_between_html_elements(self):
        await self.page.set_content(
            '<h1>Краска</h1><div class="price-block"><div><span>350 ₽</span><span>с <b>WB</b> Кошельком</span></div><div><span>380 ₽</span><span>без <b>WB</b> Кошелька</span></div></div>'
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"wb"})'
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["price"], 35000)

    async def test_hidden_wallet_price_must_not_be_used(self):
        await self.page.set_content(
            '<h1>Краска</h1><div class="price-block"><div style="display:none"><span>999 ₽</span><span>с WB Кошельком</span></div><div><span>350 ₽</span><span>без WB Кошелька</span></div></div>'
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"wb"})'
        )
        self.assertIsNone(result)

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
        self.assertEqual(result["price"], 35000)
        self.assertEqual(result["price_kind"], "wb_wallet")

    async def test_content_observer_reads_dynamic_price_before_page_finishes(self):
        requests = []

        async def route(request_route):
            requests.append(request_route.request.url)
            await request_route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><h1>Краска</h1><div id='root'></div></html>",
            )

        await self.page.route("**/*", route)
        await self.page.goto(
            "https://www.wildberries.ru/catalog/708052242/detail.aspx",
            wait_until="domcontentloaded",
        )
        await self.page.evaluate(
            "window.chrome={runtime:{onMessage:{addListener:fn=>window.collectListener=fn}}}"
        )
        folder = Path(__file__).resolve().parent.parent / "extension"
        await self.page.add_script_tag(path=str(folder / "extractor.js"))
        await self.page.add_script_tag(path=str(folder / "content.js"))
        result = await self.page.evaluate("""async () => {
            const state = await new Promise(resolve => collectListener({type:'paintwatch.ready'}, {}, resolve));
            setTimeout(() => { document.querySelector('#root').innerHTML = '<div><span class="priceBlockWalletPrice-fixture">380 ₽</span></div>'; }, 40);
            return new Promise(resolve => collectListener({type:'paintwatch.collect', job:{
                market:'wb',article:'708052242',variant:'',documentId:state.documentId,
                timeout_ms:3000,deadline:Date.now()+3000
            }}, {}, resolve));
        }""")
        self.assertEqual(result["price"], 38000)
        self.assertEqual(result["price_kind"], "wb_wallet")
        self.assertEqual(len(requests), 1)

    async def test_wb_wallet_class_from_user_markup_without_legacy_container(self):
        await self.page.set_content(
            '<h1>Краска</h1><section><span class="mo-typography mo-typography_variant_description '
            'mo-typography_variable-weight_description mo-typography_color_danger priceBlockWalletPrice-E4q0H">261&nbsp;₽</span>'
            "<span>270 ₽</span><del>379 ₽</del></section>"
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"wb"})'
        )
        self.assertEqual(result["price"], 26100)
        self.assertEqual(result["price_kind"], "wb_wallet")
        diagnostic = await self.page.evaluate(
            'PaintWatchExtract.diagnose(document,{market:"wb",article:"708052242",variant:""})'
        )
        self.assertEqual(diagnostic["matched"]["price"], 26100)
        self.assertEqual(diagnostic["walletNodes"][0]["prices"], [26100])
        self.assertEqual(diagnostic["roots"], [])

    async def test_wb_card_heading_in_button_before_non_card_final_price_from_user_markup(
        self,
    ):
        await self.page.set_content(
            '<h1>Краска</h1><section><button><div><div><h2 class="mo-typography mo-typography_variant_title2 '
            'mo-typography_variable-weight_title2 mo-typography_colors_danger">261&nbsp;₽</h2></div></div></button>'
            '<ins class="mo-typography mo-typography_variant_body mo-typography_variable-weight_body '
            'mo-typography_colors_primary priceBlockFinalPrice--yEKcc">270&nbsp;₽</ins><del>379 ₽</del></section>'
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        result = await self.page.evaluate(
            'PaintWatchExtract.visible(document,{market:"wb"})'
        )
        self.assertEqual(result["price"], 26100)
        self.assertEqual(result["price_kind"], "wb_wallet")

    async def test_wb_does_not_substitute_regular_struck_hidden_or_ambiguous_prices(
        self,
    ):
        script = str(
            Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
        )
        fixtures = [
            '<span class="price-block__final-price">270 ₽</span><del>379 ₽</del>',
            '<span class="mo-typography_color_danger">261 ₽</span>',
            '<del class="priceBlockWalletPrice-test">261 ₽</del>',
            '<span class="priceBlockWalletPrice-test" style="display:none">261 ₽</span><span>270 ₽</span>',
            '<span class="priceBlockWalletPrice-a">261 ₽</span><span class="priceBlockWalletPrice-b">260 ₽</span>',
            '<span class="prefixpriceBlockWalletPrice-test">261 ₽</span>',
            '<span class="priceBlockWalletPrice-test">261 ₽ 270 ₽</span>',
            '<h2 class="mo-typography_colors_danger">261 ₽</h2><ins class="priceBlockFinalPrice-test">270 ₽</ins>',
            '<button><h2>261 ₽</h2><h2>250 ₽</h2></button><ins class="priceBlockFinalPrice-test">270 ₽</ins>',
            '<button><h2>379 ₽</h2></button><ins class="priceBlockFinalPrice-test">270 ₽</ins>',
            '<button style="display:none"><h2>261 ₽</h2></button><ins class="priceBlockFinalPrice-test">270 ₽</ins>',
        ]
        for html in fixtures:
            with self.subTest(html=html):
                await self.page.set_content("<h1>Краска</h1>" + html)
                await self.page.add_script_tag(path=script)
                self.assertIsNone(
                    await self.page.evaluate(
                        'PaintWatchExtract.visible(document,{market:"wb"})'
                    )
                )

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
