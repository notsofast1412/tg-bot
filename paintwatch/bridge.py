"""Authenticated loopback link to a user-installed browser extension."""

import hmac
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from .domain import PriceError, Snapshot, parse_ref
from .parsers import parse_ozon


class BrowserBridge:
    def __init__(self, store, key, port=18765):
        self.store, self.key, self.port = store, key, port
        self.server = None
        self.thread = None
        self.lock = threading.Lock()
        self.seen_lock = threading.Lock()
        self.last_seen = None
        self.started = time.time()
        self.recent = deque(maxlen=4096)

    def touch(self):
        with self.seen_lock:
            now = time.monotonic()
            if self.last_seen is None or now - self.last_seen >= 10:
                self.store.configure({"extension_seen": int(time.time())})
                self.last_seen = now

    def timing(self, job, successful):
        now = time.time()
        created = job.get("created_at")
        duration = max(0, now - created) if type(created) in (int, float) else None
        self.recent.append((now, successful, duration))

    def status(self):
        now = int(time.time())
        settings = self.store.settings(
            {
                "monitoring": "0",
                "parallelism": "4",
                "cooldown_ozon": "0",
                "cooldown_wb": "0",
            }
        )
        capacity = max(1, min(32, int(settings["parallelism"])))
        monitoring = settings["monitoring"] == "1"
        with self.store.db() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM check_jobs WHERE expires>?", (now,)
            ).fetchone()[0]
            manual = db.execute("SELECT COUNT(*) FROM manual_checks").fetchone()[0]
            due = db.execute(
                "SELECT COUNT(*) FROM products WHERE enabled=1 AND next_check<=? AND id NOT IN (SELECT product_id FROM check_jobs)",
                (now,),
            ).fetchone()[0]
            ready = db.execute(
                "SELECT COUNT(*) FROM products WHERE enabled=1 AND ((? AND next_check<=?) OR id IN (SELECT product_id FROM manual_checks)) AND id NOT IN (SELECT product_id FROM check_jobs WHERE expires>?) AND ((market='ozon' AND ?<=?) OR (market='wb' AND ?<=?))",
                (
                    int(monitoring),
                    now,
                    now,
                    int(settings["cooldown_ozon"]),
                    now,
                    int(settings["cooldown_wb"]),
                    now,
                ),
            ).fetchone()[0]
            coverage = db.execute(
                "SELECT COUNT(*) enabled,COALESCE(SUM(last_success IS NULL),0) unchecked,MIN(last_success) oldest FROM products WHERE enabled=1"
            ).fetchone()
        with self.lock:
            samples = [sample for sample in self.recent if sample[0] >= now - 300]
        durations = [sample[2] for sample in samples if sample[2] is not None]
        window = max(1, min(300, now - int(self.started)))
        successful = sum(sample[1] for sample in samples)
        average = sum(durations) / len(durations) if durations else None
        return {
            "ok": True,
            "protocol": 2,
            "wb_price_kind": "wb_wallet",
            "availability_status": True,
            "monitoring": monitoring or manual > 0,
            "manual": manual,
            "parallelism": capacity,
            "active": active,
            "due": due,
            "ready": ready if active < capacity else 0,
            "performance": {
                "window_seconds": window,
                "completed": len(samples),
                "successful": successful,
                "per_minute": round(successful * 60 / window, 1),
                "average_seconds": round(average, 2) if average is not None else None,
                "estimated_cycle_seconds": round(
                    coverage["enabled"] * average / capacity
                )
                if average is not None
                else None,
                "unchecked": coverage["unchecked"],
                "oldest_success_age": max(0, now - coverage["oldest"])
                if coverage["oldest"] is not None
                else None,
            },
        }

    def job(self, client="local", slot=0):
        if not isinstance(client, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,80}", client
        ):
            raise ValueError("Неверный клиент")
        if type(slot) is not int or not 0 <= slot < 32:
            raise ValueError("Неверный слот")
        with self.lock:
            now = int(time.time())
            settings = self.store.settings(
                {
                    "interval": "600",
                    "monitoring": "0",
                    "parallelism": "4",
                    "check_timeout": "20",
                    "cooldown_ozon": "0",
                    "cooldown_wb": "0",
                    "wb_dest": "-1257786",
                }
            )
            interval = max(0, int(settings["interval"]))
            timeout = max(1, min(120, int(settings["check_timeout"])))
            with self.store.db() as db:
                expired = db.execute(
                    "SELECT product_id,payload FROM check_jobs WHERE expires<=?", (now,)
                ).fetchall()
                db.execute("DELETE FROM check_jobs WHERE expires<=?", (now,))
            for old in expired:
                request = self.store.manual_request(old["product_id"])
                self.store.failure(
                    old["product_id"],
                    PriceError("Расширение не завершило проверку карточки"),
                    interval,
                )
                self.release(old["product_id"], request)
                self.timing(json.loads(old["payload"]), False)
            monitoring = settings["monitoring"] == "1"
            capacity = max(1, min(32, int(settings["parallelism"])))
            cooldowns = {m: int(settings[f"cooldown_{m}"]) for m in ("wb", "ozon")}
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if (
                    slot >= capacity
                    or db.execute("SELECT COUNT(*) FROM check_jobs").fetchone()[0]
                    >= capacity
                    or db.execute(
                        "SELECT 1 FROM check_jobs WHERE client=? AND slot=?",
                        (client, slot),
                    ).fetchone()
                ):
                    return None
                p = db.execute(
                    "SELECT * FROM products WHERE enabled=1 AND ((? AND next_check<=?) OR id IN (SELECT product_id FROM manual_checks)) "
                    "AND id NOT IN (SELECT product_id FROM check_jobs) "
                    "AND ((market='ozon' AND ?<=?) OR (market='wb' AND ?<=?)) "
                    "ORDER BY (id IN (SELECT product_id FROM manual_checks)) DESC,next_check,COALESCE((SELECT MAX(id) FROM checks WHERE product_id=products.id),0),id LIMIT 1",
                    (
                        int(monitoring),
                        now,
                        cooldowns["ozon"],
                        now,
                        cooldowns["wb"],
                        now,
                    ),
                ).fetchone()
                if not p:
                    return None
                job = {
                    k: p[k]
                    for k in ("id", "market", "article", "variant", "url", "seller")
                }
                job.update(
                    lease=secrets.token_urlsafe(24),
                    expires=now + timeout + 15,
                    timeout_ms=timeout * 1000,
                    created_at=time.time(),
                    price_kind="wb_wallet" if p["market"] == "wb" else "ozon_card",
                    wb_dest=settings["wb_dest"],
                )
                db.execute(
                    "INSERT INTO check_jobs VALUES(?,?,?,?,?,?)",
                    (
                        p["id"],
                        job["lease"],
                        job["expires"],
                        client,
                        slot,
                        json.dumps(job),
                    ),
                )
                db.execute(
                    "UPDATE products SET last_attempt=?,status='Проверяется в Chrome' WHERE id=?",
                    (now, p["id"]),
                )
                return job

    def result(self, payload):
        with self.lock:
            with self.store.db() as db:
                row = db.execute(
                    "SELECT payload FROM check_jobs WHERE product_id=?",
                    (payload.get("id"),),
                ).fetchone()
            job = json.loads(row[0]) if row else None
            if (
                not job
                or payload.get("id") != job["id"]
                or not hmac.compare_digest(str(payload.get("lease", "")), job["lease"])
                or time.time() > job["expires"]
            ):
                raise ValueError("Задание устарело или уже обработано")
            request = self.store.manual_request(job["id"])
            p = self.store.product(job["id"])
            interval = self.store.interval()
            if (
                not p
                or not p["enabled"]
                or (self.store.setting("monitoring", "0") != "1" and not request)
            ):
                self.release(job["id"], request)
                return
            successful = False
            try:
                ref = parse_ref(payload.get("url", ""))
                if (ref.market, ref.article, ref.variant) != (
                    p["market"],
                    p["article"],
                    p["variant"],
                ):
                    raise PriceError(
                        "Расширение открыло другую карточку", code="identity"
                    )
                if payload.get("error"):
                    code = payload.get("code", "unavailable")
                    if code not in ("access", "rate_limit", "unavailable"):
                        code = "unavailable"
                    delay = max(0, min(365 * 86400, int(payload.get("retry_after", 0))))
                    raise PriceError(
                        str(payload["error"])[:300], code=code, retry_after=delay
                    )
                search_redirect = urlsplit(payload.get("url", "")).path == "/search/"
                if search_redirect and payload.get("kind") != "sold_out":
                    raise PriceError(
                        "Цены похожих товаров на странице поиска не принимаются"
                    )
                if payload.get("kind") == "sold_out":
                    evidence = (
                        "ozon_sold_out_search"
                        if search_redirect and ref.market == "ozon"
                        else "product_sold_out_heading"
                    )
                    if (
                        payload.get("availability") != "out_of_stock"
                        or payload.get("evidence") != evidence
                        or payload.get("price") is not None
                    ):
                        raise PriceError("Отсутствие товара не подтверждено")
                    snapshot = Snapshot(ref, p["name"], None, available=False)
                elif payload.get("kind") == "json":
                    data = payload.get("data", {})
                    if ref.market == "wb":
                        raise PriceError(
                            "JSON с обычной ценой WB не подтверждает цену с WB Кошельком. Обновите расширение до 0.4.2"
                        )
                    snapshot = parse_ozon(data, ref)
                    snapshot.source = (
                        "Ozon: цена по карте, расширение обычного браузера"
                    )
                elif payload.get("kind") == "visible":
                    expected = "ozon_card" if ref.market == "ozon" else "wb_wallet"
                    if (
                        payload.get("price_kind") != expected
                        or payload.get("currency") != "RUB"
                    ):
                        raise PriceError(
                            "WB: для цены с Кошельком обновите расширение до 0.4.2"
                            if ref.market == "wb"
                            else "Тип цены не подтверждён"
                        )
                    if (
                        type(payload.get("price")) is not int
                        or not 0 < payload["price"] <= 10**10
                    ):
                        raise PriceError("Видимая цена не подтверждена")
                    snapshot = Snapshot(
                        ref,
                        str(payload.get("name", ""))[:300],
                        payload.get("price"),
                        seller=str(payload.get("seller", ""))[:50],
                        source=(
                            "WB: цена с WB Кошельком"
                            if ref.market == "wb"
                            else "Ozon: цена по карте"
                        )
                        + ", видимая цена в расширении браузера",
                    )
                    if not snapshot.name:
                        raise PriceError("Название карточки не подтверждено")
                else:
                    raise PriceError("Расширение не подтвердило источник цены")
                self.store.record(p["id"], snapshot, interval)
                successful = snapshot.price is not None or snapshot.available is False
            except PriceError as error:
                self.store.failure(p["id"], error, interval)
                if error.code in ("access", "rate_limit"):
                    self.store.cooldown(
                        p["market"],
                        int(time.time()) + max(60, error.retry_after),
                        error.code,
                    )
            except (ValueError, TypeError, KeyError, AttributeError):
                self.store.failure(
                    p["id"],
                    PriceError("Расширение передало неподтверждённую структуру данных"),
                    interval,
                )
            finally:
                self.release(job["id"], request)
                self.timing(job, successful)

    def release(self, pid, request=None):
        with self.store.db() as db:
            db.execute("DELETE FROM check_jobs WHERE product_id=?", (pid,))
            if request:
                db.execute(
                    "DELETE FROM manual_checks WHERE product_id=? AND request=?",
                    (pid, request),
                )

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # Never log request headers or pairing codes.

            def origin_allowed(self):
                origin = self.headers.get("Origin", "")
                return not origin or bool(
                    re.fullmatch(r"chrome-extension://[a-p]{32}", origin)
                )

            def reply(self, status, data):
                body = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if self.origin_allowed() and (origin := self.headers.get("Origin")):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                host = self.headers.get("Host", "")
                return (
                    host == f"127.0.0.1:{bridge.port}"
                    and self.origin_allowed()
                    and hmac.compare_digest(
                        self.headers.get("Authorization", "").encode(),
                        f"Bearer {bridge.key}".encode(),
                    )
                )

            def do_OPTIONS(self):
                if not self.origin_allowed():
                    self.reply(403, {"error": "Origin denied"})
                    return
                self.send_response(204)
                if origin := self.headers.get("Origin"):
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers", "Authorization,Content-Type"
                )
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()

            def do_GET(self):
                if not self.authorized():
                    self.reply(403, {"error": "Код подключения не принят"})
                elif self.path == "/v1/status":
                    bridge.touch()
                    self.reply(200, bridge.status())
                else:
                    self.reply(404, {"error": "Not found"})

            def do_POST(self):
                if not self.authorized():
                    self.reply(403, {"error": "Код подключения не принят"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= size <= 131072:
                        self.reply(413, {"error": "Payload too large"})
                        return
                    payload = json.loads(self.rfile.read(size) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError
                    bridge.touch()
                    if self.path == "/v1/next":
                        if payload.get("protocol") != 2:
                            self.reply(
                                409,
                                {
                                    "error": "Обновите расширение PaintWatch до версии 0.3"
                                },
                            )
                            return
                        self.reply(
                            200,
                            {
                                "job": bridge.job(
                                    payload.get("client"), payload.get("slot")
                                )
                            },
                        )
                    elif self.path == "/v1/result":
                        bridge.result(payload)
                        self.reply(200, {"ok": True})
                    else:
                        self.reply(404, {"error": "Not found"})
                except (ValueError, TypeError, KeyError, AttributeError):
                    self.reply(409, {"error": "Задание или данные не приняты"})

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            raise RuntimeError(
                "Не удалось открыть локальное подключение расширения: порт 18765 занят"
            ) from None
        self.port = self.server.server_address[1]
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="PaintWatch extension", daemon=True
        )
        self.thread.start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
