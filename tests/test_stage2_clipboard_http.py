"""Offline unit tests for Stage 2 HTTP clipboard mailbox (`clipboard_http.py`)."""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from unittest import mock

import clipboard_http


class TestStage2ClipboardHttp(unittest.TestCase):
    def setUp(self):
        self.srv = clipboard_http.ClipboardMailboxServer(host="127.0.0.1", port=0)

    def tearDown(self):
        self.srv.close()

    def _request(self, method: str, path: str, body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=2.0)
        try:
            headers = {"Content-Length": str(len(body))} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def test_5000_char_unicode_emoji_url_roundtrip(self):
        sample = ("🚀🎉猎聘测试\n第二行 https://example.com/path?a=1&b=测试 " * 120)[:5000]
        self.assertEqual(len(sample), 5000)
        gen = self.srv.stage(sample)
        self.assertGreaterEqual(gen, 1)

        status, headers, raw = self._request("GET", "/clip")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(int(headers.get("Content-Length")), len(raw))
        self.assertEqual(raw.decode("utf-8"), sample)

        self.assertTrue(self.srv.wait_fetched(gen, timeout=0.2))

    def test_wait_fetched_wakeup_latency_under_100ms(self):
        gen = self.srv.stage("latency check 🚀")
        result = {"ok": False, "woke_at": 0.0}

        def waiter():
            ok = self.srv.wait_fetched(gen, timeout=1.0)
            result["woke_at"] = time.monotonic()
            result["ok"] = ok

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        time.sleep(0.025)
        t_fetch = time.monotonic()
        status, _, _ = self._request("GET", "/clip")
        self.assertEqual(status, 200)
        th.join(timeout=1.0)
        wakeup_dt = result["woke_at"] - t_fetch
        self.assertTrue(result["ok"])
        self.assertLess(wakeup_dt, 0.10)

    def test_generation_isolation_stale_fetch_does_not_satisfy_new_gen(self):
        gen1 = self.srv.stage("first")
        status, _, raw = self._request("GET", "/clip")
        self.assertEqual(status, 200)
        self.assertEqual(raw.decode("utf-8"), "first")
        self.assertTrue(self.srv.wait_fetched(gen1, timeout=0.2))

        gen2 = self.srv.stage("second")
        self.assertEqual(gen2, gen1 + 1)
        self.assertFalse(self.srv.wait_fetched(gen2, timeout=0.05))

        status, _, raw = self._request("GET", "/clip")
        self.assertEqual(status, 200)
        self.assertEqual(raw.decode("utf-8"), "second")
        self.assertTrue(self.srv.wait_fetched(gen2, timeout=0.2))

    def test_health_and_setup_endpoints(self):
        self.assertEqual(self.srv.clip_url, f"http://{self.srv.lan_ip}:{self.srv.port}/clip")
        self.assertEqual(self.srv.setup_url, f"http://{self.srv.lan_ip}:{self.srv.port}/setup")

        status, _, raw = self._request("GET", "/health")
        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["port"], self.srv.port)
        self.assertEqual(data["clip_url"], self.srv.clip_url)
        self.assertEqual(data["setup_url"], self.srv.setup_url)

        status, headers, raw = self._request("GET", "/setup")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        html_body = raw.decode("utf-8")
        self.assertIn(self.srv.clip_url, html_body)
        self.assertIn("AgentPaste", html_body)
        self.assertIn("Button 3", html_body)

    def test_post_clip_localhost_allowed_and_remote_rejected_403(self):
        payload = "POST 测试 🎉".encode("utf-8")
        status, _, raw = self._request("POST", "/clip", body=payload)
        self.assertEqual(status, 200)
        res = json.loads(raw.decode("utf-8"))
        self.assertTrue(res["ok"])
        gen = res["gen"]

        status, _, raw = self._request("GET", "/clip")
        self.assertEqual(status, 200)
        self.assertEqual(raw.decode("utf-8"), "POST 测试 🎉")
        self.assertTrue(self.srv.wait_fetched(gen, timeout=0.2))

        # Verify non-localhost client_address gets 403
        class FakeNonLocalHandler(clipboard_http._ClipboardHandler):
            def __init__(self):
                self.path = "/clip"
                self.client_address = ("192.168.1.50", 55555)
                self.headers = {"Content-Length": "4"}
                self.status_sent = None
                self.body_sent = b""

            def send_response(self, code, message=None):
                self.status_sent = code

            def send_header(self, keyword, value):
                pass

            def end_headers(self):
                pass

            @property
            def wfile(self):
                parent = self

                class _W:
                    def write(self, b):
                        parent.body_sent += b

                    def flush(self):
                        pass

                return _W()

        h = FakeNonLocalHandler()
        h.do_POST()
        self.assertEqual(h.status_sent, 403)
        self.assertIn("localhost_only", h.body_sent.decode("utf-8"))

    def test_detect_lan_ip_and_pbcopy_mocked(self):
        ip = clipboard_http.detect_lan_ip()
        self.assertTrue(isinstance(ip, str) and len(ip) > 0)
        with mock.patch("clipboard_http.subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0)
            self.assertTrue(clipboard_http.copy_to_macos_pbcopy("hello"))
            run_mock.assert_called_once()

    def test_get_clipboard_server_singleton(self):
        prev = clipboard_http._SERVER_SINGLETON
        clipboard_http._SERVER_SINGLETON = None
        try:
            s1 = clipboard_http.get_clipboard_server(port=0)
            s2 = clipboard_http.get_clipboard_server(port=0)
            self.assertIs(s1, s2)
            s1.close()
            s3 = clipboard_http.get_clipboard_server(port=0)
            self.assertIsNot(s1, s3)
            s3.close()
        finally:
            clipboard_http._SERVER_SINGLETON = prev

    def test_cross_process_remote_clipboard_client(self):
        prev = clipboard_http._SERVER_SINGLETON
        clipboard_http._SERVER_SINGLETON = None
        s0 = clipboard_http.ClipboardMailboxServer(port=0)
        try:
            remote = clipboard_http.get_clipboard_server(port=s0.port)
            self.assertIsInstance(remote, clipboard_http._RemoteClipboardClient)
            self.assertEqual(remote.port, s0.port)
            self.assertEqual(remote.clip_url, s0.clip_url)
            self.assertEqual(remote.setup_url, s0.setup_url)

            gen = remote.stage("跨进程测试 🚀")
            self.assertFalse(remote.wait_fetched(gen, timeout=0.04))

            conn = http.client.HTTPConnection("127.0.0.1", s0.port, timeout=2.0)
            try:
                conn.request("GET", "/clip")
                resp = conn.getresponse()
                status, raw = resp.status, resp.read()
            finally:
                conn.close()
            self.assertEqual(status, 200)
            self.assertEqual(raw.decode("utf-8"), "跨进程测试 🚀")
            self.assertTrue(remote.wait_fetched(gen, timeout=0.5))

            conn = http.client.HTTPConnection("127.0.0.1", s0.port, timeout=2.0)
            try:
                conn.request("GET", "/health")
                resp = conn.getresponse()
                status, hraw = resp.status, resp.read()
            finally:
                conn.close()
            self.assertEqual(status, 200)
            health = json.loads(hraw.decode("utf-8"))
            self.assertGreaterEqual(health["fetched_gen"], gen)
            remote.close()
        finally:
            s0.close()
            clipboard_http._SERVER_SINGLETON = prev


if __name__ == "__main__":
    unittest.main()
