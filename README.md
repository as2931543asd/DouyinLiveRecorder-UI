# DouyinLiveRecorder-UI

## 简介

一款**简易**的可循环值守的抖音直播录制工具，基于 FFmpeg 实现直播源录制，支持自定义配置录制。

基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 精简修改，仅保留抖音直播录制功能。

## 项目结构

```
.
└── DouyinLiveRecorder/
    ├── /config -> (配置文件: config.ini + URL_config.ini)
    ├── /logs -> (运行日志)
    ├── /backup_config -> (配置定时备份)
    ├── /downloads -> (录制文件输出目录)
    ├── /src
    │   ├── runtime.py -> (跨线程共享状态、锁、常驻 asyncio 事件循环)
    │   ├── config_loader.py -> (config.ini 读取与热重载)
    │   ├── url_config.py -> (URL_config.ini 解析与 worker 调度)
    │   ├── recorder.py -> (单直播间录制 worker，封装 ffmpeg 子进程)
    │   ├── monitor.py -> (控制台状态打印 + max_request 动态调整)
    │   ├── file_ops.py -> (URL_config 行级改写 + 配置备份)
    │   ├── spider.py -> (抖音 Web/App 端直播数据抓取)
    │   ├── stream.py -> (从抓取结果提取流地址、画质映射)
    │   ├── room.py -> (URL 解析、x-bogus 签名)
    │   ├── ab_sign.py -> (a_bogus 签名: SM3 + RC4)
    │   ├── http_clients/ -> (httpx 异步封装)
    │   ├── utils.py / logger.py / proxy.py / initializer.py
    ├── /webui
    │   ├── server.py -> (FastAPI 后端，本机 127.0.0.1:8000)
    │   ├── /static
    │   │   └── index.html -> (Vue 3 单页面前端)
    ├── main.py -> (装配层: banner → ffmpeg → 主循环)
    ├── ffmpeg_install.py -> (Windows 自动安装 ffmpeg)
```

## 使用说明

### 直播间链接示例

```
https://live.douyin.com/745964462470
https://v.douyin.com/iQFeBnt/
https://live.douyin.com/yall1102  (链接+抖音号)
https://v.douyin.com/CeiU5cbX  (主播主页地址)
```

可通过 WebUI 或在 `config/URL_config.ini` 中手动添加直播间地址，一行一个。

如需自定义配置，可修改 `config/config.ini` 文件。

### 源码运行

1. 拉取项目代码

```bash
git clone https://github.com/as2931543asd/DouyinLiveRecorder-UI.git
cd DouyinLiveRecorder-UI
```

2. 安装依赖

```bash
# 使用 pip
pip3 install -U pip && pip3 install -r requirements.txt

# 或者使用 uv (推荐)
uv sync
```

3. 安装 FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu
apt update && apt install ffmpeg

# CentOS
yum install epel-release && yum install ffmpeg
```

Windows 系统可跳过此步，程序会自动处理。

4. 运行程序

```bash
python main.py
# 或
uv run main.py
```

5. 打开 WebUI

程序启动后访问 [http://localhost:8000](http://localhost:8000)，可在网页上查看录制状态、添加/删除/暂停主播。

## 致谢

本项目基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 修改，感谢原作者及所有贡献者。
