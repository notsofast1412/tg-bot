import pytest
from alembic import command
from alembic.config import Config
from paintbot.database.store import Store
from paintbot.marketplaces.base import Card


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    db = Store(path)
    db.seed()
    yield db
    db.engine.dispose()


@pytest.fixture
def card():
    return Card("wb", "123", "https://www.wildberries.ru/catalog/123/detail.aspx", "Краска", "123", 42000, source="test")


@pytest.fixture
def product_id(store, card):
    return store.add_batch([card], "Моя краска", store.tags()[0].id)[0][0]
