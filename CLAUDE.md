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

- **main.py** — 装配层。只负责启动顺序：banner → ffmpeg 检查 → 载入 Settings → 启动后台线程 → 进入主循环调用 `url_config.load_and_dispatch`。
- **src/runtime.py** — 进程内共享的可变状态与锁（`recording` / `running_list` / `recording_time_list` / `error_window` / `state_lock` / `file_update_lock` / `max_request_lock`）+ 常驻 asyncio 事件循环 + `run_coro()`。所有跨线程共享的全局变量都集中在此。
- **src/config_loader.py** — `Settings` 类，封装 `config/config.ini` 的读取。`settings.reload()` 覆盖字段——worker 线程持有同一实例即可实时看到新值。
- **src/file_ops.py** — URL_config.ini 行级改写（`update_file` / `delete_line`）+ 配置文件定时备份。写操作统一走 `runtime.file_update_lock`。
- **src/monitor.py** — 两个 daemon 线程循环：`display_info_loop` 打印控制台状态，`adjust_max_request_loop` 按错误率微调 `runtime.max_request`。
- **src/recorder.py** — 单直播间 worker。`start_record` 内部循环 → spider/stream 获取源 → 构造 ffmpeg 命令 → `_check_subprocess` 阻塞等待 → 可选 TS→MP4 转码。
- **src/url_config.py** — 解析 `URL_config.ini`，对每个新 URL 启动一个 `recorder.start_record` daemon 线程；处理主播名回填与非法链接自动注释。
- **src/spider.py** — 抖音直播数据抓取。`get_douyin_web_stream_data()` 走 Web 端，`get_douyin_app_stream_data()` 走 App 端作为 fallback。
- **src/stream.py** — 从 spider 返回的 JSON 中提取流地址。画质映射：原画(OD) > 超清(UHD) > 高清(HD) > 标清(SD) > 流畅(LD)。检测 h265 编码时强制使用 TS 格式（FLV 不支持 h265）。
- **src/room.py** — URL 解析，提取 room_id、sec_user_id。通过 execjs 调用 x-bogus.js 生成签名。
- **src/ab_sign.py** — SM3 哈希 + RC4 加密，生成 a_bogus 签名参数。
- **src/http_clients/async_http.py** — httpx 封装，所有 API 调用均为 async，由 `runtime._bg_loop` 常驻事件循环 + `runtime.run_coro()`（`run_coroutine_threadsafe`）桥接到各 worker 线程。

### WebUI

- **webui/server.py** — FastAPI 后端，提供 REST API（状态查询、主播增删改）。由 main.py 调用 `init(url_config_file)` 注入 ini 路径，然后作为 daemon 线程启动（uvicorn），默认只绑 `127.0.0.1:8000`（本机访问，不开放到局域网）。
- **webui/static/index.html** — Vue 3 CDN 单页面应用，每 3 秒轮询 `/api/status` 刷新。支持深/浅色切换、按状态过滤、搜索、模态框添加主播。
- API 通过 `from src import runtime` 直接读共享状态。读共享状态（recording / recording_time_list / running_list）必须先在 `runtime.state_lock` 下取 snapshot 再释放；写 URL_config 走 `runtime.file_update_lock`。

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
