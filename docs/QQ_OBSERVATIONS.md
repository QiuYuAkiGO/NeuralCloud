# QQ 群聊观察存储

Runtime 服务提供一个独立的持久观察入口。它保存已获取的 QQ 群消息及作者、来源和证据，支持按群、成员读取。它不提交 Runtime 事件，不调用 Cognitive、模型、控制器、动作规划或投递 backend。

这不是已完成的 QQ 接入：目前没有自动读取 QQ 窗口或消息元数据的采集器，也没有把这些记录注入 Cognitive 的提示词或长期语义记忆，更不会发送 QQ 消息。

## 存储与认证

- 默认数据库为有效 `NC_RUNTIME_STATE_DIR` 下的 `qq-observations.sqlite3`，与当前 Runtime 状态目录一致。
- API 复用控制平面的 `Authorization: Bearer <NC_CONTROL_TOKEN>`。不要把 token 写进观察正文、证据或脚本示例。
- SQLite 在首次访问时创建，写入事务提交后才返回成功。数据库不可用时返回 `503`，不会退回内存存储。
- 独立构造 `create_control_app` 时，需要显式注入 `qq_observation_store` 才注册这些路由；可执行 Runtime 服务已经接线。

## 写入观察

`POST /v1/qq/observations`

以下是完全虚构的测试数据，不能作为真实群聊验收证据：

```json
{
  "source": {
    "platform": "qq",
    "group_id": "10001",
    "member_id": "20001",
    "message_id": "synthetic-message-1",
    "identity_source": "qq_metadata"
  },
  "author_name": "虚构成员",
  "text": "这是一条虚构的测试消息。",
  "observed_at": "2026-09-11T10:00:00+08:00",
  "message_time": "2026-09-11T09:59:00+08:00",
  "evidence": ["test-evidence://synthetic-qq/message-1"]
}
```

生产采集器必须提供实际从 QQ 获取的稳定标识：

- `group_id` 是群号，`member_id` 是消息实际作者的 QQ 号；二者为不带前导零的十进制字符串，不能填写昵称。
- `message_id` 是 QQ 来源消息标识，不能用截图 hash、正文 hash、时间戳或随机 UUID 替代。`identity_source` 必须为 `qq_metadata` 或 `qq_visible_identifiers`，表示这些 ID 来自 QQ 元数据或可核对的 QQ 标识信息。
- `author_name` 只是观察到的显示名，不参与身份合并；引用消息中的人也不能代替当前消息作者。
- `observed_at` 必填且包含时区。`message_time` 不可得时省略，不猜测。数据库统一保存 UTC 时间。
- `evidence` 必须包含至少一个可追溯的观察证据引用。此入口保留引用，不会访问引用地址、读取截图或独立验证 QQ 身份。来源真实性和证据可解析性由已认证采集器负责。

只有昵称或正文截图、无法取得稳定的群号、成员 QQ 号和消息 ID 时，采集器必须停止写入。API 会拒绝缺失、空白、昵称形式的数字 ID、常见未知占位符和明确的截图指纹；它无法识别调用者伪造的外观合法 ID，不应把模式校验描述为 QQ 在线验证。

成功响应为 `{"created": true, "observation": {...}}`。这是已持久化的观察，不是消息已发送的回执。

去重键是 `(group_id, message_id)`，在 SQLite 事务内处理，可跨进程和重启生效。同一消息重新截图或作者改昵称时返回 `created: false` 及首次存储的记录；首次证据、观察时间和名字保持原样。若作者、正文发生变化，或双方均提供但消息时间不同，返回 `409`，不覆盖原记录。一个群的消息 ID 不会与另一个群发生碰撞。

## 读取观察

`GET /v1/qq/observations?group_id=10001&member_id=20001&limit=50`

`group_id` 必填，`member_id` 可选，`limit` 为 1—100，默认 50。返回按首次观察时间降序排列的记录列表。未匹配时返回空列表，不会回退到其他群、其他成员或全局记忆。当前是有界历史读取，没有语义搜索或翻页。

## 验证与未接通环节

`python -m pytest tests/test_qq_observations.py -q` 使用虚构身份、虚构证据和临时 SQLite 数据库，覆盖认证、严格输入、重复与冲突、同名成员、跨群隔离、并发提交、超过近期会话窗口后的重新加载，以及磁盘不可用。

API 测试中的控制器在任何属性访问或动作调用时立即失败，以验证外部正文不会进入控制通道。

后续需要独立验收实际 QQ 采集器能够获得稳定来源 ID 和真实证据，再接入有范围限制的 Cognitive 上下文。真实回复还需要明确的目标群与可核对的发送回执；此模块没有发送接口。
