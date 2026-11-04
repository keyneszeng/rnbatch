"""Quota, order and protected HTTP routes for the Web App billing foundation."""
import concurrent.futures
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rnbatch.web_auth import AuthStore
from rnbatch.web_billing import BillingStore, FREE_FILES_PER_MONTH, QuotaExceeded
from rnbatch.web_cloud import make_handler
from rnbatch.web_workspace import WorkspaceStore


class TestBillingLedger(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "web-users.sqlite3"
        auth = AuthStore(self.db)
        self.alice = auth.add_user("alice", "StrongPassword_2026!")
        self.bob = auth.add_user("bobby", "AnotherStrongPassword!")
        self.billing = BillingStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_monthly_free_allocation_and_persistence(self):
        status = self.billing.status(self.alice)
        self.assertEqual(status["free_remaining"], FREE_FILES_PER_MONTH)
        self.assertFalse(status["member"])
        reservation = self.billing.reserve(self.alice, "workspace-a", 12)
        self.assertEqual(reservation, {"free_units": 12, "paid_units": 0, "renamed": 12})
        self.billing.settle(self.alice, "workspace-a")
        reloaded = BillingStore(self.db)
        self.assertEqual(reloaded.status(self.alice)["free_remaining"], 8)
        self.assertEqual(reloaded.status(self.bob)["free_remaining"], 20)
        with self.assertRaises(QuotaExceeded):
            reloaded.reserve(self.alice, "workspace-b", 9)
        self.assertEqual(reloaded.status(self.alice)["free_remaining"], 8)

    def test_failed_operation_releases_reservation_and_does_not_double_refund(self):
        self.billing.reserve(self.alice, "workspace-a", 19)
        self.billing.release(self.alice, "workspace-a")
        self.billing.release(self.alice, "workspace-a")
        self.assertEqual(self.billing.status(self.alice)["available"], 20)
        self.billing.reserve(self.alice, "workspace-a", 4)
        self.billing.settle(self.alice, "workspace-a")
        self.billing.release(self.alice, "workspace-a")
        with self.assertRaises(ValueError):
            self.billing.reserve(self.alice, "workspace-a", 1)
        self.assertEqual(self.billing.status(self.alice)["free_remaining"], 16)

    def test_concurrent_requests_cannot_overdraw_free_quota(self):
        def reserve(n):
            try:
                self.billing.reserve(self.alice, "parallel-%02d" % n, 1)
                return True
            except QuotaExceeded:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(reserve, range(28)))
        self.assertEqual(outcomes.count(True), 20)
        self.assertEqual(outcomes.count(False), 8)
        self.assertEqual(self.billing.status(self.alice)["available"], 0)

    def test_order_fulfillment_is_idempotent_and_reference_is_unique(self):
        order = self.billing.create_order(self.alice, "member_monthly")
        self.assertEqual(order["status"], "pending")
        self.assertIsNone(order["checkout_url"])
        self.assertFalse(self.billing.catalog()["checkout_enabled"])
        self.assertFalse(self.billing.status(self.alice)["member"])
        with self.assertRaises(ValueError):
            self.billing.create_order(self.alice, "evil_sku")
        payment_ref = "verified-external-2026-001"
        confirmed = self.billing.confirm_offline(order["id"], payment_ref)
        self.assertEqual(confirmed["plan"], "member_monthly")
        self.assertEqual(confirmed["days"], 30)
        self.assertFalse(confirmed["already_fulfilled"])
        self.assertTrue(self.billing.confirm_offline(order["id"], payment_ref)["already_fulfilled"])
        self.assertTrue(self.billing.status(self.alice)["member"])
        with self.assertRaises(ValueError):
            self.billing.confirm_offline(order["id"], "verified-other-2026-002")
        second = self.billing.create_order(self.alice, "member_yearly")
        with self.assertRaises(ValueError):
            self.billing.confirm_offline(second["id"], payment_ref)
        self.assertEqual(len(self.billing.orders(self.bob)), 0)

    def test_subscription_bypasses_quota_and_renewal_extends(self):
        order = self.billing.create_order(self.alice, "member_monthly")
        first = self.billing.confirm_offline(order["id"], "verified-external-2026-003")

        status = self.billing.status(self.alice)
        self.assertTrue(status["member"])
        self.assertTrue(status["unlimited"])
        self.assertIsNone(status["available"])

        # 订阅期内一次改 200 个也放行，远超每月 20 个的免费额度
        allocation = self.billing.reserve(self.alice, "big-workspace", 200)
        self.assertEqual(allocation["free_units"], 0)
        self.billing.settle(self.alice, "big-workspace")
        # 订阅用量不吃免费额度
        self.assertEqual(self.billing.status(self.alice)["free_remaining"], 20)

        # 续费从原到期时间往后延，不吞掉剩余天数
        second = self.billing.create_order(self.alice, "member_monthly")
        renewed = self.billing.confirm_offline(second["id"], "verified-external-2026-004")
        self.assertEqual(renewed["expires_at"], first["expires_at"] + 30 * 86400)

    def test_expired_subscription_falls_back_to_free_tier(self):
        order = self.billing.create_order(self.alice, "member_monthly")
        self.billing.confirm_offline(order["id"], "verified-external-2026-005")
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE billing_subscriptions SET expires_at=? WHERE user_id=?",
                         (int(time.time()) - 60, self.alice))
            conn.commit()
        finally:
            conn.close()
        status = self.billing.status(self.alice)
        self.assertFalse(status["member"])
        self.assertEqual(status["available"], 20)
        with self.assertRaises(QuotaExceeded):
            self.billing.reserve(self.alice, "over-quota", 21)

    def test_free_tier_still_caps_non_members_across_months(self):
        self.billing.reserve(self.alice, "first-workspace", 20)
        self.billing.settle(self.alice, "first-workspace")
        with self.assertRaises(QuotaExceeded):
            self.billing.reserve(self.alice, "second-workspace", 1)
        with patch.object(BillingStore, "_period", return_value="2030-01"):
            self.assertEqual(self.billing.status(self.alice)["free_remaining"], 20)


class TestBillingRoutes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = AuthStore(Path(self.temp.name) / "users.sqlite3")
        self.alice = self.auth.add_user("alice", "StrongPassword_2026!")
        self.bob = self.auth.add_user("bobby", "AnotherStrongPassword!")
        self.store = WorkspaceStore()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = "http://127.0.0.1:%s" % port
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", port), make_handler(self.auth, self.store, self.base))
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(timeout=5)
        self.store.close()
        self.temp.cleanup()

    def request(self, route, data=None, cookie="", csrf="", raw=False):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-RNBatch-CSRF"] = csrf
        if data is not None:
            headers["Origin"] = self.base
            if not raw:
                headers["Content-Type"] = "application/json"
                data = json.dumps(data).encode()
        req = urllib.request.Request(self.base + route, data=data, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        body = response.read()
        if response.headers.get("Content-Type", "").startswith("application/json"):
            return response.status, json.loads(body), response.headers
        return response.status, body, response.headers

    def login(self, username, password):
        status, _, headers = self.request("/auth/login",
                                          {"username": username, "password": password})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        token = cookie.split("=", 1)[1]
        session = self.auth.verify(token)
        return cookie, session.csrf

    def test_private_usage_and_draft_orders_no_payment_endpoint(self):
        status, _, _ = self.request("/api/billing/status")
        self.assertEqual(status, 401)
        alice_cookie, alice_csrf = self.login("alice", "StrongPassword_2026!")
        bob_cookie, bob_csrf = self.login("bobby", "AnotherStrongPassword!")
        status, data, _ = self.request("/api/billing/status", cookie=alice_cookie)
        self.assertEqual((status, data["available"]), (200, 20))
        self.assertFalse(data["member"])
        status, _, _ = self.request("/api/billing/orders", {"sku": "member_monthly"},
                                    cookie=alice_cookie)
        self.assertEqual(status, 403)
        status, order, _ = self.request("/api/billing/orders", {"sku": "member_monthly"},
                                        cookie=alice_cookie, csrf=alice_csrf)
        self.assertEqual(status, 200)
        self.assertEqual(order["status"], "pending")
        self.assertIsNone(order["checkout_url"])
        status, private_orders, _ = self.request("/api/billing/orders", cookie=bob_cookie)
        self.assertEqual((status, private_orders["orders"]), (200, []))
        status, alice_orders, _ = self.request("/api/billing/orders", cookie=alice_cookie)
        self.assertEqual(len(alice_orders["orders"]), 1)
        for route in ("/api/billing/confirm", "/api/billing/webhook",
                      "/api/billing/grant", "/api/billing/admin"):
            status, _, _ = self.request(route, {"order":order["id"]},
                                         cookie=bob_cookie, csrf=bob_csrf)
            self.assertEqual(status, 404, route)

    def test_apply_requires_server_side_quota_and_failed_apply_does_not_charge(self):
        cookie, csrf = self.login("alice", "StrongPassword_2026!")
        status, created, _ = self.request("/api/workspaces/create", {},
                                           cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        wid = created["workspace"]
        for i in range(21):
            route = "/api/workspaces/upload?" + urllib.parse.urlencode({
                "workspace": wid, "name": "file-%02d.txt" % i})
            status, response, _ = self.request(route, b"x", cookie=cookie,
                                               csrf=csrf, raw=True)
            self.assertEqual(status, 200, response)
        status, preview, _ = self.request("/api/workspaces/preview",
            {"workspace": wid, "kw": "renamed"}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, preview["changed"]), (200, 21))
        status, response, _ = self.request("/api/workspaces/apply",
            {"workspace": wid, "plan_token": preview["plan_token"]},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 402, response)
        self.assertEqual(response["code"], "quota_exceeded")
        self.assertEqual(BillingStore(self.auth.path).status(self.alice)["available"], 20)
        status, listing, _ = self.request("/api/workspaces/list",
            {"workspace": wid}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, listing["applied"]), (200, False))
        self.assertEqual(listing["count"], 21)

        status, _, _ = self.request("/api/workspaces/delete",
            {"workspace": wid}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        status, created, _ = self.request("/api/workspaces/create", {},
                                            cookie=cookie, csrf=csrf)
        wid = created["workspace"]
        route = "/api/workspaces/upload?" + urllib.parse.urlencode({
            "workspace": wid, "name": "initial.txt"})
        self.assertEqual(self.request(route, b"abc", cookie=cookie,
                                      csrf=csrf, raw=True)[0], 200)
        status, preview, _ = self.request("/api/workspaces/preview",
            {"workspace": wid, "kw": "renamed"}, cookie=cookie, csrf=csrf)
        status, error, _ = self.request("/api/workspaces/apply",
            {"workspace": wid, "plan_token": "invalid"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400, error)
        self.assertEqual(BillingStore(self.auth.path).status(self.alice)["available"], 20)
        status, result, _ = self.request("/api/workspaces/apply",
            {"workspace": wid, "plan_token": preview["plan_token"]},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["billing"]["free_units"], 1)
        self.assertEqual(BillingStore(self.auth.path).status(self.alice)["available"], 19)
        status, error, _ = self.request("/api/workspaces/apply",
            {"workspace": wid, "plan_token": preview["plan_token"]},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400, error)
        self.assertEqual(BillingStore(self.auth.path).status(self.alice)["available"], 19)


if __name__ == "__main__":
    unittest.main()
