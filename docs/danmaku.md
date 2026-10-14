# 弹幕 JSONL 格式与转换

录制弹幕时生成的 `.danmaku.jsonl` 文件格式（schema v1），以及把它转成 Bilibili XML 的脚本。怎么开启弹幕录制见 [README](../README.md#弹幕录制)。

## JSONL schema v1

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

## 转换为 Bilibili XML

默认输出同名的 `name.xml`；`--overwrite` 覆盖已有目标，`--output` 指定输出路径，`--recover-truncated-tail` 容忍被截断的最后一行：

```console
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --overwrite
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --output x.xml
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --recover-truncated-tail
```

- 默认严格：任何坏行都报行号并以非零状态退出，不覆盖目标。空白行跳过，未知扩展字段允许。
- `--recover-truncated-tail` 只忽略“最后一行、没有换行、JSON 不完整”的尾部，其他坏行照样失败。
- 输入输出不能是同一文件（包括符号链接、硬链接），先写临时文件再原子发布。
- XML 文本为 `nickname: content`；`null` 只在 XML 里显示成 `0` / `匿名用户`，不回写 JSONL。
