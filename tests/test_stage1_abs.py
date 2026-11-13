"""Offline unit tests for Stage 1 absolute-mouse host controller (`iphone_ctl.py`)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import iphone_ctl


class FakeBridge:
    def __init__(self, target_resp: str = "ok ble ble_hid=1 usb_hid=1 abs=1"):
        self.target_resp = target_resp
        self.cmds: list[str] = []
        self.closed = False

    def cmd(self, command: str, timeout: float = 6.0) -> str:
        self.cmds.append(command)
        if command == "target":
            return self.target_resp
        return "ok"

    def raw_line(self, line: str, timeout: float = 3.5) -> str:
        return "0 {}"

    def status_json(self) -> dict:
        return {"fw": "0.3.0", "abs": 1}

    def close(self):
        self.closed = True


class TestStage1AbsoluteController(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.calib_path = os.path.join(self.tmpdir.name, "iphone_calibration.json")
        self.pos_path = os.path.join(self.tmpdir.name, "iphone_pointer_pos.json")
        self.live_jpg = os.path.join(self.tmpdir.name, "live.jpg")
        self.aim0 = os.path.join(self.tmpdir.name, "aim0.jpg")
        self.aim1 = os.path.join(self.tmpdir.name, "aim1.jpg")
        self.aim2 = os.path.join(self.tmpdir.name, "aim2.jpg")

        self.patchers = [
            mock.patch.object(iphone_ctl, "CALIB_FILE", self.calib_path),
            mock.patch.object(iphone_ctl, "POINTER_POS_FILE", self.pos_path),
            mock.patch.object(iphone_ctl, "LIVE_JPG", self.live_jpg),
            mock.patch.object(iphone_ctl, "AIM_BEFORE_JPG", self.aim0),
            mock.patch.object(iphone_ctl, "AIM_AFTER_JPG", self.aim1),
            mock.patch.object(iphone_ctl, "AIM_BACK_JPG", self.aim2),
            mock.patch("iphone_ctl.time.sleep", return_value=None),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmpdir.cleanup()

    def _make_ctl(self, target_resp: str = "ok ble ble_hid=1 usb_hid=1 abs=1", env: dict | None = None):
        bridge = FakeBridge(target_resp=target_resp)
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge):
            with mock.patch.dict(os.environ, env or {}, clear=False):
                ctl = iphone_ctl.IPhoneController()
        return ctl, bridge

    def test_auto_detection_abs_and_fallback(self):
        ctl, bridge = self._make_ctl("ok ble ble_hid=1 usb_hid=1 abs=1")
        self.assertTrue(ctl.abs_mode)
        self.assertEqual(bridge.cmds[:3], ["human off", "target ble", "target"])

        ctl_old, _ = self._make_ctl("ok ble ble_hid=1 usb_hid=1")
        self.assertFalse(ctl_old.abs_mode)

        ctl_forced, _ = self._make_ctl("ok ble ble_hid=1 usb_hid=1 abs=1", env={"IPHONE_FORCE_REL": "1"})
        self.assertFalse(ctl_forced.abs_mode)

    def test_frac_to_abs_ignores_relative_offsets_and_uses_abs_bounds(self):
        with open(self.calib_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "w": 511,
                    "h": 1285,
                    "off_x": 0.032,
                    "off_y": 0.02,
                    "seen": {"x": [[0.5, 0.45], [0.8, 0.72]], "y": [[0.5, 0.52], [0.8, 0.81]]},
                },
                f,
            )
        ctl, _ = self._make_ctl()
        self.assertEqual(ctl._frac_to_abs(0.0, 0.0), (0, 0))
        self.assertEqual(ctl._frac_to_abs(0.5, 0.25), (5000, 2500))
        self.assertEqual(ctl._frac_to_abs(1.0, 1.0), (10000, 10000))
        self.assertEqual(ctl._frac_to_abs(-0.3, 1.4), (0, 10000))

        with open(self.calib_path, "w", encoding="utf-8") as f:
            json.dump({"w": 511, "h": 1285, "abs_x0": -100.0, "abs_x1": 10100.0, "abs_y0": 200.0, "abs_y1": 9800.0}, f)
        ctl2, _ = self._make_ctl()
        self.assertEqual(ctl2._frac_to_abs(0.0, 0.0), (0, 200))
        self.assertEqual(ctl2._frac_to_abs(0.5, 0.5), (5000, 5000))
        self.assertEqual(ctl2._frac_to_abs(0.2, 0.8), (1940, 7880))
        self.assertEqual(ctl2._frac_to_abs(1.0, 1.0), (10000, 9800))

    def test_tap_frac_and_tap_px_abs_fast_path(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        with mock.patch("iphone_ctl.time.sleep") as mock_sleep, mock.patch(
            "iphone_ctl.time.monotonic", side_effect=[100.0, 100.001, 101.0, 101.001, 120.0, 120.001]
        ):
            info = ctl.tap_frac(0.5, 0.4)
            self.assertEqual(info, {"ax": 5000, "ay": 4000, "abs": True, "aimed": False})
            self.assertEqual(bridge.cmds, ["abs 5000 4000", "click"])
            self.assertEqual(mock_sleep.call_args_list[-1][0][0], 0.085)

            bridge.cmds.clear()
            with mock.patch.object(ctl, "frame_size", return_value=(720, 1558)):
                ctl.double_tap_px(360, 779)
            self.assertEqual(bridge.cmds, ["abs 5000 5000", "click", "click"])
            self.assertEqual([c[0][0] for c in mock_sleep.call_args_list[-2:]], [0.060, 0.05])

            # After >10s idle, settle returns to 85ms
            bridge.cmds.clear()
            ctl.tap_frac(0.2, 0.3)
            self.assertEqual(bridge.cmds, ["abs 2000 3000", "click"])
            self.assertEqual(mock_sleep.call_args_list[-1][0][0], 0.085)

    def test_point_frac_and_tap_frac_with_aim_abs_mode(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        with mock.patch.object(ctl, "screenshot") as mock_shot, mock.patch(
            "iphone_ctl.locate_pointer_3frame", return_value=(0.48, 0.50)
        ) as mock_loc:
            info = ctl.tap_frac(0.50, 0.50, aim=True, count=2)

        self.assertEqual(mock_shot.call_count, 3)
        self.assertTrue(mock_loc.called)
        # Move to target -> wiggle (+180, back) -> miss 0.02 > 0.006 -> corrected abs -> 2 clicks
        self.assertEqual(
            bridge.cmds,
            [
                "abs 5000 5000",
                "abs 5180 5000",
                "abs 5000 5000",
                "abs 5200 5000",
                "click",
                "click",
            ],
        )
        self.assertEqual(info["corr_ax"], 5200)
        self.assertEqual(info["corr_ay"], 5000)

    def test_point_frac_aim_no_correction_within_threshold(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        with mock.patch.object(ctl, "screenshot"), mock.patch(
            "iphone_ctl.locate_pointer_3frame", return_value=(0.503, 0.498)
        ):
            info = ctl.point_frac(0.50, 0.50, aim=True)

        self.assertEqual(bridge.cmds, ["abs 5000 5000", "abs 5180 5000", "abs 5000 5000"])
        self.assertNotIn("corr_ax", info)

    def test_long_press_and_swipe_and_go_back_abs_mode(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        with mock.patch.object(ctl, "frame_size", return_value=(1000, 2000)):
            info = ctl.long_press_px(250, 1000, duration_s=1.2)
        self.assertEqual(bridge.cmds, ["abs 2500 5000", "press", "release"])
        self.assertEqual(info["hold_ms"], 1200)

        bridge.cmds.clear()
        with mock.patch.object(ctl, "frame_size", return_value=(1000, 2000)), mock.patch.object(
            ctl, "point_frac", return_value={"ax": 2500, "ay": 5000, "abs": True, "aimed": True}
        ):
            ctl.long_press_px(250, 1000, duration_s=0.1, aim=True)
        self.assertEqual(bridge.cmds, ["press", "release"])

        bridge.cmds.clear()
        ctl.swipe_frac(0.5, 0.8, 0.5, 0.2, hold=True)
        ctl.swipe_frac(0.5, 0.8, 0.5, 0.2, hold=False)
        ctl.go_back()
        self.assertEqual(
            bridge.cmds,
            [
                "absswipe 5000 8000 5000 2000 260 160",
                "absswipe 5000 8000 5000 2000 260 20",
                "absswipe 80 5000 6800 5000 220 20",
            ],
        )

    def test_relative_mode_fallback_preserved(self):
        ctl, bridge = self._make_ctl("ok ble ble_hid=1 usb_hid=1")
        self.assertFalse(ctl.abs_mode)
        bridge.cmds.clear()

        ctl.tap_frac(0.5, 0.5)
        self.assertIn("release", bridge.cmds)
        self.assertIn("click", bridge.cmds)
        self.assertTrue(any(c.startswith("move ") for c in bridge.cmds))
        self.assertFalse(any(c.startswith("abs") for c in bridge.cmds))

        bridge.cmds.clear()
        ctl.swipe_frac(0.5, 0.8, 0.5, 0.2, hold=False)
        self.assertIn("press", bridge.cmds)
        self.assertIn("release", bridge.cmds)
        self.assertFalse(any(c.startswith("absswipe") for c in bridge.cmds))

        bridge.cmds.clear()
        ctl.go_back()
        self.assertIn("press", bridge.cmds)
        self.assertIn("release", bridge.cmds)
        self.assertFalse(any(c.startswith("absswipe") for c in bridge.cmds))

    def test_extended_hid_methods_and_validation(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        self.assertEqual(ctl.toggle_keyboard(), "ok")
        self.assertEqual(ctl.power_key(), "ok")
        self.assertEqual(ctl.spotlight_key(), "ok")
        self.assertEqual(ctl.consumer(184), "ok")
        self.assertEqual(ctl.click_button(3), "ok")
        self.assertEqual(
            bridge.cmds,
            [
                "consumer 0x00B8",
                "consumer 0x0030",
                "consumer 0x0221",
                "consumer 0x00B8",
                "click 3",
            ],
        )

        for bad_btn in (0, 6, -1, True):
            with self.assertRaises(ValueError):
                ctl.click_button(bad_btn)
        for bad_usage in (0, "0x0400", "not_a_number", True):
            with self.assertRaises(ValueError):
                ctl.consumer(bad_usage)

    def test_calibrate_abs_fits_and_saves_bounds(self):
        ctl, bridge = self._make_ctl()
        bridge.cmds.clear()

        with mock.patch.object(ctl, "measure_pointer", side_effect=[(0.19, 0.21), (0.79, 0.81)]):
            diag = ctl.calibrate_abs()

        self.assertEqual(diag["abs_x0"], 100.0)
        self.assertEqual(diag["abs_x1"], 10100.0)
        self.assertEqual(diag["abs_y0"], -100.0)
        self.assertEqual(diag["abs_y1"], 9900.0)
        self.assertEqual(bridge.cmds, ["abs 2000 2000", "abs 8000 8000"])

        with open(self.calib_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["abs_x0"], 100.0)
        self.assertEqual(saved["abs_x1"], 10100.0)
        self.assertEqual(saved["abs_y0"], -100.0)
        self.assertEqual(saved["abs_y1"], 9900.0)

        ctl_rel, _ = self._make_ctl("ok ble ble_hid=1 usb_hid=1")
        with self.assertRaises(RuntimeError):
            ctl_rel.calibrate_abs()

    def test_cli_subcommands(self):
        import contextlib
        import io

        def run_cli(args: list[str], target_resp: str = "ok ble ble_hid=1 usb_hid=1 abs=1"):
            bridge = FakeBridge(target_resp=target_resp)
            buf = io.StringIO()
            with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge):
                with contextlib.redirect_stdout(buf):
                    iphone_ctl.main(args)
            return buf.getvalue().strip(), bridge

        out_status, _ = run_cli(["status"])
        self.assertIn('"abs_mode": true', out_status)

        out_abstap, b_tap = run_cli(["abstap", "0.25", "0.75"])
        self.assertEqual(json.loads(out_abstap), {"ax": 2500, "ay": 7500, "abs": True, "aimed": False})
        self.assertIn("abs 2500 7500", b_tap.cmds)
        self.assertIn("click", b_tap.cmds)

        out_cons, b_cons = run_cli(["consumer", "0x00B8"])
        self.assertEqual(out_cons, "ok")
        self.assertIn("consumer 0x00B8", b_cons.cmds)

        out_kbd, b_kbd = run_cli(["kbd_toggle"])
        self.assertEqual(out_kbd, "ok")
        self.assertIn("consumer 0x00B8", b_kbd.cmds)

        out_pwr, b_pwr = run_cli(["power"])
        self.assertEqual(out_pwr, "ok")
        self.assertIn("consumer 0x0030", b_pwr.cmds)

        out_btn, b_btn = run_cli(["btn", "3"])
        self.assertEqual(out_btn, "ok")
        self.assertIn("click 3", b_btn.cmds)

        bridge_cal = FakeBridge()
        buf = io.StringIO()
        with mock.patch("iphone_ctl.IPhoneBridge", return_value=bridge_cal), mock.patch.object(
            iphone_ctl.IPhoneController, "measure_pointer", side_effect=[(0.20, 0.20), (0.80, 0.80)]
        ), contextlib.redirect_stdout(buf):
            iphone_ctl.main(["calibrate_abs"])
        cal_out = json.loads(buf.getvalue().strip())
        self.assertEqual(cal_out["abs_x0"], 0.0)
        self.assertEqual(cal_out["abs_x1"], 10000.0)


if __name__ == "__main__":
    unittest.main()
