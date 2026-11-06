# 通用开播通知接口 v1

录制服务提供的 HTTP 拉取式开播通知接口：在 Web UI 把主播标为“特别监控”（★）后，每次检测到该主播开播就会生成一条 `live.started` 事件；外部通知程序（如 [QQ 抖音录制机器人](../../nas-ops/plugins/qq-douyin-recorder-plugin/README.md)）定期轮询拉取事件、发出通知后再回传 ACK。录制服务自身不绑任何聊天平台 SDK。

## 1. 开关特别监控

```http
PATCH /api/streamers/special-monitor
Content-Type: application/json

{"url": "https://live.douyin.com/123", "enabled": true}
```

- 状态持久化在 `config/special_monitor.json`，重启后保留。
- 开启时如果主播已经在播，会立即生成一条事件；暂停监控时自动清掉未发出的事件。

## 2. 外部程序拉取与确认（ACK）

每个消费端使用固定的 `consumer` 名称（如 `qq-group`），各自独立记录确认状态：

```http
GET /api/notifications/events?consumer=qq-group
```

返回示例：

```json
{
  "version": 1,
  "events": [{
    "id": "0123456789abcdef0123456789abcdef",
    "type": "live.started",
    "occurred_at": "2026-09-15T12:00:00+08:00",
    "data": {
      "url": "https://live.douyin.com/123",
      "name": "主播昵称",
      "title": "直播标题"
    }
  }]
}
```

发送通知成功后，调用 ACK 接口确认（未 ACK 的事件下次轮询仍会返回）：

```http
POST /api/notifications/ack
Content-Type: application/json

{"consumer": "qq-group", "event_id": "0123456789abcdef0123456789abcdef"}
```

## 去重与过期规则

- **断流重连和分段切片不会重复触发通知**：只有观测到主播下播、之后再次开播，才会生成新事件。
- **1 小时过期**：每位主播只保留最近一场的开播事件，且事件生成超过 1 小时未领取自动过期，避免机器人挂掉恢复后补发半天前的旧开播提醒。
