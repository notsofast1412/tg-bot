import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from paintbot.database.models import NotificationQueue, NotificationState, PriceHistory
from paintbot.database.store import TAGS, Store
from paintbot.scheduler.monitor import Monitor
from paintbot.services.runtime import instance_lock
from scripts.manage import backup


def queued(store):
    with store.tx() as s:
        return list(s.scalars(select(NotificationQueue).order_by(NotificationQueue.id)))


def test_strict_edges_history_errors_and_restart(store, card, product_id):
    for i, price in enumerate([42000, 40000, 39900, 38000]):
        store.record(product_id, replace(card, price=price), None, 600, now=1000+i)
    assert len(queued(store)) == 1
    store.record(product_id, None, "HTTP 403", 600, now=1010)
    assert store.product(product_id).last_price == 38000
    assert store.product(product_id).last_success == 1003
    restarted = Store(store.path)
    restarted.recover()
    restarted.record(product_id, replace(card, price=37000), None, 600, now=1020)
    assert len(queued(store)) == 1
    restarted.record(product_id, replace(card, price=41000), None, 600, now=1030)
    restarted.record(product_id, replace(card, price=39000), None, 600, now=1040)
    restarted.record(product_id, replace(card, price=39000), None, 600, now=1050)
    assert len(queued(store)) == 2
    history, errors = store.history(product_id)
    assert len(history) == 7  # unchanged price isn't an additional change
    assert len(errors) == 1
    assert store.product(product_id).last_changed == 1040
    restarted.engine.dispose()


def test_first_below_and_queue_uniqueness(store, card, product_id):
    store.record(product_id, replace(card, price=39900), None, 600)
    store.record(product_id, replace(card, price=39900), None, 600)
    assert len(queued(store)) == 1
    assert "Изменение" not in queued(store)[0].text


def test_tag_seed_and_exact_names(store):
    assert {t.name: t.threshold for t in store.tags()} == {k: v*100 for k, v in TAGS.items()}
    assert len(store.tags()) == 16
    store.edit_threshold("#arton", 12300)
    store.seed()
    assert store.tags()[0].threshold == 12300


def test_threshold_edit_baseline_without_flood(store, card, product_id):
    store.record(product_id, replace(card, price=39900), None, 600)
    store.edit_threshold("#arton", 50000)
    assert queued(store)[0].state == "cancelled"
    store.record(product_id, replace(card, price=39800), None, 600)
    assert len(queued(store)) == 1
    store.record(product_id, replace(card, price=51000), None, 600)
    store.record(product_id, replace(card, price=49000), None, 600)
    assert len(queued(store)) == 2


def test_tag_change_before_first_price_still_notifies(store, card, product_id):
    store.edit_threshold("#arton", 50000)
    store.record(product_id, replace(card, price=49000), None, 600)
    assert len(queued(store)) == 1


@pytest.mark.parametrize("price", [None, 0, -1, True, 1.5])
def test_invalid_price_never_notifies(store, card, product_id, price):
    store.record(product_id, replace(card, price=price), None, 600)
    assert store.product(product_id).last_price is None
    assert not queued(store)


def test_wrong_identity_never_updates(store, card, product_id):
    store.record(product_id, replace(card, card_id="456", price=100), None, 600)
    assert store.product(product_id).last_price is None


def test_batch_transaction_duplicates_cross_market(store, card):
    tag = store.tags()[0]
    ids, skipped = store.add_batch([card, card, replace(card, marketplace="ozon")], "краска", tag.id)
    assert len(ids) == 2 and skipped == 1
    ids, skipped = store.add_batch([card], "ещё раз", tag.id)
    assert not ids and skipped == 1


def test_pause_filters_and_schedule(store, card, product_id):
    assert len(store.due(0)) == 1
    store.record(product_id, None, "network", 600, now=100)
    assert not store.due(699)
    assert len(store.due(700)) == 1
    store.set_active(product_id, False)
    assert not store.due(10000)
    assert store.products(status="paused")[1] == 1
    assert store.products(market="ozon")[1] == 0
    assert store.products(tag="#arton", status="error")[1] == 1


async def test_monitor_independent_adapters(store, card, product_id):
    ozon = replace(card, marketplace="ozon", url="https://www.ozon.ru/product/123/")
    store.add_batch([ozon], "Ozon", store.tags()[0].id)
    adapters = {"wb": AsyncMock(), "ozon": AsyncMock()}
    adapters["wb"].fetch.side_effect = RuntimeError("broken")
    adapters["ozon"].fetch.return_value = replace(ozon, price=39000)
    monitor = Monitor(store, adapters)
    assert await monitor.cycle() == 2
    assert store.product(product_id).failures == 1
    assert len(queued(store)) == 1
    assert await monitor.cycle() == 0


async def test_overlapping_cycles_do_not_duplicate(store, card, product_id):
    gate = asyncio.Event()

    async def fetch(_):
        await gate.wait()
        return replace(card, price=39000)

    adapter = AsyncMock()
    adapter.fetch.side_effect = fetch
    monitor = Monitor(store, {"wb": adapter})
    first = asyncio.create_task(monitor.cycle())
    await asyncio.sleep(0)
    assert await monitor.cycle() == 0
    gate.set()
    assert await first == 1
    assert len(queued(store)) == 1


def test_consistent_live_backup(store, product_id, tmp_path):
    target = tmp_path / "backup.db"
    backup(store.path, target)
    copied = Store(target)
    assert copied.product(product_id).title == "Краска"
    copied.engine.dispose()
    with pytest.raises(FileExistsError):
        backup(store.path, target)


def test_instance_lock(store):
    with instance_lock(store.path):
        with pytest.raises(RuntimeError):
            with instance_lock(store.path):
                pass


def test_repeated_update(store):
    assert not store.seen_update(123)
    assert store.seen_update(123)
