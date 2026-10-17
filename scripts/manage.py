"""Offline administration; never needs Telegram credentials."""
import argparse
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from paintbot.database.store import Store
from paintbot.marketplaces import make_adapters
from paintbot.marketplaces.links import MarketError, resolve_link


def backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError("Исходная база не найдена")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive output creation protects existing backups and symlink targets.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Ошибка integrity_check резервной копии")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


async def probe(urls):
    adapters = make_adapters()
    failures = 0
    try:
        for url in urls:
            try:
                ref = await resolve_link(url, adapters)
                card = await adapters[ref.marketplace].fetch(ref)
                print(json.dumps({"url": ref.url, "card_id": card.card_id, "title": card.title,
                    "price_kopecks": card.price, "source": card.source, "warning": card.warning}, ensure_ascii=False))
                if card.price is None:
                    failures += 1
            except MarketError as exc:
                failures += 1
                print(json.dumps({"url": url, "error": str(exc)}, ensure_ascii=False))
    finally:
        for a in adapters.values():
            await a.client.aclose()
    return int(failures > 0)


def main():
    os.umask(0o077)
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "status", "backup", "probe"])
    parser.add_argument("args", nargs="*")
    opts = parser.parse_args()
    path = Path(os.environ.get("DATABASE_PATH", "var/paintbot.db"))
    if opts.action == "probe":
        if not opts.args:
            parser.error("probe требует одну или несколько прямых ссылок")
        raise SystemExit(asyncio.run(probe(opts.args)))
    if opts.action == "backup":
        if len(opts.args) != 1:
            parser.error("backup требует путь нового файла")
        backup(path, opts.args[0])
        print("Резервная копия проверена")
        return
    store = Store(path)
    try:
        if opts.action == "seed":
            store.seed()
            print("Теги инициализированы без сброса настроек")
        else:
            print(json.dumps(store.statistics(), ensure_ascii=False, indent=2))
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
