# 弹幕 JSONL 格式与转换

说明录制抖音直播时生成的 `.danmaku.jsonl` 文件格式（schema v1），以及如何把它转换成 Bilibili 标准 XML 弹幕。开启方式见 [README](../README.md#弹幕录制)。

## JSONL 格式（schema v1）

文件第一行固定是 `metadata`（记录房间号和开播录制起点时间戳），之后每行一条 `chat` 聊天弹幕：

```jsonl
{"type":"metadata","schema":"douyin-live-recorder-ui.danmaku","version":1,"platform":"douyin","room_identifier":"123456","recording_started_at_unix_ms":1787104800123,"time_units":{"recording_started_at_unix_ms":"milliseconds since Unix epoch UTC","received_at_unix_ms":"milliseconds since Unix epoch UTC","offset_ms":"milliseconds from recording start"}}
{"type":"chat","sequence":1,"method":"WebcastChatMessage","offset_ms":2345,"received_at_unix_ms":1787104802468,"user":{"id":"987654","nickname":"示例用户"},"content":"你好"}
```

- `recording_started_at_unix_ms`：FFmpeg 启动时的系统 UTC 毫秒时间戳。
- `offset_ms`：相对于录制起点的单调时钟毫秒数（不受系统校时跳变影响，定位视频和转 XML 时以此为准）。
- `received_at_unix_ms`：收到该条弹幕时的系统 UTC 毫秒时间戳。

## 转换为 Bilibili XML

使用 `scripts/danmaku_convert.py` 将 `.danmaku.jsonl` 转为同名 `.xml`（弹幕文本格式为 `昵称: 内容`）：

```console
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --overwrite
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --output custom.xml
$ uv run python scripts/danmaku_convert.py name.danmaku.jsonl --recover-truncated-tail
```

- 默认遇到损坏行会直接报错退出；如果录制中途强退导致最后一行没写完，加上 `--recover-truncated-tail` 即可自动忽略残缺的末行。
