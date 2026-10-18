from unittest.mock import AsyncMock

import pytest

from paintwatch.config import Config
from paintwatch.database.models import Base
from paintwatch.database.store import Store
from paintwatch.domain import Snapshot
from paintwatch.links import parse_link


@pytest.fixture
def config(tmp_path):
    return Config(
        _env_file=None,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        owner_id=42,
        request_gap=1,
    )


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    Base.metadata.create_all(store.engine)
    store.seed()
    yield store
    store.engine.dispose()


@pytest.fixture
def ref():
    return parse_link("https://www.wildberries.ru/catalog/123456/detail.aspx")


@pytest.fixture
def snapshot(ref):
    return Snapshot(ref, "Краска", 42000, source="fixture")


@pytest.fixture
def product(store, snapshot):
    ids, _ = store.add_batch(
        [snapshot], "Моя краска", store.tags()[0]["id"], None, {"wb": 600, "ozon": 600}
    )
    return ids[0]


@pytest.fixture
def fake_adapters():
    async def fetch(ref):
        return Snapshot(ref, "Краска " + ref.article, 42000, source="fixture")

    return {m: AsyncMock(fetch=AsyncMock(side_effect=fetch)) for m in ("wb", "ozon")}
