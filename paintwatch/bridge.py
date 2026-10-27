"""Authenticated loopback link to a user-installed browser extension."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
import time

from .domain import PriceError, Snapshot, parse_ref
from .parsers import parse_ozon, parse_wb


class BrowserBridge:
    def __init__(self, store, key, port=18765):
        self.store, self.key, self.port = store, key, port
        self.server = None
        self.thread = None
        self.lock = threading.Lock()

    def status(self):
        now = int(time.time())
        with self.store.db() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM check_jobs WHERE expires>?", (now,)
            ).fetchone()[0]
            manual = db.execute("SELECT COUNT(*) FROM manual_checks").fetchone()[0]
            due = db.execute(
                "SELECT COUNT(*) FROM products WHERE enabled=1 AND next_check<=? AND id NOT IN (SELECT product_id FROM check_jobs)",
                (now,),
            ).fetchone()[0]
        return {
            "ok": True,
            "protocol": 2,
            "monitoring": self.store.setting("monitoring", "0") == "1" or manual > 0,
            "manual": manual,
            "parallelism": self.store.parallelism(),
            "active": active,
            "due": due,
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
            interval = self.store.interval()
            with self.store.db() as db:
                expired = db.execute(
                    "SELECT product_id FROM check_jobs WHERE expires<=?", (now,)
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
            monitoring = self.store.setting("monitoring", "0") == "1"
            capacity = self.store.parallelism()
            cooldowns = {
                m: int(self.store.setting(f"cooldown_{m}", "0")) for m in ("wb", "ozon")
            }
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
                    expires=now + self.store.check_timeout() + 15,
                    timeout_ms=self.store.check_timeout() * 1000,
                    wb_dest=self.store.setting("wb_dest", "-1257786"),
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
                if payload.get("kind") == "json":
                    data = payload.get("data", {})
                    snapshot = (
                        parse_ozon(data, ref)
                        if ref.market == "ozon"
                        else parse_wb(data, ref)
                    )
                    snapshot.source = (
                        "Ozon: цена по карте"
                        if ref.market == "ozon"
                        else "WB: без WB Кошелька"
                    ) + ", расширение обычного браузера"
                elif payload.get("kind") == "visible":
                    expected = "ozon_card" if ref.market == "ozon" else "wb_product"
                    if (
                        payload.get("price_kind") != expected
                        or payload.get("currency") != "RUB"
                    ):
                        raise PriceError("Тип цены не подтверждён")
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
                        source="Видимая цена с подтверждённым типом оплаты, расширение браузера",
                    )
                    if not snapshot.name:
                        raise PriceError("Название карточки не подтверждено")
                else:
                    raise PriceError("Расширение не подтвердило источник цены")
                self.store.record(p["id"], snapshot, interval)
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
                    bridge.store.configure({"extension_seen": int(time.time())})
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
                    bridge.store.configure({"extension_seen": int(time.time())})
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
