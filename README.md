# DouyinLiveRecorder-UI

## 简介

`DouyinLiveRecorder-UI` 是一个支持抖音和小红书直播录制的轻量化工具，提供 Web UI 管理界面，方便统一维护录制任务、查看状态与日志。

项目基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 精简和改造，保留抖音录制能力，并提供小红书当前直播间的基础录制支持和更直观的前端管理页面。

![Web UI 预览](./ui.png)

## 功能特点

- 支持抖音直播间，以及小红书 `livestream` 直链和 `xhslink.com` 分享短链；
- 提供 Web UI，可直接管理主播列表、查看实时状态和运行日志；
- 持久记录每位主播上次直播结束时间，并在列表中显示距今时长；
- 自动跟随抖音最新主播昵称，同步监控列表、录制目录和文件名；
- 基于 FFmpeg 录制直播流，输出稳定，可配合现有存储目录使用；
- 抖音可选同步录制普通聊天弹幕，输出可逐行解析的结构化 JSON Lines；
- 支持根据 FFmpeg 坏流日志自动判定疑似花屏并快速重录，减少长时间写入损坏 TS 的情况；
- 支持通过 `config.ini` 和 `URL_config.ini` 调整录制与轮询策略；
- 适合通过 `systemd` 长期托管运行。

## UI 界面说明

当前 Web UI 主要分为 3 个区域，适合日常值守时快速查看整体状态和处理异常：

### 1. 顶部状态概览

- `监控中`：显示当前正在持续守候的直播间数量；
- `录制中`：显示此刻正在写入本地视频文件的直播间数量；
- `CPU 占用`：整机 CPU 使用率，由后台线程每秒采样并做 EMA 平滑，便于观察录制负载；
- `硬盘剩余`：录制目录所在磁盘的剩余空间（GB），与 CPU 一同每秒刷新；
- `瞬时错误`：统计最近短时间窗口内累计的错误次数，便于快速判断当前是否存在异常波动。

### 2. 主播列表区

- 支持按 `全部`、`录制中`、`监控中`、`已暂停` 分类查看主播，并可叠加 `未开播至少 7 / 30 / 90 / 180 天` 筛选；
- 支持以状态、未开播时长或名称作为主字段进行稳定的复合排序；
- 顶部带主播总数统计，方便快速了解任务规模；
- 右侧提供 `搜索主播 / URL` 输入框，可直接过滤目标主播；
- 支持 `添加主播` 按钮，直接在页面中补充新的监控对象；
- 未在录制的主播会显示“距上次直播”时长，悬停可查看结束时间和上次录制时长；
- 没有既往直播记录的主播会以程序首次启用该功能的时间作为统计起点；
- 主播改名后会在下一次成功轮询时自动更新列表与 `URL_config.ini`，后续录制直接使用新目录；
- 当筛选结果为空时，会显示明显的空状态提示，避免误判为页面异常。

### 3. 运行日志区

- 底部 `运行日志` 面板用于实时查看后台处理情况；
- 日志按时间和级别展示，便于快速排查轮询、开播检测、录制启动等行为；
- 支持 `自动滚动` 开关，适合值守时持续追踪最新输出；
- 支持手动 `刷新`，在排查问题时可主动拉取最新状态。

## 项目结构

```
.
└── DouyinLiveRecorder/
    ├── /config -> (配置文件及自动生成的 live_history.json)
    ├── /logs -> (运行日志)
    ├── /backup_config -> (配置定时备份)
    ├── /downloads -> (录制文件输出目录)
    ├── /src
    │   ├── runtime.py -> (跨线程共享状态、锁、常驻 asyncio 事件循环)
    │   ├── config_loader.py -> (config.ini 读取与热重载)
    │   ├── platforms.py -> (平台 URL 识别与规范化)
    │   ├── url_config.py -> (URL_config.ini 解析与 worker 调度)
    │   ├── recorder.py -> (单直播间录制 worker，封装 ffmpeg 子进程)
    │   ├── danmaku.py -> (本地伴随服务客户端与结构化 JSONL 弹幕写入)
    │   ├── monitor.py -> (根据错误率动态调整 max_request)
    │   ├── file_ops.py -> (URL_config 行级改写 + 配置备份)
    │   ├── spider.py -> (抖音与小红书直播数据抓取)
    │   ├── stream.py -> (从抓取结果提取流地址、画质映射)
    │   ├── room.py -> (抖音分享链接与用户 ID 解析)
    │   ├── ab_sign.py -> (a_bogus 签名: SM3 + RC4)
    │   ├── http_clients/ -> (httpx 异步封装)
    │   ├── utils.py / logger.py
    ├── /scripts
    │   └── danmaku_convert.py -> (JSONL 离线转换为 Bilibili XML)
    ├── /webui
    │   ├── server.py -> (FastAPI 后端，默认监听 0.0.0.0:9527)
    │   ├── /static
    │   │   └── index.html -> (Vue 3 单页面前端)
    ├── main.py -> (装配层: banner → ffmpeg → 主循环)
    ├── ffmpeg_install.py -> (FFmpeg 检查与官方安装提示)
```

## 使用说明

### 直播间链接示例

```
https://live.douyin.com/745964462470
https://v.douyin.com/iQFeBnt/
https://live.douyin.com/yall1102  (链接+抖音号)
https://v.douyin.com/CeiU5cbX  (主播主页地址)

https://www.xiaohongshu.com/livestream/570321613446566013
http://xhslink.com/xxxxxx  (直播间分享短链)
```

可通过 WebUI 或在 `config/URL_config.ini` 中手动添加直播间地址，一行一个。

小红书当前只支持录制**本场直播间**，不支持用作者主页长期发现其下一场直播；
直播间链接失效或主播下次开播生成新链接后，需要重新添加新的直播间链接。小红书
暂不支持弹幕录制。页面可匿名访问时无需 Cookie；若出现登录限制，可在
`config/config.ini` 的 `[Cookie]` 中填写 `小红书cookie`。

如需自定义配置，可修改 `config/config.ini` 文件。

### 画质与安全配置更新

- WebUI 不再提供画质选择或显示画质标签，录制统一优先请求**原画/平台最高可用档**；
  流不可用时仍沿用平台解析器的回退逻辑。旧 `config.ini` 的画质项不再生效；
  旧版 `URL_config.ini` 三列格式仍可读取，调度时会统一规范成 `原画,URL[,名称]`。
- 添加主播默认填入 `https://live.douyin.com/`，光标置于末尾；补上直播间 ID
  即可。粘贴完整的抖音或小红书链接会替换已有前缀，成功添加后恢复默认前缀。
  仅有前缀时不能提交。旧版 QQ 插件仍可传 `quality`，后端兼容接收并忽略该字段。
- `config.ini` 仅在文件变化时读取一次，使用新的解析器完整校验后整体发布；
  数值填错、INI 格式错误、空文件或读取失败时，运行中的程序保留上一份有效配置，
  不因错误热更新退出。修正并保存后会自动恢复，日志不输出 Cookie 等原始配置内容。
  删除配置项会恢复默认值，而不是沿用解析器里残留的旧值。
- **首次启动没有旧配置可回退**：如果配置无效，会明确报错并拒绝启动，避免误用
  默认录制目录。仅首次遇到空文件或缺失文件时生成默认模板，读取已有配置不回写
  注释和缺省项。数字不要带单位，开关填写 `是` / `否`。
- 网络请求信号量容量、弹幕检索根目录等在启动时绑定的设置仍需重启生效；修改
  录制根目录后，应在方便中断录制时重启 `douyinlive-status.service`。
- 主播列表的新增、暂停、删除、去重和改名使用同一把锁及**同目录临时文件 + 原子替换**。
  写入失败时保留原文件，不发布错误的停止标志；并发重复添加只会成功一次。
  API 会拒绝名称或 URL 中的控制字符和配置分隔符，名称最长 200 字符。
  手动修改文件时也建议使用编辑器的原子保存功能；程序内的锁无法约束外部编辑器。

修改 Python 后端后需重启录制服务才能加载新逻辑；只刷新网页只能更新界面。

### 弹幕录制（可选）

弹幕功能依赖 MIT 许可的 [jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive) 伴随服务。建议只把服务端口发布到本机回环地址，并固定使用已验证镜像版本：

```bash
docker run -d --name douyinlive-danmaku --restart unless-stopped \
  -p 127.0.0.1:1088:1088 \
  ghcr.io/jwwsjlm/douyinlive:v2.1.0
```

然后在 `config/config.ini` 的 `[录制设置]` 中开启：

```ini
是否录制弹幕 = 是
弹幕服务地址 = ws://127.0.0.1:1088
```

每轮 FFmpeg 录制会在视频同目录生成同一时间戳基名的
`.danmaku.jsonl` 文件。普通视频会先去掉视频扩展名，例如
`name.flv -> name.danmaku.jsonl`；分段模板会同时去掉 `_%03d`，例如
`name_%03d.ts -> name.danmaku.jsonl`，因此同一轮的所有视频分段只共用一个
JSONL。运行时只生成 JSONL，不再同时生成 XML。

JSONL 是弹幕的来源事实文件，应长期保留。它不需要文件尾闭合：运行时 writer
产生的文件物理首行总是 `metadata`，后续每行是独立的 `chat` 紧凑 JSON，均使用
UTF-8 且保留非 ASCII 文字。每写一行都会及时刷新，因此异常退出时，除最多最后半行外，之前的行仍可
独立解析。伴随服务未启动、断线或消息解析失败时会记录日志并有限退避重连，不会
中断或拖慢视频录制。创建文件使用排他模式；若同名来源文件已经存在，程序只记录
弹幕启动失败并继续视频录制，绝不会截断原文件。

#### JSONL schema v1

稳定 schema 名为 `douyin-live-recorder-ui.danmaku`，当前 `version` 为 `1`。
必需字段如下：

- `metadata`：`type`、`schema`、`version`、`platform`、`room_identifier`、
  `recording_started_at_unix_ms`、`time_units`；
- `chat`：`type`、`sequence`、`method`、`offset_ms`、
  `received_at_unix_ms`、`user.id`、`user.nickname`、`content`。

`recording_started_at_unix_ms` 是 FFmpeg 子进程成功启动时采集的 UTC Unix epoch
毫秒；同一次 `Popen` 成功后会成对采集单调时钟原点，后续 `offset_ms` 是相对这个
原点的非负毫秒，不受 NTP 或人工校时前后跳影响。`received_at_unix_ms` 是收到消息时
系统 Unix wall clock 的 UTC Unix epoch 毫秒。因此 schema 不要求
`received_at_unix_ms - recording_started_at_unix_ms == offset_ms`。所有毫秒值均
向下取整，`sequence` 从 1 严格递增。普通聊天固定为
`method=WebcastChatMessage`。昵称与正文分别保存在 `user.nickname` 和
`content`，不会在来源文件中合并；缺失的用户 ID 或昵称保存为 JSON `null`，
不会在来源文件中伪造为 `0` 或“匿名用户”。缺少 `content` 的聊天会跳过，但明确
存在的空字符串会保留。

示例（实际文件中每个对象各占一行）：

```jsonl
{"type":"metadata","schema":"douyin-live-recorder-ui.danmaku","version":1,"platform":"douyin","room_identifier":"123456","recording_started_at_unix_ms":1787104800123,"time_units":{"recording_started_at_unix_ms":"milliseconds since Unix epoch UTC","received_at_unix_ms":"milliseconds since Unix epoch UTC","offset_ms":"milliseconds from recording start"}}
{"type":"chat","sequence":1,"method":"WebcastChatMessage","offset_ms":2345,"received_at_unix_ms":1787104802468,"user":{"id":"987654","nickname":"示例用户"},"content":"你好"}
```

#### 离线生成 Bilibili XML

需要兼容 Bilibili XML 的播放器或工具时，可从 JSONL 离线派生：

```bash
python scripts/danmaku_convert.py /path/to/name.danmaku.jsonl
```

默认输出同基名 `/path/to/name.xml`。目标已存在时默认拒绝覆盖；确认要替换时：

```bash
python scripts/danmaku_convert.py /path/to/name.danmaku.jsonl --overwrite
```

也可用 `--output /path/to/custom.xml` 指定目标。输入与输出不能是同一个文件；即使
使用 `--overwrite`，转换器也会拒绝直接同路径以及已有符号链接/硬链接别名，避免
来源 JSONL 被 XML 替换。转换器会把 XML 弹幕文本组合为 `nickname: content`；
当来源用户字段为 `null` 时，仅在 XML 显示层使用 `0`、`匿名用户`，并使用转换器
本地的 `mode=1`、`font_size=25`、`color=16777215` 兼容默认值。这些值不会写回
来源 JSONL。

转换默认是严格模式：任何非法或截断的 JSON 行都会报告具体行号、以非零状态退出，
且不会覆盖已有目标。为容许人工编辑和恢复，转换器会跳过空白行，所以它验证的是
第一条非空记录为 `metadata`，并不改变运行时 writer 的物理首行保证。schema v1
上面列出的字段都必须存在；转换器同时容许未知扩展字段，以便未来加入
`message_id` 等字段。这种宽容不代表运行时会伪造尚未采集的数据。若确认文件仅因
异常退出留下了没有换行的不完整最后一行，可显式恢复之前的完整记录：

```bash
python scripts/danmaku_convert.py /path/to/name.danmaku.jsonl --recover-truncated-tail
```

恢复模式会向 stderr 明确报警，并且只忽略“文件最后一行、没有换行、JSON 确属
不完整”这三项同时满足的尾部；中间坏行、带换行的坏尾行和普通非法 JSON 仍会失败。
转换全程先写临时文件，校验成功后再原子发布。XML 和未来的 ASS 都是可从 JSONL
再生成的派生文件，不应替代原始 `.danmaku.jsonl`。

如果你遇到 TS 录制过程中偶发花屏，可关注下面几个配置项：

- `自动检测花屏并重录`：默认开启。程序会读取 FFmpeg 输出中的坏包/解码异常日志，命中阈值后主动中断并重新拉流；
- `花屏检测错误阈值`：时间窗口内累计多少条异常日志后触发重录，默认 `8`；
- `花屏检测时间窗口(秒)`：统计异常日志的滑动窗口，默认 `20` 秒；
- `花屏重录冷却时间(秒)`：触发后等待多久再次尝试录制，默认 `3` 秒；
- `花屏检测是否旁路解码验证`：默认关闭。开启后会额外让 FFmpeg 解码一路视频丢弃，用于捕获纯 `copy` 模式下看不到的比特流/解码层错误（如 `invalid nal unit`、`error while decoding`）。
  - ⚠️ **该选项会显著增加 CPU 占用**：每路录制额外消耗 1 核 CPU 的 5-15%（1080p H.264 软解基准）。并发越高代价越大，例如同时录制 10 路 1080p 直播时，整机可能多吃掉 1-2 个核。请结合顶部 `CPU 占用` 卡片观察，CPU 长期超过 80% 时建议关闭本选项或降低 `最大同时录制数`。

如果网络本身抖动较大、误判偏多，可以适当调高 `花屏检测错误阈值` 或 `花屏检测时间窗口(秒)`。

断流重连使用分级退避，兼顾网络波动时的补录速度与连续失败时的风控压力：

- `断流首次快速重连时间(秒)`：正常结束或异常断流后的首次刷新间隔，默认 `15` 秒；
- `连续断流退避上限(秒)`：短时间连续断流时按 `15 → 30 → 60 → 120` 秒的档位逐步放慢并封顶，带 `0～5` 秒向下随机抖动；
- `连续抓取失败退避上限(秒)`：接口连续抓取失败时可放慢到默认 `300` 秒，避免风控状态下频繁请求；
- `稳定录制后重置退避时间(秒)`：连续录制达到该时长后清空断流次数，默认 `300` 秒；
- 连续花屏重录独立使用 `3 → 10 → 30 → 120` 秒退避；
- 成功确认主播已下播后，恢复使用普通循环时间；连续抓取失败也会逐步放慢到退避上限。

如果监控的主播很多，建议调大下面两个参数，避免程序刚启动时集中请求过多触发风控：

- `首次启动排队读取网址时间(秒)`：仅首次启动时，每个直播间 worker 拉起前的基础等待时间；
- `首次启动随机抖动时间(秒)`：在基础等待时间上再额外随机增加 `0~N` 秒，进一步打散请求节奏。

### 源码运行

1. 拉取项目代码

```bash
git clone https://github.com/as2931543asd/DouyinLiveRecorder-UI.git
cd DouyinLiveRecorder-UI
```

2. 安装依赖

```bash
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

Windows 系统也需要先安装 FFmpeg，或从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html) 选择 Windows builds，
解压后将 `ffmpeg.exe` 放入项目根目录的 `ffmpeg` 文件夹。

4. 运行程序

```bash
uv run main.py
```

5. 打开 WebUI

程序启动后访问 [http://localhost:9527](http://localhost:9527)，可在网页上：

- 查看顶部状态卡片，快速掌握监控数量、录制数量、CPU 占用、硬盘剩余和近期错误数；
- 在主播列表中按状态和未开播时长组合筛选任务，通过搜索框定位主播或直播间 URL，并按状态、未开播时长、名称复合排序；
- 使用 `添加主播` 按钮补充新的监控对象；
- 在列表中管理主播任务，支持添加 / 暂停 / 恢复 / 删除主播；
- 底部"运行日志"面板可实时查看程序输出，并支持自动滚动与手动刷新。

### 弹幕检索与录像定位

在控制面板顶部点击 **弹幕检索**，或访问 `http://服务器地址:9527/danmaku`。

- 支持弹幕、主播、发言者/用户 ID 搜索；空格分隔多个关键词，可选任意或全部匹配。主播、发言者过滤独立于关键词范围。
- 开始/结束时间均包含边界、精确到毫秒，按**浏览器本地时区**输入与显示；可只按时间或主播浏览，支持分页和时间排序。
- 首次访问自动建立 SQLite 索引；后续点击 **刷新弹幕索引** 仅处理新增/变化的文件。正在写入的不完整末行留到下次刷新，不修改原始 JSONL。
- 点击结果的 **定位视频** 可查看完整录像路径和片内 `时:分:秒.毫秒`，直接跳转、提前 5 秒回看、复制路径或下载视频。视频接口支持 HTTP Range，不将整段录像加载进后端内存。
- **新录制**（启用弹幕和分段录制）会在同目录保存 `同基名.segments.csv`，使用 FFmpeg 实际切片边界。请和弹幕、视频一起保留；转为同基名 MP4 后仍可关联。
- **历史录像**没有分段时间表时，用 `ffprobe` 获取实际视频时长并累计估算，绝不使用当前配置的分段秒数推算旧录像。缺失前序分段、探测失败、时间越界或尚未完成的尾段会明确提示，不随意跳到相邻文件。
- 弹幕接收时间与视频媒体时间可能有启动缓冲、网络延迟、转码偏差，不能保证逐帧同步。历史录像在切片边界附近尤其需要人工核对。
- MP4 的浏览器兼容性通常最好；TS / FLV / 部分 MKV、HEVC 编码可能只能下载后用本地播放器查看，不会自动执行高负载转码。

索引使用程序启动时配置的录制目录，缓存位于 `.cache/danmaku/index-v1.sqlite3`，无需安装新依赖。录制目录配置变更后需重启服务；缓存可在停止服务后移除并重建。命令行 `danmaku_search.py` 保持可用。

**访问安全：** 此功能沿用现有控制面板的访问边界，没有新增登录系统。请只在可信局域网使用；对公网提供访问前应配置反向代理认证与 HTTPS。接口只允许读取录制目录内的指定视频格式，拒绝路径穿越、越界软链接及已排除的 CloudDrive 目录。

更新 Python 后端后，需要在方便中断录制时重启 `douyinlive-status.service`，再刷新网页；只刷新浏览器不会加载新接口。

### systemd 托管

当前项目实际运行时，推荐通过系统级服务 `douyinlive-status.service` 托管，而不是直接长期前台运行 `uv run main.py`。

如果你是通过该服务部署本项目，可使用下面这些命令：

```bash
# 查看状态
systemctl status douyinlive-status.service

# 重启服务
systemctl restart douyinlive-status.service

# 查看最近日志
journalctl -u douyinlive-status.service -n 100 --no-pager

# 开机自启
systemctl enable douyinlive-status.service
```

## 致谢

本项目基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 修改，感谢原作者及所有贡献者。
