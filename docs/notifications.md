# 通用通知接口 v1

录制服务对外提供的开播通知接口：主播被设为“特别监控”后，开播时生成 `live.started` 事件，由独立的通知适配器（如 QQ 群机器人）拉取、发送并确认。录制核心本身不发任何通知。开启方法见 [README](../README.md#开播通知)。

## 分层

- **录制核心**：成功抓取直播状态后产生标准事件，不包含 QQ/Hermes SDK、收件人、凭据或任何发送网络请求。
- **特别监控**：WebUI 星标/筛选及 `/api/streamers/special-monitor` 管理订阅；状态存储在 `config/special_monitor.json`，原子提交，重启保留。
- **通知适配器**：通过以下 HTTP 契约拉取事件，按自己的配置发送，成功后确认。每个独立消费者（例如 `mail-home`、`webhook-office`）有自己的确认记录，不互相消费掉事件。
- **首个实现**：nas-ops 里 `plugins/qq-douyin-recorder-plugin` 的 `notifications.py`。其 `NotificationPump` 依赖 `NotificationSender.send(event) -> bool` 异步协议；`QQGroupSender` 只是第一个发送实现。邮件/Webhook 可以实现此协议，也可以在独立进程中直接实现下面的 HTTP 契约，不需要依赖 Hermes。

核心项目不配置默认群、不读取机器人配置、不向所有渠道广播。没有运行适配器时，只生成事件，不会实际发送。

## 管理特别监控

```http
PATCH /api/streamers/special-monitor
Content-Type: application/json

{"url":"https://live.douyin.com/123", "enabled":true}
```

只接受列表中已有的主播，URL 精确规范化匹配；重复设置幂等。成功返回 `{"ok":true}`，不存在为 404，参数错误为 422，文件读写失败为 503。主播列表增加布尔字段 `special_monitor`。

开启时已确认在直播则立即产生一个事件；否则等下一次成功检测到直播。暂停时不产生可领取提醒，暂停操作取消待发事件但保留星标。删除/取消星标删除该主播的通知状态。

## 适配器拉取与确认

消费者 ID 必填，1–64 个 ASCII 字母、数字、`_`、`-` 或 `.`。使用持久稳定且独立的 ID，不要每次启动随机生成。

```http
GET /api/notifications/events?consumer=mail-home
```

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

`occurred_at` 是**检测到**开播的服务器时间，不是平台给出的精确开播时间。`name`/`title` 最长 200 字；标题可能为空。后续版本可能增加字段和事件类型：忽略未知字段，不发送无法理解的类型。

发送端确认目标服务接受消息后：

```http
POST /api/notifications/ack
Content-Type: application/json

{"consumer":"mail-home", "event_id":"0123456789abcdef0123456789abcdef"}
```

成功返回 `{"ok":true}`。同一消费者重复 ACK、已过期/已取消的 ID 均幂等；旧事件 ACK 不影响后续开播事件。未 ACK 的事件下次仍返回；失败不要 ACK，建议指数退避并记录错误。确认仅表示适配器报告发送成功，不保证人已阅读。

## 语义与边界

- 同一主播持续直播、ffmpeg 分段/断流重连、抓取失败都不产生新开播事件。只有成功观测到下播再上线才重新产生；进程重启保留已观测状态。
- 监控停止期间（暂停、关机或平台连续不可用）无法得知是否发生下播再开播，不承诺补全未知场次。小红书仍只支持已添加的本场直播链接。
- 每位特别监控主播只保留**最近一次**开播事件；事件最多可领取 1 小时，防止适配器恢复后发送很久以前的提醒。下一场替换上一场。已在发送途中的事件不能被取消操作撤回。
- 投递为**至少一次尝试**而非严格恰好一次。适配器应先保存成功发送的事件 ID，再 ACK；ACK 失败重试 ACK 而不重发。但外部服务已接受、进程却在保存成功记录前崩溃或请求超时，仍可能重复。接收服务支持幂等键时应使用事件 ID。
- 同一个消费者只运行一个轮询实例（接口不是分布式队列，没有领取锁）；多个独立目的地使用不同消费者 ID。
- 延迟取决于现有直播轮询周期、失败退避，再加适配器轮询周期；不承诺秒级通知。
- 沿用项目现有的无认证 WebUI/API，仅限可信网络/本机调用；消费者 ID 不是密码。对外暴露前应在反向代理增加认证与 TLS。事件与 ACK 接口不允许代填机器人令牌或任意发送目的地。

## 发布

核心代码更新要在允许中断录制时重启 `douyinlive-status.service`，浏览器刷新后出现星标开关。发送端单独部署和重载；QQ 群机器人的群选择、主动消息权限和发送失败日志见 [QQ 抖音录制机器人](../../nas-ops/plugins/qq-douyin-recorder-plugin/README.md)。
