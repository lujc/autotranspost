<p align="center">
  <img src="https://img.shields.io/badge/status-active-brightgreen" alt="Status: Active"/>
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"/>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"/>
  <img src="https://img.shields.io/badge/version-1.0.0-orange" alt="Version 1.0.0"/>
</p>

# AutoTransPost

> **自动翻译与 B 站发布流水线** — 从 YouTube / B 站 / 其它 yt-dlp 支持的平台下载视频，自动翻译外语字幕为中文，硬烧录中文字幕并自动发布到 Bilibili。
>
> **Auto-translate & publish pipeline** — Download videos from YouTube, Bilibili, and any yt-dlp-supported platform, translate foreign subtitles to Chinese via an LLM, hard-burn the Chinese captions (yellow fill + black outline), and auto-publish to Bilibili.

---

## 目录 | Table of Contents

- [简介 | Introduction](#简介--introduction)
- [核心工作流 | Core Workflow](#核心工作流--core-workflow)
- [功能特性 | Features](#功能特性--features)
- [前置依赖 | Prerequisites](#前置依赖--prerequisites)
- [安装 | Installation](#安装--installation)
- [快速开始 | Quick Start](#快速开始--quick-start)
- [交付模式 | Delivery Modes](#交付模式--delivery-modes)
- [项目结构 | Project Structure](#项目结构--project-structure)
- [许可证 | License](#许可证--license)

---

## 简介 | Introduction

**AutoTransPost** 是一个 AI 驱动的视频翻译与发布工具链，专为跨语言内容创作者设计。它整合了视频下载、字幕提取、AI 翻译、字幕渲染硬烧录和 B 站自动发布的全流程。

**AutoTransPost** is an AI-powered video translation & publishing toolchain designed for cross-language content creators. It integrates video downloading, subtitle extraction, AI translation, subtitle rendering & hard-burning, and auto-publishing to Bilibili into a single pipeline.

### 适用场景 | Use Cases

- **海外内容搬运**：下载 YouTube 视频，翻译英文字幕为中文，烧录后发布到 B 站
- **多语言字幕制作**：提取视频原始字幕，LLM 逐批翻译，生成双语 SRT/ASS
- **自动化发布**：翻译 + 烧录 + B 站投稿一键完成

---

## 核心工作流 | Core Workflow

```
下载 Download ──→ 翻译 Translation（LLM 逐批）──→ 渲染 Render ──→ 烧录 Burn ──→ 发布 B 站 Publish
   │                   │                           │
   ├─ 最佳 1080p       ├─ next-batch 逐批           ├─ 仅中文 zh-CN.ass
   ├─ 封面 Cover       ├─ 保持上下文连贯性            ├─ MiSans Bold 字体
   └─ 原文字幕 Subs    └─ 自然中文风格                └─ 黄色+黑描边
```

---

## 功能特性 | Features

### 🎬 视频下载

- 基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp)，支持 YouTube、Bilibili 及数十个平台
- 默认 **1080p H.264（60fps 优先）**，音频 AAC
- 无损封装 master.mp4（流复制，不重编码）
- 自动下载封面（JPEG）与原始语言字幕（优先手写字幕）
- 浏览器 Cookie 鉴权支持（匿名失败时自动重试 Chrome 配置）

### 🤖 AI 翻译

- 由当前会话 LLM 驱动，不依赖第三方翻译 API
- 逐批翻译（每批约 80 条字幕），保持上下文连贯性
- 源字幕锁定、逐字节不变，翻译仅输出 `id` + `translation`
- 自然中文风格：内部 `，。` 停顿转空格，cue 结尾省略标点

### 🎨 字幕渲染与烧录

- **MiSans Bold** 字体，黄色填充 + 黑色描边（Outline=3），字号 60（横屏）/ 52（竖屏）
- 生成 `source.srt`、`zh-CN.srt`、`bilingual.srt`、`bilingual.ass`、`zh-CN.ass`
- 烧录仅中文 (`zh-CN.ass`)；硬字幕检测已移除，中文字幕始终置底
- 支持编码器：`h264_nvenc` / `libx264` / `hevc_nvenc` / `av1_nvenc`
- **强制可拖拽定位**：固定关键帧间隔约 2 秒
- 码率封顶 ≤ 源码率，音频强制 AAC

### 🚀 B 站自动发布

- 分块上传视频 → 上传中文封面（1146×717，含「转载翻译」角标）→ 提交元数据
- 二维码登录（首次或 Cookie 过期）
- 转载模式默认 `copyright: 2`，需提供原视频链接
- 每阶段最多 3 次重试

### 🔒 安全与健壮性

- B 站登录凭据（`cache/`）被 `.gitignore` 排除，绝不提交
- 沙箱友好的"安全删除" shim：删除失败不中断流水线
- 绝不导出 Cookie 或打印凭据值
- 源字幕逐字节不可变

---

## 前置依赖 | Prerequisites

### Python 包

```bash
pip install -r requirements.txt
```

| 包 | 用途 |
|----|------|
| `bilibili-api>=4.0,<5.0` | B 站投稿（视频上传、封面上传、元数据提交） |
| `qrcode[pillow]>=7.0` | B 站二维码登录生成 |
| `requests>=2.28` | HTTP API 调用 |

### 系统工具

| 工具 | 用途 | 安装 |
|------|------|------|
| `ffmpeg` / `ffprobe` | 视频处理（编码、烧录字幕） | [ffmpeg.org](https://ffmpeg.org) |
| `yt-dlp` | 视频下载 | `pip install yt-dlp` |
| `node` | YouTube 签名解密（可选） | [nodejs.org](https://nodejs.org) |

### 字体

烧录字幕使用 **MiSans Bold**。脚本运行时会自动从小米官网下载安装，无需手动操作。

---

## 安装 | Installation

```bash
# 1. 克隆仓库
git clone https://github.com/lujc/autotranspost.git
cd autotranspost

# 2. 安装 Python 依赖
pip install -r requirements.txt

# 3. 安装系统工具（ffmpeg + yt-dlp）
# 详见上方「前置依赖」
```

---

## 快速开始 | Quick Start

推荐使用三条命令的流水线模式：

### Step 1：下载视频

```bash
python3 scripts/pipeline.py download -- \
  "<video-url>" --output-dir "<job-dir>" --browser-cookies auto \
  --js-runtime "<node.exe>" --merge-mp4
```

下载完成后，目录结构：
```
<job-dir>/
├── <Title>.master.mp4          # 无损封装的主视频
├── <Title>.jpg                 # 封面
└── subtitles/
    └── subtitle-manifest.json   # 字幕清单（不可直接修改）
```

### Step 2：AI 翻译（逐批进行）

```bash
# 取一批待翻译的字幕 → LLM 翻译 → 写回，重复直到 done:true
python3 scripts/subtitle_pipeline.py next-batch \
  --manifest "<job-dir>/subtitles/subtitle-manifest.json"
```

每次返回一个批次（约 80 条字幕），翻译后写回指定文件。重复直到 `done:true`。

### Step 3：渲染 + 烧录 + 发布（一步完成）

```bash
python3 scripts/pipeline.py finalize \
  --job "<job-dir>" --cn-title "<中文标题>" --publish
```

`finalize` 自动定位 master.mp4 → 渲染 `zh-CN.ass` → 烧录 → 发布 B 站。

> ⚠️ 发布是默认行为。转载视频自动标记 `copyright: 2`，需确保已提供原视频链接。

### B 站登录

首次使用需要登录 B 站：

```bash
# 生成二维码（扫码登录）
python3 scripts/publish_bilibili.py login
# 查看登录状态
python3 scripts/publish_bilibili.py status
```

---

## 交付模式 | Delivery Modes

通过 `--deliver` 参数选择交付模式（SKILL.md 定义）：

| 模式 | 产出 | 说明 |
|------|------|------|
| `full`（默认） | 硬烧录 1080p H.264 MP4 | 完整流水线，字幕仅中文 |
| `video` | 视频 + 封面 + 原始字幕 | 不翻译不烧录 |
| `subs` | 仅原始字幕文件 | — |
| `bilingual-subs` | 双语 SRT/ASS | 翻译 + 渲染，不下视频不烧录 |

---

## 项目结构 | Project Structure

```
autotranspost/
├── SKILL.md                       # WorkBuddy 技能定义（完整文档）
├── README.md                      # 本文件
├── requirements.txt               # Python 依赖
├── .gitignore                     # Git 忽略规则
├── agents/
│   └── openai.yaml                # 技能 Agent 配置文件
├── scripts/
│   ├── pipeline.py                # 流水线编排入口（download + finalize）
│   ├── fetch_video.py             # 视频下载（yt-dlp 封装）
│   ├── subtitle_pipeline.py       # 字幕提取、渲染、翻译批次管理
│   ├── burn_subtitles.py          # 字幕硬烧录（FFmpeg）
│   ├── publish_bilibili.py        # B 站登录与投稿
│   ├── verify_delivery.py         # 交付校验
│   ├── common.py                  # 共享工具（安全删除 shim 等）
│   └── skill_version.py           # 版本信息
├── references/
│   ├── translation-contract.md    # 翻译合约（LLM 行为规范）
│   ├── platform-notes.md          # yt-dlp 平台注意事项
│   └── chrome-auth.md             # Chrome 浏览器鉴权指南
└── tests/
    ├── test_burn_subtitles.py
    ├── test_subtitle_pipeline.py
    └── test_verify_delivery.py
```

---

## 许可证 | License

[MIT](LICENSE) © 2026 lujc

---

<p align="center"><i>Made with ❤️ for cross-language content creators</i></p>
