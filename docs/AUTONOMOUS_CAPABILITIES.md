# 内稳态失败恢复与共享身体能力

实现按模型修复、共享选择、留言板、QQ、隔离桌面分层接入。普通聊天仍使用原模型调用参数；默认 manifest 不开启新机会，`autonomous_chat_enabled` 仍为 `false`。本功能不建立跨机会长期目标，也没有新增休息或补能接口。

## 配置与分阶段启用

`ExperimentManifest.autonomous_capabilities` 默认值如下。将本段加入现有共享启动所使用的 manifest 即可配置；使用现有 Runtime 控制和启动流程，不启动第二个人格或 QQ GUI 执行器。

```json
{
  "autonomous_capabilities": {
    "enabled": false,
    "owners": {"board": "legacy", "qq": "legacy", "desktop": "off"},
    "allowed_capabilities": [],
    "qq_send_enabled": false,
    "quiet_seconds": 60,
    "opportunity_seconds": 1800,
    "deadline_seconds": 60
  }
}
```

1. 模型修复可独立部署。Board 客户端配置增加 `model_max_tokens`，默认 2048；单次 Board 调用使用 JSON mode。沿用原人格、模型、thinking 和温度。
2. 先验收共享框架。只在真实内稳态摘要为 `noticeable` 或 `marked`、安静期已满、队列与动作执行器空闲时生成机会。等待与失败均消耗跨能力 1800 秒冷却。
3. 留言板启用时设置 `enabled=true`、`owners.board="shared"`，允许 `expression.board.read.v1` 和 `expression.board.append.v1`。旧自主选时停用，用户邀请和未完成回执恢复保留。
4. QQ 只读阶段设置 `owners.qq="shared"` 并允许 `communication.qq.read.v1`。watcher 必须与 Runtime 一起更新。`communication.qq.send.v1` 还受 `qq_send_enabled` 控制。初始验收禁用真实发送；用户在 2026-09-12 后续明确授权主动发送后，共享部署预设 revision 2 已允许 read/send，并开启 `qq_send_enabled`。原 watcher 的 `automatic_send` 也必须为 `true`，实际发送仍需原群号、账号、观察与回执核验。
5. 隔离桌面阶段设置 `owners.desktop="shared"`，同时启用现有真实 `mistyai` Provider、身体身份及动作/观察/核验契约。显式允许所需 intent，例如 `app.open.v1`、`control.observe.v1`。只有 Provider 实时报告的 skills 与现有动作契约交集才会进入目录；静态名称不能证明可执行。原动作风险等级和批准规则继续有效。

每个通道的 `legacy/shared/off` 独立生效；`off` 关闭该通道的自主选时。切换配置会持久化新代次，旧代次未执行机会失效。原 manifest 未写此字段时，其 canonical hash 保持兼容。

## 机会与授权

Runtime 先把机会、摘要、能力目录、观察引用与绝对截止时间写入 `capability-calls.sqlite3`，再提交内部 `capability_opportunity` 事件。通用 `/v1/events` 不能伪造该入口。

同一个 `CognitiveServiceHost` 读取当前人格、记忆和对话上下文，在最多 12 秒内选择 `act/observe/wait/defer/no_action`。模型只能引用目录项和填写经过本地类型验证的参数，不能指定身体、作用域、批准信息或延长期限。结构化选择复用现有解析/编译组件，不把 `connect/rest` 固定映射到动作。

选择后 Runtime 重新协商能力、复核健康与观察；分发绑定机会、决定、身体、作用域、能力代次和截止时间。总期限不超过 60 秒，各渠道只能缩短它。QQ 分发写入账本后立即释放 Runtime 事件循环，再由 watcher 领取并回调共享认知，避免互相等待。

留言板正文准备仍经过原 BoardController，可以再次选择 `wait`。准备不再进行第二次通用能力选择。桌面仍由 ActionCoordinator 执行，并在真正调用后端前复核权限、身体、目标观察、用户活动和期限。

## 模型失败与原子性

Board 将截断、空内容、内容类型错误、JSON 错误、字段错误分开记录。安全诊断包含 event、command、attempt、耗时、finish reason 和 token 明细，不记录隐藏推理或模型的完整私人上下文。历史 500-token 失败仍属于“疑似截断”，不能因新版本增加 finish reason 就把旧样本改成确定结论。

自主正文准备共用原 12 秒预算，人工邀请共用原 25 秒预算；它们还受命令期限限制。只有 append 前的明确格式失败，剩余至少 2 秒时才允许一次修复。合法等待立即结束。网络错误、超时、取消、过期和迟到结果不修复；失败命令重放读取原结果，不重新生成。

最多两次模型调用形成一个有效决定，最多一次 append。append 开始后结果不明时只查回执与独立证据，不能追加第二次。回执已确认但记忆尚未提交的情况保留恢复状态。

## QQ 跨进程桥接

三个新 RPC 复用现有 bearer 认证：

- `POST /v1/qq/capabilities/report`：身体/群/成员/run 绑定、观察引用及读写健康，报告 15 秒失效。
- `POST /v1/qq/opportunities/claim`：领取机会，先在原 watcher ledger 持久化，再创建原 idle intent/event。
- `POST /v1/qq/opportunities/ack`：重复确认幂等。Runtime 读取原账本、决定、分段发送记录与回执证明；客户端 ack 本身不是发送证明。

watcher 继续使用唯一 GUI actor、原共享认知和发送执行器。未知结果继续对账；报告过期、归属变化、用户活动和期限失效都阻止新的副作用。网络故障不能静默退回 legacy 自主发送。

## 反馈与观察

通用桌面完成不走旧 `success=True` 的默认胜任感/安全感增量。只有有独立证据的动作事实才进入客观事件记忆；等待、拒绝和未执行失败结束时零记忆写入。正常身体快照与已有分类事实通道仍可更新内稳态。旧显式任务路径保持兼容。

写板成功、发送尝试、读取或等待都不能直接证明需要被满足，等待也不代表能量恢复。实际改善必须由后续身体测量或已验证的分类事实支持。

## 诊断、验证与回滚

认证的 `GET /v1/capabilities` 返回开关、归属、代次、支持/可用/就绪目录和近期机会状态。原 Board、QQ 回执账本及 Runtime journal 继续提供因果链；能力调用详情保存于私有 Runtime state 目录。

新增测试主要在 `tests/test_llm_response_contract.py`、`tests/test_capability_cognition.py`、`tests/test_capability_service.py`、`tests/test_autonomous_capabilities.py` 和 QQ capability 测试。自然漂移测试只在隔离引擎上重放已流逝的心跳时间，不注入 action_request 或修改生产内稳态。

真实验收每类最多五次有效机会，正常等待如实记录。私有板需要真实模型写入、回执、独立回读和记忆；隔离桌面需要非空动作及独立观察。QQ 初始阶段只读并核对零发送；后续授权发送阶段必须从机会追溯到原 idle intent/event、发送账本、回执与独立消息证据，不能把普通消息回复当作内稳态主动发送。未产生动作时结果应标记“真实执行待验证”，不得通过催写或调生产指标凑样本。

`tools/verify_capabilities.py` 默认执行只读 preflight；`--mode board` 或 `--mode desktop` 每次执行一个真实模型机会，保存独立临时 Runtime/内稳态状态和验收报告。桌面使用现有隔离身体令牌，支持 `--desktop-token-file` 或进程环境 `NC_ACCEPTANCE_DESKTOP_TOKEN`。工具不启动 QQ watcher。

2026-09-12 验收中，第二个 Board 机会取得真实写入、HTTP 与 Linux SQLite 独立回读以及隔离记忆提交；第一个机会的模型 JSON 有效，但身体写入失败且原因未确证。生产内稳态与人格源文件未更改。桌面因 Docker Desktop 启动故障尚未取得有效机会，QQ Agent 也未运行，因此两者的在线验收保留为待验证；发送 reservation 保持 23，delivery attempt 保持 41。详细记录见 `docs/milestones/evidence/homeostatic-capabilities-live-2026-09-12.json` 与 `qq-capability-readonly-2026-09-12.json`。

回滚时关闭 `enabled` 或把待停通道设为 `off`，停止新机会。已经开始的动作、未知结果和回执/记忆提交继续对账。不要清空调用数据库、历史失败、回执或记忆；恢复部署时使用原 state 目录。

共享 QQ 选时由 Runtime 负责，watcher 不再重复套用 legacy 的安静期、机会冷却和固定需要映射。发送前仍刷新共享内稳态；同一需要 episode 内的正常心跳版本前进不使调用失效，而需要 episode 改变、旧归属代次、权限变化、新消息或截止时间到达仍会阻止发送。刷新观察后必须经原 desktop actor 重新签发群核验对象，不能复制核验对象充当发送授权。旧巡检任务也须同步用户后续授权，避免按已被替代的只读要求暂停发送。
