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
        status, result = self.call("/v1/next", {})
        self.assertEqual(status, 200)
        job = result["job"]
        self.assertIsNone(self.call("/v1/next", {})[1]["job"])
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
