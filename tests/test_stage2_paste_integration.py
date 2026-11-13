"""Offline integration tests for Stage 2 clipboard paste & `FourTierIPhoneAgent.type_smart`."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import iphone_agent
import iphone_ctl


class FakeBridge:
    def __init__(self, on_cmd=None):
        self.cmds: list[str] = []
        self.on_cmd = on_cmd
        self.closed = False

    def cmd(self, command: str, timeout: float = 6.0) -> str:
        self.cmds.append(command)
        if self.on_cmd is not None:
            self.on_cmd(command)
        if command == "target":
            return "ok ble ble_hid=1 usb_hid=1 abs=1"
        return "ok"

    def raw_line(self, line: str, timeout: float = 3.5) -> str:
        return "0 {}"

    def status_json(self) -> dict:
        return {"fw": "0.3.0", "abs": 1}

    def close(self):
        self.closed = True


class TestStage2PasteIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.calib_path = os.path.join(self.tmpdir.name, "iphone_calibration.json")
        self.pos_path = os.path.join(self.tmpdir.name, "iphone_pointer_pos.json")
        self.live_jpg = os.path.join(self.tmpdir.name, "live.jpg")

        self.patchers = [
            mock.patch.object(iphone_ctl, "CALIB_FILE", self.calib_path),
            mock.patch.object(iphone_ctl, "POINTER_POS_FILE", self.pos_path),
            mock.patch.object(iphone_ctl, "LIVE_JPG", self.live_jpg),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmpdir.cleanup()

    def _make_ctl(self, on_cmd=None):
        bridge = FakeBridge(on_cmd=on_cmd)
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge):
            ctl = iphone_ctl.IPhoneController()
        return ctl, bridge

    def test_http_clipboard_paste_end_to_end_under_250ms(self):
        from clipboard_http import ClipboardMailboxServer

        srv = ClipboardMailboxServer(port=0)
        fetched_bodies: list[str] = []
        timer_holder: list[threading.Timer] = []

        def fetch_clip():
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=1.0)
            try:
                conn.request("GET", "/clip")
                resp = conn.getresponse()
                fetched_bodies.append(resp.read().decode("utf-8"))
            finally:
                conn.close()

        def on_cmd(command: str):
            if command == "click 3":
                t = threading.Timer(0.04, fetch_clip)
                timer_holder.append(t)
                t.start()

        ctl, bridge = self._make_ctl(on_cmd=on_cmd)
        bridge.cmds.clear()

        payload = "你好🚀猎聘测试\n第二行 https://example.com"
        try:
            with mock.patch("iphone_ctl.get_clipboard_server", return_value=srv), mock.patch.object(
                ctl, "wait_settled", return_value=0.0
            ):
                res = ctl.paste_text(payload, trigger="btn:3", timeout=1.50)
        finally:
            for t in timer_holder:
                t.join(timeout=1.0)
            srv.close()

        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "http_clip")
        self.assertEqual(res["chars"], len(payload))
        self.assertLess(res["ms"], 900.0)
        self.assertEqual(fetched_bodies, [payload])
        self.assertEqual(bridge.cmds, ["click 3", "key cmd+v"])

    def test_timeout_falls_back_to_ziranma_multiline_and_strips_astral(self):
        from clipboard_http import ClipboardMailboxServer

        srv = ClipboardMailboxServer(port=0)
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        typed_lines: list[str] = []

        def fake_ziranma(line: str, ocr=None):
            typed_lines.append(line)
            return [{"word": line, "kind": "zh"}]

        try:
            with mock.patch("iphone_ctl.get_clipboard_server", return_value=srv), mock.patch.object(
                ctl, "type_ziranma", side_effect=fake_ziranma
            ), mock.patch("iphone_ctl.time.sleep", return_value=None):
                res = ctl.paste_text("第一行🚀\n第二行", trigger="btn:3", timeout=0.05, fallback_ime=True)
                res_no_fb = ctl.paste_text("超时测试", trigger="key:ctrl+alt+v", timeout=0.02, fallback_ime=False)
        finally:
            srv.close()

        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "fallback_ziranma")
        self.assertEqual(typed_lines, ["第一行", "第二行"])
        self.assertEqual(bridge.cmds, ["click 3", "key enter", "key ctrl+alt+v"])

        self.assertFalse(res_no_fb["ok"])
        self.assertEqual(res_no_fb["error"], "clip_fetch_timeout")
        self.assertEqual(res_no_fb["setup_url"], srv.setup_url)

    def test_pbcopy_mode_and_fallback_pbcopy(self):
        from clipboard_http import ClipboardMailboxServer

        srv = ClipboardMailboxServer(port=0)
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        copied: list[str] = []

        def fake_pbcopy(text: str) -> bool:
            copied.append(text)
            return True

        try:
            with mock.patch("iphone_ctl.copy_to_macos_pbcopy", side_effect=fake_pbcopy), mock.patch(
                "iphone_ctl.get_clipboard_server", return_value=srv
            ), mock.patch.object(ctl, "wait_settled", return_value=0.0), mock.patch(
                "iphone_ctl.time.sleep", return_value=None
            ):
                res_direct = ctl.paste_text("直接pbcopy", trigger="pbcopy")
                with mock.patch.dict(os.environ, {"IPHONE_PBCOPY_FALLBACK": "1"}):
                    res_fb = ctl.paste_text("超时转pbcopy", trigger="btn:3", timeout=0.02)
        finally:
            srv.close()

        self.assertEqual(res_direct["mode"], "pbcopy")
        self.assertEqual(res_fb["mode"], "fallback_pbcopy")
        self.assertEqual(copied, ["直接pbcopy", "超时转pbcopy"])
        self.assertEqual(bridge.cmds, ["key cmd+v", "click 3", "key cmd+v"])

    def test_agent_type_smart_routing(self):
        bridge = FakeBridge()
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge), mock.patch.object(
            iphone_agent, "PersistentOCR", return_value=mock.MagicMock()
        ):
            agent = iphone_agent.FourTierIPhoneAgent()

        with mock.patch.object(agent.ctl, "type_ziranma", return_value=[{"word": "闹钟"}]) as mock_zh, mock.patch.object(
            agent.ctl, "type_text"
        ) as mock_txt, mock.patch.object(agent.ctl, "paste_text", return_value={"ok": True, "mode": "http_clip"}) as mock_paste:
            agent.type_smart("闹钟")
            mock_zh.assert_called_once_with("闹钟", ocr=agent.ocr)
            mock_paste.assert_not_called()

            agent.type_smart("12345")
            mock_txt.assert_called_once_with("12345")
            mock_paste.assert_not_called()

            for payload in ("你好🚀测试\n第二行", "https://example.com", "超过八个字符的中文测试"):
                agent.type_smart(payload)
            self.assertEqual(mock_paste.call_count, 3)

            with mock.patch.dict(os.environ, {"IPHONE_ALWAYS_PASTE": "1"}):
                agent.type_smart("hi")
            self.assertEqual(mock_paste.call_count, 4)

    def test_cli_paste_and_clip_serve(self):
        from clipboard_http import ClipboardMailboxServer

        srv = ClipboardMailboxServer(port=0)
        buf = io.StringIO()
        try:
            with mock.patch("iphone_ctl.IPhoneBridge", side_effect=AssertionError("must not open serial")), mock.patch(
                "iphone_ctl.get_clipboard_server", return_value=srv
            ), mock.patch("iphone_ctl.time.sleep", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(buf):
                iphone_ctl.main(["clip_serve"])
        finally:
            srv.close()

        serve_out = json.loads(buf.getvalue().strip())
        self.assertTrue(serve_out["ok"])
        self.assertEqual(serve_out["clip_url"], srv.clip_url)
        self.assertEqual(serve_out["setup_url"], srv.setup_url)

        bridge = FakeBridge()
        buf_paste = io.StringIO()
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge), mock.patch.object(
            iphone_ctl.IPhoneController, "paste_text", return_value={"ok": True, "mode": "http_clip", "chars": 5, "ms": 120.0}
        ) as mock_paste, contextlib.redirect_stdout(buf_paste):
            iphone_ctl.main(["paste", "hello", "world"])
        mock_paste.assert_called_once_with("hello world")
        self.assertEqual(json.loads(buf_paste.getvalue().strip())["mode"], "http_clip")


if __name__ == "__main__":
    unittest.main()
