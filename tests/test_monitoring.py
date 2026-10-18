from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from paintwatch.database.models import (
    CheckLog,
    Notification,
    NotificationState,
    PriceHistory,
)
from paintwatch.database.store import Store
from paintwatch.domain import FetchError, Snapshot, timestamp


def count(store, model):
    with store.sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_all_exact_tags_and_idempotent_seed(store):
    expected = {
        "#arton": 400,
        "#2arton": 750,
        "#6arton": 2200,
        "#belazo": 350,
        "#2belazo": 700,
        "#6belazo": 2000,
        "#trane": 350,
        "#3trane": 1000,
        "#6trane": 2000,
        "#flame": 500,
        "#molotow400": 5000,
        "#burner": 800,
        "#joggle": 300,
        "#2jiggle": 600,
        "#6jiggle": 1800,
        "#titans": 230,
    }
    assert {t["name"]: t["threshold"] for t in store.tags()} == {
        k: v * 100 for k, v in expected.items()
    }
    tag = store.tags()[0]
    store.update_tag(tag["id"], 12300)
    store.seed()
    assert len(store.tags()) == 16
    assert store.tags()[0]["threshold"] == 12300


def test_strict_transition_and_error_preserves_state(store, product, snapshot):
    for price in [42000, 40000, 39900, 38000]:
        store.apply(product, replace(snapshot, price=price), None, 600)
    assert count(store, Notification) == 1
    store.apply(product, None, FetchError("access_blocked", "CAPTCHA"), 600)
    assert store.get_product(product)["last_price"] == 38000
    store.apply(product, replace(snapshot, price=37000), None, 600)
    assert count(store, Notification) == 1
    store.apply(product, replace(snapshot, price=41000), None, 600)
    store.apply(product, replace(snapshot, price=39000), None, 600)
    assert count(store, Notification) == 2
    with store.sessions() as session:
        assert session.get(NotificationState, product).generation == 2


def test_first_success_below_and_no_price_waiting(store, snapshot):
    pending = replace(snapshot, price=None, issue="Нет цены")
    ids, _ = store.add_batch([pending], "Краска", 1, None, {"wb": 600})
    p = ids[0]
    assert count(store, Notification) == 0
    assert store.get_product(p)["last_success"] is None
    store.apply(p, replace(snapshot, price=39900), None, 600)
    assert count(store, Notification) == 1


def test_only_changes_in_history_every_attempt_logged(store, product, snapshot):
    store.apply(product, snapshot, None, 600)
    store.apply(product, snapshot, None, 600)
    assert count(store, PriceHistory) == 1
    assert count(store, CheckLog) == 3


def test_identity_mismatch_and_stale_price_do_not_notify(store, product, snapshot):
    from paintwatch.links import article_ref

    store.apply(
        product, replace(snapshot, ref=article_ref("wb", "777"), price=10000), None, 600
    )
    assert store.get_product(product)["status"] == "identity_mismatch"
    store.apply(
        product,
        replace(snapshot, price=10000, observed_at=timestamp() - 121),
        None,
        600,
    )
    assert count(store, Notification) == 0
    assert store.get_product(product)["last_price"] == 42000


def test_tag_edit_rebases_without_alert_and_cancels_old_pending(
    store, product, snapshot
):
    store.apply(product, replace(snapshot, price=39000), None, 600)
    store.update_tag(1, 50000)
    store.apply(product, replace(snapshot, price=38000), None, 600)
    assert count(store, Notification) == 1
    with store.sessions() as session:
        assert session.scalar(select(Notification)).status == "cancelled"
    store.apply(product, replace(snapshot, price=51000), None, 600)
    store.apply(product, replace(snapshot, price=49000), None, 600)
    assert count(store, Notification) == 2


def test_custom_threshold_survives_tag_edit(store, product):
    store.update_product_threshold(product, 12300)
    store.update_tag(1, 99900)
    assert store.get_product(product)["threshold"] == 12300


def test_seller_change_not_silently_accepted_and_owner_metadata_persists(
    store, snapshot
):
    snapshot = replace(
        snapshot,
        seller_id="123",
        characteristics={"Владелец: Количество в упаковке": "6"},
    )
    ids, _ = store.add_batch([snapshot], "Краска", 1, None, {"wb": 600})
    store.apply(ids[0], replace(snapshot, characteristics={}, price=43000), None, 600)
    assert (
        store.get_product(ids[0])["details"]["characteristics"][
            "Владелец: Количество в упаковке"
        ]
        == "6"
    )
    store.apply(ids[0], replace(snapshot, seller_id="999", price=39000), None, 600)
    assert store.get_product(ids[0])["status"] == "seller_changed"
    assert store.get_product(ids[0])["last_price"] == 43000
    assert count(store, Notification) == 0


def test_duplicate_batch_and_repeat_save(store, snapshot):
    ids, skipped = store.add_batch([snapshot, snapshot], "Краска", 1, None, {"wb": 600})
    assert len(ids) == 1 and skipped == 1
    assert store.add_batch([snapshot], "Краска", 1, None, {"wb": 600}) == ([], 1)


def test_concurrent_checks_single_logical_alert(store, product, snapshot):
    with ThreadPoolExecutor(4) as executor:
        list(
            executor.map(
                lambda _: store.apply(
                    product, replace(snapshot, price=39000), None, 600
                ),
                range(8),
            )
        )
    assert count(store, Notification) == 1


def test_restart_schedule_state_and_queue(store, product, snapshot):
    store.apply(product, replace(snapshot, price=39000), None, 600)
    fresh = Store(str(store.engine.url))
    assert fresh.get_product(product)["next_check"] > timestamp()
    fresh.apply(product, replace(snapshot, price=38000), None, 600)
    assert count(fresh, Notification) == 1
    fresh.set_group(-100123)
    event = fresh.claim_notification()
    assert event["status"] == "sending"
    fresh.recover_queue()
    assert fresh.uncertain_notifications()[0]["id"] == event["id"]
    assert fresh.claim_notification() is None
    fresh.engine.dispose()


def test_failure_backoff_and_pause(store, product):
    store.apply(product, None, FetchError("network", "Нет сети"), 600)
    p = store.get_product(product)
    assert 595 <= p["next_check"] - timestamp() <= 600
    store.apply(product, None, FetchError("network", "Нет сети"), 600)
    assert store.get_product(product)["next_check"] - timestamp() >= 1195
    store.toggle(product)
    assert not store.get_product(product)["active"]
    assert store.due() == []


@pytest.mark.parametrize("price", [0, -1, True, 1.5])
def test_bad_prices_rejected(ref, price):
    with pytest.raises(FetchError):
        Snapshot(ref, "Краска", price)
