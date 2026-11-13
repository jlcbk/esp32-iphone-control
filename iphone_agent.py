#!/usr/bin/env python3
"""
Optimized 4-Tier Cascading iPhone Local Control Agent:
  Tier 1:   Persistent Apple Vision OCR (`./screen_ocr --serve`, warm in-memory, ~80-120ms)
  Tier 2:   Direct Text/Coordinate Relative BLE HID Action (`IPhoneController`, ~0.2-0.35s/tap, 0 tokens)
  Tier 2.5: Local Millisecond Red-Badge & UISwitch Detectors (RGB pixel geometry + OCR binding, ~8-25ms, 0 tokens)
  Tier 3:   Grounded Local Multimodal Small Brain (`360x779` JPEG + indexed OCR -> `qwen3.5:4b` via Ollama, ~3-4s)
  Tier 4:   Cloud Big Brain Escalation (only when Tier 3 explicitly returns `escalate` or hits 3-step stuck breaker)

Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
"""

from __future__ import annotations

import base64
from collections import deque
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from PIL import Image
from iphone_ctl import IPhoneController, LIVE_JPG, frames_changed
from som_grounding import detect_unlabeled_icons, render_som_jpeg

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_BIN = os.path.join(WORKSPACE_DIR, "screen_ocr")
INTENTS_FILE = os.path.join(WORKSPACE_DIR, "iphone_intents.json")
RUN_TMP_DIR = "/tmp"
LAST_RUN_FILE = "/tmp/iphone_last_run.json"
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")


class PersistentOCR:
    """Keeps `screen_ocr --serve` warm in memory over stdin/stdout (~250ms/frame vs ~750ms cold)."""

    def __init__(self, bin_path: str = OCR_BIN):
        self.bin_path = bin_path
        self.proc: subprocess.Popen | None = None
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            [self.bin_path, "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def recognize(self, image_path: str = LIVE_JPG) -> list[dict]:
        abs_path = os.path.abspath(image_path)
        for _ in range(2):
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
                self.proc.stdin.write(abs_path + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                if line:
                    return json.loads(line)
            except Exception:
                self.close()
        out = subprocess.check_output([self.bin_path, abs_path], timeout=8).decode("utf-8", errors="replace")
        return json.loads(out) if out.strip() else []

    def close(self):
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write("QUIT\n")
                    self.proc.stdin.flush()
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                pass
            self.proc = None


def run_ocr(image_path: str = LIVE_JPG) -> list[dict]:
    """One-shot OCR helper for standalone callers."""
    out = subprocess.check_output([OCR_BIN, image_path], timeout=8).decode("utf-8", errors="replace")
    return json.loads(out) if out.strip() else []


def find_ocr_match(items: list[dict], target_text: str, exact: bool = False) -> dict | None:
    """Tier 2: Match target text against local OCR items."""
    q = target_text.strip().lower()
    if exact:
        for it in items:
            if it["text"].strip().lower() == q:
                return it
    for it in items:
        if q in it["text"].strip().lower():
            return it
    return None


def detect_red_badges(image_path: str = LIVE_JPG, ocr_items: list[dict] | None = None) -> list[dict]:
    """
    Tier 2.5: Local ~45ms crimson notification dot (`🔴`) detector with resolution-independent OCR binding.
    Uses NEAREST 1/2 subsampling so small 8-12px red dots are never washed out by bilinear white-neighbor averaging.
    """
    with Image.open(image_path) as im:
        orig_w, orig_h = im.size
        scale = 2 if orig_w >= 600 else 1
        sw, sh = orig_w // scale, orig_h // scale
        small = im.convert("RGB").resize((sw, sh), Image.Resampling.NEAREST) if scale > 1 else im.convert("RGB")
        px = list(small.getdata())

    mask = [False] * (sw * sh)
    for idx, (r, g, b) in enumerate(px):
        if r >= 205 and g <= 65 and b <= 68 and (r - g) >= 150:
            mask[idx] = True

    visited = [False] * (sw * sh)
    badges: list[dict] = []
    y_min, y_max = int(sh * 0.045), int(sh * 0.98)
    min_area = max(5, round(6 * ((orig_w / 720.0) ** 2) * ((2.0 / scale) ** 2)))
    max_area = round(680 * ((orig_w / 720.0) ** 2) * ((2.0 / scale) ** 2))
    max_dim = round(52 * (orig_w / 720.0) * (2.0 / scale))

    for y in range(y_min, y_max):
        row_off = y * sw
        for x in range(sw):
            start = row_off + x
            if not mask[start] or visited[start]:
                continue
            q = deque([start])
            visited[start] = True
            sx = sy = cnt = 0
            min_x = max_x = x
            min_y = max_y = y
            while q:
                cur = q.popleft()
                cy, cx = divmod(cur, sw)
                sx += cx
                sy += cy
                cnt += 1
                if cx < min_x:
                    min_x = cx
                if cx > max_x:
                    max_x = cx
                if cy < min_y:
                    min_y = cy
                if cy > max_y:
                    max_y = cy
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < sw and 0 <= ny < sh:
                        ni = ny * sw + nx
                        if mask[ni] and not visited[ni]:
                            visited[ni] = True
                            q.append(ni)

            bw = max_x - min_x + 1
            bh = max_y - min_y + 1
            fill_ratio = cnt / float(max(1, bw * bh))
            if (
                min_area <= cnt <= max_area
                and max(bw, bh) <= max_dim
                and (max(bw, bh) / max(1, min(bw, bh))) <= 2.8
                and fill_ratio >= 0.38
            ):
                bx = round((sx / cnt) * scale)
                by = round((sy / cnt) * scale)
                x0_px, x1_px = min_x * scale - 6, (max_x + 1) * scale + 6
                y0_px, y1_px = min_y * scale - 6, (max_y + 1) * scale + 6
                entry: dict = {"badge_x": bx, "badge_y": by, "area": cnt}
                if ocr_items:
                    # 1) Check if a numeric count ('3', '12', '99+') sits inside the red badge
                    for it in ocr_items:
                        t = it["text"].strip().rstrip("+")
                        if t.isdigit() and x0_px <= it["x"] <= x1_px and y0_px <= it["y"] <= y1_px:
                            entry["count"] = it["text"].strip()
                            break
                    # 2) Bind to app/tab label below-left of top-right badge (fdx > 0, fdy in [0.032, 0.108])
                    best = None
                    best_dist = 1e9
                    for it in ocr_items:
                        if it.get("text", "").strip() == entry.get("count"):
                            continue
                        fdx = (bx - it["x"]) / float(orig_w)
                        fdy = (it["y"] - by) / float(orig_h)
                        if 0.008 <= fdx <= 0.125 and 0.032 <= fdy <= 0.108:
                            dist = math.hypot(fdx - 0.055, fdy - 0.068)
                            if dist < best_dist:
                                best_dist = dist
                                best = it
                    # 3) Or bind to a horizontal list row label on the left (e.g. inside iOS Settings row)
                    row_best = None
                    if best is None and "count" in entry:
                        for it in ocr_items:
                            if it.get("text", "").strip() == entry.get("count"):
                                continue
                            if abs(it["y"] - by) <= 28 and it["x"] < bx:
                                row_best = it
                                break
                    if best is not None:
                        entry["label"] = best["text"]
                        entry["label_x"] = best["x"]
                        entry["label_y"] = best["y"]
                        entry["tap_x"] = best["x"]
                        entry["tap_y"] = round((by + best["y"]) / 2.0)
                    elif row_best is not None:
                        entry["label"] = row_best["text"]
                        entry["tap_x"] = row_best["x"]
                        entry["tap_y"] = by
                    else:
                        # Suppress unnumbered large icon arcs (e.g. Chrome logo) or non-tab-bar color blobs
                        if "count" not in entry and (cnt >= 55 or by < orig_h * 0.88):
                            continue
                        entry["tap_x"] = max(16, bx - round(0.035 * orig_w))
                        entry["tap_y"] = min(orig_h - 16, by + round(0.02 * orig_h))
                badges.append(entry)

    badges.sort(key=lambda b: (b["badge_y"], b["badge_x"]))
    return badges


def detect_ios_switches(image_path: str = LIVE_JPG, ocr_items: list[dict] | None = None) -> list[dict]:
    """
    Tier 2.5 Deterministic iOS UISwitch State Detector (~8ms, 0 tokens, 0% hallucination).
    Inspects right-column switch regions aligned with OCR row labels:
      - iOS ON switch (`#34C759`): strong green channel (`G >= 165, G - R >= 70`)
      - iOS OFF switch: dark/light neutral grey + white circular knob (`R,G,B >= 225`) with zero green fill.
    """
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        px = rgb.load()

    if not ocr_items:
        return []

    x_start, x_end = int(w * 0.78), int(w * 0.96)
    results = []
    seen_y: list[int] = []
    for it in sorted(ocr_items, key=lambda item: item["y"]):
        if it["x"] > w * 0.55 or it["y"] < h * 0.12 or it["y"] > h * 0.90:
            continue
        cy = int(it["y"])
        if any(abs(cy - py) < 55 for py in seen_y):
            continue
        y0, y1 = max(0, cy - 28), min(h, cy + 28)
        green_px = 0
        knob_white_px = 0
        for sy in range(y0, y1, 2):
            for sx in range(x_start, x_end, 2):
                r, g, b = px[sx, sy]
                if g >= 165 and (g - r) >= 70 and (g - b) >= 40:
                    green_px += 1
                elif r >= 230 and g >= 230 and b >= 230:
                    knob_white_px += 1
        # A real UISwitch always has a bright white circular knob (`knob_white_px >= 45` at step=2 -> ~180px full-res)
        if knob_white_px >= 45:
            seen_y.append(cy)
            results.append({
                "label": it["text"],
                "y": cy,
                "on": green_px >= 25,
                "green_samples": green_px,
                "knob_samples": knob_white_px,
            })
    return results


def encode_downscaled_jpeg(image_path: str, max_width: int = 360, quality: int = 72) -> str:
    """Downscale screenshot in memory (720x1558 -> 360x779) for 4.5x faster VLM vision encoding."""
    with Image.open(image_path) as im:
        w, h = im.size
        if w > max_width:
            nh = round(h * (max_width / float(w)))
            im = im.convert("RGB").resize((max_width, nh), Image.Resampling.BILINEAR)
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def ask_local_small_brain(
    goal: str,
    items: list[dict] | None = None,
    image_path: str = LIVE_JPG,
    size: tuple[int, int] = (720, 1558),
    history: list[str] | None = None,
    switches: list[dict] | None = None,
    badges: list[dict] | None = None,
    stuck_warning: str | None = None,
) -> dict:
    """
    Tier 3 Grounded Local Brain: Queries M1 Air `qwen3.5:4b` with Set-of-Mark 360x779 JPEG +
    indexed native-res Apple Vision OCR & unlabeled icon elements + deterministic switch/badge facts.
    """
    ocr_list = items or []
    # Filter top iOS status bar (`y <= 4.2%`) and group into 24px rows up to 64 items so bottom tabs stay intact
    min_y = size[1] * 0.042
    indexed_ocr = [
        it for it in sorted(ocr_list, key=lambda d: (d["y"] // 24, d["x"]))
        if len(it["text"].strip()) >= 1 and it["y"] > min_y
    ][:64]
    icons = detect_unlabeled_icons(image_path, indexed_ocr, max_icons=14)
    indexed_elements = indexed_ocr + icons
    b64_img = render_som_jpeg(image_path, indexed_elements, max_width=360, quality=72)
    ocr_line = " ".join(f"[{i}]{it['text'].strip()[:16]}" for i, it in enumerate(indexed_elements))

    extra_facts: list[str] = []
    if stuck_warning:
        extra_facts.append(stuck_warning)
    if switches:
        extra_facts.append("开关实测:" + ",".join(f"{s['label']}={'ON' if s['on'] else 'OFF'}" for s in switches))
    if badges:
        extra_facts.append("红点角标:" + ",".join(f"{b.get('label') or '图标'}({b.get('count') or 'dot'})" for b in badges))
    if history:
        extra_facts.append("已执行:" + " -> ".join(history[-4:]))

    facts_block = ("\n".join(extra_facts) + "\n") if extra_facts else ""
    prompt = (
        f"任务: {goal}\n"
        f"OCR元素: {ocr_line}\n"
        f"{facts_block}"
        "规则: 优先用tap_ocr按编号点文字或<图标:...>(图中红色编号框)；点搜索框/输入框并打字可直接用tap_and_type一步完成；仅查看/检查状态且当前画面已有答案时直接输出done(严禁点击开关)；桌面未见目标App用open_app；涉及支付/发消息/删除/授权必须输出confirm；未进目标页严禁瞎猜。\n"
        "输出单行纯JSON(任选一动作)：\n"
        '1) 点OCR文字: {"action":"tap_ocr","id":编号,"target":"说明"}\n'
        '2) 点框并输入: {"action":"tap_and_type","id":编号,"text":"内容","submit":true或false}\n'
        '3) 点纯图标(0~1000坐标): {"action":"tap","x":整数,"y":整数,"target":"说明"}\n'
        '4) 打开应用: {"action":"open_app","name":"应用名"}\n'
        '5) 输入文字: {"action":"type","text":"内容"}\n'
        '6) 滚动列表: {"action":"scroll","dir":"down或up"}\n'
        '7) 返回/桌面: {"action":"back"} 或 {"action":"home"}\n'
        '8) 敏感确认: {"action":"confirm","reason":"将要执行的敏感操作"}\n'
        '9) 完成任务: {"action":"done","summary":"基于当前画面的准确结论"}\n'
        '10) 升级云端: {"action":"escalate","reason":"原因"}'
    )
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [b64_img]}],
        "format": "json",
        "think": False,
        "keep_alive": -1,
        "options": {"num_predict": 160, "temperature": 0.0},
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = opener.open(req, timeout=35).read().decode("utf-8")
    content = json.loads(raw)["message"]["content"].strip()
    if "```" in content:
        content = content.split("```")[1].replace("json", "").strip()
    dec = json.loads(content)
    dec["_indexed_ocr"] = indexed_elements
    return dec


class FourTierIPhoneAgent:
    def __init__(self):
        self.ctl = IPhoneController()
        self.ocr = PersistentOCR()

    def close(self):
        self.ocr.close()
        self.ctl.close()

    def type_chinese(self, text: str) -> list[dict]:
        """Type Chinese/mixed text via Ziranma Shuangpin with closed-loop candidate bar selection."""
        return self.ctl.type_ziranma(text, ocr=self.ocr)

    def type_smart(self, text: str):
        """
        IME-safe text entry: routes long/multiline/URL/Emoji text (or when `IPHONE_ALWAYS_PASTE=1`)
        through `paste_text` (~200ms `AgentPaste` HTTP clipboard + `cmd+v`, with Ziranma fallback);
        keeps short <=8-char queries on `type_chinese` / `type_text` for zero-dependency speed.
        """
        if (
            os.environ.get("IPHONE_ALWAYS_PASTE") == "1"
            or "\n" in text
            or "://" in text
            or any(ord(c) > 0xFFFF for c in text)
            or len(text) > 8
        ):
            return self.ctl.paste_text(text, fallback_ime=True, ocr=self.ocr)
        if any(c.isalpha() or ord(c) > 127 for c in text):
            return self.type_chinese(text)
        self.ctl.type_text(text)
        return [{"word": text, "kind": "sym"}]

    def scroll_screen(self, direction: str = "down", mt: float | None = None):
        """Deterministic pointer drag scroll (immune to where the AssistiveTouch cursor was resting)."""
        if mt is None:
            mt = self.ctl.live_mtime()
        if direction == "up":
            self.ctl.swipe_frac(0.5, 0.26, 0.5, 0.76, hold=True)
        else:
            self.ctl.swipe_frac(0.5, 0.76, 0.5, 0.24, hold=True)
        self.ctl.wait_settled(mt, change_wait=0.7, quiet=0.25, max_wait=1.3, min_wait=0.40)

    def open_app(self, app_name: str) -> dict:
        """Open an iOS app via Spotlight with closed-loop Ziranma candidate verification."""
        is_ascii = all(ord(ch) < 128 for ch in app_name)
        return self.ctl.open_app_spotlight(app_name, raw_ascii=is_ascii, ocr=self.ocr)

    def inspect_screen(self, snap_path: str = LIVE_JPG, detect_badges: bool = False) -> dict:
        t0 = time.perf_counter()
        if os.path.abspath(snap_path) != os.path.abspath(LIVE_JPG):
            self.ctl.screenshot(snap_path)
        size = self.ctl.frame_size()
        t_ocr0 = time.perf_counter()
        items = self.ocr.recognize(snap_path)
        ocr_ms = round((time.perf_counter() - t_ocr0) * 1000, 1)
        badges = []
        badge_ms = 0.0
        if detect_badges:
            t_b0 = time.perf_counter()
            badges = detect_red_badges(snap_path, items)
            badge_ms = round((time.perf_counter() - t_b0) * 1000, 1)
        return {
            "size": size,
            "items": items,
            "badges": badges,
            "ocr_ms": ocr_ms,
            "badge_ms": badge_ms,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    def tap_text_or_fallback(
        self,
        target_text: str,
        exact: bool = False,
        settle: bool = True,
    ) -> dict:
        """
        Execute optimized 4-tier cascade to tap `target_text`:
          Tier 1+2: Persistent OCR + relative BLE tap_px (~0.5s total)
          Tier 3:   Downscaled 360x779 M1 Air qwen3.5:9b visual grounding (~6s)
          Tier 4:   Escalate to Cloud Brain
        """
        snap = "/tmp/iphone_step.jpg"
        info = self.inspect_screen(snap, detect_badges=False)
        items, size = info["items"], info["size"]
        hit = find_ocr_match(items, target_text, exact=exact)
        if hit:
            mt = self.ctl.live_mtime()
            use_aim = (hit.get("w", 80) < 52) or (hit.get("h", 44) < 34) or (hit["x"] < 75) or (hit["x"] > 615)
            aim_info = self.ctl.tap_px(hit["x"], hit["y"], aim=use_aim)
            settle_ms = self.ctl.wait_settled(mt) if settle else 0.0
            return {
                "tier": 2,
                "action": "tap_ocr_aim" if use_aim else "tap_ocr",
                "matched": hit["text"],
                "x": hit["x"],
                "y": hit["y"],
                "aim": aim_info,
                "ocr_ms": info["ocr_ms"],
                "settle_ms": settle_ms,
            }

        decision = ask_local_small_brain(f"点击『{target_text}』", items, snap, size)
        act = decision.get("action")
        mt = self.ctl.live_mtime()
        if act == "tap" and "x" in decision and "y" in decision:
            fx, fy = float(decision["x"]) / 1000.0, float(decision["y"]) / 1000.0
            self.ctl.tap_frac(fx, fy)
            settle_ms = self.ctl.wait_settled(mt) if settle else 0.0
            return {"tier": 3, "action": "tap_vlm", "fx": round(fx, 4), "fy": round(fy, 4), "settle_ms": settle_ms, "decision": decision}
        if act == "swipe":
            self.ctl.swipe_frac(
                float(decision["x1"]) / 1000.0,
                float(decision["y1"]) / 1000.0,
                float(decision["x2"]) / 1000.0,
                float(decision["y2"]) / 1000.0,
            )
            settle_ms = self.ctl.wait_settled(mt) if settle else 0.0
            return {"tier": 3, "action": "swipe_vlm", "settle_ms": settle_ms, "decision": decision}
        if act == "home":
            self.ctl.go_home()
            return {"tier": 3, "action": "home_vlm", "decision": decision}
        if act == "done":
            return {"tier": 3, "action": "done", "decision": decision}

        return {"tier": 4, "action": "escalate_cloud", "decision": decision, "ocr_count": len(items), "screenshot": snap}

    def run_goal(self, goal: str, max_steps: int = 8) -> dict:
        """
        Autonomous multi-step goal loop with:
          - Grounded perception (Persistent Apple Vision OCR + UISwitch pixel detector + crimson badge detector)
          - Indexed `tap_ocr` exact bounding-box clicking (zero 4B coordinate estimation error)
          - Stuck-screen detection (`frames_changed`): auto-upgrades repeated tap to closed-loop visual `aim=True`
          - Safety gate (`confirm` pauses immediately before payment/message-send/delete/permission actions)
          - Automatic trajectory recording to `/tmp/iphone_last_run.json` for `save_intent` replay
        """
        t_start = time.perf_counter()
        history: list[str] = []
        steps_log: list[dict] = []
        stuck_count = 0

        for step_idx in range(1, max_steps + 1):
            snap_before = os.path.join(RUN_TMP_DIR, f"iphone_goal_step_{step_idx}.jpg")
            info = self.inspect_screen(snap_before, detect_badges=True)
            items, size, badges = info["items"], info["size"], info["badges"]
            switches = detect_ios_switches(snap_before, items)

            t_vlm = time.perf_counter()
            stuck_warn = (
                "【系统警告】上一步点击后画面无变化！严禁重复同一动作，请改点其他编号、scroll或escalate！"
                if stuck_count >= 1
                else None
            )
            dec = ask_local_small_brain(
                goal,
                items=items,
                image_path=snap_before,
                size=size,
                history=history,
                switches=switches,
                badges=badges,
                stuck_warning=stuck_warn,
            )
            vlm_ms = round((time.perf_counter() - t_vlm) * 1000, 1)
            indexed_ocr = dec.pop("_indexed_ocr", [])
            act = dec.get("action", "escalate")

            if act == "done":
                result = {
                    "status": "done",
                    "summary": dec.get("summary", ""),
                    "steps": steps_log,
                    "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                }
                with open(LAST_RUN_FILE, "w") as f:
                    json.dump({"goal": goal, **result}, f, ensure_ascii=False, indent=2)
                return result

            if act in ("confirm", "escalate"):
                return {
                    "status": act,
                    "reason": dec.get("reason", ""),
                    "steps": steps_log,
                    "screenshot": snap_before,
                    "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                }

            mt = self.ctl.live_mtime()
            replay_step: dict = {"action": act}
            force_aim = stuck_count >= 1

            if act in ("tap_ocr", "tap_and_type"):
                raw_id = dec.get("id", -1)
                if isinstance(raw_id, list):
                    raw_id = raw_id[0] if raw_id else -1
                try:
                    idx = int(str(raw_id).strip("[] "))
                except ValueError:
                    idx = -1
                target_label = dec.get("target", "")
                hit = indexed_ocr[idx] if 0 <= idx < len(indexed_ocr) else find_ocr_match(items, target_label)
                if hit is None:
                    history.append(f"miss({target_label})")
                    stuck_count += 1
                    continue
                tx, ty = float(hit["x"]), float(hit["y"])
                # On SpringBoard Home Screen (`搜索` pill at y~1300), tap app icon center (`ty - 60`) above label
                on_home = any("搜索" in it["text"] and 1250 <= it["y"] <= 1350 for it in items)
                if on_home and hit.get("kind") != "icon" and 180 <= ty <= 1220 and hit.get("h", 40) <= 34 and act == "tap_ocr":
                    ty -= 60
                use_aim = force_aim or hit.get("w", 80) < 52 or tx < 75 or tx > 615
                self.ctl.tap_px(tx, ty, aim=use_aim)
                self.ctl.wait_settled(mt, change_wait=0.55, quiet=0.18, max_wait=0.85, min_wait=0.35)
                if act == "tap_and_type" and dec.get("text"):
                    txt = str(dec["text"])
                    self.type_smart(txt)
                    if dec.get("submit"):
                        mt_s = self.ctl.live_mtime()
                        self.ctl.key("enter")
                        self.ctl.wait_settled(mt_s)
                    replay_step = {"action": "tap_and_type", "text_anchor": hit["text"], "text": txt, "submit": bool(dec.get("submit"))}
                    history.append(f"tap_and_type({hit['text']}->{txt[:10]})")
                else:
                    replay_step = {"action": "tap_text", "text": hit["text"], "fx": round(tx / size[0], 4), "fy": round(ty / size[1], 4)}
                    history.append(f"tap_ocr({hit['text']})")

            elif act == "tap" and "x" in dec and "y" in dec:
                fx, fy = float(dec["x"]) / 1000.0, float(dec["y"]) / 1000.0
                self.ctl.tap_frac(fx, fy, aim=(force_aim or fx < 0.10 or fx > 0.85))
                self.ctl.wait_settled(mt)
                replay_step = {"action": "tap_frac", "fx": round(fx, 4), "fy": round(fy, 4)}
                history.append(f"tap({dec.get('target', '')})")

            elif act == "open_app" and dec.get("name"):
                app_name = str(dec["name"]).strip()
                self.open_app(app_name)
                replay_step = {"action": "open_app", "name": app_name}
                history.append(f"open_app({app_name})")

            elif act == "type" and dec.get("text"):
                txt = str(dec["text"])
                self.type_smart(txt)
                replay_step = {"action": "type", "text": txt}
                history.append(f"type({txt[:10]})")

            elif act == "scroll":
                direction = str(dec.get("dir", "down")).lower()
                self.scroll_screen(direction, mt=mt)
                replay_step = {"action": "scroll", "dir": direction}
                history.append(f"scroll({direction})")

            elif act == "back":
                self.ctl.go_back()
                self.ctl.wait_settled(mt)
                replay_step = {"action": "back"}
                history.append("back")

            elif act == "home":
                self.ctl.go_home()
                replay_step = {"action": "home"}
                history.append("home")

            snap_after = os.path.join(RUN_TMP_DIR, f"iphone_goal_after_{step_idx}.jpg")
            self.ctl.screenshot(snap_after)
            changed = frames_changed(snap_before, snap_after, 0.03)
            stuck_count = 0 if changed else (stuck_count + 1)
            replay_step["ref_anchors"] = [it["text"].strip() for it in indexed_ocr[:6]]
            steps_log.append({
                "step": step_idx,
                "decision": dec,
                "vlm_ms": vlm_ms,
                "changed": changed,
                "replay": replay_step,
            })
            if stuck_count == 2:
                # Dismiss potential invisible overlay / AssistiveTouch menu before final retry
                self.ctl.key("esc")
                history.append("stuck_esc")
            elif stuck_count >= 3:
                return {
                    "status": "escalate",
                    "reason": "连续3步画面无响应，自动熔断升级云端大脑",
                    "steps": steps_log,
                    "screenshot": snap_after,
                    "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                }

        return {
            "status": "max_steps_exceeded",
            "steps": steps_log,
            "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
        }

    def save_intent(self, name: str, from_file: str = LAST_RUN_FILE) -> dict:
        """Save the clean replayable steps of the last successful `run_goal` into `iphone_intents.json`."""
        data = json.load(open(from_file))
        clean_steps = [s["replay"] for s in data.get("steps", []) if s.get("changed", True)]
        store = json.load(open(INTENTS_FILE)) if os.path.exists(INTENTS_FILE) else {}
        store[name] = {"goal": data.get("goal", name), "steps": clean_steps, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(INTENTS_FILE + ".tmp", "w") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(INTENTS_FILE + ".tmp", INTENTS_FILE)
        return store[name]

    def run_intent(self, name: str) -> dict:
        """
        Fast-path 0-VLM intent replay (~0.8-1.5s/step) with reference-screen OCR guard:
        automatically drops back to `run_goal(goal)` if a popup or unexpected screen appears!
        """
        t0 = time.perf_counter()
        if not os.path.exists(INTENTS_FILE):
            raise RuntimeError(f"No saved intents in {INTENTS_FILE}")
        entry = json.load(open(INTENTS_FILE))[name]
        goal = entry["goal"]
        for idx, st in enumerate(entry["steps"], 1):
            items = self.ocr.recognize()
            act = st["action"]
            mt = self.ctl.live_mtime()
            if act in ("tap_text", "tap_and_type"):
                anchor = st["text"] if act == "tap_text" else st["text_anchor"]
                hit = find_ocr_match(items, anchor)
                if not hit:
                    # Layout/screen diverged -> seamless handoff to live VLM `run_goal`!
                    fallback = self.run_goal(goal)
                    fallback["replayed_prefix"] = idx - 1
                    return fallback
                tx, ty = float(hit["x"]), float(hit["y"])
                on_home = any("搜索" in it["text"] and 1250 <= it["y"] <= 1350 for it in items)
                if on_home and 180 <= ty <= 1220 and hit.get("h", 40) <= 34 and act == "tap_text":
                    ty -= 60
                self.ctl.tap_px(tx, ty)
                self.ctl.wait_settled(mt, change_wait=0.55, quiet=0.18, max_wait=0.85, min_wait=0.35)
                if act == "tap_and_type":
                    self.type_smart(st["text"])
                    if st.get("submit"):
                        mt_s = self.ctl.live_mtime()
                        self.ctl.key("enter")
                        self.ctl.wait_settled(mt_s)
            elif act == "tap_frac":
                self.ctl.tap_frac(st["fx"], st["fy"])
                self.ctl.wait_settled(mt)
            elif act == "open_app":
                self.open_app(st["name"])
            elif act == "type":
                self.type_smart(st["text"])
            elif act == "scroll":
                self.scroll_screen(st.get("dir", "down"), mt=mt)
            elif act == "back":
                self.ctl.go_back()
                self.ctl.wait_settled(mt)
            elif act == "home":
                self.ctl.go_home()
        # Final verification read on terminal screen
        final_snap = "/tmp/iphone_intent_done.jpg"
        info = self.inspect_screen(final_snap, detect_badges=True)
        switches = detect_ios_switches(final_snap, info["items"])
        return {
            "status": "replayed",
            "name": name,
            "steps_executed": len(entry["steps"]),
            "switches": switches,
            "badges": info["badges"],
            "ocr_top": [it["text"] for it in info["items"][:16]],
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ocr"
    agent = FourTierIPhoneAgent()
    try:
        if cmd == "ocr":
            info = agent.inspect_screen(detect_badges=True)
            print(json.dumps(info, ensure_ascii=False, indent=2))
        elif cmd == "badges":
            img = sys.argv[2] if len(sys.argv) > 2 else LIVE_JPG
            info = agent.inspect_screen(img, detect_badges=True)
            print(json.dumps({"ocr_ms": info["ocr_ms"], "badge_ms": info["badge_ms"], "badges": info["badges"]}, ensure_ascii=False, indent=2))
        elif cmd == "tap_text":
            res = agent.tap_text_or_fallback(" ".join(sys.argv[2:]))
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif cmd == "goal":
            res = agent.run_goal(" ".join(sys.argv[2:]))
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif cmd == "intent_save":
            res = agent.save_intent(sys.argv[2])
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif cmd == "intent_run":
            res = agent.run_intent(sys.argv[2])
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif cmd == "intent_list":
            store = json.load(open(INTENTS_FILE)) if os.path.exists(INTENTS_FILE) else {}
            print(json.dumps(store, ensure_ascii=False, indent=2))
        elif cmd == "ask_9b":
            goal = " ".join(sys.argv[2:]) or "描述当前页面并指出可点击的主按钮"
            info = agent.inspect_screen("/tmp/iphone_step.jpg")
            t0 = time.perf_counter()
            res = ask_local_small_brain(goal, info["items"], "/tmp/iphone_step.jpg", info["size"])
            res.pop("_indexed_ocr", None)
            res["_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        agent.close()
