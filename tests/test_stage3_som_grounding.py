"""Offline unit tests for Stage 3 Set-of-Mark icon grounding & USB capture yield."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import iphone_agent
import iphone_ctl
import som_grounding


class FakeBridge:
    def __init__(self):
        self.cmds: list[str] = []

    def cmd(self, command: str, timeout: float = 6.0) -> str:
        self.cmds.append(command)
        if command == "target":
            return "ok ble ble_hid=1 usb_hid=1 abs=1"
        return "ok"

    def raw_line(self, line: str, timeout: float = 3.5) -> str:
        return "0 {}"

    def status_json(self) -> dict:
        return {"fw": "0.3.0", "abs": 1}

    def close(self):
        pass


class _FakeHTTPResp:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._data


class TestStage3SomGrounding(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ui_jpg = os.path.join(self.tmpdir.name, "ui_720x1558.jpg")
        self.live_jpg = os.path.join(self.tmpdir.name, "live.jpg")
        self.last_run = os.path.join(self.tmpdir.name, "last_run.json")
        self.calib_path = os.path.join(self.tmpdir.name, "iphone_calibration.json")
        self.pos_path = os.path.join(self.tmpdir.name, "iphone_pointer_pos.json")
        self.usb_hb = os.path.join(self.tmpdir.name, "usb_hb.json")
        self._make_synthetic_ui(self.ui_jpg)

        self.patchers = [
            mock.patch.object(iphone_ctl, "LIVE_JPG", self.live_jpg),
            mock.patch.object(iphone_ctl, "CALIB_FILE", self.calib_path),
            mock.patch.object(iphone_ctl, "POINTER_POS_FILE", self.pos_path),
            mock.patch.object(iphone_ctl, "USB_HEARTBEAT_FILE", self.usb_hb),
            mock.patch.object(iphone_agent, "LIVE_JPG", self.live_jpg),
            mock.patch.object(iphone_agent, "RUN_TMP_DIR", self.tmpdir.name),
            mock.patch.object(iphone_agent, "LAST_RUN_FILE", self.last_run),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmpdir.cleanup()

    @staticmethod
    def _make_synthetic_ui(path: str):
        im = Image.new("RGB", (720, 1558), (248, 248, 250))
        d = ImageDraw.Draw(im)
        # Status bar noise at y=30 (must be masked out: y < 0.045 * 1558 = 70.1)
        d.rectangle((28, 18, 86, 42), outline=(20, 20, 20), width=3)
        d.rectangle((620, 20, 680, 40), outline=(20, 20, 20), width=3)

        # '<' back chevron (top-left around x=54, y=130)
        d.line([(64, 114), (44, 130), (64, 146)], fill=(25, 25, 25), width=4)

        # 'x' close icon (top-right around x=664, y=130)
        d.line([(650, 116), (678, 144)], fill=(25, 25, 25), width=4)
        d.line([(678, 116), (650, 144)], fill=(25, 25, 25), width=4)

        # Search magnifying glass icon in search bar (around x=120, y=290)
        d.ellipse((104, 274, 130, 300), outline=(60, 60, 60), width=3)
        d.line([(127, 297), (140, 310)], fill=(60, 60, 60), width=3)

        # OCR-covered title & SpringBoard search label (must be excluded by OCR mask)
        d.rectangle((280, 112, 440, 148), outline=(40, 40, 40), width=3)
        d.rectangle((310, 1285, 410, 1315), outline=(40, 40, 40), width=3)

        im.save(path, quality=92)

    def test_detect_unlabeled_icons_and_masks(self):
        ocr_items = [
            {"text": "设置", "x": 360, "y": 130, "w": 160, "h": 36},
            {"text": "搜索", "x": 360, "y": 1300, "w": 100, "h": 30},
        ]
        icons = som_grounding.detect_unlabeled_icons(self.ui_jpg, ocr_items)
        self.assertGreaterEqual(len(icons), 3)

        # None in top status bar or OCR-covered title
        for ic in icons:
            self.assertGreaterEqual(ic["y"], int(0.045 * 1558))
            self.assertFalse(abs(ic["x"] - 360) < 90 and abs(ic["y"] - 130) < 28)

        # Verify '<', '×', '🔍' centers & zones
        back_icon = min(icons, key=lambda d: abs(d["x"] - 54) + abs(d["y"] - 130))
        self.assertLessEqual(abs(back_icon["x"] - 54), 6)
        self.assertLessEqual(abs(back_icon["y"] - 130), 6)
        self.assertEqual(back_icon["zone"], "左上")
        self.assertEqual(back_icon["text"], "<图标:左上>")

        close_icon = min(icons, key=lambda d: abs(d["x"] - 664) + abs(d["y"] - 130))
        self.assertLessEqual(abs(close_icon["x"] - 664), 6)
        self.assertLessEqual(abs(close_icon["y"] - 130), 6)
        self.assertEqual(close_icon["zone"], "右上")

        search_icon = min(icons, key=lambda d: abs(d["x"] - 120) + abs(d["y"] - 290))
        self.assertLessEqual(abs(search_icon["x"] - 120), 6)
        self.assertLessEqual(abs(search_icon["y"] - 290), 6)
        self.assertEqual(search_icon["zone"], "图标")

    def test_render_som_jpeg_valid_and_fast(self):
        ocr_items = [{"text": "设置", "x": 360, "y": 130, "w": 160, "h": 36}]
        icons = som_grounding.detect_unlabeled_icons(self.ui_jpg, ocr_items)
        t0 = time.perf_counter()
        b64 = som_grounding.render_som_jpeg(self.ui_jpg, ocr_items + icons, max_width=360, quality=72)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        raw = base64.b64decode(b64)
        with Image.open(io.BytesIO(raw)) as out_im:
            self.assertEqual(out_im.format, "JPEG")
            self.assertEqual(out_im.size, (360, 779))
        self.assertLess(elapsed_ms, 200.0)

    def test_run_goal_clicks_icon_without_springboard_offset_and_injects_stuck_warning(self):
        with open(self.ui_jpg, "rb") as src, open(self.live_jpg, "wb") as dst:
            dst.write(src.read())

        ocr_items = [
            {"text": "设置", "x": 360, "y": 130, "w": 160, "h": 36},
            {"text": "搜索", "x": 360, "y": 1300, "w": 100, "h": 30},
        ]
        # Pre-compute expected icon index in unified elements list (ocr_items has 2 items -> first icon is id=2)
        icons = som_grounding.detect_unlabeled_icons(self.ui_jpg, ocr_items, max_icons=14)
        left_top_idx = next(
            len(ocr_items) + i for i, ic in enumerate(icons) if ic["zone"] == "左上"
        )
        expected_icon = icons[left_top_idx - len(ocr_items)]
        # Verify its h <= 34 or any height wouldn't get -60 offset even when on_home=True
        expected_ax = round(expected_icon["x"] / 720.0 * 10000)
        expected_ay = round(expected_icon["y"] / 1558.0 * 10000)

        captured_prompts: list[str] = []

        class _FakeOpener:
            def __init__(self):
                self.calls = 0

            def open(self, req, timeout=35):
                body = json.loads(req.data.decode("utf-8"))
                captured_prompts.append(body["messages"][0]["content"])
                self.calls += 1
                if self.calls == 1:
                    reply = {"action": "tap_ocr", "id": left_top_idx, "target": "<图标:左上>"}
                else:
                    reply = {"action": "done", "summary": "已响应系统警告并结束"}
                return _FakeHTTPResp({"message": {"content": json.dumps(reply, ensure_ascii=False)}})

        fake_ocr = mock.MagicMock()
        fake_ocr.recognize.return_value = ocr_items
        bridge = FakeBridge()

        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge), mock.patch.object(
            iphone_agent, "PersistentOCR", return_value=fake_ocr
        ), mock.patch("iphone_agent.urllib.request.build_opener", return_value=_FakeOpener()), mock.patch(
            "iphone_agent.frames_changed", return_value=False
        ), mock.patch.object(
            iphone_ctl.IPhoneController, "wait_settled", return_value=0.0
        ), mock.patch("iphone_ctl.time.sleep", return_value=None):
            agent = iphone_agent.FourTierIPhoneAgent()
            res = agent.run_goal("点左上角返回", max_steps=3)

        self.assertEqual(res["status"], "done")
        self.assertEqual(len(captured_prompts), 2)
        self.assertIn("<图标:左上>", captured_prompts[0])
        self.assertNotIn("【系统警告】", captured_prompts[0])
        self.assertIn("【系统警告】上一步点击后画面无变化", captured_prompts[1])
        # Verify tap landed at exact icon Y (not Y - 60); tx < 75 triggers aim=True -> abs + click
        self.assertIn(f"abs {expected_ax} {expected_ay}", bridge.cmds)
        self.assertIn("click", bridge.cmds)

    def test_usb_capture_active_and_status_flag(self):
        # Missing file -> False
        self.assertFalse(iphone_ctl.usb_capture_active())

        # Fresh active heartbeat -> True
        with open(self.usb_hb, "w", encoding="utf-8") as f:
            json.dump({"active": True, "ts": time.time(), "w": 720, "h": 1558}, f)
        self.assertTrue(iphone_ctl.usb_capture_active())

        # CLI status reports usb_capture: true
        buf = io.StringIO()
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=FakeBridge()), contextlib.redirect_stdout(buf):
            iphone_ctl.main(["status"])
        self.assertIn('"usb_capture": true', buf.getvalue())

        # Stale heartbeat -> False
        with open(self.usb_hb, "w", encoding="utf-8") as f:
            json.dump({"active": True, "ts": time.time() - 5.0}, f)
        self.assertFalse(iphone_ctl.usb_capture_active())

        # Inactive heartbeat -> False
        with open(self.usb_hb, "w", encoding="utf-8") as f:
            json.dump({"active": False, "ts": time.time()}, f)
        self.assertFalse(iphone_ctl.usb_capture_active())

    def test_spotlight_safety_net_triggers_on_bottom_open_pill(self):
        bridge = FakeBridge()
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge):
            ctl = iphone_ctl.IPhoneController()
        bridge.cmds.clear()

        fake_ocr = mock.MagicMock()
        fake_ocr.recognize.side_effect = [
            [],  # initial leftover check
            [
                {"text": "最传搜索结果", "x": 140, "y": 140, "w": 160, "h": 28},
                {"text": "设置", "x": 118, "y": 280, "w": 60, "h": 28},
                {"text": "设置 — 打开", "x": 320, "y": 1445, "w": 180, "h": 34},
            ],
        ]
        with mock.patch.object(ctl, "wait_settled", return_value=0.0), mock.patch.object(
            ctl, "frame_size", return_value=(720, 1558)
        ), mock.patch("iphone_ctl.time.sleep", return_value=None):
            ctl.open_app_spotlight("设置", raw_ascii=True, ocr=fake_ocr)

        # Safety net tapped the app icon above `设置` (`y = 280 - 55 = 225`)
        expected_ax = round(118 / 720.0 * 10000)
        expected_ay = round(225 / 1558.0 * 10000)
        self.assertIn(f"abs {expected_ax} {expected_ay}", bridge.cmds)
        self.assertIn("click", bridge.cmds)


if __name__ == "__main__":
    unittest.main()
