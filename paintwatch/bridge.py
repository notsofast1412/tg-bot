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

    def job(self):
        with self.lock:
            now = int(time.time())
            if self.store.setting("monitoring", "0") != "1":
                self.store.configure({"extension_job": "null"})
                return None
            interval = max(600, int(self.store.setting("interval", "600")))
            old = json.loads(self.store.setting("extension_job", "null"))
            if old and old["expires"] > now:
                return None
            if old:
                self.store.failure(
                    old["id"],
                    PriceError("Расширение не завершило проверку карточки"),
                    interval,
                )
                self.store.configure({"extension_job": "null"})
            if float(self.store.setting("extension_next_request", "0")) > time.time():
                return None
            for p in reversed(self.store.products()):
                if (
                    not p["enabled"]
                    or p["next_check"] > now
                    or int(self.store.setting(f"cooldown_{p['market']}", "0")) > now
                ):
                    continue
                job = {
                    k: p[k]
                    for k in ("id", "market", "article", "variant", "url", "seller")
                }
                job.update(
                    lease=secrets.token_urlsafe(24),
                    expires=now + 90,
                    wb_dest=self.store.setting("wb_dest", "-1257786"),
                )
                with self.store.db() as db:
                    db.execute(
                        "UPDATE products SET last_attempt=?,status='Проверяется в Chrome',issue='' WHERE id=?",
                        (now, p["id"]),
                    )
                self.store.configure(
                    {
                        "extension_job": json.dumps(job),
                        "extension_next_request": time.time() + 3,
                    }
                )
                return job
            return None

    def result(self, payload):
        with self.lock:
            job = json.loads(self.store.setting("extension_job", "null"))
            if (
                not job
                or payload.get("id") != job["id"]
                or not hmac.compare_digest(str(payload.get("lease", "")), job["lease"])
                or time.time() > job["expires"]
            ):
                raise ValueError("Задание устарело или уже обработано")
            p = self.store.product(job["id"])
            interval = max(600, int(self.store.setting("interval", "600")))
            if (
                not p
                or not p["enabled"]
                or self.store.setting("monitoring", "0") != "1"
            ):
                self.store.configure({"extension_job": "null"})
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
                        source="Видимая цена с явной подписью типа оплаты, расширение браузера",
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
                        int(time.time()) + max(interval, error.retry_after),
                        error.code,
                    )
            except (ValueError, TypeError, KeyError, AttributeError):
                self.store.failure(
                    p["id"],
                    PriceError("Расширение передало неподтверждённую структуру данных"),
                    interval,
                )
            finally:
                self.store.configure({"extension_job": "null"})

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
                    self.reply(
                        200,
                        {
                            "ok": True,
                            "monitoring": bridge.store.setting("monitoring", "0")
                            == "1",
                        },
                    )
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
                        self.reply(200, {"job": bridge.job()})
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
