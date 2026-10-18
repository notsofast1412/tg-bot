import asyncio
from dataclasses import replace

import pytest

from paintwatch.database.models import Product
from paintwatch.domain import FetchError, timestamp
from paintwatch.services import BatchService, Monitor


async def test_batch_adjacent_partial_errors_and_duplicates(
    store, config, fake_adapters
):
    service = BatchService(store, fake_adapters, config)
    a = "https://wildberries.ru/catalog/123/detail.aspx"
    b = "https://ozon.ru/product/123/"
    snaps, errors, skipped = await service.collect(
        a + b + a + "\nhttps://evil.test/", []
    )
    assert len(snaps) == 2 and skipped == 1 and len(errors) == 1
    ids, _ = store.add_batch(
        [
            __import__("paintwatch.domain", fromlist=["Snapshot"]).Snapshot.from_dict(s)
            for s in snaps
        ],
        "Пакет",
        1,
        None,
        {"wb": 600, "ozon": 600},
    )
    assert len(ids) == 2
    assert (await service.collect(a, []))[2] == 1


async def test_card_without_price_can_be_added(store, config, fake_adapters, snapshot):
    fake_adapters["wb"].fetch.return_value = replace(
        snapshot, price=None, issue="нет цены"
    )
    fake_adapters["wb"].fetch.side_effect = None
    service = BatchService(store, fake_adapters, config)
    snaps, errors, _ = await service.collect(snapshot.ref.url, [])
    assert len(snaps) == 1 and not errors


def test_draft_survives_restart_expires_and_cancels(store, config):
    from paintwatch.database.models import Draft
    from paintwatch.database.store import Store

    store.save_draft(42, {"step": "links"}, 60)
    reopened = Store(str(store.engine.url))
    assert reopened.draft(42)["step"] == "links"
    with store.transaction() as session:
        session.get(Draft, 42).expires_at = timestamp() - 1
    assert reopened.draft(42) == {"expired": True}
    assert reopened.draft(42) is None
    store.save_draft(42, {"step": "confirm"}, 60)
    store.clear_draft(42)
    assert store.draft(42) is None
    reopened.engine.dispose()


async def test_scheduler_independent_markets_and_manual_respects_due(
    store, config, fake_adapters, snapshot
):
    from paintwatch.links import article_ref

    other = replace(snapshot, ref=article_ref("ozon", "999"))
    ids, _ = store.add_batch(
        [snapshot, other], "Краска", 1, None, {"wb": 600, "ozon": 600}
    )
    with store.transaction() as session:
        for id in ids:
            session.get(Product, id).next_check = 0
    fake_adapters["wb"].fetch.side_effect = FetchError("network", "Нет сети")
    monitor = Monitor(store, fake_adapters, config)
    assert await monitor.check_due() == 2
    assert store.get_product(ids[0])["status"] == "network"
    assert store.get_product(ids[1])["status"] == "ok"
    assert await monitor.check_due() == 0


async def test_scheduler_prevents_parallel_checks(store, config, fake_adapters):
    monitor = Monitor(store, fake_adapters, config)
    await monitor.lock.acquire()
    assert await monitor.check_due() is None
    monitor.lock.release()


async def test_monitor_lifecycle_and_backup(store, config, fake_adapters):
    monitor = Monitor(store, fake_adapters, config)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.03)
    assert monitor.running and monitor.heartbeat
    assert list((config.data_dir / "backups").glob("*.sqlite3"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not monitor.running
