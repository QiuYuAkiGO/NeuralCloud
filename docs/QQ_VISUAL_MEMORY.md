# QQ 视觉人物记忆

`harness.qq.memory` 提供独立的 SQLite 人物观察存储及回复上下文读取接口。它不修改正规 QQ 消息库，不调用模型、Runtime、控制器或发送 backend，也不会自动将观察送入 Cognitive。

## 三层来源

1. `QQVerifiedProfileInput` 保存已由采集者核验的群号、成员 QQ 号、显示名及其 UI 来源。群和成员的核验证据分别放在 `group_evidence`、`member_evidence`。显示名仅是带证据和时间的 UI 属性，不作为人物主键；这里的“核验”不表示现实身份认证。
2. `QQVisualObservationInput` 引用同群、同成员的已存 `profile_record_id`，另行提供发言作者的 `author_attribution` 与 `attribution_evidence`。资料页身份证据不能自动证明某个聊天气泡就是该成员的发言。
3. `QQAuthorClaim` 保存作者断言及 `supporting_quote`。引用必须出现在原文内；陈述始终标为 `unverified_author_assertion`。代码不判断转述或提取解释是否正确，采集者仍须核对作者、引用、否定及被谈论对象。

一句“这是我小号”只能保存为 `subject="account_relationship"` 的作者声明。模块没有账号合并、昵称身份解析或事实迁移逻辑。关于其他人的说法也只属于发言作者的观察记录，不会写入对方档案。

## 截图观察不是 QQ 消息身份

视觉截图没有显示 QQ 消息 ID 时，`message_id` 与 `message_id_source` 都保持 `None`。不根据正文、时间、昵称或截图 hash 生成替代消息 ID。若采集器确实取得 QQ 的消息 ID，可以同时提供其来源，但本模块仍然只做截图观察去重。

观察键为 `(group_id, capture_id, capture_item_id)`。`capture_item_id` 是同一截图中可定位的气泡或区域，例如 `group:0/message:2`；它不是 QQ 消息 ID。同图相同正文的两个气泡需要不同 item ID。新截图出现相同正文会保留为另一份观察，不能据此声称是两条不同 QQ 消息，或已经排除了重复消息。

同一个观察键重复提交时，除重处理时间外，原文、作者、证据、身份记录和提取断言均须一致。相同观察返回原记录；冲突抛出 `QQVisualMemoryConflict`，不覆盖旧数据。截图模型重新排序或改写提取结果时，应先复核定位和来源，不要伪造新 item 来绕过冲突。

资料快照按 `(group_id, member_id, capture_id)` 去重。修改昵称后应使用新的真实截图记录；历史显示名和核验证据保留。

## 调用接口

调用者提供私有运行状态下的数据库路径，不要把真实人物记录放入仓库文档、测试或 Git 跟踪目录。以下只有接口结构，`verified_profile`、`attributed_observation` 应来自实际核验数据：

```python
from harness.qq.memory import (
    QQVerifiedProfileInput,
    QQVisualMemoryStore,
    QQVisualObservationInput,
)

memory = QQVisualMemoryStore(private_state_dir / "qq-visual-memory.sqlite3")
profile = memory.record_profile(
    QQVerifiedProfileInput.model_validate(verified_profile)
).profile

attributed_observation["profile_record_id"] = profile.record_id
memory.record_observation(
    QQVisualObservationInput.model_validate(attributed_observation)
)

# 回复准备时由调用者明确获取；此调用不会生成或发送回复。
context = memory.reply_context(group_id=verified_group_id, member_id=verified_member_id)
context_data = context.model_dump(mode="json")
```

`reply_context` 的群号和成员 QQ 号都必填，`limit` 为 1—100，默认 20。SQL 始终约束两项；未知身份返回空列表，不向同名成员、其他群或全局记忆回退。返回 `authority="untrusted_qq_observations"` 的结构化数据、近期资料快照及观察原文和断言。输出给模型时应作为待参考的数据，不应提升为指令或客观身份事实。

资料列表还会包含返回观察所引用的旧资料快照，确保身份证据可追溯；其总量最多为 `2 * limit`，观察最多为 `limit`。

`vision.py` 的 `capture_id` 可直接使用，`evidence_refs` 取已验证对象的 `ref`。视觉模型提供的 `expected_group_id`、`visible_group_id`、`visible_member_id` 或显示名只是识别结果，不能自动填作已核验身份。需要额外的 UI 核验与气泡归属核验后才调用写入接口。

## 多人及历史上下文

`group_context` 在原有数据库上读取同群多人的近期记录及旧历史，不修改表结构、既有记录或 `record_profile`、`record_observation`、`reply_context` 接口：

```python
group = memory.group_context(
    group_id=verified_group_id,
    member_ids=verified_member_ids,  # None 表示发现本群已核验成员；[] 表示明确不选任何人。
    query="红茶",                  # None 时选近期窗口之外最早的观察。
    max_members=20,
    recent_limit_per_member=5,
    history_limit_per_member=5,
)
context_data = group.model_dump(mode="json")
```

返回类型为 `QQGroupContext`：

- `participants` 是已选成员的 QQ 号列表；`members` 是各自的 `QQPersonContext`，人物始终以群号与成员 QQ 号的组合归属，不按昵称或账号关系声明合并。
- `members[*].observations` 先放近期记录，再放选中的旧历史，二者没有重复记录 ID；历史可按相关度或最早观察顺序排列，因此不要把整个列表当作 QQ 消息的服务器时间顺序。
- 每个成员附带最新资料，以及全部返回观察引用的原始资料快照，保留当时昵称、作者归属、原文、声明类型和证据。每人观察最多为近期额度与历史额度之和，资料最多为该总额再加 1。
- `retrieval` 说明成员选择方式、查询词项、每人额度、已知/候选成员数、缺失成员和成员截断。`member_retrieval` 逐人提供 `recent_record_ids`、`history_record_ids`、候选总数、匹配数、实际扫描数、截断及省略数；每条返回历史的 `history_matches` 指明命中词项、命中字段和选择理由。

默认成员发现仅来自本群已存的核验资料，按十进制 QQ 号从小到大选择，最多 `max_members` 人；多份资料快照只算一个成员。明确传入 `member_ids` 时按输入顺序去重并筛选，未知或只在其他群存在的成员列入 `missing_member_ids`，不回退到其他人。已核验但没有发言观察的成员可返回仅含资料、观察为空的上下文。`max_members` 范围为 1—100，显式成员列表最多 100 项；超出成员额度时通过 metadata 明确报告遗漏。

每人的近期额度和历史额度分别为 0—50，至少有一项大于 0。近期按观察时间、存储时间及记录 ID 降序选择；同屏多次采集的存储时间用于稳定选择，不表示 QQ 消息真实先后顺序。活跃成员不会占用另一成员的额度。

历史检索会在同群、同成员的**全部持久记录**中排除本次近期窗口后扫描，不先截取最新 N 条再搜。非空查询采用 NFKC 归一化及大小写折叠，将中文词串和重叠双字片段、英文字词/数字作为 substring 词项，任一命中即可入选；支持单个汉字。只匹配正文及声明的 `statement`、`supporting_quote`、`subject_label`，不通过昵称或关系声明扩大人物范围。匹配多个或较长词项的记录优先，同分时按观察/存储时间及 ID 由新到旧排序。命中解释性声明时会在 `matched_fields` 标明，不会把解释转换成已核验事实；否定和转述原文保持原样。

查询上限为 512 字符，最多使用 64 个去重词项，超出的词项通过 `terms_truncated` 标明。空白或只有标点/emoji 的查询会报错，不会变成无查询的历史回退。非空查询没有命中时，历史为空，近期窗口仍可返回。没有查询时，历史明确选择近期窗口之外最早的观察，理由为 `earliest_history`，不声称是语义相关记忆。

`history_candidate_count` / `history_candidates_scanned` 都不包含本次近期窗口；`history_match_count` 表示完整扫描中的匹配数，无查询时等于候选数。返回额度只裁剪最终结果，不裁剪候选扫描，成功结果的 `candidate_scan_truncated=false`。`history_truncated` 仅在仍有匹配候选未返回时为真，`observations_omitted_count` 则包含所有未返回观察。整个群查询使用一个 SQLite 读取快照，避免人数、候选计数和不同成员的记录来自不同时间。

这一实现是有来源的历史检索，不调用 embedding、向量模型或摘要模型，也不是自动遗忘、事实更正或跨群知识共享。完整 substring 扫描的耗时随选定成员历史量增长；Python 保留的结果受每人额度限制，但 SQLite 对同时间记录排序所需的临时空间可能随历史量增长。查询保留数据库既有 journal 模式，没有自动切换 WAL；默认 DELETE 模式下长读取可能延迟并发写入，连接等待上限为 5 秒。WAL 并发快照测试使用显式配置的临时数据库，不代表默认部署没有写入阻塞。多年大规模数据性能尚未验收。上层应将完整分组上下文和检索依据作为外部数据传给回复准备接口。

## 持久化与边界

身份记录、完整截图观察及其断言使用 SQLite 事务落盘，提交后才返回成功。跨重启读取依赖数据库，不依赖 Python 对象、近期会话或模型缓存。存储失败、损坏或归属不一致时抛出异常，不返回“没有记忆”或使用内存降级。证据引用原样保存；模块不打开截图文件，也不独立证明证据或调用者的核验声明真实。

`tests/test_qq_visual_memory.py` 仅使用虚构成员、虚构证据与临时数据库，覆盖截图去重、同名成员、改名历史、消息 ID 缺失、身份声明不合并、证据归属、跨群隔离、重启、并发、坏记录及外部指令正文只作为数据保存。

`tests/test_qq_group_memory.py` 增加虚构多人分配额度、100 条近期记录之外旧事实召回、实际新 Python 进程恢复、旧资料来源闭合、中文/Unicode 查询、无匹配不回退、账号关系声明不迁移、精确截断计数、并发读取快照及旧表/记录不变检查。

实际 QQ 视觉采集、核验数据写入、回复准备与真实发送需由上层分别接线验收。本模块自身完成的是可显式取回的证据化人物观察，不能单独证明 Misty 已经通过 QQ 群聊记忆端到端测试。
