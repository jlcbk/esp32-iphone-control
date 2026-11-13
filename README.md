# ESP32 iPhone Control

简体中文 · [English](README.en.md)

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT%20%2F%20GPL--3.0-111111?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/version-v0.3.0-111111?style=flat-square" alt="v0.3.0">
  <img src="https://img.shields.io/badge/iOS-18%2B-111111?style=flat-square&logo=apple" alt="iOS 18+">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon-111111?style=flat-square&logo=apple" alt="macOS Apple Silicon">
  <img src="https://img.shields.io/badge/board-ESP32--S3-111111?style=flat-square" alt="ESP32-S3">
</p>

**ESP32 iPhone Control** 是一套零越狱、免安装 iOS App 的真机自动化控制系统：利用一块低成本 ESP32-S3 开发板作为无线投屏与蓝牙触控桥梁，配合主控 Mac、局域网本地千问视觉模型与上层兜底 Agent 的协同架构，让 AI 像真人一样看得懂屏幕、点得准界面、顺畅输入中英文，并能以极低成本稳定完成日常手机任务。

---

## 运行环境与协作方式

传统的 AI 手机操控往往陷入两难：如果每点一步都把截图发给云端大模型，单步延迟动辄 3–8 秒且持续烧掉视觉 Token；如果完全交给轻量级本地模型，虽然免费，却很容易在陌生弹窗或复杂排版前原地卡死。

本项目采用 **“硬件无感桥接 + 三级智能接力”** 的分层架构：确定性的屏幕感知与触控执行在主控 Mac 上毫秒级本地完成，日常多步界面导航由局域网内的千问视觉推理 Mac 免费承担，而顶层大模型 Agent（Claude / Gemini / Antigravity 等）**只在遇到异常卡死或敏感确认时才看图介入**——兼顾响应速度、稳定性与极低使用成本。

### 各层硬件与角色分工

- **📱 被控真机 · iPhone（iOS 18+）**
  一台接入日常家庭 Wi-Fi 的普通手机。无需越狱、无需开发者签名或安装任何测试 App，仅开启系统自带的「屏幕镜像」与「辅助触控」即可无感接入，充电口完全空闲，手机可继续正常联网使用。

- **🔌 硬件桥接层 · ESP32-S3 硬件桥**
  横跨在手机与电脑之间的“无线采集卡 + 蓝牙外设”。一方面接入家庭路由器接收手机投屏画面并实时转发给主控端；另一方面通过蓝牙模拟鼠标、键盘与多媒体键去操控真机（点击、滑动、打字、呼出搜索、返回主屏幕与锁屏唤醒），全程不占用 Mac 自身的蓝牙。

- **🖥️ 感知与执行中枢 · 主控 Mac**
  通过 USB 连接 ESP32-S3 的控制大本营。实时接收无线投屏（或可选 USB 有线热备）画面，在本地直接完成中英文文字识别、无字图标自动编号、开关状态与未读红点判断，以及极速中文输入（局域网秒贴与自然码双拼选词）。像“点某个词”、“搜某个 App”或“回放保存过的常用流程”，主控 Mac 在 **0.1–0.2 秒** 内即可独立办妥，无需惊动任何大模型。

- **🧠 日常视觉大脑 · 千问视觉推理 Mac（局域网 Ollama 节点）**
  局域网内专门运行千问（Qwen）多模态视觉模型的另一台 Mac（通过 `OLLAMA_API_URL` 接入；如果手头只有一台高配 Apple Silicon Mac，也可与主控同机部署）。负责看懂日常陌生页面、规划跨步跳转与滑动操作——**高频执行零 API 费用，且独立算力完全不会拖慢主控端的实时看屏与操控手感**。

- **🛡️ 总控与兜底大脑 · 顶层兜底读图 Agent（Claude / Gemini / Antigravity 等）**
  直接与人对话、拆解复杂任务的“总指挥”兼“安全把关人”。平时它只向主控 Mac 下发目标指令，**日常跑流程零读图 Token 消耗**；只有当本地千问模型遇到罕见复杂界面、连续操作未生效触发卡死熔断，或走到**付款、发消息、删除数据**等敏感节点时，顶层 Agent 才会按需调取现场截图，亲自读图排障接管或向用户发起确认。

### 一次任务的真实协作流转

1. **下达意图**：你用自然语言向顶层 Agent 交代目标（例如“打开设置看看蓝牙状态，再去备忘录记一条提醒”）。
2. **本地直达（0 Token）**：已知应用启动、明确文字点击、长文本粘贴或已固化的高频流程，由**主控 Mac** 本地百毫秒直达执行。
3. **局域网导航（0 账单）**：需要看界面动态决定下一步点哪里时，**主控 Mac** 将带文字与图标编号的画面发给**千问视觉推理 Mac** 本地闭环推进。
4. **按需兜底（稳妥可控）**：全程顺利则直接汇报结果；仅在连续受阻熔断或涉及付款、发送、删除确认时，才将现场截图上报给**顶层兜底读图 Agent** 介入决策或请求确认。

---

## 系统架构

<p align="center">
  <img src="assets/architecture-zh.png" alt="ESP32 iPhone Control 多机协同系统架构" width="100%">
</p>

---

## 能实现的效果与核心功能

- **零越狱、零安装，手机正常联网使用**
  直接基于 iOS 自带「屏幕镜像」与「辅助触控」工作，不需要 Xcode 签名、TestFlight 或开发者证书；ESP32-S3 接入家庭路由器后，iPhone 全程保持正常 Wi-Fi 上网，也不占用手机物理充电口。
- **指哪打哪的快速点击与完整真机手势**
  支持全屏绝对坐标精准定位（首次自动双点校准后即指哪打哪，无指针累积漂移），单次点击约 **0.2 秒**；支持连续快速点击、平滑上下左右滚动翻页、返回主屏幕、Spotlight 快捷搜 App，以及一键切换屏幕软键盘。
- **清晰稳定的实时画面（无线投屏 + 有线热备无缝切换）**
  无线投屏画面清晰稳定、无花屏伪影，遇到网络波动或黑屏停顿会自动恢复；需要更高画质或更低延迟时，随手插上 USB 数据线即可自动切换为有线画面采集，拔线后自动无感交还无线投屏。
- **既认得中英文文字，也点得准“无字图标”与开关状态**
  毫秒级识别屏幕上的中英文文字（支持直接“按文字点击”）；面对没有文字的图标（如返回 `<`、关闭 `×`、搜索 🔍、加号 `+`、设置齿轮 ⚙️ 等），自动叠加上带编号的视觉标签供 AI 精准点击；同时能直接读出系统开关的 ON/OFF 状态与未读红点角标，不浪费大模型 Token。
- **丝滑的中文、长文案与 Emoji 输入**
  提供 **~0.2 秒**无系统弹窗的局域网极速粘贴，长段落、链接、换行和 Emoji 都能一步到位；针对需要原生打字的场景，支持自然码双拼输入并在键盘候选栏自动核对、翻页选出正确词条（有效避免同音误选）。
- **分层省流协作、卡死自救与固定流程极速回放**
  常规操作由主控 Mac 本地秒办，多步跳转交由局域网千问模型免费推理，仅在卡死或敏感操作（如付款、发送、删除前弹出 `confirm` 确认）时才请上层读图 Agent 兜底；跑通的高频流程支持一键固化保存，日后以 **~1 秒/步**、零大模型消耗极速回放，遇到意外弹窗也能自动切回视觉推理接管。

---

## 快速开始（macOS + ESP32-S3）

### 1. 烧录 ESP32-S3 固件

复制并编辑 Wi-Fi 凭据文件（`secrets.h` 已被 `.gitignore` 忽略）：

```bash
cp hardware/hid_bridge/secrets.example.h hardware/hid_bridge/secrets.h
# 编辑 hardware/hid_bridge/secrets.h，填入你的家庭 Wi-Fi SSID 与密码
```

安装编译环境与依赖库，并按开发板型号烧录（支持默认 OPI PSRAM、N8R2 等 QSPI 型号及 N16R8）：

```bash
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "NimBLE-Arduino@2.5.1"

# 默认（如 Seeed XIAO ESP32S3，8MB Flash + 8MB OPI PSRAM）：
hardware/flash.sh --sta

# ESP32-S3-WROOM-1 N8R2 / N4R2（2MB QSPI PSRAM）：
hardware/flash.sh --sta --qspi

# ESP32-S3 N16R8（16MB Flash + 8MB OPI PSRAM）：
hardware/flash.sh --sta --16m
```

**iPhone 端配对与设置（iOS 18+）：**
1. 打开 **设置 → 蓝牙**，配对名为 **`ESP32 Agent XXXX`** 的设备（`XXXX` 为板载后缀）。
2. 打开 **设置 → 辅助功能 → 触控 → 辅助触控**，开启 **辅助触控**。
3. 如需使用自然码双拼打字（`type_zh`），请确认外接硬件键盘的中文拼音布局设置为 **自然码双拼**。
4. 下拉 **控制中心 → 屏幕镜像**，选择 **`ESP32 Agent`** 开始投屏。

### 2. 编译主控 Mac 原生辅助工具

```bash
swiftc -O screen_ocr.swift -o screen_ocr
swiftc -O usb_capture.swift -o usb_capture   # 可选：USB 有线画面热备采集
swiftc -O vt_decoder.swift -o vt_decoder     # 可选：独立硬件解码辅助工具
```

### 3. 安装 Python 依赖与配置 `.env`

```bash
pip install pyserial av Pillow
cp .env.example .env
```

在用于跑千问推理的 Mac（专用的局域网推理 Mac，或本机同机运行）上安装并拉取 Ollama 视觉模型：

```bash
ollama pull qwen3.5:4b
```

编辑主控 Mac 上的 `.env`（由 `iphone_ctl.py` 与 `iphone_agent.py` 自动加载）：

| 变量名 | 默认值 / 示例 | 说明 |
| :--- | :--- | :--- |
| `ESP32_SERIAL_PORT` | 自动探测 `/dev/cu.usbmodem*` | 如有多块串口设备，可显式固定 ESP32-S3 串口路径 |
| `OLLAMA_API_URL` | `http://127.0.0.1:11434/api/chat` | 千问推理地址：指向**专用千问推理 Mac** 时填局域网地址（如 `http://192.168.x.x:11434/api/chat`）；单台 Mac 同机运行保留 `127.0.0.1` 即可 |
| `OLLAMA_MODEL` | `qwen3.5:4b` | 本地千问多模态视觉模型名称 |
| `IPHONE_CLIP_PORT` | `8766` | 局域网极速粘贴服务端口 |
| `IPHONE_PASTE_TRIGGER` | `btn:3` | 触发 iPhone 粘贴快捷指令的鼠标按键或快捷键（`btn:2..5`、`key:ctrl+alt+v` 或 `pbcopy`） |

> 💡 **提示**：如果千问模型部署在局域网另一台专用 Mac 上，请在该机器启动 Ollama 时设置 `OLLAMA_HOST=0.0.0.0` 允许局域网访问，再在主控 Mac 的 `.env` 中将 `OLLAMA_API_URL` 改为 `http://<千问Mac局域网IP>:11434/api/chat`。

### 4. 启动看屏与极速粘贴服务

```bash
# 检查串口、蓝牙链路与开发板状态
python3 iphone_ctl.py status

# 启动无线投屏接收服务（持续刷新实时画面）
python3 iphone_ctl.py daemon

# 启动局域网极速粘贴服务（供长文案与 Emoji 秒贴使用）
python3 iphone_ctl.py clip_serve

# 可选：启动 USB 有线热备采集（插线时自动接管画面，拔线无缝交还无线）
python3 iphone_ctl.py usb_daemon
```

**iPhone 端极速粘贴（`AgentPaste`）3 步配置：**
启动 `clip_serve` 后，用 iPhone Safari 打开终端显示的引导页 `http://<主控Mac_IP>:8766/setup`：
1. 在 iPhone「快捷指令」App 中新建名为 **`AgentPaste`** 的快捷指令，仅需 2 步：**获取 URL 内容**（填入页面显示的 `http://<主控Mac_IP>:8766/clip`）→ **拷贝至剪贴板**（展开选项并开启 **仅限本地**）。
2. 打开 **设置 → 辅助功能 → 触控 → 辅助触控 → 设备**，选中已连接的 ESP32 鼠标，将 **按钮 3** 绑定为 **`AgentPaste`**（对应 `IPHONE_PASTE_TRIGGER=btn:3`）。
3. 手动运行一次该快捷指令，允许访问本地网络即可。

### 5. 常用命令与智能体任务示例

```bash
# 首次使用建议执行一次自动校准，实现全屏指哪打哪
python3 iphone_ctl.py calibrate_abs

# 主控 Mac 本地快速直达操作（零大模型消耗）
python3 iphone_ctl.py screenshot /tmp/snap.jpg
python3 iphone_ctl.py abstap 0.50 0.52
python3 iphone_ctl.py tap_aim 360 800
python3 iphone_ctl.py spotlight 备忘录
python3 iphone_ctl.py paste "长文本 + Emoji 🚀🎉 局域网极速粘贴无弹窗直达！"
python3 iphone_ctl.py type_zh "自然码双拼闭环候选词校验测试通过"
python3 iphone_ctl.py kbd_toggle
python3 iphone_ctl.py home

# 识别与智能体任务（本地识别 -> 局域网千问推理 -> 上层 Agent 兜底）
python3 iphone_agent.py ocr
python3 iphone_agent.py tap_text "无线局域网"
python3 iphone_agent.py goal "打开设置，进入蓝牙页面并汇报当前蓝牙开关状态"

# 固定流程固化保存与零大模型极速回放
python3 iphone_agent.py intent_save check_bluetooth
python3 iphone_agent.py intent_run check_bluetooth
```

---

## 仓库目录结构与测试

```text
iphone_ctl.py       主控端核心：串口通信、无线/有线实时画面守护、全屏点击手势与极速输入
iphone_agent.py     分层视觉智能体：本地文字/图标/开关识别、千问多步规划、熔断兜底与流程回放
clipboard_http.py   局域网 HTTP 剪贴板粘贴服务（支持无弹窗长文本与 Emoji 投递）
som_grounding.py    无文字图标自动检测与编号标注器
ziranma.py          中文分词与自然码双拼编码器
screen_ocr.swift    macOS 原生中英文快速文字识别服务
usb_capture.swift   macOS 原生 USB 有线画面采集守护进程
vt_decoder.swift    可选 macOS 独立硬件视频解码辅助工具
tests/              离线单元测试套件（无需连接真实硬件即可运行）
hardware/           ESP32-S3 桥接固件源码与硬件端纯 C 测试
assets/             中英文系统架构图（architecture-zh.png、architecture-en.png）
.env.example        串口、千问推理地址与极速粘贴配置模板
```

运行离线测试套件（无需连接真实硬件）：

```bash
# Python 主机端单元测试
python3 -m unittest discover -s tests -v

# ESP32 固件纯 C 核心模块测试（需安装 mbedtls 3.x）
make -C hardware/tests test
```

---

## 开源协议与致谢（License & Acknowledgments）

- **主体许可**：本仓库主体主机端代码、智能体框架与新增扩展采用 **MIT License** 开源（`Copyright (c) 2026 jlcbk`），完整协议见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。
- **上游合规署名**：`Based on [ESP32 iPhone Agent](https://github.com/onehumanbeing/esp32-iphone-agent) by Henry Yuan.`
  `hardware/` 下的底层 ESP32 AirPlay 接收端与 BLE/USB HID 桥接固件基于 Henry Yuan 的 **[ESP32 iPhone Agent](https://github.com/onehumanbeing/esp32-iphone-agent)** 改造与扩展（`Copyright (c) 2026 Henry Yuan`，MIT License）。任何复制、二次分发或基于本项目的公开演示、视频与文章，请按 MIT 协议要求保留版权声明并显著署名上游项目。
- **GPL-3.0 组件声明**：`hardware/hid_bridge/src/airplay/` 目录下的 AirPlay 镜像解码模块源自 [UxPlay](https://github.com/FDH2/UxPlay)，遵循 **GPL-3.0** 协议（详见 [`hardware/hid_bridge/src/airplay/LICENSE-GPL.md`](hardware/hid_bridge/src/airplay/LICENSE-GPL.md)），链接该模块编译生成的固件二进制文件按 GPL-3.0 分发。
