import logging
import sqlite3

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from paintwatch.cli import ROOT, migrate
from paintwatch.config import Config
from paintwatch.database.store import Store
from paintwatch.runtime import Redactor, instance_lock


def test_migrations_fresh_and_repeatable(config):
    migrate(config)
    migrate(config)
    store = Store(config.database_url)
    assert len(store.tags()) == 16
    with store.engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar()
            == "0001"
        )
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    alembic = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(ROOT / "migrations"))
    alembic.set_main_option("sqlalchemy.url", config.database_url)
    command.check(alembic)
    store.engine.dispose()


def test_backup_is_consistent_with_open_wal_database(store, product, tmp_path):
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT id FROM products").fetchone()[0] == product
    assert backup.stat().st_mode & 0o777 == 0o600


def test_single_instance(config):
    with instance_lock(config.data_dir):
        with pytest.raises(RuntimeError):
            with instance_lock(config.data_dir):
                pass
    with instance_lock(config.data_dir):
        pass


def test_secret_redaction():
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    formatter = Redactor(token)
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        "",
        0,
        f"https://api.telegram.org/bot{token}/sendMessage https://user:password@proxy.test",
        (),
        None,
    )
    rendered = formatter.format(record)
    assert token not in rendered and "password" not in rendered


def test_configuration_bounds():
    with pytest.raises(ValueError):
        Config(_env_file=None, check_interval=10)
    with pytest.raises(ValueError):
        Config(_env_file=None).require_telegram()


def test_cli_restore_retains_products_and_migrations(config, monkeypatch, capsys):
    import sys
    import paintwatch.cli as cli
    from paintwatch.domain import Snapshot
    from paintwatch.links import article_ref

    migrate(config)
    store = Store(config.database_url)
    store.add_batch(
        [Snapshot(article_ref("wb", "123"), "Краска", 42000)],
        "Моя краска",
        1,
        None,
        {"wb": 600},
    )
    backup = config.data_dir / "known-backup.sqlite3"
    store.backup(backup)
    store.update_tag(1, 99900)
    store.engine.dispose()
    monkeypatch.setattr(cli, "Config", lambda: config)
    monkeypatch.setattr(sys, "argv", ["paintwatch.cli", "restore", str(backup)])
    cli.main()
    restored = Store(config.database_url)
    assert restored.tags()[0]["threshold"] == 40000
    assert restored.products()[1] == 1
    assert list((config.data_dir / "backups").glob("before-restore-*.sqlite3"))
    restored.engine.dispose()
