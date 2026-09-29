# NeuralCloud 具身认知、内稳态与长期记忆深度重构

状态：生产机制首版实现与 Linux 隔离验收中（2026-09-22）。限定范围的 Linux 检索/准备、五秒心跳及暂停样本已通过目标；MistyAI 资源身体已独立安装，真实恢复及回执持久性验收通过。兼容接口候选尚未通过整组验收；Sonnet 4.6 原生协议六项结构检查通过，但仍有事实扩张，尚未选定可切换的主模型。尚未切换线上会话，未完成完整生产验收或七天观察。见 [本轮 Linux 记录](production-linux-validation.md) 与 [模型续测](production-model-validation.md)。

目标：**保留小雾的人格、共同经历和认识历史，重建以身体事实、经验学习、人物认识和自主决策为核心的生产系统。**

本文件替换旧方案中“保持七维内稳态轨迹、后期整理机制”的要求。Python、Cognitive / Homeostasis / Runtime / GUI 四服务和 SQLite 保留。连接组实验仍为独立研究；GA 盲化、对照策略、旧 manifest/hash 继续可回放，开启 GA 不等于生产迁移。

## 1. 双仓基线与状态权威

- NeuralCloud：`0b09fad6c948ec6d040ce6a043ff2ac116be5488`，`codex/grounded-agency-rebuild-plan`，已合并 grounded-agency。
- MistyAI：`309c59a771528f20de431f32c14faded4bfd3eb3`。原工作区有未提交工作，本轮使用独立 worktree `C:/Projects/MistyAI-embodied-production`、分支 `codex/embodied-production`。
- 原工作区、旧 ID、原文、来源和解释历史不得覆盖。生产功能显式 opt-in，本轮不自动修改已有部署配置。
- MistyAI 独占身体事实、消耗和恢复账本；NeuralCloud 负责感知、解释、学习与选择。Runtime 独占授权、预算、调度和交付编排，Cognitive 独占人格、认识和情绪解释。
- QQ 保持唯一发送执行器、修订/期限/分段/UNKNOWN 对账和受限错误通知期限。耐久准备、真实交付、领域提交和结算用稳定回执衔接。

## 2. 身体与内稳态

生产模型可理解资源余量、成本及后果；严格中性编码留给独立实验。七维语义驱动、固定社交收益、恢复策略排序退出生产写入，旧值带机制版本保留为历史。

身体协议传递 instance、episode、sample sequence、ledger revision、有效时间与证据。模型在真实 HTTP 尝试边界记账；只把服务商实际返回字段视为原始用量，缺失不是零。Cognitive 先持久化 spool，Runtime 保存 outbox，MistyAI 幂等结算。聊天、工具动作、协议交付分别记账，UNKNOWN 交付在真回执出现前不扣费。失联成本按原 body/episode 补报，不转扣新身体。

模型显式选择开始/结束休息，MistyAI 根据持久状态与时间恢复；文本不能恢复资源。版本化首版参数：容量 100、初始 65、基础消耗 0.05 单位/分钟、休息恢复 2 单位/分钟。生产耗尽可恢复，实验 sticky terminal 保留。

断连、耗尽或休息期间保留用户受限对话和记忆读取，暂停普通动作及探索，允许恢复所需的休息选择。未知读数保持未知，历史身体身份只用于对账。

MistyAI 使用 `physiology.mode=production_v1`。已冻结实验账本不可原地改模式；先做显式实例迁移并保留旧 episode/证据链。结算 token 独立于普通身体 token，缺少权限保留待对账。身体端见 MistyAI `docs/design/0010-production-rest-accounting.md`。

## 3. 经历、人物认识与记忆

统一“来源经历 → 可修订认识 → 检索投影”：经历保存原文、作者、时间、结果、权限、更正/撤销；认识保存对象、情境、时间、支持、反证和历次解释；可达性保存重要性、使用与资格，与可信度分离。

- 同一 QQ 账号跨群和私聊同一身份，同名不同人不合并；跨平台必须显式绑定。
- 私聊可以影响对同一人的态度；事实、解释和叙事按当前受众证据权限过滤。私有目标也不能进入群聊上下文。
- 本人明确更正立即形成带时间与归属的最新自述；旧经历保留。自述不证明人格或可靠性。
- 检索命中、入模与决策使用分开记录。反复回忆不增加事实可信度，修订必须有新证据或反证。
- 用户要求记住的内容永久保留检索资格；模型的重要标记必须有理由与证据。每轮仍有相关性和候选/文本预算。
- 撤销、删除、过期和遗忘在所有入口一致，pin 不能覆盖权限和撤销。普通经历可淡化，认识可亲近、回避、中立或修复，无指定好感走向。
- 人物叙述和检索索引均可重建。旧印象以 `legacy_interpretation` 保留；旧关系分数不转为身体奖励，不批量调用模型重写过去。

`harness/memory/production.py` 提供摄取、修订、保留、使用、撤销和有范围召回。慢模型提交前重新验证实际使用的来源和认识版本。

模型验收暴露的自述误归属与证据角色混用，已接入第二版来源清单、分组提交和关系投影，见 [自述与证据关系第二版实现报告](production-memory-evidence-v2.md)；历史探针与旧验收结论保留。
Linux、模型调用及实现验收结果见 [第二版验收记录](production-evidence-v2-validation.md)；实现接入不等于线上 writer、数据库或模型配置已切换。

## 4. 情绪与自主性

情绪是持续、可修订的主观解释，结合身体、经历和认识，保存时间、依据、版本与历史。可以影响注意、表达、选择，不能改写事实或提高判断可信度。

新经历、环境变化、未完成目标和有界定期评估触发自主选择。模型可以探索、联系已授权对象、用已授权工具、休息、等待或放弃。

- 自主决策及后续真实模型尝试合计每滚动小时最多 **12 次**，包括重试，跨重启有效。用户直接发起的对话另算。
- 最多 4 个活动目标、每计划 32 步，连续 3 次无进展暂停；实际回执维护计数，模型不能指定计数。
- 联系时段与频率由模型在总预算和既有通道限制内决定；不另加静默时段或按人限频。
- 普通消息进入感知后，模型可立即回复、说明后继续、延后或拒绝，不机械取消所有自主活动。
- 停止、暂停、撤权立即生效。暂停后拒绝新的自主预算授权；计算结果仍需通过 epoch、来源和权限检查才能执行。

生产配置与研究互斥，禁止生产旧 semantic expression / goal regulation。主模型负责角色选择，独立记忆模型保留。GUI 展示身体、情绪解释、人物认识与证据、目标及维护状态。

## 5. 实施和迁移顺序

1. 冻结双仓基线、未提交工作、有效配置与状态权威；建立一致性副本、代表性回放和迁移清单。
2. 先打通身体协议、成本、显式休息、恢复、耗尽/断连行为和生产/研究边界。
3. 实现身份、经历、认识、重要记忆、受众过滤、统一召回和情绪；先验证机制及副本迁移。
4. QQ 端到端：真实身份/消息 → 身体与人物上下文 → 模型选择 → 原发送执行器 → 经历/用量/认识回流；再接自主目标与工具。
5. 受管任务、独立心跳与控制、初始两个前台计算槽、短锁冻结快照、锁外计算、相关版本校验、增量投影和耐久维护；持续前台流量不能无限推迟学习。
6. 副本迁移与回退演练后按明确授权会话单写切换；观察至少七天，再退役旧生产路径。

首版覆盖 2–5 核心路径。后台仍有旧 preset 整体 DTO 保存的兼容桥接，全面退役未完成；不能将 SQL 查询性能推断为所有维护操作有界。用户已选择现有 Linux 环境，首批群/私聊清单待提供。

### 副本与回退

先停 writer，再用 `tools.freeze_production_baseline` 捕获 preset 和 SQLite backup，核对前后哈希与双仓 Git 状态。备份包括 WAL 已提交页，但跨文件一致性仍依赖停写。真实聊天、密钥与快照不提交到 Git。

```sh
python -m tools.freeze_production_baseline --preset /snapshot-source/misty.json --shared-database /snapshot-source/shared-memory.sqlite3 --destination /private/snapshots/production-v1 --writers-stopped --repo /opt/neuralcloud/current --repo /opt/mistyai/current
python -m harness.memory.production_migration inspect /private/snapshots/production-v1/manifest.json
python -m harness.memory.production_migration migrate /private/snapshots/production-v1/manifest.json --destination /private/migrations/production.sqlite3
```

这些是路径示例，需替换为核实后的实例位置。迁移可中断继续并核对身份、旧 ID 和证据，详见 [production-memory-migration.md](production-memory-migration.md)。

代码回退不得回退事实：新交付、修订、撤销、用量 spool/outbox、身体结算和休息回执全部保留。禁止旧 snapshot 覆盖上线后状态。未知交付先对账；旧代码不能处理新状态时保持暂停/只读恢复，不清库、不复发。

## 6. 验收

机制：身份连续且不串档；更正/反证生效并留历史；重放不虚增；重要经历长期可检索；所有入口一致撤销/遗忘；私有事实/解释不进群聊；情绪可追溯。

身体/自主：休息产生核验恢复；重启不重复结算；耗尽可恢复；断连不造读数，恢复后对账；模型可提出目标、用工具、等待、休息或放弃；普通消息不机械取消自主活动；控制即时生效，12 次预算跨重启。

故障注入覆盖计算、准备、发送、回执、最终提交，验证不重复交付、不丢耐久学习任务。

Linux 单机 SSD 目标：慢模型下暂停阻止新工作 ≤500 ms；5 秒心跳调度延迟 p99 ≤250 ms；10 万条授权记忆检索加编译 p95 ≤300 ms（不含外部 embedding）；另测百万条、冷缓存及持续积压。线上配置、规模和性能在第一阶段实测，不沿用历史数字。

固定真实模型场景比较人物区分、更正、反证、长期回忆和自主选择，保留模型/提示/机制版本、输入与评价。测试全绿只证明工程合同，不能证明长期学习效果。上线前完成迁移、恢复、回退演练，七天观察覆盖资源、积压、修订和实际交付。

本轮实测与待验证项见 [production-validation.md](production-validation.md)。
