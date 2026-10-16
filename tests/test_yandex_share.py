import httpx
import pytest

from paintbot.marketplaces import YandexMarketAdapter
from paintbot.marketplaces.links import MarketError, parse_link, resolve_link
from paintbot.services.batch import Batch, collect
from tests.test_adapters import html

CARD = "https://market.yandex.ru/card/test-paint/4841362188"
OFFER = "wsz4128JPab4V9Bx9uvJ7w"
SELECTED = CARD + "?offerid=" + OFFER + "&publicId=shop123"


def test_modern_card_offer_and_seller_identity():
    ref = parse_link(SELECTED + "&do-waremd5=" + OFFER + "&cpc=tracking&utm_medium=sharing")
    assert ref.url == SELECTED
    assert ref.card_id == "card:4841362188|offerid:" + OFFER + "|publicId:shop123"
    assert parse_link(CARD + "?do-waremd5=" + OFFER + "&publicId=shop123") == ref
    assert parse_link(SELECTED.replace(OFFER, "anotherOffer")) != ref
    assert parse_link(SELECTED.replace("shop123", "shop456")) != ref


@pytest.mark.parametrize("suffix", ["?offerid=a&do-waremd5=b", "?offerid=a&offerid=b", "?publicId=", "?do-waremd5=", "?sku="])
def test_reject_ambiguous_or_empty_selectors(suffix):
    with pytest.raises(MarketError):
        parse_link(CARD + suffix)


async def test_short_link_batch_and_price_with_exact_offer_url(store):
    ref = parse_link(SELECTED)
    def respond(request):
        if request.url.path.startswith("/cc/"):
            return httpx.Response(302, headers={"Location": SELECTED + "&utm_medium=sharing"})
        return httpx.Response(200, text=html(ref, sku="4841362188"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YandexMarketAdapter(client=client, gap=0)
        adapters = {"yandex": adapter}
        assert await resolve_link("https://market.yandex.ru/cc/BKAgAV", adapters) == ref
        batch = Batch()
        await collect("https://market.yandex.ru/cc/BKAgAV " + SELECTED, batch, adapters, store)
        assert len(batch.cards) == 1 and batch.skipped == 1 and not batch.errors
        assert batch.cards[0].price == 38950  # synthetic; not a real marketplace price


@pytest.mark.parametrize("location", ["https://evil.com/card/a/123", "http://market.yandex.ru/card/a/123",
    "https://www.ozon.ru/product/123/", "https://user:pass@market.yandex.ru/card/a/123",
    "https://market.yandex.ru:443/card/a/123", "https://market.yandex.ru/search?text=paint"])
async def test_short_link_does_not_follow_unsafe_destination(location):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": location})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MarketError):
            await YandexMarketAdapter(client=client, gap=0).resolve_short("https://market.yandex.ru/cc/BKAgAV")
    assert len(calls) == 1


@pytest.mark.parametrize("short", [True, False])
async def test_captcha_stops_before_following(short):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": "https://market.yandex.ru/showcaptcha?token=opaque"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YandexMarketAdapter(client=client, gap=0)
        with pytest.raises(MarketError, match="CAPTCHA") as caught:
            if short:
                await adapter.resolve_short("https://market.yandex.ru/cc/BKAgAV")
            else:
                await adapter.fetch(parse_link(SELECTED))
        assert "opaque" not in str(caught.value)
    assert len(calls) == 1


async def test_selected_offer_cannot_be_replaced_by_generic_card_price():
    async with httpx.AsyncClient() as client:
        with pytest.raises(MarketError):
            YandexMarketAdapter(client=client).parse_html(parse_link(SELECTED), html(parse_link(CARD), sku="4841362188"))


async def test_card_redirect_cannot_drop_offer_or_seller():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": CARD}))) as client:
        with pytest.raises(MarketError, match="другой товар"):
            await YandexMarketAdapter(client=client, gap=0).fetch(parse_link(SELECTED))
