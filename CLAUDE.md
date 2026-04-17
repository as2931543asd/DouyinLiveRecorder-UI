# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

抖音专用直播录制工具，基于 FFmpeg 实现循环值守录制。Fork 自 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)，已精简为仅支持抖音平台。

## Commands

```bash
# 安装依赖
pip3 install -r requirements.txt
# 或使用 uv（推荐）
uv sync

# 运行
python main.py
# 或
uv run main.py
```

需要 Python >= 3.10，需要系统安装 FFmpeg（Windows 下程序会自动处理）。

## Architecture

### 录制流程

```
main.py (主循环, 每3秒检查一次)
  → 读取 config/config.ini + config/URL_config.ini
  → 为每个直播间 URL 启动 start_record() 线程
      → spider.py: 调用抖音 API 获取直播流元数据
      → stream.py: 提取可播放的流地址（m3u8/flv），处理画质选择
      → ffmpeg 子进程录制 → 可选转码为 MP4
```

### 核心模块

- **main.py** — 主程序入口。管理全局状态（recording set、running_list）、线程调度、配置热加载、错误率动态调节并发数。
- **src/spider.py** — 抖音直播数据抓取。`get_douyin_web_stream_data()` 走 Web 端，`get_douyin_app_stream_data()` 走 App 端作为 fallback。
- **src/stream.py** — 从 spider 返回的 JSON 中提取流地址。画质映射：原画(OD) > 超清(UHD) > 高清(HD) > 标清(SD) > 流畅(LD)。检测 h265 编码时强制使用 TS 格式（FLV 不支持 h265）。
- **src/room.py** — URL 解析，提取 room_id、sec_user_id。通过 execjs 调用 x-bogus.js 生成签名。
- **src/ab_sign.py** — SM3 哈希 + RC4 加密，生成 a_bogus 签名参数。
- **src/http_clients/async_http.py** — httpx 封装，所有 API 调用均为 async，由 main.py 里常驻的后台事件循环 + `run_coro()`（`run_coroutine_threadsafe`）桥接到各 worker 线程。

### WebUI

- **webui/server.py** — FastAPI 后端，提供 REST API（状态查询、主播增删改）。在 main.py 中作为 daemon 线程启动（uvicorn），默认只绑 `127.0.0.1:8000`（本机访问，不开放到局域网）。
- **webui/static/index.html** — Vue 3 CDN 单页面应用，每 3 秒轮询 `/api/status` 刷新。
- API 通过 `import sys.modules[__name__]` 读取 main.py 全局状态。读共享状态（recording / recording_time_list / running_list）必须走 `state_lock` 的 snapshot；写 URL 配置走 `file_update_lock`。

### 线程模型

- 主线程：配置读取 + URL 调度
- Display 线程（daemon）：每5秒刷新监控状态
- Error adjuster 线程（daemon）：监控错误率，动态调整 max_request
- WebUI 线程（daemon）：FastAPI/uvicorn HTTP 服务器
- N 个 worker 线程：每个 URL 一个，阻塞在 ffmpeg 子进程

### 配置

- **config/config.ini** — 录制参数（格式、画质、代理、并发数、循环间隔等），`[Cookie]` 节存放抖音 cookie（录制必填）。
- **config/URL_config.ini** — 直播间地址列表，格式：`[画质],URL[,自定义名称]`，行首加 `#` 跳过该直播间。

## Git Remotes

- `origin` — `git@github.com:as2931543asd/DouyinLiveRecorder-UI.git`（本项目）
- `upstream` — `https://github.com/ihmily/DouyinLiveRecorder.git`（原仓库）

同步上游抖音相关修复时，用 cherry-pick 或手动同步，不要直接 merge（会引入已删除的多平台代码）。
