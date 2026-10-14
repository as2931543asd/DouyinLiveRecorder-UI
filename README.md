# DouyinLiveRecorder-UI

抖音和小红书直播录制工具，带 Web UI 管理录制任务、状态和日志。基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 精简改造。

本机线上实例在 `/srv/apps/douyinlive-recorder/current`，由 `douyinlive-status.service` 管理；配置、日志、弹幕索引在 `/srv/apps/douyinlive-recorder/{config,logs,data}`，视频在 `/srv/nas/Record`。本目录是源码，修改后要建发布副本再切换服务。

![Web UI 预览](./ui.png)

## 功能

- 抖音直播间；小红书 `livestream` 直链和 `xhslink.com` 分享短链。
- FFmpeg 录制，按坏流日志判定疑似花屏并快速重录。
- 记录每位主播上次直播结束时间；自动跟随抖音主播改名，同步监控列表、录制目录和文件名。
- 抖音可选同步录制聊天弹幕（JSON Lines），并提供弹幕检索和录像定位页面。
- 特别监控的主播开播时发出通知事件。

## 运行

```bash
uv sync
uv run main.py          # Web UI：http://localhost:9527（监听 0.0.0.0）
```

需要 FFmpeg（`apt install ffmpeg`；Windows 把 `ffmpeg.exe` 放进项目根目录的 `ffmpeg/`）。长期运行用 `douyinlive-status.service`。**修改 Python 后端要重启服务，只刷新网页不会加载新逻辑**，建议挑不影响录制的时间重启。

## Web UI

- **顶部**：监控中、录制中、CPU 占用（每秒采样、EMA 平滑）、录制盘剩余空间、近期错误数。
- **主播列表**：按全部 / 录制中 / 监控中 / 已暂停 / ★ 特别监控筛选，可叠加“未开播至少 7 / 30 / 90 / 180 天”；按状态、未开播时长或名称排序；搜索主播或 URL；添加、暂停、恢复、删除。未录制的主播显示距上次直播的时长，悬停看结束时间和上次录制时长。
- **运行日志**：实时日志，可自动滚动或手动刷新。

添加主播时输入框预填 `https://live.douyin.com/`，补上房间号即可；粘贴完整的抖音或小红书链接会替换前缀。也可以直接在 `config/URL_config.ini` 里一行写一个地址：

```text
https://live.douyin.com/745964462470
https://v.douyin.com/iQFeBnt/
https://live.douyin.com/yall1102           (链接+抖音号)
https://v.douyin.com/CeiU5cbX              (主播主页)
https://www.xiaohongshu.com/livestream/570321613446566013
http://xhslink.com/xxxxxx                  (小红书分享短链)
```

小红书只能录**当前这一场**，不能从作者主页发现下一场；下次开播要重新添加新链接，也不支持弹幕。遇到登录限制时在 `config.ini` 的 `[Cookie]` 填 `小红书cookie`。

## 配置

录制和轮询策略在 `config/config.ini`，主播列表在 `config/URL_config.ini`，`backup_config/` 是定时备份。

- 录制统一请求原画/平台最高档，流不可用时按平台解析器回退。
- `config.ini` 变化后自动重读并完整校验，有错（数值、INI 格式、空文件）时保留上一份有效配置继续运行，修正后自动恢复；删除的配置项恢复默认值。**首次启动配置无效会直接拒绝启动**，避免误用默认录制目录。数字不要带单位，开关填 `是` / `否`。
- 网络请求并发上限、录制根目录、弹幕检索根目录在启动时绑定，改了要重启。
- 主播列表的增删改用同一把锁和“同目录临时文件 + 原子替换”写入，失败时保留原文件。手动编辑时用编辑器的原子保存；程序的锁管不到外部编辑器。

### 花屏重录

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `自动检测花屏并重录` | 开 | 读 FFmpeg 坏包/解码异常日志，命中阈值就中断重新拉流 |
| `花屏检测错误阈值` | 8 | 时间窗口内多少条异常日志触发重录 |
| `花屏检测时间窗口(秒)` | 20 | 滑动窗口 |
| `花屏重录冷却时间(秒)` | 3 | 触发后多久再录 |
| `花屏检测是否旁路解码验证` | 关 | 额外解码一路视频，捕获 `copy` 模式下看不到的错误 |

旁路解码每路约多占 1 核的 5～15%（1080p H.264），10 路并发可能多吃 1～2 核；CPU 长期超过 80% 时关掉它或降低 `最大同时录制数`。网络抖动导致误判多时，调高阈值或时间窗口。

### 断流与启动退避

- `断流首次快速重连时间(秒)`：断流后首次刷新间隔，默认 15。
- `连续断流退避上限(秒)`：连续断流按 15 → 30 → 60 → 120 秒逐级放慢并封顶，带 0～5 秒随机抖动。
- `连续抓取失败退避上限(秒)`：接口连续失败时最多放慢到 300 秒，避免触发风控。
- `稳定录制后重置退避时间(秒)`：连续录制这么久后清零断流计数，默认 300。
- 连续花屏重录单独按 3 → 10 → 30 → 120 秒退避；确认下播后恢复普通循环时间。
- 主播多时调大 `首次启动排队读取网址时间(秒)` 和 `首次启动随机抖动时间(秒)`，把启动时的请求打散。

## 开播通知

在主播列表点 **☆** 开启特别监控。开播检测生成通用 `live.started` 事件，由通知适配器发送；核心不绑定 QQ、微信或邮箱。目前的适配器是 QQ 抖音录制机器人（`nas-ops/plugins/qq-douyin-recorder-plugin`），只发到指定的已绑定群。

暂停的主播不提醒；重连不重复产生事件；星标和去重状态重启后保留。接口和接入方式见 [通用通知接口 v1](docs/notifications.md)。

## 弹幕录制

依赖 MIT 许可的 [jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive) 伴随服务，端口只发布到本机回环：

```bash
docker run -d --name douyinlive-danmaku --restart unless-stopped \
  -p 127.0.0.1:1088:1088 \
  ghcr.io/jwwsjlm/douyinlive:v2.1.0
```

在 `config.ini` 的 `[录制设置]` 开启：

```ini
是否录制弹幕 = 是
弹幕服务地址 = ws://127.0.0.1:1088
```

- 每轮录制在视频同目录生成同基名的 `.danmaku.jsonl`：`name.flv → name.danmaku.jsonl`，分段模板 `name_%03d.ts → name.danmaku.jsonl`，同一轮所有分段共用一个文件。
- **JSONL 是原始记录，要长期保留**；XML 等格式都从它派生。
- 逐行写入并刷新，异常退出最多丢最后半行。伴随服务断线或解析失败只记日志并退避重连，不影响视频录制。文件以排他模式创建，同名文件已存在时放弃弹幕、继续录视频，不会截断原文件。

### JSONL schema v1

schema 名 `douyin-live-recorder-ui.danmaku`，`version` 为 `1`。首行是 `metadata`，之后每行一个 `chat`，UTF-8、紧凑 JSON。

- `metadata` 必需字段：`type`、`schema`、`version`、`platform`、`room_identifier`、`recording_started_at_unix_ms`、`time_units`。
- `chat` 必需字段：`type`、`sequence`、`method`、`offset_ms`、`received_at_unix_ms`、`user.id`、`user.nickname`、`content`。

时间字段的含义：

- `recording_started_at_unix_ms`：FFmpeg 子进程启动成功时的 UTC 毫秒，同时记下单调时钟原点。
- `offset_ms`：相对单调时钟原点的毫秒，不受校时影响。
- `received_at_unix_ms`：收到消息时的系统时钟 UTC 毫秒。所以 `received_at - recording_started` 不一定等于 `offset_ms`。

其他约定：毫秒值向下取整；`sequence` 从 1 严格递增；`method` 固定为 `WebcastChatMessage`；缺失的用户 ID 或昵称存 `null`，不伪造；没有 `content` 的消息跳过，空字符串保留。

```jsonl
{"type":"metadata","schema":"douyin-live-recorder-ui.danmaku","version":1,"platform":"douyin","room_identifier":"123456","recording_started_at_unix_ms":1787104800123,"time_units":{"recording_started_at_unix_ms":"milliseconds since Unix epoch UTC","received_at_unix_ms":"milliseconds since Unix epoch UTC","offset_ms":"milliseconds from recording start"}}
{"type":"chat","sequence":1,"method":"WebcastChatMessage","offset_ms":2345,"received_at_unix_ms":1787104802468,"user":{"id":"987654","nickname":"示例用户"},"content":"你好"}
```

### 转换为 Bilibili XML

```bash
uv run python scripts/danmaku_convert.py name.danmaku.jsonl                 # 输出 name.xml
uv run python scripts/danmaku_convert.py name.danmaku.jsonl --overwrite     # 覆盖已有目标
uv run python scripts/danmaku_convert.py name.danmaku.jsonl --output x.xml  # 指定输出
uv run python scripts/danmaku_convert.py name.danmaku.jsonl --recover-truncated-tail
```

- 默认严格：任何坏行都报行号并以非零状态退出，不覆盖目标。空白行跳过，未知扩展字段允许。
- `--recover-truncated-tail` 只忽略“最后一行、没有换行、JSON 不完整”的尾部，其他坏行照样失败。
- 输入输出不能是同一文件（包括符号链接、硬链接），先写临时文件再原子发布。
- XML 文本为 `nickname: content`；`null` 只在 XML 里显示成 `0` / `匿名用户`，不回写 JSONL。

## 弹幕检索与录像定位

控制面板顶部点 **弹幕检索**，或访问 `http://服务器地址:9527/danmaku`。

- 按弹幕、主播、发言者/用户 ID 搜索；空格分隔多个关键词，可选任意或全部匹配。时间范围含边界、精确到毫秒，按浏览器本地时区。
- 首次访问自动建 SQLite 索引（`.cache/danmaku/index-v1.sqlite3`），之后 **刷新弹幕索引** 只处理新增或变化的文件；正在写入的不完整末行留到下次。停服务后可删缓存重建。命令行可用 `danmaku_search.py`。
- **定位视频** 显示录像路径和片内时间，可跳转、提前 5 秒回看、复制路径或下载；视频接口支持 HTTP Range。
- 启用弹幕和分段录制时，同目录会生成 `同基名.segments.csv`，记录 FFmpeg 实际切片边界，要和视频、弹幕一起保留；转成同基名 MP4 后仍能关联。没有这个文件的旧录像用 `ffprobe` 累计实际时长估算，缺分段或探测失败会明确提示。
- 弹幕时间和视频时间之间有启动缓冲、网络延迟，不保证逐帧同步，切片边界附近要人工核对。浏览器对 MP4 兼容最好，TS / FLV / HEVC 可能只能下载后本地播放。

**访问安全**：沿用控制面板的访问边界，没有登录系统，只在可信局域网使用；公网访问前要配反向代理认证和 HTTPS。接口只读录制目录内的指定视频格式，拒绝路径穿越、越界软链接和已排除的 CloudDrive 目录。

## 代码结构

```text
main.py              装配：banner → ffmpeg → 主循环
src/runtime.py       跨线程共享状态、锁、常驻 asyncio 事件循环
src/config_loader.py config.ini 读取与热重载
src/url_config.py    URL_config.ini 解析与 worker 调度
src/recorder.py      单直播间录制 worker（ffmpeg 子进程）
src/danmaku.py       伴随服务客户端与 JSONL 写入
src/monitor.py       按错误率动态调整 max_request
src/spider.py        抖音、小红书直播数据抓取
src/stream.py        流地址提取与画质映射
src/room.py          抖音分享链接与用户 ID 解析
src/ab_sign.py       a_bogus 签名（SM3 + RC4）
webui/server.py      FastAPI 后端
webui/static/        Vue 3 单页前端
scripts/danmaku_convert.py  JSONL → Bilibili XML
```

## 致谢

基于 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) 修改，感谢原作者及所有贡献者。
