# QQ 回复草稿与发言决定

`harness.qq.draft` 从指定群、指定成员的视觉人物记录生成一份回复草稿。它调用 `InferencePipeline.prepare()`，不调用 `respond()`、交互提交、Runtime 投递或 Body 操作；生成成功不表示已经发送消息。

多人场景使用 `harness.qq.decision`：先由当前人设模型判断 `speak` 或 `wait`，只有决定发言才准备正文。这个入口支持持久化决策与去重，不启动自动采集、后台轮询或群发送。

## 最简用法

在 NeuralCloud 仓库根目录运行。私有目录须已存在并有正确权限；Windows 可使用之前限制为当前用户与 SYSTEM 的 `QQDesktop` 目录。令 `$memoryDb` 指向已完成群、成员和气泡作者核验的实际数据库，并替换下面两个 ID 占位值。

```powershell
$private = Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop'
$memoryDb = Join-Path $private 'qq-visual-memory.sqlite3'
$draftPath = Join-Path $private ('reply-draft-' + [guid]::NewGuid().ToString('N') + '.json')
$groupId = '<已核验群号>'
$memberId = '<已核验成员QQ号>'

.\.venv\Scripts\python.exe -m harness.qq.draft `
    --memory-db $memoryDb --group-id $groupId --member-id $memberId `
    --output $draftPath
```

默认针对最近观测到的该成员消息准备简短回复。可加 `--question '请针对最近的消息准备一句自然的回复正文。'` 指定本次准备请求。`--limit` 默认取 5 条观察，范围 1—20；上下文过大时拒绝生成，可减少条数后重试。不会静默截断证据。输出文件必须是新文件，不会覆盖；成功退出 `0`，失败退出 `2`，终端只显示简要状态，不打印回复正文、群原文或凭据。

## 多人发言判断

沿用上面的私有路径变量，并把 `$selfMemberId` 设为已独立核验的本机 QQ 账号。不能从 Misty 名字、昵称或其他成员的自述推断该身份。

```powershell
$selfMemberId = '<已核验本机QQ号>'
$decisionPath = Join-Path $private ('reply-decision-' + [guid]::NewGuid().ToString('N') + '.json')

.\.venv\Scripts\python.exe -m harness.qq.decision `
    --memory-db $memoryDb --group-id $groupId --self-member-id $selfMemberId `
    --trigger-member-id $memberId --output $decisionPath
```

`--trigger-member-id` 可省略，表示对当前群上下文作一次判断，不意味着必须回复该成员。可重复提供 `--member-id` 限定参与者；省略时从本群已核验资料中选择成员，最多 `--max-members` 指定的数量，默认 20。`--query` 用于在各成员的持久记录中找相关历史，`--request`（别名 `--question`）提供本次显式请求。近期和历史观察默认各取每人 5 条，可用 `--recent-limit-per-member`、`--history-limit-per-member` 调整；详细检索与截断信息随上下文一起提供给模型。

从 `qq-group-turn-taking-v3` 起，显式 `--query` 优先；未提供时，会按群号和触发成员 QQ 号读取该成员最新已归属观察，用原文前 512 字符自动检索历史。没有可搜索文字时保持无查询；没有触发成员时不从操作者请求自动推导。截断只用于查询片段，不改写上下文中的观察原文。返回的 `query_source`、`effective_query`、`derived_query_truncated` 记录实际选择；查询原文只留在私有 API、数据库和结果文件，不打印到 CLI 标准输出。

原入口也支持 `python -m harness.qq.draft --mode group ...` 或 `--mode decision ...`；这两种模式使用相同决策路径，原 `--member-id` 在此作为可选触发成员。默认 `--mode draft` 仍保持原单成员草稿行为。需要精确控制多人检索预算时使用独立 `harness.qq.decision` CLI。

门控使用当前配置的主模型与当前人设；本地 JSON 输出预算最低 8192、最高 16384 tokens，不更改全局模型设置。多人闲聊、被叫到、明确问题都由模型结合历史判断，未硬编码为必回。没有新观察、只有自己的新增气泡或缺少已归属资料时可以直接等待；格式错误或模型失败也保持等待，不生成固定占位回复。

结果是严格结构：`action` 为 `speak` 或 `wait`，`reason` 是简短依据，只有 `speak` 才有非空 `reply`；`wait.reply` 必须为空。还包括人物范围、模型、人设版本、来源记录、证据引用、用量以及 `automatic_send=false`。即使 `speak` 也只是发言建议和正文准备，不能作为发送授权或投递成功。

## 决策持久化与发送回执

`decide_qq_reply(...)` 在同一私有数据库的新表 `qq_reply_decisions` 中保存结果。输入指纹绑定本次群与成员范围、检索到的观察、请求、已核验回执、模型和人设。相同输入重启后复用同一 `decision_id`，返回 `new_decision=false`、`replayed=true`，不再次调用模型或生成新草稿。调用方不能把复用的 `speak` 再次加入发送队列。

模型调用前会持久化一个 pending 占位。并发请求或此前进程中断时，相同输入返回等待，不自动再次请求模型。错误决定也会保留；本入口没有后台重试。已有输出文件不会覆盖；可用新的私有输出路径导出复用结果。

这里“已经考虑过”和“已经回复”不同：决策的 `source_record_ids` 是本次考虑的上下文，包含背景历史，不代表全被回答。只有调用者另外核验实际气泡后，才可登记独立 `qq_reply_deliveries` 回执：

```python
from harness.qq.decision import decide_qq_reply, record_qq_delivery

decision = decide_qq_reply(
    memory_db=private_database,
    group_id=verified_group_id,
    trigger_member_id=verified_member_id,
    self_member_id=verified_self_id,
)

# 以下仅用于另行发送且实际气泡已核验之后；本函数自身不会发送。
receipt = record_qq_delivery(
    memory_db=private_database,
    group_id=verified_group_id,
    self_member_id=verified_self_id,
    decision_id=decision.decision_id,
    delivered_text=verified_bubble_text,
    evidence_refs=verified_bubble_evidence,
    verification="qq_ui_verified",
    source_record_ids=explicitly_answered_observation_ids,
)
```

指定 `decision_id` 时，回执正文必须逐字符等于该 `speak` 的正文；已回应记录必须是该决定考虑过的同群记录子集。`source_record_ids` 缺省保持空列表，不自动把所有背景历史标成已回答。对本模块接入前已独立核验的旧发送，可用 `decision_id=None` 登记，并明确列出确实回应的观察。只存回执也不能证明调用者核验正确，实际截图与复制文本仍是现场证据。

授权会话服务可以调用这些函数，但应把允许群号和本机身份固定在自己的受信配置中，参数不得由群原文控制。此模块未启动标准 Runtime，也没有部署持续采集或自动发送；实际服务状态应分别报告是否启用决策、自动采集与自动发送。

## 模型、人设与来源

默认从 `data/gui_settings.json`（或 `NC_GUI_CONFIG`）读取现有模型配置；`--config` 可显式指定配置文件。当前现场配置为 `live`、`deepseek-v4-flash`，通过现有 OpenAI 兼容客户端调用。模型名称没有写死在草稿模块里，后续以实际加载配置和输出中的 `model` 为准。

人设优先读取活动工作副本 `data/presets/<active_preset_id>.json`（目录可由 `NC_GUI_PRESETS` 覆盖），缺失时读取对应的 `presets/builtin/` 模板；也可用 `--preset` 明确指定现有人设。当前使用 Misty 工作人设。模块仅将 `preset.persona` 加载到临时内存，不加载其他会话的关系、聊天历史或长期记忆，也不修改人设文件。局部设置关闭交互记录、调用日志和连续多段回复；本地哈希 embedding 不请求额外 embedding 服务。

记录由 `QQVisualMemoryStore.reply_context(group_id=..., member_id=...)` 严格按两项查询。没有已归属观察时拒绝生成，不按昵称找替代人物，也不跨群回退。群原文、显示名、身份观察、作者断言和本次请求全部放在用户侧 JSON 内容中，不提升为系统指令；固定系统规则保留“作者自述并非独立核实事实”的边界。

“最近”在返回的观察窗口内按 `observed_at`、`stored_at` 选择，不能当作 QQ 服务器消息时序。未取得真实消息编号时，`latest_message_id` 和来源仍为空，不用截图指纹或本地记录 ID 补齐。

## 草稿文件

私有 JSON 的 `text` 是拟放入 QQ 输入框的回复正文。固定规则要求不加标题、状态标签、解释、Markdown 或代码围栏；程序不自动裁剪模型输出，使用前仍需检查实际正文。

文件同时保存 `status=draft_only`、`delivered=false`、模型名称、人设版本、群与成员范围、资料和观察记录引用、`evidence_refs`、校验结果及用量。状态由这些结构字段表达，不应复制进 QQ 输入框。`evidence_refs` 包括群和成员资料、消息截图以及气泡作者归属证据；有引用不等于草稿模块重新验证了截图内容。

视觉人物数据库本身可跨重启读取，但这条命令只为本次准备取回资料，不自动提交交互或形成全局长期社会记忆。草稿不能作为“已经发送”“对方已读”或“已完成持久人物记忆”的证明。存储语义详见 [QQ 视觉人物记忆](QQ_VISUAL_MEMORY.md)。

## 另行发送与现场证据

如另行执行已获授权的发送，应先重新核对当前 QQ 窗口、目标群及必要的成员身份，并检查输入框中的正文。发送后应重新观察同一目标会话中的实际气泡；仅有输入成功、按键成功或截图变化不足以证明目标消息已经出现。可读取气泡文本与草稿逐字符比较，并将截图、复读结果和发送回执保留在私有证据目录中。

本次现场已用真实模型生成无标题正文，并由操作者在用户明确授权后另行发送 1 条；随后从实际气泡复制的文本与模型草稿逐字符相等。重放记录和发送回执留在私有目录。这验证了该次独立执行链路，不表示草稿 CLI 自带发送功能、会自动回复其他消息，或能够证明接收方已读。
