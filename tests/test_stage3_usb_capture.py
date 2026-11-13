"""Offline unit tests for Stage 3 CoreMediaIO USB capture CLI (`usb_capture`)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_PATH = os.path.join(REPO_ROOT, "usb_capture")


class TestStage3UsbCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BIN_PATH):
            subprocess.run(
                ["swiftc", "-O", os.path.join(REPO_ROOT, "usb_capture.swift"), "-o", BIN_PATH],
                check=True,
                timeout=30,
            )

    def test_help_flag(self):
        proc = subprocess.run([BIN_PATH, "--help"], capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--list", proc.stdout)
        self.assertIn("--heartbeat", proc.stdout)
        self.assertIn("--max-fps", proc.stdout)

    def test_list_json_output(self):
        proc = subprocess.run([BIN_PATH, "--list"], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout.strip())
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["devices"], list)
        for dev in data["devices"]:
            self.assertIn("id", dev)
            self.assertIn("name", dev)
            self.assertIn("model", dev)
            self.assertNotIn("facetime", dev["name"].lower())

    def test_daemon_writes_inactive_heartbeat_when_no_usb_device(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_jpg = os.path.join(tmpdir, "live.jpg")
            hb_json = os.path.join(tmpdir, "hb.json")
            proc = subprocess.Popen(
                [BIN_PATH, "--out", out_jpg, "--heartbeat", hb_json, "--udid", "NONEXISTENT_TEST_UDID"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not os.path.exists(hb_json):
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(hb_json))
                with open(hb_json, "r", encoding="utf-8") as f:
                    hb = json.load(f)
                self.assertFalse(hb["active"])
                self.assertIsInstance(hb["ts"], (int, float))
            finally:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
