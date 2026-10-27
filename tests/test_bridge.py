import http.client
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from paintwatch.bridge import BrowserBridge
from paintwatch.domain import parse_ref
from paintwatch.store import Store


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.add([parse_ref("196525150", "ozon")], "Paint", "#arton")
        self.store.configure({"monitoring": 1})
        self.bridge = BrowserBridge(self.store, "fixture-pairing-key", port=0)
        self.bridge.start()

    def tearDown(self):
        self.bridge.close()
        self.temp.cleanup()

    def call(self, path, body=None, auth="fixture-pairing-key", origin=None):
        client = http.client.HTTPConnection("127.0.0.1", self.bridge.port, timeout=5)
        headers = {
            "Authorization": f"Bearer {auth}",
            "Content-Type": "application/json",
        }
        if origin:
            headers["Origin"] = origin
        try:
            client.request(
                "POST" if body is not None else "GET",
                path,
                json.dumps(body).encode() if body is not None else None,
                headers,
            )
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_auth_origin_and_loopback_binding(self):
        self.assertEqual(self.bridge.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.call("/v1/status", auth="wrong")[0], 403)
        self.assertEqual(self.call("/v1/status", origin="https://www.ozon.ru")[0], 403)
        self.assertEqual(
            self.call("/v1/status", origin="chrome-extension://" + "a" * 32)[0], 200
        )

    def test_lease_price_and_replay_protection(self):
        status, result = self.call(
            "/v1/next", {"protocol": 2, "client": "fixture", "slot": 0}
        )
        self.assertEqual(status, 200)
        job = result["job"]
        self.assertIsNone(
            self.call("/v1/next", {"protocol": 2, "client": "fixture", "slot": 0})[1][
                "job"
            ]
        )
        payload = {
            "id": job["id"],
            "lease": job["lease"],
            "url": job["url"],
            "kind": "visible",
            "name": "Краска",
            "price": 35000,
            "price_kind": "ozon_card",
            "currency": "RUB",
        }
        self.assertEqual(self.call("/v1/result", payload)[0], 200)
        self.assertEqual(self.store.products()[0]["price"], 35000)
        self.assertEqual(len(self.store.queue()), 1)
        self.assertEqual(self.call("/v1/result", payload)[0], 409)
        self.assertEqual(len(self.store.history(job["id"])), 1)

    def test_wrong_price_kind_or_wrong_card_never_updates_price(self):
        job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": job["url"],
                "kind": "visible",
                "name": "Краска",
                "price": 35000,
                "currency": "RUB",
                "price_kind": "ordinary",
            }
        )
        self.assertIsNone(self.store.products()[0]["price"])
        self.store.resume_after_verification(job["id"])
        with patch("paintwatch.bridge.time.time", return_value=time.time() + 4):
            job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": "https://www.ozon.ru/product/1/",
                "kind": "visible",
                "name": "Other",
                "price": 35000,
                "currency": "RUB",
                "price_kind": "ozon_card",
            }
        )
        self.assertIsNone(self.store.products()[0]["price"])
        self.assertEqual(self.store.queue(), [])

    def test_access_pause_and_stopped_monitoring(self):
        job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": job["url"],
                "error": "HTTP 429",
                "code": "rate_limit",
                "retry_after": 1200,
            }
        )
        self.assertGreater(int(self.store.setting("cooldown_ozon")), time.time() + 1100)
        self.assertIsNone(self.bridge.job())
        self.store.configure({"monitoring": 0})
        self.assertIsNone(self.bridge.job())

    def test_malformed_data_records_failure_with_backoff(self):
        job = self.bridge.job()
        self.bridge.result(
            {
                "id": job["id"],
                "lease": job["lease"],
                "url": job["url"],
                "kind": "json",
                "data": {"widgetStates": []},
            }
        )
        p = self.store.products()[0]
        self.assertIsNone(p["price"])
        self.assertGreater(p["next_check"], time.time() + 500)

    def visible(self, job):
        return {
            "id": job["id"],
            "lease": job["lease"],
            "url": job["url"],
            "kind": "visible",
            "name": "Paint",
            "price": 35000,
            "price_kind": "ozon_card",
            "currency": "RUB",
        }

    def test_parallel_http_clients_respect_global_limit_and_unique_products(self):
        from concurrent.futures import ThreadPoolExecutor

        self.store.add([parse_ref(str(n), "ozon") for n in range(1, 201)], "", "#arton")
        self.store.configure({"parallelism": 4, "interval": 0})

        def claim(n):
            return self.call(
                "/v1/next", {"protocol": 2, "client": f"client{n}", "slot": 0}
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(claim, range(8)))
        self.assertTrue(all(code == 200 for code, _ in replies))
        jobs = [reply["job"] for _, reply in replies if reply["job"]]
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({j["id"] for j in jobs}), 4)
        self.assertEqual(self.bridge.status()["active"], 4)
        # A freed slot can refill while three other cards are still running.
        self.bridge.result(self.visible(jobs[0]))
        replacement = self.bridge.job("replacement", 0)
        self.assertIsNotNone(replacement)
        self.assertNotIn(replacement["id"], {j["id"] for j in jobs})
        self.assertEqual(self.bridge.status()["active"], 4)

    def test_zero_interval_fairly_visits_all_200_products_within_one_second(self):
        self.store.delete(self.store.products()[0]["id"])
        self.store.add([parse_ref(str(n), "ozon") for n in range(1, 201)], "", "#arton")
        self.store.configure({"interval": 0})
        seen = set()
        # Frozen time catches starvation from second-resolution timestamps.
        with patch("time.time", return_value=2000000000):
            for _ in range(200):
                job = self.bridge.job()
                self.assertNotIn(job["id"], seen)
                seen.add(job["id"])
                self.bridge.result(self.visible(job))
            self.assertEqual(len(seen), 200)
            self.assertIsNotNone(self.bridge.job())
        self.assertEqual(len(self.store.queue()), 200)

    def test_expiry_restart_pause_and_deleted_product(self):
        self.store.configure({"interval": 0, "check_timeout": 1})
        job = self.bridge.job()
        restored = BrowserBridge(Store(self.temp.name), "fixture-pairing-key")
        self.assertIsNone(restored.job())
        with patch("time.time", return_value=job["expires"] + 1):
            self.assertIsNone(restored.job())  # Expired product gets failure backoff.
            self.assertEqual(restored.status()["active"], 0)
        with self.assertRaises(ValueError):
            restored.result(self.visible(job))
        p = self.store.product(job["id"])
        self.assertGreater(p["next_check"], job["expires"] + 1)
        self.store.resume_after_verification(job["id"])
        job = self.bridge.job()
        self.store.configure({"monitoring": 0})
        self.bridge.result(self.visible(job))
        self.assertIsNone(self.store.product(job["id"])["price"])
        self.assertEqual(self.bridge.status()["active"], 0)
        self.store.configure({"monitoring": 1})
        self.store.resume_after_verification(job["id"])
        job = self.bridge.job()
        self.store.delete(job["id"])
        self.assertEqual(self.bridge.status()["active"], 0)
        with self.assertRaises(ValueError):
            self.bridge.result(self.visible(job))

    def test_small_interval_and_legacy_protocol(self):
        self.assertEqual(self.call("/v1/next", {})[0], 409)
        self.store.configure({"interval": 1})
        with patch("time.time", return_value=2000000000):
            job = self.bridge.job()
            self.bridge.result(self.visible(job))
            self.assertEqual(self.store.product(job["id"])["next_check"], 2000000001)
            self.assertIsNone(self.bridge.job())
        with patch("time.time", return_value=2000000001):
            self.assertIsNotNone(self.bridge.job())

    def test_parallel_error_cannot_shorten_rate_limit_or_unlock_it_as_captcha(self):
        self.store.add([parse_ref("2", "ozon")], "", "#arton")
        first = self.bridge.job("fixture", 0)
        second = self.bridge.job("fixture", 1)
        for job, code, delay in ((first, "rate_limit", 1200), (second, "access", 0)):
            self.bridge.result(
                {
                    "id": job["id"],
                    "lease": job["lease"],
                    "url": job["url"],
                    "error": code,
                    "code": code,
                    "retry_after": delay,
                }
            )
        self.assertGreater(int(self.store.setting("cooldown_ozon")), time.time() + 1100)
        self.assertEqual(self.store.setting("cooldown_reason_ozon"), "rate_limit")
        with self.assertRaises(ValueError):
            self.store.resume_after_verification(second["id"])
