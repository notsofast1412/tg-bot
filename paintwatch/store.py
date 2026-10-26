from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
import time

from .domain import TAGS, PriceError


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "prices.sqlite3"
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Эта база создана другой версией программы")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tags(name TEXT PRIMARY KEY,threshold INTEGER NOT NULL CHECK(threshold>0));
                CREATE TABLE IF NOT EXISTS products(
                    id INTEGER PRIMARY KEY,market TEXT NOT NULL,article TEXT NOT NULL,variant TEXT NOT NULL,
                    url TEXT NOT NULL,name TEXT NOT NULL,batch TEXT NOT NULL DEFAULT '',
                    tag TEXT NOT NULL REFERENCES tags(name),custom_threshold INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,price INTEGER,last_success INTEGER,
                    last_attempt INTEGER,next_check INTEGER NOT NULL DEFAULT 0,
                    below INTEGER,generation INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'Ожидает проверки',issue TEXT NOT NULL DEFAULT '',
                    seller TEXT NOT NULL DEFAULT '',color TEXT NOT NULL DEFAULT '',
                    volume TEXT NOT NULL DEFAULT '',pack TEXT NOT NULL DEFAULT '',
                    UNIQUE(market,article,variant)
                );
                CREATE TABLE IF NOT EXISTS history(
                    id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    price INTEGER NOT NULL,observed_at INTEGER NOT NULL,source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checks(
                    id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    checked_at INTEGER NOT NULL,ok INTEGER NOT NULL,reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,price INTEGER NOT NULL,threshold INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL,
                    next_try INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',UNIQUE(product_id,generation)
                );
                CREATE INDEX IF NOT EXISTS checks_product ON checks(product_id,checked_at);
                CREATE INDEX IF NOT EXISTS history_product ON history(product_id,observed_at);
                PRAGMA user_version=1;
            """)
            db.executemany("INSERT OR IGNORE INTO tags VALUES(?,?)", TAGS)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def setting(self, key, default=""):
        with self.db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default

    def configure(self, values):
        with self.db() as db:
            db.executemany(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, str(v)) for k, v in values.items()],
            )

    def tags(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tags ORDER BY rowid")]

    def products(self):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT p.*,COALESCE(p.custom_threshold,t.threshold) threshold FROM products p JOIN tags t ON t.name=p.tag ORDER BY p.id DESC"
                )
            ]

    def product(self, pid):
        return next((p for p in self.products() if p["id"] == pid), None)

    def add(self, refs, name, tag, threshold=None):
        added = 0
        with self.db() as db:
            for ref in refs:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO products(market,article,variant,url,name,batch,tag,custom_threshold) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        ref.market,
                        ref.article,
                        ref.variant,
                        ref.url,
                        name or ref.article,
                        name,
                        tag,
                        threshold,
                    ),
                )
                added += cursor.rowcount
        return added

    def _rebase(self, db, pid):
        db.execute(
            "UPDATE products SET below=CASE WHEN price IS NULL THEN NULL ELSE price < COALESCE(custom_threshold,(SELECT threshold FROM tags WHERE name=products.tag)) END WHERE id=?",
            (pid,),
        )
        db.execute(
            "UPDATE outbox SET state='cancelled',error='Условия мониторинга изменены' WHERE product_id=? AND state IN ('pending','blocked')",
            (pid,),
        )

    def edit(self, pid, name, tag, threshold, color, volume, pack):
        with self.db() as db:
            old = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not old:
                return
            db.execute(
                "UPDATE products SET name=?,tag=?,custom_threshold=?,color=?,volume=?,pack=? WHERE id=?",
                (name, tag, threshold, color, volume, pack, pid),
            )
            if old["tag"] != tag or old["custom_threshold"] != threshold:
                self._rebase(db, pid)

    def edit_tag(self, name, threshold):
        with self.db() as db:
            db.execute("UPDATE tags SET threshold=? WHERE name=?", (threshold, name))
            for row in db.execute(
                "SELECT id FROM products WHERE tag=? AND custom_threshold IS NULL",
                (name,),
            ).fetchall():
                self._rebase(db, row["id"])

    def enable(self, pid, enabled):
        with self.db() as db:
            db.execute("UPDATE products SET enabled=? WHERE id=?", (int(enabled), pid))
            if not enabled:
                db.execute(
                    "UPDATE outbox SET state='cancelled',error='Товар на паузе' WHERE product_id=? AND state IN ('pending','blocked')",
                    (pid,),
                )

    def delete(self, pid):
        with self.db() as db:
            db.execute("DELETE FROM products WHERE id=?", (pid,))

    def request_check(self, pid=None):
        # Manual checks respect the minimum interval and persisted marketplace cooldown.
        interval = max(600, int(self.setting("interval", "600")))
        with self.db() as db:
            if pid:
                db.execute(
                    "UPDATE products SET next_check=COALESCE(last_attempt,0)+? WHERE id=?",
                    (interval, pid),
                )
            else:
                db.execute(
                    "UPDATE products SET next_check=COALESCE(last_attempt,0)+? WHERE enabled=1",
                    (interval,),
                )

    def cooldown(self, market, until, reason):
        self.configure(
            {f"cooldown_{market}": until, f"cooldown_reason_{market}": reason}
        )

    def resume_after_verification(self, pid):
        p = self.product(pid)
        if not p:
            return
        reason = self.setting(f"cooldown_reason_{p['market']}")
        if (
            reason == "rate_limit"
            and int(self.setting(f"cooldown_{p['market']}", "0")) > time.time()
        ):
            raise ValueError("Пауза HTTP 429 ещё действует; дождитесь окончания")
        self.cooldown(p["market"], 0, "")
        with self.db() as db:
            db.execute("UPDATE products SET next_check=0 WHERE id=?", (pid,))

    def record(self, pid, snapshot, interval, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT p.*,COALESCE(p.custom_threshold,t.threshold) threshold FROM products p JOIN tags t ON t.name=p.tag WHERE id=?",
                (pid,),
            ).fetchone()
            if not row or not row["enabled"]:
                return
            p = dict(row)
            if (
                snapshot.ref.market != p["market"]
                or snapshot.ref.article != p["article"]
                or snapshot.ref.variant != p["variant"]
            ):
                raise PriceError("Получены данные другой карточки", code="identity")
            if p["seller"] and snapshot.seller != p["seller"]:
                raise PriceError(
                    "Продавец изменился или не подтверждён; проверьте карточку",
                    code="seller_changed",
                )
            if snapshot.price is not None and (
                type(snapshot.price) is not int or not 0 < snapshot.price <= 10**10
            ):
                raise PriceError("Некорректная цена", code="invalid_price")
            db.execute(
                "INSERT INTO checks(product_id,checked_at,ok,reason) VALUES(?,?,?,?)",
                (pid, now, int(snapshot.price is not None), snapshot.issue),
            )
            if snapshot.price is None:
                db.execute(
                    "UPDATE products SET status='Цена неизвестна',issue=?,last_attempt=?,next_check=?,failures=failures+1 WHERE id=?",
                    (snapshot.issue, now, now + interval, pid),
                )
                return
            below = snapshot.price < p["threshold"]
            generation = p["generation"]
            if snapshot.price != p["price"]:
                db.execute(
                    "INSERT INTO history(product_id,price,observed_at,source) VALUES(?,?,?,?)",
                    (pid, snapshot.price, now, snapshot.source),
                )
            if below and p["below"] != 1:
                generation += 1
                db.execute(
                    "INSERT OR IGNORE INTO outbox(product_id,generation,price,threshold,created_at) VALUES(?,?,?,?,?)",
                    (pid, generation, snapshot.price, p["threshold"], now),
                )
            elif not below:
                db.execute(
                    "UPDATE outbox SET state='cancelled',error='Цена больше не ниже порога' WHERE product_id=? AND state IN ('pending','blocked')",
                    (pid,),
                )
            db.execute(
                "UPDATE products SET name=CASE WHEN name=article THEN ? ELSE name END,price=?,below=?,generation=?,last_success=?,last_attempt=?,next_check=?,failures=0,status='Проверено',issue='',seller=?,color=CASE WHEN color='' THEN ? ELSE color END,volume=CASE WHEN volume='' THEN ? ELSE volume END,pack=CASE WHEN pack='' THEN ? ELSE pack END WHERE id=?",
                (
                    snapshot.name,
                    snapshot.price,
                    int(below),
                    generation,
                    now,
                    now,
                    now + interval,
                    snapshot.seller,
                    snapshot.color,
                    snapshot.volume,
                    snapshot.pack,
                    pid,
                ),
            )

    def failure(self, pid, error, interval, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as db:
            p = db.execute(
                "SELECT failures FROM products WHERE id=?", (pid,)
            ).fetchone()
            if not p:
                return
            delay = max(
                min(86400, interval * 2 ** min(p["failures"], 7)), error.retry_after
            )
            db.execute(
                "UPDATE products SET last_attempt=?,next_check=?,failures=failures+1,status=?,issue=? WHERE id=?",
                (
                    now,
                    now + delay,
                    "Нужна ручная проверка" if error.code == "access" else "Ошибка",
                    str(error)[:400],
                    pid,
                ),
            )
            db.execute(
                "INSERT INTO checks(product_id,checked_at,ok,reason) VALUES(?,?,0,?)",
                (pid, now, str(error)[:400]),
            )

    def history(self, pid):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 200",
                    (pid,),
                )
            ]

    def queue(self):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT o.*,p.name,p.url,p.tag,p.market,p.price current_price,p.status current_status,p.last_success,p.last_attempt,p.enabled,p.below,p.generation current_generation,COALESCE(p.custom_threshold,t.threshold) current_threshold FROM outbox o JOIN products p ON p.id=o.product_id JOIN tags t ON t.name=p.tag ORDER BY o.id DESC"
                )
            ]

    def delivery(self, oid, state, error="", delay=0):
        with self.db() as db:
            db.execute(
                "UPDATE outbox SET state=?,error=?,next_try=?,attempts=attempts+1 WHERE id=?",
                (state, error[:300], int(time.time()) + delay, oid),
            )

    def recover(self):
        with self.db() as db:
            db.execute(
                "UPDATE outbox SET state='uncertain',error='Программа завершилась во время отправки; проверьте группу' WHERE state='sending'"
            )

    def backup(self, destination=None):
        if destination is None:
            folder = self.directory / "backups"
            folder.mkdir(exist_ok=True)
            destination = folder / f"prices-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
        destination = Path(destination)
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise ValueError("Выберите новый файл резервной копии")
        with self.db() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        if destination.parent == self.directory / "backups":
            for old in sorted(destination.parent.glob("prices-*.sqlite3"))[:-14]:
                old.unlink()
        return destination
