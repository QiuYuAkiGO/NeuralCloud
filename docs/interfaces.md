# 接口与接入

服务端定义见 [api.py](../src/neuralcloud/api.py) 与 [contracts.py](../src/neuralcloud/contracts.py)；客户端见 [index.mjs](../plugins/dsh-neuralcloud/src/index.mjs)。本文只解释使用方式，字段全集以源码及生成契约为准。

## HTTP 边界

全部路由要求 `Authorization: Bearer <token>`，令牌至少 32 个字符。服务默认只监听 127.0.0.1，入口使用单 worker；FastAPI 自动文档路由关闭。请求体超过 128 KiB 返回 413；认证失败返回 401；存储 Refusal 映射为 409，SQLite 错误映射为 503。模型字段验证仍由 FastAPI/Pydantic 处理。

- `GET /v1/health`：数据库与检索能力信息，以及已加载 API 模块路径和 SHA-256；配置了能力不代表每次查询都能成功。
- `POST /v1/ingest`：接收 SourceEvent v1 或带 channel 的 v2，返回 DURABLE 回执。
- `POST /v1/candidate`：仅接受有 source_ids 的 summary 或 relationship 候选。
- `POST /v1/corrections`：按 expected_revision 更正或撤回。
- `GET /v1/receipts?source_instance=...&event_id=...`：查询提交是否已经持久化。
- `GET /v1/records/{record_id}?offset=0&length=4096`：有界读取正文；length 上限 16384。
- `GET /v1/changes?after=0&limit=100`：从序号之后读取变化，limit 上限 500。
- `POST /v1/recall`：查询在线记忆，返回记录与证据判断。

## 写入时必须明确的身份

SourceEvent 必须提供 character_id、source_instance、event_id、session_id、occurred_at 和 content。默认 kind 是 observation，默认 privacy 是 private。occurred_at 是非负时间数值；调用方应保持事件身份稳定，以便超时后查询回执或安全重试。

v2 额外携带 ChannelContext：平台、账号、消息、发送者及会话身份。昵称不代替账号。身份线索也不构成工具操作授权。模型禁止未声明的额外字段，不能往请求中随意附带临时调试属性。

最小查询体为 `{"query":"之前提到的决定是什么"}`。需要范围约束时可提供 subject、targets、intent、constraints 等字段；其枚举和上限见 Recall。工作流的 compat 分支拒绝它不能落实的 constraints、targets 或非 auto intent，避免表面接受条件却实际忽略。

## Harness 记忆插件

[package.json](../plugins/dsh-neuralcloud/package.json) 要求 Node >=24.9.0，并声明 Cordis、dsh-agent、dsh-tools 的 peerDependencies。`ServiceClient` 只连接 `http://127.0.0.1`，默认请求超时 2000 ms，限制响应体为 512 KiB。

`MemoryClient` 根据服务 generation 和工作流索引状态失效缓存，短时复用相同查询。服务断开时返回 STALE_UNVERIFIED 或 CACHE_MISS，items 为空，不把旧文本当成当前事实。

`DurableOutbox` 用 SQLite 保存待发送事件和会话游标，默认上限 1000 条、16 MiB。排队与服务已接收是不同状态，故障处理应核对服务回执。`bindMemory(agent, client)` 为指定 agent 注册 memory_recall 和 memory_read，并返回解除绑定函数；调用它的宿主负责判断哪些 agent 有权使用记忆。

## 手机渠道

[channel.mjs](../plugins/mobile-chat/channel.mjs) 定义 mobile:private 与 mobile:group，默认服务地址为 `http://127.0.0.1:8786`；[invited.mjs](../plugins/mobile-chat/invited.mjs) 和 [join.mjs](../plugins/mobile-chat/join.mjs) 提供邀请与加入相关实现。

手机渠道依赖宿主和外部聊天服务；此 Python 记忆服务不会启动它。定位手机收发问题时分别核对渠道接入、宿主会话和消息服务，不要把记忆接口健康等同于手机交付成功。

## 生成契约

[tools/export_resident_contracts.py](../tools/export_resident_contracts.py) 根据 Pydantic 模型生成 JSON Schema、TypeScript 声明及相关接口产物。修改模型后应使用该生成器更新产物，不直接编辑生成的 DTO。CLI 的 export-contracts 子命令只导出其明确列出的基础模型，不能替代完整生成器。
