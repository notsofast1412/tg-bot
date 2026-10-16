import httpx
import pytest

from paintbot.marketplaces import OzonAdapter
from paintbot.marketplaces.links import MarketError, from_article, resolve_link
from paintbot.services.batch import Batch, collect
from tests.test_adapters import html


async def test_short_link_then_same_card_redirect_and_batch_dedup(store):
    ref = from_article("ozon", "123")
    calls = []
    def respond(request):
        calls.append(str(request.url))
        if request.url.path.startswith("/t/"):
            return httpx.Response(301, headers={"Location": "https://www.ozon.ru/product/paint-123/?from=share"})
        if not request.url.query:
            return httpx.Response(307, headers={"Location": "?__rr=1"})
        return httpx.Response(200, text=html(ref))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OzonAdapter(client=client, gap=0)
        adapters = {"ozon": adapter}
        assert await resolve_link("https://ozon.ru/t/abc123", adapters) == ref
        assert (await adapter.fetch(ref)).price == 38950
        batch = Batch()
        await collect("https://ozon.ru/t/abc123 " + ref.url, batch, adapters, store)
        assert len(batch.cards) == 1 and batch.skipped == 1 and not batch.errors
        assert batch.cards[0].url == ref.url


@pytest.mark.parametrize("target", [
    "https://evil.com/product/123/", "http://127.0.0.1/", "http://www.ozon.ru/product/123/",
    "https://www.ozon.ru/product/456/", "https://www.ozon.ru:443/product/123/",
    "https://user:pass@www.ozon.ru/product/123/", "https://www.ozon.ru/security/",
])
async def test_card_redirect_rejects_unsafe_or_changed_identity(target):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": target})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MarketError):
            await OzonAdapter(client=client, gap=0).fetch(from_article("ozon", "123"))
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["https://evil.com/t/abc", "https://www.wildberries.ru/catalog/123/detail.aspx",
                                    "http://www.ozon.ru/product/123/", "https://ozon.ru"])
async def test_short_redirect_rejects_non_ozon_card(target):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(301, headers={"Location": target})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MarketError):
            await OzonAdapter(client=client, gap=0).resolve_short("https://ozon.ru/t/abc123")
    assert len(calls) == 1


async def test_short_link_with_appended_time_is_not_silently_corrected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected network request"))) as client:
        with pytest.raises(MarketError):
            await resolve_link("https://ozon.ru/t/8M08OsF14:20", {"ozon": OzonAdapter(client=client, gap=0)})


async def test_redirect_loop_is_bounded():
    ref = from_article("ozon", "123")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": ref.url}))) as client:
        with pytest.raises(MarketError, match="Цикл"):
            await OzonAdapter(client=client, gap=0).fetch(ref)
