"""Real Chromium with intercepted fixtures; no external network or live prices."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from paintwatch.browser import BrowserCollector
from paintwatch.bridge import BrowserBridge
from paintwatch.domain import PriceError, parse_ref
from paintwatch.store import Store


@unittest.skipUnless(
    os.getenv("PAINTWATCH_TEST_BROWSER"),
    "Set PAINTWATCH_TEST_BROWSER for Chromium fixture integration",
)
class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wb_color_price_can_have_nested_payment_labels(self):
        await self.page.set_content(
            '<h1>Краска</h1><div class="price-block"><div><span class="mo-typography_colors_danger">350 ₽</span><span>с <b>WB</b> Кошельком</span></div><div><span>380 ₽</span><span>без <b>WB</b> Кошелька</span></div></div>'
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
            '<h1>Краска</h1><div class="price-block"><div style="display:none"><span class="mo-typography_colors_danger">999 ₽</span><span>с WB Кошельком</span></div><div><span>350 ₽</span><span>без WB Кошелька</span></div></div>'
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
            '<h1>Краска</h1><div class="price-block"><div><span class="mo-typography_colors_danger">350 ₽</span> с WB Кошельком</div><div><span>380 ₽</span><span>без WB Кошелька</span></div></div>'
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
            setTimeout(() => { document.querySelector('#root').innerHTML = '<div><span class="mo-typography_colors_danger">380 ₽</span></div>'; }, 40);
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
        self.assertEqual(len(diagnostic["roots"]), 1)

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

    async def test_wb_without_h1_accepts_nested_wallet_price_and_later_updates_name(
        self,
    ):
        ref = parse_ref("708059747", "wb")
        requests = []

        async def route(request_route):
            requests.append(request_route.request.url)
            await request_route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    '<html><body><button class="mo-button priceBlockWalletPrice-dxLZN"><span class="mo-typography '
                    "mo-typography_variant_description mo-typography_variable-weight_description "
                    'mo-typography_colors_danger priceBlockWalletPrice--Eq4oH">261&nbsp;₽</span></button>'
                    '<span class="mo-typography priceBlockFinalPrice--B5CAH">270&nbsp;₽</span></body></html>'
                ),
            )

        await self.page.route("**/*", route)
        await self.page.goto(ref.url, wait_until="domcontentloaded")
        await self.page.evaluate(
            "window.chrome={runtime:{onMessage:{addListener:fn=>window.collectListener=fn}}}"
        )
        folder = Path(__file__).resolve().parent.parent / "extension"
        await self.page.add_script_tag(path=str(folder / "extractor.js"))
        await self.page.add_script_tag(path=str(folder / "content.js"))
        diagnostic = await self.page.evaluate(
            'PaintWatchExtract.diagnose(document,{market:"wb",article:"708059747",variant:""})'
        )
        self.assertFalse(diagnostic["titleFound"])
        self.assertEqual(diagnostic["nameSource"], "article")
        self.assertIsNone(diagnostic["reason"])
        self.assertEqual(diagnostic["matched"]["price"], 26100)
        self.assertEqual(len(diagnostic["walletNodes"]), 1)
        self.store.add([ref], "", "#arton")
        self.store.configure({"monitoring": 1})
        bridge = BrowserBridge(self.store, "fixture")
        for title in (None, "Краска Arton"):
            if title:
                await self.page.evaluate(
                    "document.body.insertAdjacentHTML('afterbegin','<h1>Краска Arton</h1>')"
                )
                self.store.request_scan()
            job = bridge.job()
            result = await self.page.evaluate(
                """job => new Promise(resolve => collectListener({type:'paintwatch.collect',
                job:{...job,deadline:Date.now()+3000}}, {}, resolve))""",
                job,
            )
            bridge.result({**result, "id": job["id"], "lease": job["lease"]})
            p = self.store.product(job["id"])
            self.assertEqual(p["price"], 26100)
            self.assertEqual(p["name"], title or ref.article)
        self.assertEqual(len(self.store.queue()), 1)
        self.assertEqual(len(requests), 1)

    async def test_wb_missing_title_does_not_make_unconfirmed_price_acceptable(self):
        await self.page.set_content(
            '<div style="color:red">261 ₽</div><span>270 ₽</span>'
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        diagnostic = await self.page.evaluate(
            'PaintWatchExtract.diagnose(document,{market:"wb",article:"708059747",variant:""})'
        )
        self.assertIsNone(diagnostic["matched"])
        self.assertFalse(diagnostic["titleFound"])
        self.assertIn("Цена по WB Карте", diagnostic["reason"])

    async def test_wb_card_price_is_stable_across_tags_wrappers_hashes_and_missing_title(
        self,
    ):
        script = str(
            Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
        )
        fixtures = [
            '<span class="mo-typography_colors_danger arbitrary-A">261&nbsp;₽</span><span>270 ₽</span>',
            '<button><h2 class="mo-typography_colors_danger">261 ₽</h2></button><ins>270 ₽</ins>',
            '<button><span><strong class="mo-typography_colors_danger random-B">261 ₽</strong></span></button><span>270 ₽</span>',
            '<div><button><p class="mo-typography_colors_danger">261 ₽</p></button></div><ins>270 ₽</ins>',
            '<span class="mo-typography_color_danger">261 ₽</span><span>270 ₽</span>',
            '<button class="mo-typography_colors_danger"><span class="mo-typography_colors_danger">261 ₽</span></button><span>270 ₽</span>',
        ]
        for card, regular, old in ((261, 270, 379), (444, 664, 869)):
            for n, template in enumerate(fixtures):
                html = template.replace("261", str(card)).replace("270", str(regular))
                for title in ("<h1>Краска</h1>", ""):
                    with self.subTest(n=n, title=bool(title), card=card):
                        await self.page.set_content(
                            title
                            + html
                            + f'<span class="priceBlockOldPrice--WPpN1">{old} ₽</span>'
                        )
                        await self.page.add_script_tag(path=script)
                        result = await self.page.evaluate(
                            '(article) => PaintWatchExtract.visible(document,{market:"wb",article,variant:""})',
                            str(708059747 + n),
                        )
                        self.assertEqual(result["price"], card * 100)
                        self.assertEqual(result["price_kind"], "wb_wallet")
                        self.assertEqual(
                            result["name"], "Краска" if title else str(708059747 + n)
                        )

    async def test_wb_diagnostic_distinguishes_conflicting_card_prices_from_missing_title(
        self,
    ):
        await self.page.set_content(
            '<span class="mo-typography_colors_danger">261 ₽</span><span class="mo-typography_colors_danger">260 ₽</span>'
        )
        await self.page.add_script_tag(
            path=str(
                Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
            )
        )
        result = await self.page.evaluate(
            'PaintWatchExtract.diagnose(document,{market:"wb",article:"708059747",variant:""})'
        )
        self.assertFalse(result["titleFound"])
        self.assertIsNone(result["matched"])
        self.assertEqual(set(result["cardPrices"]), {26100, 26000})
        self.assertEqual(result["reasonCode"], "ambiguous_card_price")

    async def test_wb_does_not_substitute_regular_struck_hidden_or_ambiguous_prices(
        self,
    ):
        script = str(
            Path(__file__).resolve().parent.parent / "extension" / "extractor.js"
        )
        fixtures = [
            "<span>270 ₽</span><del>379 ₽</del>",
            '<span style="color:red">261 ₽</span>',
            '<del class="mo-typography_colors_danger">261 ₽</del>',
            '<span class="mo-typography_colors_danger" style="display:none">261 ₽</span><span>270 ₽</span>',
            '<span class="mo-typography_colors_danger">261 ₽</span><span class="mo-typography_colors_danger">260 ₽</span>',
            '<span class="prefixmo-typography_colors_danger">261 ₽</span>',
            '<span class="mo-typography_colors_danger" style="text-decoration:line-through">869 ₽</span>',
            '<span aria-label="Цена по WB Карте">261 ₽</span>',
            '<span class="mo-typography_colors_danger">261 ₽ 270 ₽</span>',
            '<span class="mo-typography_colors_danger">Скидка 15 ₽</span>',
            '<span class="mo-typography_colors_danger">-15 ₽</span>',
            '<span class="mo-typography_colors_danger">15%</span>',
            '<button style="display:none"><h2 class="mo-typography_colors_danger">261 ₽</h2></button><ins>270 ₽</ins>',
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
