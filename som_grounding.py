#!/usr/bin/env python3
"""
Set-of-Mark (SoM) unlabeled UI icon detector and visual overlay renderer for iPhone screenshots.
Enables small local VLMs (`qwen3.5:4b`) to click pure icons (`<`, `×`, `🔍`, `+`, `⋯`, `⚙️`) by
numeric `[id]` without coordinate guessing.

Based on ESP32 iPhone Agent (https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan
Copyright (c) 2026 Henry Yuan. Licensed under the MIT License (see LICENSE and NOTICE).
"""

from __future__ import annotations

import base64
import io
import re

from PIL import Image, ImageChops, ImageDraw, ImageFont

_NONZERO_RUN_RE = re.compile(b"\xff+")


def _load_badge_font(size: int = 11):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def detect_unlabeled_icons(
    image_path: str,
    ocr_items: list[dict] | None = None,
    max_icons: int = 16,
    grad_threshold: int = 38,
) -> list[dict]:
    """
    Detect compact unlabeled UI icons (`<`, `×`, `🔍`, `+`, `⋯`, `⚙️`, etc.) outside OCR boxes,
    top status bar (`y < 0.045*h`), and bottom home indicator (`y > 0.965*h`) in <20ms.
    """
    with Image.open(image_path) as im:
        orig_w, orig_h = im.size
        scale = 2 if orig_w >= 600 else 1
        sw, sh = max(4, orig_w // scale), max(4, orig_h // scale)
        gray = im.convert("L").resize((sw, sh), Image.Resampling.BILINEAR) if scale > 1 else im.convert("L")

    # Compute |I(x+1,y) - I(x-1,y)| + |I(x,y+1) - I(x,y-1)| in Pillow C
    gx = ImageChops.difference(gray.crop((2, 1, sw, sh - 1)), gray.crop((0, 1, sw - 2, sh - 1)))
    gy = ImageChops.difference(gray.crop((1, 2, sw - 1, sh)), gray.crop((1, 0, sw - 1, sh - 2)))
    grad = ImageChops.add(gx, gy)
    lut = [0] * (grad_threshold + 1) + [255] * (255 - grad_threshold)
    edge_mask = grad.point(lut)

    ew, eh = edge_mask.size  # (sw - 2, sh - 2), shifted by (+1, +1) relative to gray
    draw = ImageDraw.Draw(edge_mask)

    # Mask top status bar (y < 0.045 * h) and bottom home bar (y > 0.965 * h)
    top_cut = max(0, int((0.045 * orig_h) / scale) - 1)
    bot_cut = max(0, int((0.965 * orig_h) / scale) - 1)
    if top_cut > 0:
        draw.rectangle((0, 0, ew, min(eh, top_cut)), fill=0)
    if bot_cut < eh:
        draw.rectangle((0, max(0, bot_cut), ew, eh), fill=0)

    # Mask existing OCR bounding boxes expanded by 10px in full-res coords
    pad = 10.0
    for it in ocr_items or []:
        cx = float(it.get("x", 0))
        cy = float(it.get("y", 0))
        bw = float(it.get("w", 40) or 40)
        bh = float(it.get("h", 24) or 24)
        x0 = max(0, int((cx - bw / 2.0 - pad) / scale) - 1)
        y0 = max(0, int((cy - bh / 2.0 - pad) / scale) - 1)
        x1 = min(ew - 1, int((cx + bw / 2.0 + pad) / scale))
        y1 = min(eh - 1, int((cy + bh / 2.0 + pad) / scale))
        if x1 >= x0 and y1 >= y0:
            draw.rectangle((x0, y0, x1, y1), fill=0)

    raw = edge_mask.tobytes()
    cell = max(3, sw // 72)  # 5px at 360w (= 10px full-res)
    cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for m in _NONZERO_RUN_RE.finditer(raw):
        for off in range(m.start(), m.end()):
            ey, ex = divmod(off, ew)
            gx_i = ex + 1
            gy_i = ey + 1
            cells.setdefault((gx_i // cell, gy_i // cell), []).append((gx_i, gy_i))

    visited: set[tuple[int, int]] = set()
    icons: list[dict] = []

    for ck in cells:
        if ck in visited:
            continue
        stack = [ck]
        visited.add(ck)
        pts: list[tuple[int, int]] = []
        while stack:
            qx, qy = stack.pop()
            pts.extend(cells[(qx, qy)])
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nb = (qx + dx, qy + dy)
                    if nb in cells and nb not in visited:
                        visited.add(nb)
                        stack.append(nb)

        if len(pts) < 10:
            continue

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        sub_bw = max_x - min_x + 1
        sub_bh = max_y - min_y + 1
        density = len(pts) / float(max(1, sub_bw * sub_bh))
        if density < 0.06:
            continue

        bw = sub_bw * scale
        bh = sub_bh * scale
        if not (16 <= bw <= 96 and 16 <= bh <= 96):
            continue
        aspect = bw / float(max(1, bh))
        if not (0.40 <= aspect <= 2.40):
            continue

        cx = round(((min_x + max_x) / 2.0) * scale)
        cy = round(((min_y + max_y) / 2.0) * scale)
        if cy < 0.045 * orig_h or cy > 0.965 * orig_h:
            continue

        if cy < 0.16 * orig_h and cx < 0.28 * orig_w:
            zone = "左上"
        elif cy < 0.16 * orig_h and cx > 0.72 * orig_w:
            zone = "右上"
        elif cy > 0.86 * orig_h:
            zone = "底栏"
        else:
            zone = "图标"

        icons.append(
            {
                "kind": "icon",
                "text": f"<图标:{zone}>",
                "x": cx,
                "y": cy,
                "w": bw,
                "h": bh,
                "zone": zone,
            }
        )

    icons.sort(key=lambda d: (d["y"] // 32, d["x"]))
    return icons[:max_icons]


def render_som_jpeg(
    image_path: str,
    indexed_elements: list[dict],
    max_width: int = 360,
    quality: int = 72,
) -> str:
    """
    Resize screenshot to `max_width` and draw crisp Set-of-Mark overlays on unlabeled icons
    (2px bright red/magenta box + high-contrast solid red pill `[idx]` badge) and subtle 1px
    cyan outlines on OCR items, returning base64-encoded JPEG.
    """
    with Image.open(image_path) as im:
        orig_w, orig_h = im.size
        if orig_w > max_width:
            nh = max(1, round(orig_h * (max_width / float(orig_w))))
            canvas = im.convert("RGB").resize((max_width, nh), Image.Resampling.BILINEAR)
        else:
            canvas = im.convert("RGB")

    cw, ch = canvas.size
    sx = cw / float(max(1, orig_w))
    sy = ch / float(max(1, orig_h))
    draw = ImageDraw.Draw(canvas)
    font = _load_badge_font(size=11)

    for idx, el in enumerate(indexed_elements):
        ex = float(el.get("x", 0)) * sx
        ey = float(el.get("y", 0)) * sy
        ew = max(8.0, float(el.get("w", 32) or 32) * sx)
        eh = max(8.0, float(el.get("h", 24) or 24) * sy)
        x0 = max(0, int(round(ex - ew / 2.0)))
        y0 = max(0, int(round(ey - eh / 2.0)))
        x1 = min(cw - 1, int(round(ex + ew / 2.0)))
        y1 = min(ch - 1, int(round(ey + eh / 2.0)))
        if x1 <= x0 or y1 <= y0:
            continue

        if el.get("kind") == "icon":
            draw.rectangle((x0, y0, x1, y1), outline=(255, 24, 96), width=2)
            label = str(idx)
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            pw, ph = tw + 6, th + 4
            bx0 = x1 + 2 if (x1 + 2 + pw <= cw) else max(0, x0 - pw - 2)
            by0 = max(0, min(ch - ph, y0 - 2))
            draw.rounded_rectangle((bx0, by0, bx0 + pw, by0 + ph), radius=3, fill=(225, 16, 64))
            draw.text((bx0 + 3, by0 + 1), label, fill=(255, 255, 255), font=font)
        else:
            # Subtle thin cyan corner/box on OCR elements without occluding text
            draw.rectangle((x0, y0, x1, y1), outline=(0, 185, 220), width=1)

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")
