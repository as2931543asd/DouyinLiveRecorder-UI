# DouyinLiveRecorder-UI

## 简介

一款**简易**的可循环值守的抖音直播录制工具，基于 FFmpeg 实现直播源录制，支持自定义配置录制。

基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 精简修改，仅保留抖音直播录制功能。

## 项目结构

```
.
└── DouyinLiveRecorder/
    ├── /config -> (配置文件)
    ├── /logs -> (运行日志)
    ├── /backup_config -> (配置备份)
    ├── /src
    │   ├── spider.py -> (获取直播数据)
    │   ├── stream.py -> (获取直播流地址)
    │   ├── utils.py -> (工具函数)
    │   ├── logger.py -> (日志处理)
    │   ├── room.py -> (获取房间信息)
    │   ├── ab_sign.py -> (生成抖音签名)
    ├── /webui
    │   ├── server.py -> (WebUI 后端 API)
    │   ├── /static
    │   │   └── index.html -> (WebUI 前端页面)
    ├── main.py -> (主程序)
    ├── ffmpeg_install.py -> (ffmpeg 安装脚本)
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
