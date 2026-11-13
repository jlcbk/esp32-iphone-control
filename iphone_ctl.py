#!/usr/bin/env python3
"""
Pure USB-Serial + Persistent Wi-Fi TCP (:7201) controller for ESP32-S3 iPhone Agent.
Never touches macOS Bluetooth — 100% of BLE stays dedicated to the iPhone HID Mouse.

Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
"""

from __future__ import annotations

import glob
import io
import json
import math
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import serial

try:
    from PIL import Image, ImageChops
except ImportError:
    Image = ImageChops = None

try:
    from clipboard_http import copy_to_macos_pbcopy, get_clipboard_server
except ImportError:
    copy_to_macos_pbcopy = None  # type: ignore[assignment]
    get_clipboard_server = None  # type: ignore[assignment]

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(dotenv_path: str | None = None) -> None:
    """Load key=value pairs from local .env without external dependencies (existing env vars win)."""
    path = dotenv_path or os.path.join(WORKSPACE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key:
                    os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv()

_LOCAL_FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
FFMPEG_BIN = shutil.which("ffmpeg") or (_LOCAL_FFMPEG if os.path.exists(_LOCAL_FFMPEG) else "ffmpeg")
LIVE_JPG = "/tmp/iphone_live.jpg"
USB_HEARTBEAT_FILE = "/tmp/iphone_usb_capture.json"
STREAM_CACHE = "/tmp/iphone_stream_cache.h264"
STREAM_TS_FILE = "/tmp/iphone_stream_ts.json"
POINTER_POS_FILE = "/tmp/iphone_pointer_pos.json"
CALIB_FILE = os.path.join(WORKSPACE_DIR, "iphone_calibration.json")


def usb_capture_active(heartbeat_path: str | None = None, max_age: float = 2.0) -> bool:
    """Return True iff the native CoreMediaIO USB capture daemon reported active within `max_age` seconds."""
    path = heartbeat_path or USB_HEARTBEAT_FILE
    try:
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return (
            data.get("active") is True
            and (time.time() - float(data.get("ts", 0))) <= max_age
        )
    except Exception:
        return False

VF_START = 1
VF_END = 2
VF_IDR = 4
VF_CONFIG = 8
WV_AUDIO = 0x80


def find_serial_port() -> str:
    env_port = os.environ.get("ESP32_SERIAL_PORT", "").strip()
    if env_port:
        return env_port
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    if ports:
        return ports[0]
    raise RuntimeError("ESP32-S3 USB CDC port (/dev/cu.usbmodem*) not found; set ESP32_SERIAL_PORT in .env")


class IPhoneBridge:
    def __init__(self, port=None, baud=115200):
        self.port = port or find_serial_port()
        self.ser = serial.Serial(self.port, baud, timeout=3.0)
        self.req_id = random.randint(1000, 8000)

    def raw_line(self, line: str, timeout: float = 3.5) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((line.strip() + "\n").encode("utf-8"))
        self.ser.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.ser.readline().decode("utf-8", errors="replace").strip()
            if resp:
                return resp
        return ""

    def cmd(self, command: str, timeout: float = 6.0) -> str:
        self.req_id += 1
        rid = str(self.req_id)
        self.ser.reset_input_buffer()
        self.ser.write(f"{rid} {command.strip()}\n".encode("utf-8"))
        self.ser.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self.ser.readline().decode("utf-8", errors="replace").strip()
            if resp.startswith(rid + " "):
                return resp[len(rid) + 1:]
        return "timeout"

    def status_json(self) -> dict:
        resp = self.raw_line("0 status")
        if resp.startswith("0 {"):
            return json.loads(resp[2:])
        raise RuntimeError(f"Bad status response: {resp}")

    def netinfo_json(self) -> dict:
        resp = self.raw_line("0 netinfo")
        if resp.startswith("0 {"):
            return json.loads(resp[2:])
        raise RuntimeError(f"Bad netinfo response: {resp}")

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()


def run_video_daemon(out_path=LIVE_JPG):
    """
    Zero-buffering persistent TCP :7201 receiver + in-process PyAV (`libavcodec` SLICE-threaded)
    H.264 decoder with seamless `hello <vtok> at <last_ts>` resume.
    Updates /tmp/iphone_live.jpg in ~0.8ms per frame without external subprocesses or pipe buffering.
    """
    import av

    print(f"[video-daemon] Starting PyAV zero-latency stream -> {out_path}", flush=True)
    tmp_jpg = out_path + ".tmp.jpg"

    def make_decoder():
        c = av.CodecContext.create("h264", "r")
        c.thread_type = "SLICE"
        return c

    codec = make_decoder()
    last_save = 0.0
    decode_errs = 0

    def feed_bytes(h264_bytes: bytes, emit: bool = True, force_emit: bool = False):
        nonlocal last_save, decode_errs
        last_frame = None
        for pkt in codec.parse(h264_bytes):
            try:
                for f in codec.decode(pkt):
                    last_frame = f
                    decode_errs = 0
            except Exception:
                decode_errs += 1
        if emit and last_frame is not None:
            now = time.monotonic()
            if force_emit or (now - last_save) >= 0.05:
                last_save = now
                if not usb_capture_active():
                    try:
                        last_frame.to_image().save(tmp_jpg, format="JPEG", quality=85)
                        os.replace(tmp_jpg, out_path)
                    except Exception:
                        pass

    ip = vtok = None
    last_ts = None
    pending_cfg = b""

    # Warm-start from local stream cache if ESP32 is still on the same boot/session
    if os.path.exists(STREAM_CACHE) and os.path.exists(STREAM_TS_FILE):
        try:
            saved_ts = json.load(open(STREAM_TS_FILE))["ts"]
            b = IPhoneBridge()
            st = b.status_json()
            ni = b.netinfo_json()
            b.close()
            ip, vtok = ni["ip"], ni["vtok"]
            if st.get("fz") and st.get("up", 0) > saved_ts and os.path.getsize(STREAM_CACHE) > 256:
                cache_bytes = open(STREAM_CACHE, "rb").read()
                t0 = time.monotonic()
                feed_bytes(cache_bytes + b"\x00\x00\x00\x01\x09\xf0", emit=True, force_emit=True)
                print(
                    f"[video-daemon] Warm-started PyAV with {len(cache_bytes)} bytes in {time.monotonic()-t0:.2f}s @ ts={saved_ts}",
                    flush=True,
                )
                last_ts = saved_ts
        except Exception as e:
            print(f"[video-daemon] Warm-start skipped: {e}", flush=True)

    while True:
        sock = None
        try:
            if not ip or not vtok:
                b = IPhoneBridge()
                ni = b.netinfo_json()
                b.close()
                ip, vtok = ni["ip"], ni["vtok"]
                if not ip or ip == "0.0.0.0":
                    ip = None
                    time.sleep(1.5)
                    continue

            sock = socket.create_connection((ip, 7201), timeout=4.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = f"hello {vtok} at {last_ts}\n" if last_ts is not None else f"hello {vtok}\n"
            print(f"[video-daemon] Connected TCP :7201 -> {hello.strip()}", flush=True)
            sock.sendall(hello.encode("ascii"))
            sock.settimeout(0.5)

            buf = bytearray()
            cur = None
            expect = None
            last_ka = time.monotonic()
            last_rx = time.monotonic()
            verified_follow = False
            connected_at = time.monotonic()

            while True:
                now = time.monotonic()
                if now - last_ka >= 0.9:
                    sock.sendall(b"ka\n")
                    last_ka = now
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    last_rx = time.monotonic()
                    buf.extend(chunk)
                except socket.timeout:
                    if (not verified_follow and (now - connected_at) >= 0.8) or (now - last_rx >= 5.0):
                        try:
                            b = IPhoneBridge()
                            vd = b.raw_line("0 vdbg")
                            st = b.status_json()
                            b.close()
                            last_rx = now
                            if st.get("ap") != "streaming":
                                verified_follow = True
                                continue
                            live_age = (time.time() - os.path.getmtime(out_path)) if os.path.exists(out_path) else 9999.0
                            esp_vage = st.get("vage", 999999) / 1000.0
                            if "fg1" in vd and (esp_vage + 2.5 >= live_age):
                                verified_follow = True
                            else:
                                print(f"[video-daemon] Re-syncing GOP ({vd}, vage={esp_vage:.1f}s vs live={live_age:.1f}s)", flush=True)
                                last_ts = (st["up"] - st["vage"]) if st.get("fz") else None
                                break
                        except Exception:
                            # Serial busy because controller is active — retry check on next 0.5s tick
                            last_rx = now - 4.4

                gap_hit = False
                while len(buf) >= 2:
                    rec_len = struct.unpack_from("<H", buf, 0)[0]
                    if rec_len == 0:
                        del buf[:2]
                        continue
                    if len(buf) < 2 + rec_len:
                        break
                    rec = bytes(buf[2 : 2 + rec_len])
                    del buf[: 2 + rec_len]
                    if rec_len < 3:
                        continue
                    seq, flags = struct.unpack_from("<HB", rec, 0)
                    if flags & WV_AUDIO:
                        continue
                    if expect is not None and seq != expect:
                        gap_hit = True
                        break
                    expect = (seq + 1) & 0xFFFF
                    if flags & VF_START:
                        ts = struct.unpack_from("<I", rec, 3)[0]
                        cur = (flags, ts, bytearray(rec[7:]))
                    elif cur is not None:
                        cur[2].extend(rec[3:])
                    if (flags & VF_END) and cur is not None:
                        fl, ts, data = cur[0] | flags, cur[1], bytes(cur[2])
                        cur = None
                        verified_follow = True
                        if fl & VF_CONFIG:
                            pending_cfg = data
                            codec = make_decoder()
                            try:
                                with open(STREAM_CACHE, "wb") as cf:
                                    cf.write(data)
                            except Exception:
                                pass
                        else:
                            pkt = bytearray()
                            if pending_cfg:
                                pkt.extend(pending_cfg)
                                pending_cfg = b""
                            pkt.extend(data)
                            pkt.extend(b"\x00\x00\x00\x01\x09\xf0")
                            feed_bytes(bytes(pkt), emit=True, force_emit=False)
                            last_ts = ts
                            try:
                                if os.path.exists(STREAM_CACHE) and os.path.getsize(STREAM_CACHE) < 16 * 1024 * 1024:
                                    with open(STREAM_CACHE, "ab") as cf:
                                        cf.write(data)
                                    with open(STREAM_TS_FILE + ".tmp", "w") as tf:
                                        json.dump({"ts": ts}, tf)
                                    os.replace(STREAM_TS_FILE + ".tmp", STREAM_TS_FILE)
                            except Exception:
                                pass
                if gap_hit:
                    print(f"[video-daemon] Seq gap hit, resuming at ts={last_ts}", flush=True)
                    break

        except Exception as e:
            if "Operation timed out" in str(e) or "Connection refused" in str(e):
                ip = vtok = None
            time.sleep(0.2)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            time.sleep(0.1)


CAND_BAND_JPG = "/tmp/iphone_cand_band.jpg"
CAND_BEFORE_JPG = "/tmp/iphone_cand_before.jpg"
CAND_AFTER_JPG = "/tmp/iphone_cand_after.jpg"
AIM_BEFORE_JPG = "/tmp/iphone_aim_0.jpg"
AIM_AFTER_JPG = "/tmp/iphone_aim_1.jpg"
AIM_BACK_JPG = "/tmp/iphone_aim_2.jpg"


def frames_changed(path_a: str, path_b: str, frac: float = 0.045) -> bool:
    """
    Coarse 64x64 grayscale grid diff (~1.5ms) with top-6% iOS status-bar mask (`y >= 4` rows).
    Ignores clock minute rollovers, battery/signal icons, and a moved AssistiveTouch pointer
    while reliably detecting real screen/page transitions (`homescreen`, navigation, sheets).
    """
    if Image is None or not (os.path.exists(path_a) and os.path.exists(path_b)):
        return True
    with Image.open(path_a) as ia, Image.open(path_b) as ib:
        if ia.size != ib.size:
            return True
        ga = ia.convert("L").resize((64, 64), Image.Resampling.NEAREST).tobytes()
        gb = ib.convert("L").resize((64, 64), Image.Resampling.NEAREST).tobytes()
    skip = 4 * 64  # mask top 4/64 (~6.2%) status bar row
    diff = sum(1 for a, b in zip(ga[skip:], gb[skip:]) if abs(a - b) > 24)
    return diff > frac * (60 * 64)


def _biggest_blob(pts: list[tuple[int, int]], cell: int) -> list[tuple[int, int]]:
    """8-connected grid-cell clustering to isolate the circular AssistiveTouch disc from thin noise."""
    cells: dict[tuple[int, int], list[int]] = {}
    for idx, (x, y) in enumerate(pts):
        k = (x // cell, y // cell)
        cells.setdefault(k, []).append(idx)
    seen: set[tuple[int, int]] = set()
    best: list[int] = []
    for k in cells:
        if k in seen:
            continue
        blob: list[int] = []
        stack = [k]
        seen.add(k)
        while stack:
            qx, qy = stack.pop()
            blob.extend(cells[(qx, qy)])
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nb = (qx + dx, qy + dy)
                    if nb in cells and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        if len(blob) > len(best):
            best = blob
    return [pts[i] for i in best]


def locate_pointer_3frame(
    before_path: str,
    after_path: str,
    back_path: str | None,
    near_fx: float,
    near_fy: float,
    shift_fx: float,
    reach_fx: float = 0.14,
    reach_fy: float = 0.10,
) -> tuple[float, float] | None:
    """
    3-frame symmetric AssistiveTouch pointer detector (`before` -> nudged `after` -> restored `back`).
    Only pixels that changed in `after` AND returned to `before` in `back` count, rejecting ticking
    clocks, blinking cursors, and animated badges. Returns `(fx, fy)` in `[0..1]` screen fractions.
    """
    if Image is None:
        return None
    with Image.open(before_path) as im_a, Image.open(after_path) as im_b:
        if im_a.size != im_b.size:
            return None
        # Process at 1/2 resolution (~360x779) for ~4ms speed while preserving ~3px centroid precision
        orig_w, orig_h = im_a.size
        w, h = orig_w // 2, orig_h // 2
        ba = im_a.convert("L").resize((w, h), Image.Resampling.BILINEAR).tobytes()
        bb = im_b.convert("L").resize((w, h), Image.Resampling.BILINEAR).tobytes()
        bc = None
        if back_path and os.path.exists(back_path):
            with Image.open(back_path) as im_c:
                if im_c.size == im_a.size:
                    bc = im_c.convert("L").resize((w, h), Image.Resampling.BILINEAR).tobytes()

    left = max(0, int((near_fx - reach_fx) * w))
    top = max(int(0.045 * h), int((near_fy - reach_fy) * h))
    right = min(w, int((near_fx + reach_fx) * w))
    bottom = min(h, int((near_fy + reach_fy) * h))
    if right <= left or bottom <= top:
        return None

    pts: list[tuple[int, int]] = []
    for y in range(top, bottom):
        row = y * w
        for x in range(left, right):
            i = row + x
            av, bv = ba[i], bb[i]
            if abs(av - bv) <= 26:
                continue
            if bc is not None:
                cv = bc[i]
                if abs(cv - bv) <= 26 or abs(cv - av) > 28:
                    continue
            pts.append((x, y))

    if not pts:
        return None
    pts = _biggest_blob(pts, max(3, w // 80))
    n = len(pts)
    area = (right - left) * (bottom - top)
    if n < 6 or n > area // 4:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(xs) - min(xs)) > 0.16 * w or (max(ys) - min(ys)) > 0.08 * h:
        return None
    cx = sum(xs) / float(n)
    cy = sum(ys) / float(n)
    return (cx / float(w) - shift_fx / 2.0, cy / float(h))


def detect_candidate_row_ys(img_path: str, before_path: str | None = None) -> list[int]:
    """
    Locate center Y coordinate(s) of the active numbered row (1..3 or 1..5) of the iOS BLE
    hardware-keyboard floating candidate bar (~20-40ms).
    Prioritizes the frame-diff vertical bounding box (`before_path` -> `img_path`) when available.
    """
    if Image is None:
        return []
    with Image.open(img_path) as im:
        orig_w, orig_h = im.size
        sw, sh = orig_w // 2, orig_h // 2
        rgb = im.convert("RGB").resize((sw, sh), Image.Resampling.BILINEAR)
        px = rgb.load()

        ranges: list[tuple[int, int]] = []
        full_lo, full_hi = int(sh * 0.08), int(sh * 0.96)
        if before_path and os.path.exists(before_path):
            try:
                with Image.open(before_path) as bim:
                    brgb = bim.convert("RGB").resize((sw, sh), Image.Resampling.BILINEAR)
                    diff = ImageChops.difference(rgb, brgb).point(lambda v: 255 if v > 24 else 0).convert("L")
                    bbox = diff.getbbox()
                    if bbox:
                        d_lo = max(full_lo, bbox[1] - 10)
                        d_hi = min(full_hi, bbox[3] + 10)
                        if d_hi - d_lo >= 28:
                            ranges.append((d_lo, d_hi))
            except Exception:
                pass
        ranges.append((full_lo, full_hi))

        found_ys: list[int] = []
        for y_lo, y_hi in ranges:
            best_score = 0.0
            best_y: int | None = None
            for cy in range(y_lo + 14, y_hi - 14, 2):
                y_top = cy - 13
                y_bot = cy + 13
                for x_edge in range(68, 198, 4):
                    r_t = px[x_edge + 18, y_top]
                    if max(r_t) - min(r_t) > 24:
                        continue
                    r_b = px[x_edge + 18, y_bot]
                    if sum(abs(a - b) for a, b in zip(r_t, r_b)) > 30:
                        continue
                    ok_bar = True
                    for dx in (50, 88, 126):
                        xx = x_edge + dx
                        if xx >= sw - 4:
                            ok_bar = False
                            break
                        pt = px[xx, y_top]
                        pb = px[xx, y_bot]
                        if sum(abs(a - b) for a, b in zip(r_t, pt)) > 32 or sum(abs(a - b) for a, b in zip(r_b, pb)) > 32:
                            ok_bar = False
                            break
                    if not ok_bar:
                        continue

                    l_t = px[x_edge - 22, y_top]
                    l_b = px[x_edge - 22, y_bot]
                    if sum(abs(a - b) for a, b in zip(l_t, l_b)) > 34:
                        continue
                    l_l = px[max(4, x_edge - 40), y_top]
                    if sum(abs(a - b) for a, b in zip(l_t, l_l)) > 34:
                        continue

                    step = sum(abs(a - b) for a, b in zip(l_t, r_t)) + sum(abs(a - b) for a, b in zip(l_b, r_b))
                    if step < 190 or sum(l_t) <= sum(r_t) + 65:
                        continue
                    if step > best_score:
                        best_score = step
                        best_y = cy * 2
            if best_y is not None and all(abs(best_y - ey) > 36 for ey in found_ys):
                found_ys.append(best_y)
        return found_ys


def parse_candidate_band_ocr(items: list[dict]) -> list[dict]:
    """
    Parse OCR items from a 92px horizontal band crop of the active iOS candidate row into
    ordered `[{'slot': int, 'text': str, 'x': int}, ...]`.
    """
    import re

    cleaned = []
    for it in sorted(items, key=lambda d: d["x"]):
        t = it["text"].strip()
        if not t or t in ("V", "v", "⌄", "×", "X", "|", "Q搜索", "Q 搜索", "词频", "部首", "笔画"):
            continue
        cleaned.append({"text": t, "x": int(it["x"]), "w": int(it.get("w", 40))})

    tokens: list[dict] = []
    pending_digit: int | None = None
    pending_x = 0

    for it in cleaned:
        t = it["text"]
        x = it["x"]
        w = it["w"]
        if re.fullmatch(r"[1-5]", t):
            pending_digit = int(t)
            pending_x = x
            continue

        # Reject date/time/ID strings like "2024/8/8", "13:47", "2409262918绑定码"
        if re.search(r"\d{2,}|\d[/:\.]\d", t):
            pending_digit = None
            continue

        pairs = list(re.finditer(r"(?<![\d/:\.])([1-5])\s*([\u4e00-\u9fffA-Za-z]+)", t))
        if pairs:
            pending_digit = None
            n = len(pairs)
            left = x - w // 2
            for idx, m in enumerate(pairs):
                sub_x = left + int((idx + 0.5) * (w / max(1, n)))
                tokens.append({"explicit": int(m.group(1)), "text": m.group(2), "x": sub_x})
        elif re.fullmatch(r"[\u4e00-\u9fffA-Za-z]+", t):
            exp = None
            if pending_digit is not None and 0 <= (x - pending_x) <= 135:
                exp = pending_digit
            pending_digit = None
            tokens.append({"explicit": exp, "text": t, "x": x})
        else:
            pending_digit = None

    # Every genuine iOS candidate bar row contains at least one explicit slot digit (1..5)
    if not any(tok["explicit"] is not None for tok in tokens):
        return []

    out: list[dict] = []
    last_slot = 0
    for tok in tokens:
        slot = tok["explicit"] if tok["explicit"] is not None else (last_slot + 1)
        if slot <= last_slot or slot > 5:
            continue
        last_slot = slot
        out.append({"slot": slot, "text": tok["text"], "x": tok["x"]})
    if not out or out[0]["slot"] > 2:
        return []
    return out


class IPhoneController:
    def __init__(self, bridge: IPhoneBridge | None = None):
        self.bridge = bridge if bridge is not None else IPhoneBridge()
        # Disable click drag-jitter so iOS modal sheets & UIMenus register crisp taps
        try:
            self.bridge.cmd("human off")
        except Exception:
            pass
        t_info = ""
        try:
            self.bridge.cmd("target ble")
            t_info = self.bridge.cmd("target") or ""
        except Exception:
            t_info = ""
        self.abs_mode: bool = ("abs=1" in t_info) and os.environ.get("IPHONE_FORCE_REL") != "1"
        if os.path.exists(CALIB_FILE):
            with open(CALIB_FILE, encoding="utf-8") as f:
                c = json.load(f)
            self.span_w, self.span_h = c.get("w", 511), c.get("h", 1285)
            self.off_x, self.off_y = c.get("off_x", 0.0), c.get("off_y", 0.0)
            raw_seen = c.get("seen", {})
            self.seen: dict[str, list[list[float]]] = {
                "x": [list(p) for p in raw_seen.get("x", []) if len(p) == 2][-12:],
                "y": [list(p) for p in raw_seen.get("y", []) if len(p) == 2][-12:],
            }
            self.abs_x0 = float(c.get("abs_x0", 0.0))
            self.abs_x1 = float(c.get("abs_x1", 10000.0))
            self.abs_y0 = float(c.get("abs_y0", 0.0))
            self.abs_y1 = float(c.get("abs_y1", 10000.0))
        else:
            self.span_w, self.span_h = 511, 1285
            self.off_x, self.off_y = 0.032, 0.0
            self.seen = {"x": [], "y": []}
            self.abs_x0, self.abs_x1 = 0.0, 10000.0
            self.abs_y0, self.abs_y1 = 0.0, 10000.0
        self.pos: tuple[int, int] | None = self._load_pos()
        self.last_action_mtime: float = self.live_mtime()
        self._last_abs_ts: float = 0.0
        self._ocr_proc: subprocess.Popen | None = None

    def _save_calib(self):
        try:
            payload = {
                "w": self.span_w,
                "h": self.span_h,
                "off_x": self.off_x,
                "off_y": self.off_y,
                "seen": self.seen,
                "abs_x0": self.abs_x0,
                "abs_x1": self.abs_x1,
                "abs_y0": self.abs_y0,
                "abs_y1": self.abs_y1,
            }
            with open(CALIB_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(CALIB_FILE + ".tmp", CALIB_FILE)
        except Exception:
            pass

    def _fix_axis(self, target_f: float, axis: str) -> float:
        """Online 12-point least-squares drift correction (`landed = a * aimed + b`)."""
        pts = self.seen.get(axis, [])
        if len(pts) < 2:
            return target_f
        n = float(len(pts))
        mc = sum(p[0] for p in pts) / n
        mf = sum(p[1] for p in pts) / n
        var = sum((p[0] - mc) ** 2 for p in pts)
        cov = sum((p[0] - mc) * (p[1] - mf) for p in pts)
        a = (cov / var) if var > 1e-4 else 1.0
        if a <= 0.85 or a >= 1.15:
            a = 1.0
        b = mf - a * mc
        if abs(b) < 0.08:
            return (target_f - b) / a
        return target_f

    def _remember_landing(self, aimed_fx: float, aimed_fy: float, found_fx: float, found_fy: float):
        if max(abs(found_fx - aimed_fx), abs(found_fy - aimed_fy)) <= 0.06:
            self.seen["x"] = (self.seen["x"] + [[aimed_fx, found_fx]])[-12:]
            self.seen["y"] = (self.seen["y"] + [[aimed_fy, found_fy]])[-12:]
            self._save_calib()

    def _load_pos(self) -> tuple[int, int] | None:
        try:
            if os.path.exists(POINTER_POS_FILE):
                d = json.load(open(POINTER_POS_FILE))
                # iOS AssistiveTouch auto-hides the BLE pointer after ~15s idle; re-home if older than 12s
                if time.time() - d.get("ts", 0) < 12.0:
                    return (int(d["ux"]), int(d["uy"]))
        except Exception:
            pass
        return None

    def _save_pos(self, pos: tuple[int, int] | None):
        self.pos = pos
        try:
            if pos is None:
                if os.path.exists(POINTER_POS_FILE):
                    os.remove(POINTER_POS_FILE)
            else:
                with open(POINTER_POS_FILE + ".tmp", "w") as f:
                    json.dump({"ux": pos[0], "uy": pos[1], "ts": time.time()}, f)
                os.replace(POINTER_POS_FILE + ".tmp", POINTER_POS_FILE)
        except Exception:
            pass

    def _ocr_recognize(self, image_path: str, ocr=None) -> list[dict]:
        if ocr is not None:
            return ocr.recognize(image_path)
        ocr_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_ocr")
        abs_p = os.path.abspath(image_path)
        for _ in range(2):
            try:
                if self._ocr_proc is None or self._ocr_proc.poll() is not None:
                    self._ocr_proc = subprocess.Popen(
                        [ocr_bin, "--serve"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        bufsize=1,
                    )
                assert self._ocr_proc.stdin is not None and self._ocr_proc.stdout is not None
                self._ocr_proc.stdin.write(abs_p + "\n")
                self._ocr_proc.stdin.flush()
                line = self._ocr_proc.stdout.readline()
                if line:
                    return json.loads(line)
            except Exception:
                self._close_ocr()
        out = subprocess.check_output([ocr_bin, abs_p], timeout=8).decode("utf-8", errors="replace")
        return json.loads(out) if out.strip() else []

    def _close_ocr(self):
        if self._ocr_proc is not None:
            try:
                if self._ocr_proc.stdin:
                    self._ocr_proc.stdin.write("QUIT\n")
                    self._ocr_proc.stdin.flush()
                self._ocr_proc.terminate()
                self._ocr_proc.wait(timeout=1.0)
            except Exception:
                pass
            self._ocr_proc = None

    def close(self):
        self._close_ocr()
        self.bridge.close()

    def live_mtime(self) -> float:
        return os.path.getmtime(LIVE_JPG) if os.path.exists(LIVE_JPG) else 0.0

    def wait_settled(
        self,
        prev_mtime: float | None = None,
        change_wait: float = 0.90,
        quiet: float = 0.28,
        max_wait: float = 2.6,
        min_wait: float = 0.45,
    ) -> float:
        """
        Event-driven settlement wait on the live AirPlay stream:
        1) Waits at least `min_wait` so iOS push/pop transition animations start after initial tap highlight.
        2) Waits up to `change_wait` for new frames after `prev_mtime`.
        3) Returns as soon as the stream has stayed quiet for `quiet` seconds, capped at `max_wait`.
        """
        t0 = time.perf_counter()
        base = prev_mtime if prev_mtime is not None else self.last_action_mtime
        changed = False
        while True:
            now_perf = time.perf_counter()
            elapsed = now_perf - t0
            if elapsed >= max_wait:
                break
            mt = self.live_mtime()
            if mt > base:
                changed = True
                if elapsed >= min_wait and (time.time() - mt) >= quiet:
                    break
            elif not changed and elapsed >= max_wait.real and elapsed >= change_wait:
                break
            if not changed and elapsed >= max(min_wait, change_wait):
                break
            time.sleep(0.025)
        return round((time.perf_counter() - t0) * 1000, 1)

    def screenshot(self, out_path="/tmp/iphone_latest.jpg") -> str:
        if not os.path.exists(LIVE_JPG):
            raise RuntimeError(f"{LIVE_JPG} not ready yet")
        if os.path.abspath(out_path) != os.path.abspath(LIVE_JPG):
            shutil.copyfile(LIVE_JPG, out_path)
        return out_path

    def frame_size(self) -> tuple[int, int]:
        if Image is not None and os.path.exists(LIVE_JPG):
            with Image.open(LIVE_JPG) as im:
                return im.size
        return (720, 1558)

    def safe_home(self):
        """
        Guaranteed 0-drop slam to exact top-left (0, 0) from any screen edge (~0.68s total).
        """
        self.bridge.cmd("release")
        self.bridge.cmd("move 2 2 2")
        time.sleep(0.06)
        for _ in range(5):
            self.bridge.cmd("move -320 0 32")
            time.sleep(0.068)
        for _ in range(7):
            self.bridge.cmd("move 0 -320 32")
            time.sleep(0.068)
        time.sleep(0.04)
        self._save_pos((0, 0))

    def safe_move(self, dx: int, dy: int, step: int = 10):
        """
        Move pointer by (dx, dy) in <=13-report Euclidean chunks (`hypot(dx, dy) <= step * 13`)
        with 62ms BLE drain pauses, matching firmware `pace.c` (`ceil(hypot/step)`) with 0% drops.
        """
        rem_x, rem_y = int(round(dx)), int(round(dy))
        max_chunk = step * 13
        while rem_x != 0 or rem_y != 0:
            dist = math.hypot(rem_x, rem_y)
            if dist <= max_chunk:
                cx, cy = rem_x, rem_y
            else:
                ratio = max_chunk / float(dist)
                cx = int(round(rem_x * ratio))
                cy = int(round(rem_y * ratio))
            if cx == 0 and cy == 0:
                break
            self.bridge.cmd(f"move {cx} {cy} {step}")
            rem_x -= cx
            rem_y -= cy
            if rem_x != 0 or rem_y != 0:
                time.sleep(0.062)

    @staticmethod
    def _speed_ratio(v: float, full_speed: float = 10.0) -> float:
        """ Empirical iOS AssistiveTouch low-speed pointer deceleration curve (`v < 10` units/report). """
        pts = [(1.0, 0.45), (2.0, 0.54), (4.0, 0.68), (6.0, 0.80), (8.0, 0.90), (full_speed, 1.0)]
        if v <= pts[0][0]:
            return pts[0][1]
        for (v0, r0), (v1, r1) in zip(pts, pts[1:]):
            if v <= v1:
                return r0 + (r1 - r0) * (v - v0) / (v1 - v0)
        return 1.0

    @classmethod
    def _paced_units(cls, mx: int, my: int, full_speed: float = 10.0) -> tuple[int, int]:
        """
        4-step fixed-point compensation (`Pointer.swift` `paced`) so short sub-10-unit moves
        overcome iOS low-speed deceleration and converge in a single correction step.
        """
        d = math.hypot(mx, my)
        if d <= 0.0 or d >= full_speed * 3:
            return mx, my
        length = d
        for _ in range(4):
            reports = max(math.ceil(length / full_speed), 1)
            length = d / cls._speed_ratio(length / reports, full_speed)
        k = length / d
        return int(round(mx * k)), int(round(my * k))

    def _frac_to_units(self, fx: float, fy: float) -> tuple[int, int]:
        adj_x = self._fix_axis(fx, "x") - self.off_x
        adj_y = self._fix_axis(fy, "y") - self.off_y
        ux = round(max(0.0, min(1.0, adj_x)) * self.span_w)
        uy = round(max(0.0, min(1.0, adj_y)) * self.span_h)
        return ux, uy

    def _frac_to_abs(self, fx: float, fy: float) -> tuple[int, int]:
        """Map screen fractions `[0.0, 1.0]` linearly to `0..10000` absolute HID coordinates."""
        fx_c = max(0.0, min(1.0, float(fx)))
        fy_c = max(0.0, min(1.0, float(fy)))
        ax = round(self.abs_x0 + fx_c * (self.abs_x1 - self.abs_x0))
        ay = round(self.abs_y0 + fy_c * (self.abs_y1 - self.abs_y0))
        return max(0, min(10000, int(ax))), max(0, min(10000, int(ay)))

    def _abs_move_and_settle(self, ax: int, ay: int) -> None:
        """Move absolute pointer and wait 85ms if AssistiveTouch may be idle (>10s) else 60ms."""
        now = time.monotonic()
        settle = 0.085 if (self._last_abs_ts <= 0.0 or (now - self._last_abs_ts) > 10.0) else 0.060
        self.bridge.cmd(f"abs {ax} {ay}")
        self._last_abs_ts = time.monotonic()
        time.sleep(settle)

    def measure_pointer_abs(self, fx: float, fy: float, nudge: int = 180) -> tuple[float, float] | None:
        """
        Absolute-mode 3-frame AssistiveTouch pointer detector: wiggles `abs {ax+d} {ay}` / `abs {ax} {ay}`
        by `+-nudge` units in `0..10000` and locates the real pointer via `locate_pointer_3frame`.
        """
        ax, ay = self._frac_to_abs(fx, fy)
        d_nudge = -nudge if (fx >= 0.86 or ax + nudge > 10000) else nudge
        ax_nudged = max(0, min(10000, ax + d_nudge))
        actual_d = ax_nudged - ax

        self.screenshot(AIM_BEFORE_JPG)
        mt1 = self.live_mtime()
        self.bridge.cmd(f"abs {ax_nudged} {ay}")
        self._last_abs_ts = time.monotonic()
        self.wait_settled(mt1, change_wait=0.55, quiet=0.14, max_wait=0.75, min_wait=0.24)
        self.screenshot(AIM_AFTER_JPG)

        mt2 = self.live_mtime()
        self.bridge.cmd(f"abs {ax} {ay}")
        self._last_abs_ts = time.monotonic()
        self.wait_settled(mt2, change_wait=0.55, quiet=0.14, max_wait=0.75, min_wait=0.24)
        self.screenshot(AIM_BACK_JPG)

        span_x = (self.abs_x1 - self.abs_x0) or 10000.0
        shift_fx = float(actual_d) / float(span_x)
        pt = locate_pointer_3frame(AIM_BEFORE_JPG, AIM_AFTER_JPG, AIM_BACK_JPG, fx, fy, shift_fx, 0.14, 0.10)
        if pt is not None:
            return pt
        return locate_pointer_3frame(AIM_BEFORE_JPG, AIM_AFTER_JPG, AIM_BACK_JPG, 0.5, 0.5, shift_fx, 0.5, 0.5)

    def measure_pointer(self, fx: float, fy: float, nudge: int | None = None) -> tuple[float, float] | None:
        """
        Wiggles the BLE pointer horizontally and locates the real AssistiveTouch disc on screen
        via 3-frame symmetric diff (`locate_pointer_3frame`).
        """
        if self.abs_mode:
            return self.measure_pointer_abs(fx, fy, nudge=180 if nudge is None else nudge)
        rel_nudge = 14 if nudge is None else nudge
        d_nudge = -rel_nudge if fx >= 0.86 else rel_nudge
        self.screenshot(AIM_BEFORE_JPG)
        mt1 = self.live_mtime()
        self.bridge.cmd(f"move {d_nudge} 0 7")
        self.wait_settled(mt1, change_wait=0.55, quiet=0.14, max_wait=0.75, min_wait=0.24)
        self.screenshot(AIM_AFTER_JPG)

        mt2 = self.live_mtime()
        self.bridge.cmd(f"move {-d_nudge} 0 7")
        self.wait_settled(mt2, change_wait=0.55, quiet=0.14, max_wait=0.75, min_wait=0.24)
        self.screenshot(AIM_BACK_JPG)

        shift_fx = float(d_nudge) / float(self.span_w)
        pt = locate_pointer_3frame(AIM_BEFORE_JPG, AIM_AFTER_JPG, AIM_BACK_JPG, fx, fy, shift_fx, 0.14, 0.10)
        if pt is not None:
            return pt
        return locate_pointer_3frame(AIM_BEFORE_JPG, AIM_AFTER_JPG, AIM_BACK_JPG, 0.5, 0.5, shift_fx, 0.5, 0.5)

    def point_frac(self, fx: float, fy: float, aim: bool = False, force_rehome: bool = False) -> dict:
        """
        Move pointer to `(fx, fy)`. When `aim=True`, visually locates the AssistiveTouch pointer
        after moving, corrects any miss (>0.6% in abs_mode, >0.7% in rel_mode), and in relative
        mode updates online linear calibration.
        """
        if self.abs_mode:
            ax, ay = self._frac_to_abs(fx, fy)
            self.bridge.cmd(f"abs {ax} {ay}")
            self._last_abs_ts = time.monotonic()
            time.sleep(0.025)
            info: dict = {"ax": ax, "ay": ay, "abs": True, "aimed": aim}
            if aim:
                found = self.measure_pointer_abs(fx, fy)
                if found is not None:
                    found_fx, found_fy = found
                    info["found_fx"] = round(found_fx, 4)
                    info["found_fy"] = round(found_fy, 4)
                    err_x, err_y = fx - found_fx, fy - found_fy
                    if abs(err_x) > 0.006 or abs(err_y) > 0.006:
                        span_x = (self.abs_x1 - self.abs_x0) or 10000.0
                        span_y = (self.abs_y1 - self.abs_y0) or 10000.0
                        corr_ax = max(0, min(10000, int(round(ax + err_x * span_x))))
                        corr_ay = max(0, min(10000, int(round(ay + err_y * span_y))))
                        self.bridge.cmd(f"abs {corr_ax} {corr_ay}")
                        self._last_abs_ts = time.monotonic()
                        time.sleep(0.025)
                        ax, ay = corr_ax, corr_ay
                        info["ax"] = ax
                        info["ay"] = ay
                        info["corr_ax"] = corr_ax
                        info["corr_ay"] = corr_ay
            return info

        ux, uy = self._frac_to_units(fx, fy)
        if force_rehome or self.pos is None:
            self.safe_home()
        dx = ux - self.pos[0]
        dy = uy - self.pos[1]
        if dx != 0 or dy != 0:
            self.safe_move(dx, dy, step=10)
            time.sleep(0.06)
        self._save_pos((ux, uy))

        info = {"ux": ux, "uy": uy, "aimed": aim}
        if aim:
            found = self.measure_pointer(fx, fy)
            if found is not None:
                found_fx, found_fy = found
                info["found_fx"] = round(found_fx, 4)
                info["found_fy"] = round(found_fy, 4)
                # Record normal drift (away from bezel clamp) to improve future open-loop taps
                if 0.09 <= fx <= 0.86 and 0.07 <= fy <= 0.90:
                    self._remember_landing(self._fix_axis(fx, "x"), self._fix_axis(fy, "y"), found_fx, found_fy)
                err_x, err_y = fx - found_fx, fy - found_fy
                if abs(err_x) > 0.007 or abs(err_y) > 0.007:
                    raw_cx = round(err_x * self.span_w)
                    raw_cy = round(err_y * self.span_h)
                    corr_ux, corr_uy = self._paced_units(raw_cx, raw_cy, full_speed=8.0)
                    self.safe_move(corr_ux, corr_uy, step=8)
                    time.sleep(0.05)
                    ux += raw_cx
                    uy += raw_cy
                    self._save_pos((ux, uy))
                    info["corr_ux"] = corr_ux
                    info["corr_uy"] = corr_uy
        return info

    def tap_units(self, ux: int, uy: int, step: int = 10, force_rehome: bool = False, count: int = 1):
        if force_rehome or self.pos is None:
            self.safe_home()
        dx = ux - self.pos[0]
        dy = uy - self.pos[1]
        if dx != 0 or dy != 0:
            self.safe_move(dx, dy, step=step)
            time.sleep(0.06)
        self.last_action_mtime = self.live_mtime()
        for i in range(max(1, count)):
            self.bridge.cmd("click")
            if i + 1 < count:
                time.sleep(0.05)
        # If near screen border, AssistiveTouch bezel clamping shifts origin; invalidate tracked pos
        if ux <= round(0.08 * self.span_w) or uy <= round(0.06 * self.span_h) or ux >= round(0.88 * self.span_w) or uy >= round(0.91 * self.span_h):
            self._save_pos(None)
        else:
            self._save_pos((ux, uy))
        time.sleep(0.05)

    def tap_frac(self, fx: float, fy: float, force_rehome: bool = False, aim: bool = False, count: int = 1) -> dict:
        if self.abs_mode:
            n_taps = max(1, count)
            if aim:
                info = self.point_frac(fx, fy, aim=True, force_rehome=force_rehome)
                self.last_action_mtime = self.live_mtime()
                for i in range(n_taps):
                    self.bridge.cmd("click")
                    if i + 1 < n_taps:
                        time.sleep(0.05)
                time.sleep(0.03)
                return info
            ax, ay = self._frac_to_abs(fx, fy)
            self.last_action_mtime = self.live_mtime()
            self._abs_move_and_settle(ax, ay)
            for i in range(n_taps):
                self.bridge.cmd("click")
                if i + 1 < n_taps:
                    time.sleep(0.05)
            return {"ax": ax, "ay": ay, "abs": True, "aimed": False}

        info = self.point_frac(fx, fy, aim=aim, force_rehome=force_rehome)
        ux, uy = info["ux"], info["uy"]
        self.last_action_mtime = self.live_mtime()
        for i in range(max(1, count)):
            self.bridge.cmd("click")
            if i + 1 < count:
                time.sleep(0.05)
        if ux <= round(0.08 * self.span_w) or uy <= round(0.06 * self.span_h) or ux >= round(0.88 * self.span_w) or uy >= round(0.91 * self.span_h):
            self._save_pos(None)
        else:
            self._save_pos((ux, uy))
        time.sleep(0.05)
        return info

    def tap_px(self, x_px: float, y_px: float, force_rehome: bool = False, aim: bool = False, count: int = 1) -> dict:
        w, h = self.frame_size()
        return self.tap_frac(x_px / w, y_px / h, force_rehome=force_rehome, aim=aim, count=count)

    def double_tap_px(self, x_px: float, y_px: float, aim: bool = False) -> dict:
        return self.tap_px(x_px, y_px, aim=aim, count=2)

    def long_press_px(self, x_px: float, y_px: float, duration_s: float = 1.0, aim: bool = False) -> dict:
        w, h = self.frame_size()
        fx, fy = x_px / w, y_px / h
        if self.abs_mode:
            hold_ms = max(250, int(round(duration_s * 1000)))
            if aim:
                info = self.point_frac(fx, fy, aim=True)
                self.last_action_mtime = self.live_mtime()
                self.bridge.cmd("press")
                time.sleep(hold_ms / 1000.0)
                self.bridge.cmd("release")
                time.sleep(0.05)
                return info
            ax, ay = self._frac_to_abs(fx, fy)
            self.last_action_mtime = self.live_mtime()
            self._abs_move_and_settle(ax, ay)
            self.bridge.cmd("press")
            time.sleep(hold_ms / 1000.0)
            self.bridge.cmd("release")
            time.sleep(0.05)
            return {"ax": ax, "ay": ay, "abs": True, "aimed": False, "hold_ms": hold_ms}

        info = self.point_frac(fx, fy, aim=aim)
        self.last_action_mtime = self.live_mtime()
        self.bridge.cmd("press")
        time.sleep(max(0.25, duration_s))
        self.bridge.cmd("release")
        time.sleep(0.08)
        return info

    def swipe_frac(self, fx1: float, fy1: float, fx2: float, fy2: float, hold: bool = True):
        if self.abs_mode:
            ax1, ay1 = self._frac_to_abs(fx1, fy1)
            ax2, ay2 = self._frac_to_abs(fx2, fy2)
            hold_end_ms = 160 if hold else 20
            self.last_action_mtime = self.live_mtime()
            self.bridge.cmd(f"absswipe {ax1} {ay1} {ax2} {ay2} 260 {hold_end_ms}")
            return

        x1, y1 = self._frac_to_units(fx1, fy1)
        x2, y2 = self._frac_to_units(fx2, fy2)
        if self.pos is None:
            self.safe_home()
        self.safe_move(x1 - self.pos[0], y1 - self.pos[1], step=10)
        time.sleep(0.04)
        self.bridge.cmd("press")
        time.sleep(0.04)
        self.safe_move(x2 - x1, y2 - y1, step=12)
        if hold:
            time.sleep(0.18)
        self.bridge.cmd("release")
        self._save_pos(None)  # invalidate after drag/swipe
        time.sleep(0.08)

    def swipe_px(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True):
        w, h = self.frame_size()
        self.swipe_frac(x1 / w, y1 / h, x2 / w, y2 / h, hold=hold)

    def key(self, combo: str):
        self.bridge.cmd(f"key {combo}")
        time.sleep(0.06)

    def type_text(self, text: str):
        """
        Send ASCII text over BLE HID. Splits on spaces and dispatches explicit `key space`
        because firmware `line.trim()` strips leading/trailing spaces from `type <arg>`.
        """
        parts = text.split(" ")
        for i, part in enumerate(parts):
            if part:
                self.bridge.cmd(f"type {part}")
            if i + 1 < len(parts):
                self.key("space")
        time.sleep(0.06)

    def read_active_candidates(
        self,
        after_path: str = LIVE_JPG,
        before_path: str | None = None,
        ocr=None,
    ) -> list[dict]:
        """
        Inspect the floating iOS hardware-keyboard candidate bar and return parsed
        `[{'slot': 1, 'text': '...', 'x': ...}, ...]`.
        Uses 92px horizontal band cropping (~80ms total, 100% recall on 5-column grids)
        with full-frame OCR fallback.
        """
        if Image is not None:
            cand_ys = detect_candidate_row_ys(after_path, before_path)
            with Image.open(after_path) as im:
                w, h = im.size
                for y0 in cand_ys:
                    band = im.crop((0, max(0, y0 - 46), w, min(h, y0 + 46)))
                    band.save(CAND_BAND_JPG, format="JPEG", quality=92)
                    cands = parse_candidate_band_ocr(self._ocr_recognize(CAND_BAND_JPG, ocr=ocr))
                    if cands:
                        return cands

        # Fallback: full-frame OCR -> find horizontal row containing '1' / '1 xxx'
        full_items = self._ocr_recognize(after_path, ocr=ocr)
        anchor_ys: list[int] = []
        for it in full_items:
            t = it["text"].strip()
            if t == "1" or (len(t) >= 2 and t[0] == "1" and not t[1].isdigit()):
                if all(abs(it["y"] - ay) > 28 for ay in anchor_ys):
                    anchor_ys.append(int(it["y"]))
        if Image is not None and anchor_ys:
            with Image.open(after_path) as im:
                w, h = im.size
                for y0 in anchor_ys:
                    band = im.crop((0, max(0, y0 - 46), w, min(h, y0 + 46)))
                    band.save(CAND_BAND_JPG, format="JPEG", quality=92)
                    cands = parse_candidate_band_ocr(self._ocr_recognize(CAND_BAND_JPG, ocr=ocr))
                    if cands:
                        return cands
        return []

    def _commit_candidate_slot(self, slot: int):
        mt = self.live_mtime()
        self.bridge.cmd(f"type {slot}")
        self.wait_settled(mt, change_wait=0.48, quiet=0.14, max_wait=0.70, min_wait=0.20)

    def _try_select_code(
        self,
        target_word: str,
        code: str,
        max_extra_rows: int = 2,
        ocr=None,
    ) -> dict | None:
        """
        Type `code` WITHOUT space/enter, inspect the floating candidate bar, and commit the
        matching candidate slot (`1..9`). If not in the collapsed bar, expands the 5-column
        grid (`down` -> Row 2, `up` -> Row 1 full 5-col, then `down` through deeper rows).
        If still not found, cleanly erases `code` via `backspace` x `len(code)` and returns None.
        """
        t0 = time.perf_counter()
        self.screenshot(CAND_BEFORE_JPG)
        mt = self.live_mtime()
        self.bridge.cmd(f"type {code}")
        min_w = min(0.55, 0.32 + 0.035 * len(code))
        self.wait_settled(mt, change_wait=0.75, quiet=0.22, max_wait=1.05, min_wait=min_w)
        self.screenshot(CAND_AFTER_JPG)

        cands = self.read_active_candidates(CAND_AFTER_JPG, CAND_BEFORE_JPG, ocr=ocr)
        if not cands:
            # One extra 0.32s frame check in case multi-key BLE burst + iOS IME bar rendered on the next frame
            time.sleep(0.32)
            self.screenshot(CAND_AFTER_JPG)
            cands = self.read_active_candidates(CAND_AFTER_JPG, CAND_BEFORE_JPG, ocr=ocr)
        if not cands:
            # Fail fast (~0.7s): genuinely no candidate bar rendered (text input not focused or English mode)
            for _ in range(len(code)):
                self.key("backspace")
            return {"word": target_word, "code": code, "error": "no_candidate_bar"}

        for c in cands:
            if c["text"] == target_word:
                self._commit_candidate_slot(c["slot"])
                return {
                    "word": target_word,
                    "code": code,
                    "slot": c["slot"],
                    "view": "collapsed",
                    "candidates": [f"{x['slot']}:{x['text']}" for x in cands],
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                }

        # Expand 5-column candidate grid:
        # Step 1: `down` -> inspects Row 2 (`1..5`)
        # Step 2: `up`   -> inspects Row 1 full 5-col (`1..5`, exposing hidden 4th/5th col like `5:聘`)
        # Step 3+: `down` x2 (`Row 3`), `down` (`Row 4`), `down` (`Row 5`) for single-char homophones
        nav_plan: list[tuple[str, list[str]]] = [
            ("grid_row2", ["down"]),
            ("grid_row1_full", ["up"]),
        ]
        if max_extra_rows >= 3:
            nav_plan.append(("grid_row3", ["down", "down"]))
        for r_idx in range(4, max_extra_rows + 2):
            nav_plan.append((f"grid_row{r_idx}", ["down"]))

        expanded = False
        for view_name, keys in nav_plan:
            self.screenshot(CAND_BEFORE_JPG)
            for k in keys:
                mt_nav = self.live_mtime()
                self.key(k)
                expanded = True
                self.wait_settled(mt_nav, change_wait=0.55, quiet=0.18, max_wait=0.80, min_wait=0.28)
            self.screenshot(CAND_AFTER_JPG)
            cands = self.read_active_candidates(CAND_AFTER_JPG, CAND_BEFORE_JPG, ocr=ocr)
            for c in cands:
                if c["text"] == target_word:
                    self._commit_candidate_slot(c["slot"])
                    return {
                        "word": target_word,
                        "code": code,
                        "slot": c["slot"],
                        "view": view_name,
                        "candidates": [f"{x['slot']}:{x['text']}" for x in cands],
                        "ms": round((time.perf_counter() - t0) * 1000, 1),
                    }

        # Target word not found under `code` — erase uncommitted composition cleanly!
        _ = expanded
        for _ in range(len(code)):
            self.key("backspace")
        time.sleep(0.18)
        return None

    def select_ziranma_word(self, word_plan: dict, ocr=None) -> list[dict]:
        """
        Closed-loop Ziranma input for one word plan from `ziranma.encode_ziranma_plan()`.
        1) Tries full word `word_plan['code']` first (checks collapsed bar + Row 2 + Row 1 full 5-col).
        2) If phrase is not in candidate bar, automatically erases uncommitted code and selects
           each character individually (trying polyphone alternate codes if needed).
        """
        target = word_plan["word"]
        max_rows = 4 if len(target) == 1 else 2
        res = self._try_select_code(target, word_plan["code"], max_extra_rows=max_rows, ocr=ocr)
        if res is not None:
            return [res]

        # Per-character closed-loop fallback (or polyphone alternate code for 1-char words)
        logs: list[dict] = []
        for ch_info in word_plan["chars"]:
            ch = ch_info["char"]
            codes = ch_info["codes"]
            if len(target) == 1:
                codes = codes[1:]  # primary code already tried above
            matched = None
            for c_code in codes:
                matched = self._try_select_code(ch, c_code, max_extra_rows=4, ocr=ocr)
                if matched is not None:
                    if matched.get("error") == "no_candidate_bar":
                        return [matched]
                    matched["fallback_from"] = target if len(target) > 1 else "polyphone_alt"
                    logs.append(matched)
                    break
            if matched is None:
                fallback_code = ch_info["codes"][0]
                self.bridge.cmd(f"type {fallback_code}")
                time.sleep(0.18)
                self._commit_candidate_slot(1)
                logs.append({"word": ch, "code": fallback_code, "slot": 1, "view": "fallback_slot1", "ms": 0.0})
        return logs

    def type_ziranma(self, text: str, ocr=None) -> list[dict]:
        """
        Closed-loop Ziranma Shuangpin (自然码双拼) input:
        - Segments Chinese clauses into natural words (`CFStringTokenizer`).
        - After typing each Shuangpin code, inspects the floating candidate bar via OCR and
          selects the exact matching candidate number (`1..9`) instead of blindly pressing space!
        - Returns a structured selection trace for every committed segment.
        """
        from ziranma import encode_ziranma_plan

        trace: list[dict] = []
        for step in encode_ziranma_plan(text):
            if step["kind"] == "zh":
                w_logs = self.select_ziranma_word(step, ocr=ocr)
                trace.extend(w_logs)
                if any(x.get("error") == "no_candidate_bar" for x in w_logs):
                    break
            elif step["kind"] == "en":
                mt = self.live_mtime()
                self.bridge.cmd(f"type {step['text']}")
                self.key("enter")
                self.wait_settled(mt, change_wait=0.35, quiet=0.12, max_wait=0.50, min_wait=0.14)
                trace.append({"word": step["text"], "kind": "en"})
            else:
                self.type_text(step["text"])
                trace.append({"word": step["text"], "kind": "sym"})
        return trace

    def open_app_spotlight(self, query: str, raw_ascii: bool = False, ocr=None) -> dict:
        """
        Vision-verified Spotlight app launcher with closed-loop Ziranma candidate selection:
        1) Resets to Home Screen first so `cmd+space` never toggles an already-open Spotlight closed.
        2) Clears leftover Spotlight query via the `⊗` button at `(660, 1445)` (never `(320, 1445)` which hits `[— 打开]`).
        3) For Chinese app names (`raw_ascii=False`), uses `type_ziranma(query)` to inspect the candidate bar and pick the exact app name (e.g. `uivs` -> `时钟`, never `始终`!).
        4) Waits for Spotlight top hit (`最佳搜索结果`) to refresh, then presses `enter` (with direct icon tap safety net).
        """
        t0 = time.perf_counter()
        self.go_home()
        mt = self.live_mtime()
        self.key("cmd+space")
        self.wait_settled(mt, change_wait=0.75, quiet=0.25, max_wait=1.1, min_wait=0.50)

        # Ensure Spotlight text field is focused and completely empty
        self.key("cmd+a")
        self.key("backspace")
        self.key("backspace")
        time.sleep(0.15)
        sp_items = self._ocr_recognize(LIVE_JPG, ocr=ocr)
        has_leftover = any(
            1405 <= it["y"] <= 1490 and 85 <= it["x"] <= 560 and it["text"].strip() not in ("", "搜索", "Q搜索", "Q 搜索")
            for it in sp_items
        ) or any("最佳搜索结果" in it["text"] or ("打开" in it["text"] and it["y"] > 1380) for it in sp_items)
        if has_leftover:
            # Tap `⊗` clear icon on right side of Spotlight search pill (clears & focuses in 1 click!)
            mt_f = self.live_mtime()
            self.tap_px(660, 1445)
            self.wait_settled(mt_f, change_wait=0.55, quiet=0.20, max_wait=0.80, min_wait=0.40)

        q = query.strip()
        trace: list[dict] = []
        mt_top = self.live_mtime()
        if raw_ascii:
            self.bridge.cmd(f"type {q}")
            self.key("enter")
            trace.append({"word": q, "kind": "en"})
        else:
            trace = self.type_ziranma(q, ocr=ocr)
            mt_top = self.live_mtime()
            if any(x.get("error") for x in trace):
                return {"query": q, "ms": round((time.perf_counter() - t0) * 1000, 1), "trace": trace}

        # Wait until Spotlight finishes indexing `最佳搜索结果` (`— 打开` pill takes ~0.65-0.80s)
        self.wait_settled(mt_top, change_wait=0.85, quiet=0.24, max_wait=1.15, min_wait=0.72)
        mt2 = self.live_mtime()
        self.key("enter")
        self.wait_settled(mt2, change_wait=1.5, quiet=0.40, max_wait=2.6, min_wait=1.30)

        # Safety net: if Spotlight stayed open (`最佳...` or bottom `— 打开` pill still visible), tap top-hit app icon directly
        post_items = self._ocr_recognize(LIVE_JPG, ocr=ocr)
        if any("最佳" in it["text"] or ("打开" in it["text"] and it["y"] > 1380) for it in post_items):
            app_hit = next((it for it in post_items if q in it["text"] and 160 <= it["y"] <= 360), None)
            mt3 = self.live_mtime()
            if app_hit:
                self.tap_px(app_hit["x"], max(180, app_hit["y"] - 55))
            else:
                self.tap_px(118, 225)
            self.wait_settled(mt3, change_wait=1.2, quiet=0.35, max_wait=2.2, min_wait=0.90)
        return {
            "query": q,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "trace": trace,
        }

    def go_home(self):
        """
        Reliable Home Screen transition: sends HID Consumer `AC_HOME` (`homescreen`) and waits for
        SpringBoard settlement. Avoids blind swipe fallbacks when already resting on Home Screen Page 1.
        """
        mt = self.live_mtime()
        self.bridge.cmd("homescreen")
        self.wait_settled(mt, change_wait=1.0, quiet=0.35, max_wait=1.6, min_wait=0.82)

    def go_back(self):
        """iOS edge-swipe back gesture (0.008, 0.45 -> 0.62, 0.45)."""
        if self.abs_mode:
            mt = self.live_mtime()
            self.last_action_mtime = mt
            self.bridge.cmd("absswipe 80 5000 6800 5000 220 20")
            return
        x1, y1 = self._frac_to_units(0.008, 0.45)
        dx = round(0.61 * self.span_w)
        if self.pos is None:
            self.safe_home()
        self.safe_move(x1 - self.pos[0], y1 - self.pos[1], step=12)
        time.sleep(0.04)
        self.bridge.cmd("press")
        self.bridge.cmd(f"move {dx} 0 30")
        self.bridge.cmd("release")
        self._save_pos(None)
        time.sleep(0.15)

    def consumer(self, usage: int | str) -> str:
        """Send a 16-bit HID Consumer Control usage (hex string like `0x00B8` or int)."""
        usage_str = f"0x{usage:04X}" if isinstance(usage, int) and not isinstance(usage, bool) else str(usage).strip()
        try:
            val = int(usage_str, 0)
        except ValueError as exc:
            raise ValueError(f"Invalid consumer usage: {usage!r}") from exc
        if val < 1 or val > 0x03FF:
            raise ValueError(f"Consumer usage out of range (1..0x03FF): {usage!r}")
        resp = self.bridge.cmd(f"consumer {usage_str}")
        time.sleep(0.05)
        return resp

    def toggle_keyboard(self) -> str:
        """Send Consumer Eject (`0x00B8`) to toggle the iOS soft keyboard while BLE HID stays connected."""
        return self.consumer("0x00B8")

    def power_key(self) -> str:
        """Send Consumer Power (`0x0030`) to wake or lock the iPhone screen."""
        return self.consumer("0x0030")

    def spotlight_key(self) -> str:
        """Send Consumer AC Search (`0x0221`) to open Spotlight."""
        return self.consumer("0x0221")

    def click_button(self, button: int = 1) -> str:
        """Click HID Mouse Button `1..5` (used for AssistiveTouch Button 2..5 shortcuts)."""
        if isinstance(button, bool) or not isinstance(button, int) or button < 1 or button > 5:
            raise ValueError(f"button must be an integer in 1..5, got {button!r}")
        self.last_action_mtime = self.live_mtime()
        resp = self.bridge.cmd(f"click {button}")
        time.sleep(0.05)
        return resp

    def calibrate_abs(self) -> dict:
        """
        Probe `(0.20, 0.20)` and `(0.80, 0.80)` via `measure_pointer` in `abs_mode`,
        fit linear `abs_x0, abs_x1, abs_y0, abs_y1` endpoints, and save to `iphone_calibration.json`.
        """
        if not self.abs_mode:
            raise RuntimeError("calibrate_abs requires abs_mode=True")
        old_bounds = (self.abs_x0, self.abs_x1, self.abs_y0, self.abs_y1)
        self.abs_x0, self.abs_x1 = 0.0, 10000.0
        self.abs_y0, self.abs_y1 = 0.0, 10000.0
        try:
            self.point_frac(0.20, 0.20, aim=False)
            p1 = self.measure_pointer(0.20, 0.20)
            self.point_frac(0.80, 0.80, aim=False)
            p2 = self.measure_pointer(0.80, 0.80)
            if p1 is None or p2 is None:
                raise RuntimeError(f"Failed to locate pointer during calibrate_abs: p1={p1}, p2={p2}")
            dfx = p2[0] - p1[0]
            dfy = p2[1] - p1[1]
            if dfx < 0.2 or dfy < 0.2:
                raise RuntimeError(f"Degenerate calibration span: p1={p1}, p2={p2}")
            span_x = 6000.0 / dfx
            span_y = 6000.0 / dfy
            self.abs_x0 = round(2000.0 - p1[0] * span_x, 2)
            self.abs_x1 = round(self.abs_x0 + span_x, 2)
            self.abs_y0 = round(2000.0 - p1[1] * span_y, 2)
            self.abs_y1 = round(self.abs_y0 + span_y, 2)
            self._save_calib()
            return {
                "probe_20": [round(p1[0], 4), round(p1[1], 4)],
                "probe_80": [round(p2[0], 4), round(p2[1], 4)],
                "abs_x0": self.abs_x0,
                "abs_x1": self.abs_x1,
                "abs_y0": self.abs_y0,
                "abs_y1": self.abs_y1,
            }
        except Exception:
            self.abs_x0, self.abs_x1, self.abs_y0, self.abs_y1 = old_bounds
            raise

    @staticmethod
    def _clipboard_funcs():
        pbcopy_fn = copy_to_macos_pbcopy
        srv_fn = get_clipboard_server
        if pbcopy_fn is None or srv_fn is None:
            from clipboard_http import copy_to_macos_pbcopy as _pbcopy, get_clipboard_server as _get_srv

            pbcopy_fn = pbcopy_fn or _pbcopy
            srv_fn = srv_fn or _get_srv
        return srv_fn, pbcopy_fn

    def _paste_via_pbcopy(self, text: str, mode: str, t0: float, pbcopy_fn) -> dict:
        pbcopy_fn(text)
        time.sleep(float(os.environ.get("IPHONE_PBCOPY_WAIT", "2.0")))
        mt = self.live_mtime()
        self.key("cmd+v")
        self.wait_settled(mt, change_wait=0.55, quiet=0.16, max_wait=0.85, min_wait=0.18)
        return {
            "ok": True,
            "mode": mode,
            "chars": len(text),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    def paste_text(
        self,
        text: str,
        trigger: str | None = None,
        timeout: float = 0.90,
        fallback_ime: bool = True,
        ocr=None,
    ) -> dict:
        """
        Paste arbitrary UTF-8 text (Chinese, English, Emoji, multiline, URLs) onto the iPhone via
        the local HTTP clipboard mailbox (`AgentPaste` Shortcut) + `cmd+v`, with optional `pbcopy`
        or Ziranma IME fallback on timeout.
        """
        t0 = time.perf_counter()
        srv_fn, pbcopy_fn = self._clipboard_funcs()
        trig = (trigger or os.environ.get("IPHONE_PASTE_TRIGGER", "btn:3")).strip()
        if trig == "pbcopy":
            return self._paste_via_pbcopy(text, "pbcopy", t0, pbcopy_fn)

        srv = srv_fn()
        gen = srv.stage(text)
        if trig.startswith("btn:"):
            self.click_button(int(trig.split(":", 1)[1]))
        elif trig.startswith("key:"):
            self.key(trig.split(":", 1)[1].lower())
        else:
            raise ValueError(f"Unsupported paste trigger: {trig!r}")

        fetched = srv.wait_fetched(gen, timeout=timeout)
        if fetched:
            time.sleep(0.045)
            mt = self.live_mtime()
            self.key("cmd+v")
            self.wait_settled(mt, change_wait=0.55, quiet=0.16, max_wait=0.85, min_wait=0.18)
            return {
                "ok": True,
                "mode": "http_clip",
                "chars": len(text),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }

        if os.environ.get("IPHONE_PBCOPY_FALLBACK") == "1":
            return self._paste_via_pbcopy(text, "fallback_pbcopy", t0, pbcopy_fn)
        if fallback_ime:
            lines = text.splitlines()
            trace: list[dict] = []
            for idx, line in enumerate(lines):
                bmp_line = "".join(ch for ch in line if ord(ch) <= 0xFFFF)
                if bmp_line:
                    trace.extend(self.type_ziranma(bmp_line, ocr=ocr))
                if idx + 1 < len(lines):
                    self.key("enter")
            return {
                "ok": True,
                "mode": "fallback_ziranma",
                "chars": len(text),
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "trace": trace,
            }
        return {"ok": False, "error": "clip_fetch_timeout", "setup_url": srv.setup_url}


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    action = args[0] if args else "status"
    if action == "status":
        ctl = IPhoneController()
        st = ctl.bridge.status_json()
        st["abs_mode"] = ctl.abs_mode
        st["usb_capture"] = usb_capture_active()
        print("status:", json.dumps(st))
        print("vdbg:  ", ctl.bridge.raw_line("0 vdbg"))
        print("target:", ctl.bridge.cmd("target"))
        print("human: ", ctl.bridge.cmd("human"))
        ctl.close()
    elif action == "daemon":
        run_video_daemon()
    elif action == "usb_daemon":
        usb_bin = os.path.join(WORKSPACE_DIR, "usb_capture")
        usb_src = os.path.join(WORKSPACE_DIR, "usb_capture.swift")
        if not os.path.exists(usb_bin):
            subprocess.check_call(["swiftc", "-O", usb_src, "-o", usb_bin])
        os.execv(usb_bin, [usb_bin] + args[1:])
    elif action == "screenshot":
        out = args[1] if len(args) > 1 else "/tmp/iphone_latest.jpg"
        c = IPhoneController()
        p = c.screenshot(out)
        c.close()
        print(f"Saved {p} ({os.path.getsize(p)} bytes, age={time.time() - os.path.getmtime(LIVE_JPG):.1f}s)")
    elif action == "tap_px":
        c = IPhoneController()
        c.tap_px(float(args[1]), float(args[2]))
        c.close()
        print("ok")
    elif action == "tap_aim":
        c = IPhoneController()
        info = c.tap_px(float(args[1]), float(args[2]), aim=True)
        c.close()
        print(json.dumps(info))
    elif action == "double_tap":
        c = IPhoneController()
        info = c.double_tap_px(float(args[1]), float(args[2]), aim=("--aim" in args))
        c.close()
        print(json.dumps(info))
    elif action == "longpress":
        c = IPhoneController()
        dur = float(args[3]) / 1000.0 if len(args) > 3 else 1.0
        info = c.long_press_px(float(args[1]), float(args[2]), duration_s=dur)
        c.close()
        print(json.dumps(info))
    elif action == "tap_frac":
        c = IPhoneController()
        c.tap_frac(float(args[1]), float(args[2]))
        c.close()
        print("ok")
    elif action == "abstap":
        c = IPhoneController()
        info = c.tap_frac(float(args[1]), float(args[2]))
        c.close()
        print(json.dumps(info))
    elif action == "tap_units":
        c = IPhoneController()
        c.tap_units(int(args[1]), int(args[2]))
        c.close()
        print("ok")
    elif action == "swipe_px":
        c = IPhoneController()
        c.swipe_px(float(args[1]), float(args[2]), float(args[3]), float(args[4]))
        c.close()
        print("ok")
    elif action == "key":
        c = IPhoneController()
        c.key(args[1])
        c.close()
        print("ok")
    elif action == "consumer":
        c = IPhoneController()
        res = c.consumer(args[1])
        c.close()
        print(res)
    elif action == "kbd_toggle":
        c = IPhoneController()
        res = c.toggle_keyboard()
        c.close()
        print(res)
    elif action == "power":
        c = IPhoneController()
        res = c.power_key()
        c.close()
        print(res)
    elif action == "btn":
        c = IPhoneController()
        res = c.click_button(int(args[1]))
        c.close()
        print(res)
    elif action == "calibrate_abs":
        c = IPhoneController()
        res = c.calibrate_abs()
        c.close()
        print(json.dumps(res))
    elif action == "type":
        c = IPhoneController()
        c.type_text(" ".join(args[1:]))
        c.close()
        print("ok")
    elif action == "type_zh":
        c = IPhoneController()
        res = c.type_ziranma(" ".join(args[1:]))
        c.close()
        print(json.dumps(res, ensure_ascii=False))
    elif action == "spotlight":
        c = IPhoneController()
        q_str = " ".join(args[1:])
        is_ascii = all(ord(ch) < 128 for ch in q_str)
        res = c.open_app_spotlight(q_str, raw_ascii=is_ascii)
        c.close()
        print(json.dumps(res, ensure_ascii=False))
    elif action == "home":
        c = IPhoneController()
        c.go_home()
        c.close()
        print("ok")
    elif action == "cmd":
        b = IPhoneBridge()
        print(b.cmd(" ".join(args[1:])))
        b.close()
    elif action == "paste":
        c = IPhoneController()
        res = c.paste_text(" ".join(args[1:]))
        c.close()
        print(json.dumps(res, ensure_ascii=False))
    elif action == "clip_serve":
        srv_fn, _ = IPhoneController._clipboard_funcs()
        srv = srv_fn()
        print(
            json.dumps(
                {"ok": True, "clip_url": srv.clip_url, "setup_url": srv.setup_url, "port": srv.port},
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            srv.close()


if __name__ == "__main__":
    main()
