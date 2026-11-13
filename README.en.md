# ESP32 iPhone Control

[简体中文](README.md) · English

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT%20%2F%20GPL--3.0-111111?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/version-v0.3.0-111111?style=flat-square" alt="v0.3.0">
  <img src="https://img.shields.io/badge/iOS-18%2B-111111?style=flat-square&logo=apple" alt="iOS 18+">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-111111?style=flat-square&logo=apple" alt="macOS Apple Silicon">
  <img src="https://img.shields.io/badge/board-ESP32--S3-111111?style=flat-square" alt="ESP32-S3">
</p>

**ESP32 iPhone Control** is a macOS host-side, zero-jailbreak local vision and GUI automation system for controlling a physical iPhone via an ESP32-S3 AirPlay & BLE HID bridge. No iOS app installation or code signing is required — the Mac receives real-time screen frames over LAN AirPlay (or optional wired USB capture) and drives the iPhone closed-loop over composite BLE HID.

---

## Core Advantages

- **Zero Jailbreak, Zero iOS App Signing**: Relies solely on built-in iOS Screen Mirroring (AirPlay) and AssistiveTouch — no Xcode signing, TestFlight, or developer certificates needed.
- **iPhone Stays on Normal Home Wi-Fi**: The ESP32-S3 joins your home LAN in STA mode (`--sta`), keeping the iPhone online with full internet access and leaving its Lightning / USB-C port free.
- **Zero macOS Bluetooth Overhead**: 100% of the BLE HID link (mouse + keyboard + consumer control) is handled by the ESP32-S3 over USB CDC serial, never touching macOS Bluetooth.
- **Millisecond Local Vision Loop**: Combines zero-buffering PyAV H.264 decoding (`~0.8ms`), persistent Apple Vision OCR (`~80–120ms`), Set-of-Mark unlabeled icon grounding (`<20ms`), and local Ollama VLM reasoning (`qwen3.5:4b`).

---

## Architecture

<p align="center">
  <img src="assets/architecture-en.png" alt="ESP32 iPhone Control Architecture" width="100%">
</p>

---

## Six Core Capabilities (v0.3.0)

### 1. `0..10000` 7-Byte BLE Absolute Mouse + 5-Button & Consumer Control
- **7-Byte Absolute Pointer (Report ID 3)**: Global `0..10000` coordinate space (`abstap`, `absswipe`, `target ... abs=1`) eliminates cumulative relative cursor drift.
- **Dual-Point Auto Calibration**: `calibrate_abs` probes `(0.20, 0.20)` and `(0.80, 0.80)` via `measure_pointer` and saves linear endpoints into `iphone_calibration.json`, achieving **±3px open-loop accuracy** and **~210ms taps**.
- **5-Button Mouse & Consumer Controls (Report ID 2)**: Supports `click 1..5` (AssistiveTouch Shortcut bindings), `0x00B8` soft-keyboard toggle (`kbd_toggle`), `0x0030` power (`power`), `0x0221` Spotlight, and `CapsLock`.
- **Legacy `0.2.x` Relative Fallback**: Auto-detects `abs` capability (override via `IPHONE_FORCE_REL=1`) and gracefully falls back to 3-frame pointer aiming with 12-point online linear regression.

### 2. Self-Healing AirPlay `:7201` Decoder + CoreMediaIO USB Hot-Standby
- **OPI PSRAM `ks_aes_ctr` Artifact Fix**: In `hardware/hid_bridge/src/airplay/keystream.c`, AES-CTR decryption copies chunks through a 512-byte internal SRAM stack buffer before calling `mbedtls_aes_crypt_ctr`, eliminating screen corruption caused by decrypting directly against ESP32-S3 OPI PSRAM pointers.
- **Zero-Buffering In-Process PyAV Decoder**: Uses `libavcodec` SLICE threading to atomically update `/tmp/iphone_live.jpg` in `~0.8ms/frame`, with `hello <vtok> at <ts>` GOP resume and automatic `vdbg` / `vage` stall recovery.
- **CoreMediaIO USB Wired Hot-Standby (`usb_capture.swift`)**: Running `python3 iphone_ctl.py usb_daemon` captures wired frames scaled to `720px` width and updates `/tmp/iphone_usb_capture.json`. While active, the AirPlay `daemon` stays warm in memory and yields writes to `/tmp/iphone_live.jpg`, delivering 0ms failover if the cable is unplugged.

### 3. Persistent Apple Vision OCR + Set-of-Mark Icon Grounding + 0-Token Pixel Detectors
- **Persistent Vision OCR (`screen_ocr --serve`)**: Keeps `VNRecognizeTextRequest` (`zh-Hans`, `zh-Hant`, `en-US`, `.accurate`) resident in memory over stdin/stdout for **~80–120ms** full-screen recognition.
- **Set-of-Mark Unlabeled Icon Grounding (`som_grounding.py`)**: Fast (`<20ms`) Pillow edge-gradient clustering detects pure UI icons (`<`, `×`, `🔍`, `+`, `⋯`, `⚙️`) outside the status bar, home indicator, and OCR text boxes, rendering numbered `[#ID]` badges onto the VLM image for deterministic `tap_ocr` targeting.
- **0-Token Pixel Geometry Detectors**: `detect_ios_switches` (`~8ms`) reads `#34C759` green track + knob geometry to verify Settings toggles without VLM tokens; `detect_red_badges` (`~25–45ms`) detects crimson notification dots and counts via `NEAREST` subsampling.

### 4. ~200ms Zero-Popup HTTP Clipboard Mailbox + Closed-Loop Ziranma IME
- **LAN HTTP Clipboard Mailbox (`clipboard_http.py`)**: Thread-safe `ClipboardMailboxServer` (`GET /clip`, `GET /setup`, `GET /health`, localhost-only `POST /clip`) paired with a 2-action iOS Shortcut (`AgentPaste` bound to `btn:3`). Delivers long text, multiline paragraphs, URLs, and Emoji in **~200ms** with generation-tracked fetch confirmation, cross-process `clip_serve` proxying, and `pbcopy` / Ziranma IME fallbacks.
- **Closed-Loop Ziranma Shuangpin (`ziranma.py`)**: Uses macOS CoreFoundation `CFStringTokenizer` + `CFStringTransform("Han-Latin")` for contextual polyphone disambiguation, encodes Ziranma keystrokes, and OCR-verifies the floating iOS candidate bar (`92px` band) with multi-row grid navigation and character-level fallback.

### 5. 4-Tier Local GUI Agent + Stuck Circuit Breaker + 0-VLM Intent Replay
- **4-Tier Cascade (`iphone_agent.py`)**:
  - **Tier 1**: Persistent Apple Vision OCR (`~80–120ms`)
  - **Tier 2**: Direct OCR / Set-of-Mark BLE HID taps (0 VLM tokens)
  - **Tier 2.5**: Deterministic `UISwitch` & red-badge state injection (0 VLM tokens)
  - **Tier 3 / 4**: Grounded Ollama (`qwen3.5:4b`) `run_goal` loop (`tap_ocr`, `tap_and_type`, `type_smart`, `scroll_screen`, `open_app`) with cloud escalation (`escalate`).
- **3-Step Circuit Breaker & `confirm` Gate**: Automatically presses `esc` to clear hidden overlays and escalates after 3 consecutive unchanged frames; pauses with `confirm` before payments, sends, deletions, or permission prompts.
- **0-VLM Intent Replay (`save_intent` / `run_intent`)**: Distills verified `run_goal` runs into `iphone_intents.json` and replays them at `~0.8–1.5s/step` using OCR anchors, seamlessly falling back to live `run_goal` if an unexpected popup appears.

### 6. Multi-Variant ESP32-S3 Board Support
- **Default**: 8MB Flash + 8MB OPI PSRAM (e.g., Seeed Studio XIAO ESP32S3).
- **`--qspi`**: `ESP32-S3-WROOM-1 N8R2 / N4R2` (2MB Quad PSRAM, dynamic PSRAM GOP/tail buffer scaling).
- **`--16m`**: `ESP32-S3 N16R8` (16MB Flash + 8MB OPI PSRAM).

---

## Quick Start (macOS + ESP32-S3)

### 1. Build & Flash ESP32-S3 Firmware

Configure your home Wi-Fi credentials in `hardware/hid_bridge/secrets.h` (gitignored):

```bash
cp hardware/hid_bridge/secrets.example.h hardware/hid_bridge/secrets.h
# Edit hardware/hid_bridge/secrets.h with your WIFI_SSID and WIFI_PASS
```

Install the ESP32 core and NimBLE library, then flash your board:

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "NimBLE-Arduino@2.5.1"

# Default (Seeed XIAO ESP32S3, 8MB Flash + 8MB OPI PSRAM):
hardware/flash.sh --sta

# ESP32-S3-WROOM-1 N8R2 (2MB QSPI PSRAM):
hardware/flash.sh --sta --qspi

# ESP32-S3 N16R8 (16MB Flash + 8MB OPI PSRAM):
hardware/flash.sh --sta --16m
```

**On your iPhone (iOS 18+):**
1. Pair **`ESP32 Agent XXXX`** in **Settings → Bluetooth**.
2. Enable **Settings → Accessibility → Touch → AssistiveTouch**.
3. If using Ziranma Shuangpin (`type_zh`), set the hardware Chinese Pinyin keyboard layout to **自然码双拼**.
4. Open **Control Center → Screen Mirroring → `ESP32 Agent`**.

### 2. Compile Native macOS Swift Helpers

```bash
swiftc -O screen_ocr.swift -o screen_ocr
swiftc -O usb_capture.swift -o usb_capture   # optional CoreMediaIO USB screen capture daemon
swiftc -O vt_decoder.swift -o vt_decoder     # optional standalone VideoToolbox H.264 decoder
```

### 3. Install Python Dependencies & Configure `.env`

```bash
pip install pyserial av Pillow
cp .env.example .env
```

For Tier 3 local VLM control, pull the Ollama model:

```bash
ollama pull qwen3.5:4b
```

`.env` variables (auto-loaded by `iphone_ctl.py` and `iphone_agent.py` with zero external dotenv dependency):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `ESP32_SERIAL_PORT` | Auto-discovered `/dev/cu.usbmodem*` | Pin a specific ESP32-S3 USB CDC serial port |
| `OLLAMA_API_URL` | `http://127.0.0.1:11434/api/chat` | Ollama chat API endpoint |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Vision-language model for Tier 3 `run_goal` |
| `IPHONE_CLIP_PORT` | `8766` | Local HTTP clipboard mailbox port |
| `IPHONE_PASTE_TRIGGER` | `btn:3` | Trigger for `AgentPaste` (`btn:2..5`, `key:ctrl+alt+v`, or `pbcopy`) |

### 4. Start Video & Clipboard Daemons

```bash
# Check serial, BLE HID, and firmware status
python3 iphone_ctl.py status

# Start self-healing AirPlay TCP :7201 decoder daemon (/tmp/iphone_live.jpg)
python3 iphone_ctl.py daemon

# Start standalone HTTP clipboard mailbox (for cross-process paste / agent typing)
python3 iphone_ctl.py clip_serve

# Optional: CoreMediaIO wired USB capture (auto-yields AirPlay writes while active)
python3 iphone_ctl.py usb_daemon
```

**iPhone `AgentPaste` 3-Step Setup:**
Open `http://<Mac_IP>:8766/setup` in Safari on your iPhone:
1. Create a Shortcut named **`AgentPaste`** with 2 actions: **Get Contents of URL** (`http://<Mac_IP>:8766/clip`) → **Copy to Clipboard** (enable **Local Only**).
2. In **Settings → Accessibility → Touch → AssistiveTouch → Devices**, select the ESP32 mouse and map **Button 3** to **`AgentPaste`**.
3. Run the shortcut once to grant local network permission.

### 5. Common Commands & Agent Examples

```bash
# Optional first-time dual-point absolute calibration (saved to iphone_calibration.json)
python3 iphone_ctl.py calibrate_abs

# Low-level controller commands (0 VLM tokens)
python3 iphone_ctl.py screenshot /tmp/snap.jpg
python3 iphone_ctl.py abstap 0.50 0.52
python3 iphone_ctl.py tap_aim 360 800
python3 iphone_ctl.py spotlight 备忘录
python3 iphone_ctl.py paste "Long text + Emoji 🚀🎉 pasted in ~200ms over LAN!"
python3 iphone_ctl.py type_zh "自然码双拼闭环候选词校验测试通过"
python3 iphone_ctl.py kbd_toggle
python3 iphone_ctl.py home

# 4-Tier GUI Agent
python3 iphone_agent.py ocr
python3 iphone_agent.py tap_text "无线局域网"
python3 iphone_agent.py goal "打开设置，进入蓝牙页面并汇报当前蓝牙开关状态"

# Distill & replay 0-VLM fast intents
python3 iphone_agent.py intent_save check_bluetooth
python3 iphone_agent.py intent_run check_bluetooth
```

---

## Repository Layout & Tests

```text
iphone_ctl.py       USB-Serial bridge, self-healing TCP :7201 PyAV stream daemon, 0..10000 BLE pointer & Ziranma IME
iphone_agent.py     4-Tier cascading GUI agent (Persistent Vision OCR, Set-of-Mark icons, UISwitch/badge detectors, Ollama VLM)
clipboard_http.py   Local HTTP clipboard mailbox server (/clip, /setup, /health) for ~200ms zero-popup AgentPaste
som_grounding.py    Pure-Python + Pillow Set-of-Mark unlabeled icon detector and badge renderer
ziranma.py          Zero-dependency macOS CoreFoundation Chinese word segmenter & Ziranma Shuangpin encoder
screen_ocr.swift    Native Apple Vision OCR CLI & persistent `--serve` JSON pipe server
usb_capture.swift   Native CoreMediaIO + AVFoundation USB iPhone screen capture daemon
vt_decoder.swift    Optional macOS VideoToolbox hardware H.264 -> JPEG decoder
tests/              Offline unit test suites (Stage 1 abs mouse, Stage 2 HTTP clipboard, Stage 3 SoM & USB capture)
hardware/           ESP32-S3 Arduino firmware (AirPlay receiver, composite BLE/USB HID, GOP video relay, host C tests)
assets/             Bilingual system architecture diagrams (`architecture-zh.png`, `architecture-en.png`)
.env.example        Template for local serial port, Ollama endpoint & HTTP clipboard configuration
```

Run offline tests (no physical hardware required):

```bash
# Python host-side unit tests
python3 -m unittest discover -s tests -v

# ESP32 firmware pure-C core tests (requires mbedtls 3.x)
make -C hardware/tests test
```

---

## License & Acknowledgments

- **Main License**: Host-side controller, GUI agent, and extensions in this repository are licensed under the **MIT License** (`Copyright (c) 2026 jlcbk`). See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
- **Upstream Attribution**: Based on [ESP32 iPhone Agent](https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan.
  The ESP32 AirPlay receiver and BLE/USB HID bridge firmware under `hardware/` is adapted from and built on Henry Yuan's **[ESP32 iPhone Agent](https://github.com/onehumanbeing/esp32-iphone-agent)** (`Copyright (c) 2026 Henry Yuan`, MIT License). Keep the copyright notices and `LICENSE`/`NOTICE` in any copy or derivative work, and visibly credit **ESP32 iPhone Agent by Henry Yuan** in READMEs as well as public demos, videos, or posts.
- **GPL-3.0 Component (`hardware/hid_bridge/src/airplay/`)**: Ported from [UxPlay](https://github.com/FDH2/UxPlay) and licensed under **GPL-3.0** (see [`hardware/hid_bridge/src/airplay/LICENSE-GPL.md`](hardware/hid_bridge/src/airplay/LICENSE-GPL.md)); compiled firmware binaries linking this component are distributed under GPL-3.0.
