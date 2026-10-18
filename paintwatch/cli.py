import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from .config import Config
from .database.store import Store
from .domain import FetchError, timestamp
from .links import parse_link
from .marketplaces import adapters
from .marketplaces.web import Web
from .runtime import instance_lock

ROOT = Path(__file__).resolve().parent.parent


def migrate(config):
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    alembic = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(ROOT / "migrations"))
    alembic.set_main_option("sqlalchemy.url", config.database_url.replace("%", "%%"))
    command.upgrade(alembic, "head")
    store = Store(config.database_url)
    store.seed()
    store.engine.dispose()


async def probe(config, url):
    web = Web(config)
    try:
        ref = parse_link(url)
        result = await adapters(web, config)[ref.market].fetch(ref)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.price is not None else 2
    except FetchError as e:
        print(
            json.dumps(
                {"status": "failed", "code": e.code, "reason": str(e)},
                ensure_ascii=False,
            )
        )
        return 2
    finally:
        await web.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="PaintWatch: обслуживание без вывода секретов"
    )
    parser.add_argument(
        "action", choices=["init-db", "doctor", "backup", "probe", "restore"]
    )
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    config = Config()
    if args.action == "init-db":
        with instance_lock(config.data_dir):
            migrate(config)
        print("Миграции и начальные теги применены")
    elif args.action == "probe":
        if not args.value:
            parser.error("probe требует URL карточки")
        raise SystemExit(asyncio.run(probe(config, args.value)))
    elif args.action == "doctor":
        result = {
            "BOT_TOKEN": "задан" if config.bot_token.get_secret_value() else "не задан",
            "OWNER_ID": "задан" if config.owner_id > 0 else "не задан",
            "database_exists": (config.data_dir / "paintwatch.sqlite3").is_file(),
        }
        if result["database_exists"]:
            store = Store(config.database_url)
            result["statistics"] = store.statistics()
            with store.engine.connect() as connection:
                result["integrity_check"] = connection.exec_driver_sql(
                    "PRAGMA integrity_check"
                ).scalar()
            store.engine.dispose()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.action == "backup":
        store = Store(config.database_url)
        destination = (
            Path(args.value)
            if args.value
            else config.data_dir
            / "backups"
            / f"paintwatch-manual-{timestamp()}.sqlite3"
        )
        if destination.exists():
            parser.error("Файл назначения уже существует; выберите новое имя")
        store.backup(destination)
        store.engine.dispose()
        print(f"Согласованная копия сохранена: {destination}")
    else:
        if not args.value:
            parser.error("restore требует путь к резервной копии")
        backup = Path(args.value).resolve()
        if (
            not backup.is_file()
            or backup == (config.data_dir / "paintwatch.sqlite3").resolve()
        ):
            parser.error("Некорректный путь к копии")
        with instance_lock(config.data_dir):
            with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Копия не прошла integrity_check")
                tables = {
                    r[0]
                    for r in source.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if (
                    not {"products", "tags", "notification_queue", "alembic_version"}
                    <= tables
                ):
                    raise RuntimeError("Копия не является базой PaintWatch")
                store = Store(config.database_url)
                if (config.data_dir / "paintwatch.sqlite3").exists():
                    store.backup(
                        config.data_dir
                        / "backups"
                        / f"before-restore-{timestamp()}.sqlite3"
                    )
                store.engine.dispose()
                with sqlite3.connect(
                    config.data_dir / "paintwatch.sqlite3"
                ) as destination:
                    source.backup(destination)
            migrate(config)
        print(
            "База восстановлена; перед запуском проверьте /queue: старый backup может не содержать отправки после его создания"
        )


if __name__ == "__main__":
    main()
