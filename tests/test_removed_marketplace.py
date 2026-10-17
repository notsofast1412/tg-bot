from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from paintbot.database.models import NotificationQueue, Product
from paintbot.marketplaces.links import MarketError, from_article, parse_link, resolve_link
from paintbot.scheduler.monitor import Monitor


@pytest.mark.parametrize('url', [
    'https://market.yandex.ru/product/123',
    'https://market.yandex.ru/card/paint/4841362188?sku=123',
    'https://market.yandex.ru/offer/abcDEF-123',
    'https://market.yandex.ru/cc/BKAgAV',
])
async def test_removed_marketplace_links_are_rejected_without_requests(url):
    with pytest.raises(MarketError):
        parse_link(url)
    with pytest.raises(MarketError):
        await resolve_link(url, {})


@pytest.mark.parametrize('article', ['123', 'abcDEF-123'])
def test_removed_marketplace_articles_are_rejected(article):
    with pytest.raises(MarketError):
        from_article('yandex', article)


async def test_legacy_products_stop_without_losing_history(store, card, product_id):
    # Emulate a database written by the previous version.
    store.record(product_id, replace(card, price=100), None, 600, now=1)
    with store.tx() as session:
        product = session.get(Product, product_id)
        product.marketplace = 'yandex'
        product.url = 'https://market.yandex.ru/product/123'
        product.next_check = 0
    history = [(p.price, p.checked_at) for p in store.history(product_id)[0]]
    adapters = {'wb': AsyncMock(), 'ozon': AsyncMock()}
    assert await Monitor(store, adapters).cycle() == 0
    assert store.claim_notification(100) is None

    store.seed()
    store.seed()
    legacy = store.product(product_id)
    assert not legacy.active
    assert legacy.status == 'Поддержка площадки прекращена'
    assert [(p.price, p.checked_at) for p in store.history(product_id)[0]] == history
    assert set(store.statistics()['markets']) == {'wb', 'ozon'}
    assert store.queue() == []
    with store.tx() as session:
        notification = session.scalar(select(NotificationQueue))
        assert notification.state == 'cancelled'
        notification.state = 'failed'
        queue_id = notification.id
    with pytest.raises(ValueError, match='Поддержка площадки прекращена'):
        store.set_active(product_id, True)
    with pytest.raises(ValueError, match='Поддержка площадки прекращена'):
        store.retry_notification(queue_id)
    assert store.claim_notification(100) is None
    with pytest.raises(ValueError, match='WB и Ozon'):
        store.add_batch([replace(card, marketplace='yandex', card_id='456')], 'test', store.tags()[0].id)

    # Remaining marketplaces continue to be scheduled and produce notifications.
    new_id = store.add_batch([replace(card, card_id='456')], 'test', store.tags()[0].id)[0][0]
    assert [p.id for p in store.due(100)] == [new_id]
    store.record(new_id, replace(card, card_id='456', price=100), None, 600, now=1)
    assert store.claim_notification(100).product_id == new_id
