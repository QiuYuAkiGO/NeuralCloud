# Qwen 情绪评估与演绎

## 2026-09-17 独立 DeepSeek 情绪通道

`Settings.emotion_llm_config`（环境变量 `NC_EMOTION_LLM_CONFIG`）可指定独立 JSON 配置文件：

```json
{"model":"deepseek-v4-flash","api_key_file":"/etc/neuralcloud/emotion-deepseek.key"}
```

密钥文件必须是绝对路径，内容为单独一行密钥；配置文件与密钥均应由服务账户持有、权限 0600。Linux 在 `deploy.json.bindings` 中设置该配置路径。只将情绪评估和情绪指导路由到官方 `https://api.deepseek.com`，主模型与其他辅助任务沿用原配置。未设置时保留原有辅助模型路由。

情绪调用关闭 thinking，使用 JSON object 模式，将 schema 放入系统提示，继续由现有情绪类型及语义校验验证结果。原有阶段超时、整轮剩余预算和降级逻辑继续生效。独立通道允许与主模型使用相同模型名和不同密钥。

真实服务器合成输入验证：评估 1.13 秒、指导 1.23 秒，两阶段状态均为 model；100 项专项测试通过。已发布 `20260917-emotion-deepseek-v1`，六个业务服务均为 active/running。上线后前四次真实情绪调用全部成功，约 1.14–1.34 秒；长期稳定性仍待观察。单次探针不代表长期延迟或质量验收。

当前状态：工程接线与独立 Linux 模型部署已完成，候选配置尚未通过发布验收。真实探针仍有旧刺激重复归因的风险，需结合上下文校准；独立盲评和 QQ 在线端到端延迟尚未完成。

## 工作方式

Runtime 在角色表达前发送 `ObserveEmotion`。Cognitive 从当前人格、Runtime 冻结的七维内稳态、当前会话真实消息构建输入，Qwen 评估新增事件，程序合成动态占比，再由 Qwen 写出演绎提示。主模型收到唯一 `emotion_guidance` 段。

第一次 Qwen 读取完整的受限会话窗口与人格投影；第二次只读取冻结 Frame、简短人格特质及本会话已提交的评估摘要。第二次不再读取原始对白或长篇台词习惯，以免小模型误把任务理解成扮演角色。输出严格的 `feeling / delivery / needs` 三字段，程序按主导来源排列并拼成不超过300字的一段提示。辅助模板版本为 `emotion-director-v4`，Runtime 架构版本仍为 `qwen-emotion-v1`。

覆盖本地对话、QQ 群聊/私聊、自主判断与表达、留言板及能力/目标决策中直接生成的语音脚本。语音脚本仍遵守既有文本、style、strength 和身体权限契约。

性格描述通常习惯。强烈且有依据的情绪可以暂时让措辞更直接、短促或激烈；身份、既有事实、知识边界及核心价值继续有效。系统没有新增饥饿机制，低精力也不能被表述为饥饿或疼痛。

## 动态占比

- 内稳态 `H`：七维归一化偏离乘可信度的最大值；Runtime 投影保留全部七维和对应 hard bounds。
- 性格 `P`：默认基线 0.25，也作为 Qwen 理解事件的条件。
- 对话 `D`：全局短期情绪各类别余额的最大值。
- 三路占比为 `s³ / Σs³`，整体激活强度为 `max(H,D)`。零刺激保持平静。
- 每个新事件只应用一次：`q=intensity×confidence`，同类别更新 `A=1−(1−A)(1−q)`。
- 各类别独立保存，半衰期默认 600 秒。明确的修复证据可削弱相应余额，每个事件最多 50%。

例如 H=0.30、P=0.25、D=0.95 时，对话约占95%；H=0.95、P=0.25、D=0.20 时，内稳态约占97%。参数是可标定的工程初值，不代表已验证的心理学测量。

## 来源、窗口和提交

近期文本按会话隔离，默认20条/30分钟，当前触发消息优先。Qwen 另有输入预算，过大的输入使用有记录的降级。窗口移出不清除情绪余额，跨会话只共享类别、强度和时间，不共享其他会话的人物、原话或原因。

QQ 从核验后的原始观察及真实发送回执读取窗口，排除检索到的远期历史与草稿。自己的送达内容只充当解释上下文，不产生新的外部情绪增量。

`ObserveEmotion` 的输入在调用模型前持久化；原始评估、余额与 Frame 原子提交在现有 Cognitive 状态文件的 `emotion` 区块，保留修复证据与评估哈希。Frame 记录各类别情绪的上升、稳定或恢复趋势。真实刺激独立于发送结果存在，因此 wait、生成失败和发送失败不回滚情绪。第二次 Qwen 的提示在主模型生成前缓存，改写、续句、重试复用。同来源 ID/内容冲突被拒绝。旧状态文件初始化为空情绪状态。

原有 Homeostasis 的确定性状态更新、critical 暂停与研究条件继续独立运行。情绪组件不会自动写回 Homeostasis；grounded-agency 研究 manifest 要求 emotion_mode=off。

## 配置和部署

已有 `NC_AUX_LLM_BASE_URL`、`NC_AUX_LLM_MODEL`、`NC_AUX_LLM_API_KEY` 供两次 Qwen 调用复用。启用独立 Qwen，避免辅助调用回到主模型。共享 Cognitive 的模式由 Runtime manifest 冻结，独立开发入口使用 `NC_EMOTION_MODE`。

提供两个基于现有 shared-misty-regulation 的配置：

- `presets/runtime/shared-misty-emotion-shadow-v1.json`：保留旧主模型输入，生成和缓存情绪结果供对照。
- `presets/runtime/shared-misty-emotion-on-v1.json`：启用新提示，prompt_version 为 `qwen-emotion-v1`。

两次调用默认最多8秒、4秒，并预留正文生成时间，服从原回合 deadline。自主判断后的正文生成及传输重试也沿用冻结的模型时限。JSON不合法、来源不匹配、超时或预算不足时使用混合可信需要、共享心情与恢复趋势的语义降级提示；诊断记录具体原因。配置、模型或人格重建后，共享服务继续绑定同一份情绪状态。

服务端 JSON Schema 约束和程序严格校验共同生效，不在输入正文重复整份 schema。文本校验还拒绝角色台词、机制术语与数值、无依据身体症状、决定停止对话等越界指示，以及主导需要或明显心情的遗漏。最多两种余额至少0.15的心情进入覆盖检查，包括平静中仍有余怒的情况；情绪余额下降须明确表示减弱，不能只说“愤怒恢复”。主导需要正在缓和时也要明确体现。校验版本 `emotion-guidance-guard-v2` 独立进入诊断与评测配置散列。这些检查是可复现的词语覆盖防护，不能替代独立的表达质量判断。

远端独立 Qwen 的安装、集显配置与仅本机监听的 SSH 转发见 [Linux 辅助模型部署](AUX_QWEN_LINUX_20260915.md)。本地 Ollama 无需密钥时将 `NC_AUX_LLM_API_KEY=not-needed`，避免继承主模型密钥。

部署顺序：配置可用Qwen端点 → 隔离环境验证 → shadow对照 → 完成盲评和延迟门槛 → 切换on manifest并重启对应实例。回滚使用原manifest；不要清除情绪/命令账本。应用现有launch绑定流程切换配置，勿并行启动第二个正常角色实例。

## 验收

测试覆盖强刺激、积累、恢复、静默衰减、重启、跨会话来源隔离、同事件重放、失败生成、部分回执、旧提示移除、critical及opaque隔离。真实模型评测需另外运行，离线假模型通过不等于表达效果或在线P95达标。

沿用既有独立配对盲评：4–6轮连续情境；状态适配提升至少0.5/5且差值置信区间下界大于0，人格/自然度非劣界−0.25，QQ端到端P95不超过30秒。比较旧链路、新规则降级、新Qwen，并记录固定/动态占比消融。保留实际模型、输入/提示哈希、Frame、模式、版本、耗时和降级率。

## 本次工程验证（2026-09-15）

- 最终情绪、Runtime、QQ、留言板、辅助路由及评测专项 **152 passed**，Ruff通过，保留 [最终 JUnit](milestones/evidence/qwen-emotion-regression-final-2026-09-15.xml)。此前474项相关组合回归以及额外QQ/记忆验证与该范围重叠，不相加。
- 旧链路／规则降级／脚本 Qwen 共54条 [最终离线对照](../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-offline-guard2-final-20260915/summary.json)。
- 4个场景、每个5轮、固定／动态两种占比，共40条 [最终离线多轮对照](../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-multiturn-offline-guard2-final-20260915/summary.json)。两组使用相同评估输出和主辅模型可见历史；脚本正文不证明真实表达收益。
- 增加已发表留言板回读、QQ分段回执稳定标识、协议时钟偏差、零有效信号、四种表达入口重启后的原始deadline、主次心情遗漏及角色台词拒收回归。QQ回忆证据桥对可信记录和主模型记录使用相同情绪投影后仍严格比对，文本与来源篡改继续被拒绝。

早期真实探针发现无 schema 约束、导演生成台词、遗漏混合情绪及主模型缺少多轮历史等问题，失败原始记录均保留；见 [评测说明](harness/17-qwen-emotion-director-evaluation.md)。最终验证与部署记录汇总在 [实施证据](milestones/evidence/qwen-emotion-implementation-2026-09-15.json)。

最终一批 [v4真实工程对照](../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-full-live-v4-20260915/summary.json) 使用 Qwen 4B 与 DeepSeek V4 Flash，四场景各五轮、两臂共40条；实际60次辅助调用、40次主调用。包含1次导演超时、1次真实评估漏报源事件（另一臂回放同一失败）、1次主输出预算耗尽且正文为空。两段辅助调用平均约4.43秒／3.19秒，这些不是 QQ 端到端 P95。

该批旧校验接收39条导演输出；逐条审阅后补上机制词与恢复方向校验。[离线重审](../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-v4-post-guard-audit-20260915.json)进一步拒收其中14条，25条仍通过当前规则，替代降级提示均通过校验。重审没有重新生成主模型正文，不能把它写成新版本真实表达验收。后续应重新生成候选、校准刺激归因，并完成旧链路／规则／Qwen的独立盲评。

现有正式角色实例保持原配置；新 on 预设的发布门槛尚需真实表达盲评和 QQ 在线端到端延迟证据。
