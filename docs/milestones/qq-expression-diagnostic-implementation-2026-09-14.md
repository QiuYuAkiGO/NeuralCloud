# QQ 情绪表达诊断实施记录（2026-09-14）

> 状态：工程候选、离线流程与首轮真实云端 pilot 采样已完成；真实 QQ 历史样本归因、独立人工确认和发布验证未完成。
>
> `release_eligible=false`。本报告中的合成 HTTP 捕获和 offline fake 结果均不构成模型表达效果证据。
>
> 依据：[主模型情绪表达与参数语义调研计划](../harness/16-emotional-expression-diagnostic-research-plan.md)。工程事实与未完成的效果验证分别记录。

交付摘要：[本轮采样、费用与验收状态](evidence/qq-expression-pilot-2026-09-14.json)。

## 1. Git 同步与本地工作保护

已完成远端同步，合并提交为 `6c7a473`，两个父提交分别为本地 `1c7d4a0` 和远端 `3442ed5`；双方原提交均被保留。远端新增研究计划，并更新 README 与 PROJECT_PROGRESS 的入口和记录。

同步前备份位于 `C:\Users\22850\.codex\artifacts\qq-expression\20260914T124219Z\sync-backup`。其中 `post-pull-verification.json` 记录核验了 16079 个非目标文件，`non_target_git_diff_unchanged=true`。`.env.example` 的变化为换行归一化，其规范化后的 Git diff 与原备份一致。

README 与 PROJECT_PROGRESS 在恢复本地改动时发生冲突，已保留双方内容完成合并；`stash@{0}` 仍保留恢复点。本次新代码当前未提交、未推送。此报告只新增本文件，不再修改上述冲突文档。

## 2. P0 已取得的证据与缺口

### 2.1 配置审计

[配置审计](evidence/qq-expression-configuration-audit-2026-09-14.json) 是只读磁盘配置快照，证据类型为 `read_only_disk_configuration_not_process_attestation`。它记录了工作副本 Persona、活动配置文件、manifest、profile、snapshot 与 summary 的散列，没有用 builtin Persona 替代工作副本。

该快照中的配置请求 model 为 `deepseek-v4-flash`，端点为 `https://api.deepseek.com`，表达模式为 `responses`；snapshot 与 summary 的 revision 同为 14660。该次磁盘审计没有取得响应 model，也未确认 provider 默认值是否生效。这些字段不能证明正在运行的进程采用同一配置，也不能证明供应商后端版本。后续隔离 pilot 的响应身份单独记录在第 5.2 节，不回填或覆盖这份历史快照。

未导入历史最终 HTTP payload，用户标注“平淡”的样本数为 0；普通背景回复样本也未形成可审计请求包。真实历史回复请求仍为 `payload_unverified`，不能据磁盘配置或 GUI 状态给用户反馈作模型能力归因。

### 2.2 修复前后合成捕获

修复产品接线前先保存了 [baseline](evidence/qq-expression-wiring-baseline-2026-09-14.json)，随后保存 [candidate](evidence/qq-expression-wiring-candidate-2026-09-14.json)。两份文件分别记录捕获时的相关源文件 SHA-256，baseline 未被候选结果覆盖。

捕获执行真实代码链：`RuntimeSupervisor._dispatch_qq_turn → CognitiveServiceHost.prepare_qq_response → decide_qq_reply → InferencePipeline → OpenAICompatClient`。HTTP 边界使用 `httpx.MockTransport`，保存实际构造的请求正文；QQ 来源、身份和正文均为匿名合成 fixture，响应为脚本结果。来源有效性检查显式 stub，辅助模型采用 fake，`external_side_effects=0`。因此它证明序列化与接线行为，不证明真实 QQ 原始数据、网络交付或模型情绪表现。

三条路径的可见性结论分别为：

- **QQ Direct 群聊**：baseline 中 Runtime 命令没有顶层内稳态 summary，最终生成请求也没有 typed summary 编译的必需 system segment。candidate 中两处均存在，并来自共享 Runtime 的同一 revision。
- **QQ Direct 私聊**：新增回归覆盖 `responses`、`full` 下的受信 summary、最终 system segment 与 revision 一致性，保留私聊作用域隔离。已落盘的前后捕获各只有两条群聊记录；本报告不把群聊 baseline 当作私聊修复前证据。
- **QQ desktop 群聊**：baseline 已携带上游 summary，并可在用户消息中的 QQ 上下文投影里出现，但没有作为 typed summary 传入 pipeline 的必需 system segment。candidate 补齐后者。这里的缺口不能描述为“原请求完全没有任何状态文字”。

两份群聊捕获中，每条路径均有两次主模型 HTTP 请求，即 gate 和最终表达；候选没有为该合成场景增加主模型 HTTP 调用。candidate 同时保存阶段诊断事件，能区分 `qq_gate` 与 `qq_persona` 下的 `generate`。该计数不代表真实系统全部 helper、重试或修订的固定调用数。

## 3. 已实现的最小 QQ 接线候选

[RuntimeSupervisor](../../harness/runtime/supervisor.py) 在新 QQ 命令首次受理时，仅在现有 `responses`/`full` 模式下从共享 Runtime 获取受信摘要；body 提供的摘要不能覆盖该来源。命令 payload 与 version vector 使用同一内稳态 revision。`off`/`shadow` 不向表达链注入摘要，critical 与不匹配的 profile 在模型调用前被阻止。

已受理命令重试继续复用原冻结 envelope、summary 与 version vector。后续状态变化，包括变成 critical，不会把同一历史命令重新绑定成新请求，也不会让重试再调用模型。已有截止时间、幂等键和缓存指纹机制保留。idle 请求还需验证其冻结摘要与受信状态一致；失效或在关闭模式下的 idle 不能被重新绑定成一次新的主动机会。

[CognitiveServiceHost](../../harness/cognitive/service.py) 复用同 revision 摘要校验，并由 [QQ decision](../../harness/qq/decision.py) 将 typed summary 传给 `pipeline.prepare`。状态获得者仍是共享 Runtime，未新建第二个状态 owner。

候选保留原有权限与来源检查、私聊隔离、心理状态作用域过滤、Persona 和状态更新算法。没有直接删除 `psych=None`，也没有把共享心理或关系状态无条件灌入 QQ。此次接线修复不等于全部心理、关系或角色经历已进入 QQ 最终请求；这些字段仍需按合法作用域逐条取证。

## 4. 已实现的诊断基础设施

[HTTP 诊断观察器](../../infra/trace/diagnostics.py) 与 [LLM 客户端](../../harness/llm/client.py) 增加显式 opt-in 的请求/响应证据采集。默认不开启原文采集；启用后记录实际序列化正文、SHA-256、请求与响应身份、实际参数、可见正文、finish reason、数值用量及耗时。HTTP header、凭据、隐藏推理内容、tool arguments 和异常正文不进入证据。

准备事件标为未验证，获得响应后才保存对应 HTTP 请求证据。observed 模式拒绝可能修改请求的 request/response hooks 和自定义 auth，并在发送前失败；证据 sink 失败也不能继续发送。默认无 observer 的调用行为保持原样。

[Pipeline](../../harness/pipeline/pipeline.py) 的诊断事件覆盖编译上下文、初稿、修订、Validator 结果和最终文本。QQ scope 记录通道、transport、群聊/私聊类别、summary/profile revision 与散列、Persona 散列，不把真实会话 ID 或私有上下文塞入诊断 metadata。合成修订测试证明这些阶段可以关联，尚未证明真实 Validator 削弱了表达。

[隔离诊断包](../../eval/emotional_expression_diagnostics/README.md) 已提供 `preflight`、`audit`、`pilot`、`judge`、`analyze` 工作流，包含冻结配置、条件 hash、信息清单、独立理解探针、盲包与人工评分导入、按独立场景聚合的配对分析、bootstrap 区间和 Holm 校正。

`A-prod` 仅接受身份已核验的原始 HTTP 正文，并保持字节重放；合成生产渲染器条件明确称为 `A-synthetic-proxy`。B/C/D 在发送前标为 `manipulated_pending_http`。A 仅是产品参考，分析不将其与中性模板的差异当作受控因果结论。若真实 A 缺少兼容的有界 token cap，runner 拒绝该请求，不会偷偷添加 cap 后声称原样重放。

预算总上限为 50 元，包含未结算预留在内达到 45 元停止接纳新请求；每次发送前预留完整输入 envelope 与输出上限，未知费用继续占用预留。runner 拒绝多 completion、双重输出 cap、tools 和未知计费字段，并检查完整序列化正文。真实 HTTP client 必须显式使用 `live=True`，通过冻结授权/价格预检，且 endpoint 一致、配置证据 sink；离线入口只接收 fake client。

真实运行的失败接纳策略同时冻结于 manifest 与最终 summary：首次 HTTP、传输、证据或身份错误停止接纳新请求；连续 3 个无效响应也停止，最多 4 个已在途请求继续收齐。没有自动重试、改变生成规格或更换 ledger。HTTP 错误也先保存安全的 status、identity 与数值 usage，随后关闭 response；没有可结算用量时保留整笔预留。每笔预留保存自己的输入 cap。model 或已知 system fingerprint 漂移会停止，合法 schema 下探针答案错误继续计分，不等同于请求失败。

诊断 runner 没有 QQ 交付、生产记忆写回或身体动作路径。`ga_opaque` 不接受语义表达处理；这些隔离测试不能替代完整 Grounded Agency 研究门禁。

## 5. 已执行的验证

### 5.1 工程回归与先行离线流程

- QQ 受信摘要契约与新增接线回归：**43 passed**，覆盖 Direct 群聊、Direct 私聊、desktop 的 `responses`/`full`，关闭模式、critical、profile 不匹配、受理后重试、idle 一致性及修复前证据。
- HTTP 证据与公开 runner 门禁专项复核：**4 passed**，验证 request hook、response hook、auth 均在 observed 模式发送前失败，默认无 observer 行为保留，真实 client 无法借离线默认参数发送请求。
- 诊断核心测试：本次离线流程汇总记录 **24 passed**，ruff 通过。上述测试集合可能重叠，不能相加为全仓测试总数。
- 真实调用安全专项：[独立测试](../../tests/test_expression_live_safety.py) **19 passed**，全部使用 MockTransport，覆盖错误证据、停止接纳与在途收齐、预算预留、身份漂移、无效响应阈值及探针答错的区分；相关文件 Ruff 检查通过。

完整离线 CLI 已依次执行 `preflight/audit/pilot/judge/analyze`，五个命令 exit code 均为 0。私有本地证据为 `eval/reports/emotional_expression_diagnostics/private/20260914-211654-offline-validation/offline-validation-summary.json`，其结果为：

- 计划 216 次，实际完成 204 次，包括 168 次合成生成与 36 次独立探针；失败 0。
- 跳过 12 次不可见对照，涉及同档 intensity 与平静/证据不足的 A-proxy；相同可见输入没有送去“验证模型理解失败”。
- 导出盲包 168 条，人工评分 0；分析结果为 `inconclusive`。
- `live=false`，支出与预留均为 0，`release_eligible=false`。

这一阶段的 204 次是 offline fake 调用，该阶段新增 DeepSeek 请求和费用均为 0。后续真实 pilot 的调用与费用见下节；两批证据不合并计为一次模型实验。

### 5.2 已封存的真实云端 pilot

真实运行目录为 `eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/`。封存结果见 [run-result.json](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/run-result.json)、[pilot-results-summary.json](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/pilot-results-summary.json) 与 [descriptive-summary.json](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/descriptive-summary.json)。配置 SHA-256 为 `e993bc1f7dd9b2cca154ee68a6877f6520dafd8982740c1db9f2408a56b753b4`，冻结后未为改善结果重试或改变条件。

运行于北京时间 2026-09-14 21:52:48 至 21:56:53，端到端耗时 **244.3 秒**。[运行 manifest](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/run-manifest.json) 记录请求端点 `https://api.deepseek.com/chat/completions`、请求 model `deepseek-v4-flash`，实际响应 model 均为 **`deepseek-flash`**。响应返回一个稳定的 system fingerprint，`backend_identity_drift=false`；这只表示该时间窗内返回的身份字段未出现漂移。

实发配置固定为 `thinking=enabled`、`reasoning_effort=high`、`temperature=0.7`、`max_tokens=8192`。记录已发送参数不等于证明各参数对 provider 的实际效果；本次没有 enabled/disabled 模式对照，也没有供应商对照。

- 6 个独立场景族、12 个变体，主条件各重复 3 次；计划 216 次请求，实际 **204 次全部完成**，包括 **168 次生成、36 次独立探针**，runner 记载的失败 **0**。这里的失败计数涉及调用与响应有效性，不表示已经人工确认事实、行动或表达违规为零。
- 另有 **12 次不可见对照被跳过**：同档 intensity 与平静/证据不足场景中的 A-proxy 输入未形成可见差异。实际请求数未把这些排除项算作模型失败。
- Provider 报告输入 **109951 tokens**、输出 **136164 tokens**，合计 **246115 tokens**。输入中 cache hit 为 57338、cache miss 为 52613；这些是用量字段，不是可见正文长度或情绪质量评分。
- Ledger 按冻结的保守单价计入 **1.309214 元**，未结算预留 **0 元**，风险敞口同为 1.309214 元，低于 45 元停止线。该值是诊断账本的保守 token 费用估算，不是供应商账单结算凭证；没有把缓存优惠另行假定为已兑现退款。
- 共形成 **168 条盲样本**，当前独立人工评分 **0**，168 条均缺两位人工评分。统计输出为 `inconclusive`，`release_eligible=false`。

独立探针按冻结契约严格匹配：B0-values 为 **11/12**，B1-defined 与 C-verbal 各为 **12/12**。三个条件在主体、方向、趋势和已证实原因四类项目上均为 12/12；唯一差异是 B0 的 unknown_fields 一次未匹配。这是小规模描述性结果，不能据一次差异确认补定义有效。

[独立证据审计](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/independent-evidence-audit.json) 对 204 对请求/响应逐一核验正文散列、冻结 payload、实发字节、响应内容和用量，证据错误为 0。唯一探针差异是 B0 将 `hint_intensity` 额外列为未知；该条件移除了定义，保守处理具有合理解释，不能把它直接归为模型误解。原始计分仍保留 35/36，没有事后改答案。

按运行前冻结的官方闲时价格与缓存用量，诊断 token 费用另估为 **0.59841576 元**；[审计附录](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/independent-evidence-audit-appendix.json) 复算通过。这仍不是供应商账单，预算账本继续保留更保守的 1.309214 元。没有通过共享账户余额变化推算本次费用。

Git 提交只标识同步基线，不能单独复现本次未提交的诊断代码。[源码归档清单](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/source-snapshot-manifest.json) 与同目录 `source-snapshot.zip` 保留 540 个源码、测试、schema、builtin preset 和冻结规格文件，以及依赖版本；归档内文件散列和 ZIP CRC 均已核验。12 个关键源文件与请求前冻结散列全部一致；其余源码明确为运行后归档，不能冒充请求前完整代码快照。认证、凭据、生产状态和真实聊天数据不在归档范围内。

这些探针主要检验给定字段的提取、目标带比较与显式未知信息处理。它们没有直接覆盖字段含义复述、强度解释或两个状态的偏离幅度比较，也不能证明正式生成利用了这些信息。因此，接近满分的探针结果不能排除 H1，更不能替代情境适配、人格、自然度和事实边界的人工评分。

本轮输入仍为匿名合成 QQ fixture，A 条件使用 builtin Persona 与生产 renderer 构成的 **A-synthetic-proxy**，不是历史真实 QQ 正文请求，也不是工作副本 Persona 的实际 A-prod 重放。生成采用 `raw_replay_no_validator`，没有执行真实 QQ 的 Validator 或交付链；本轮结果不衡量候选接线在生产对话中的最终收益。

后续人工评分使用 [中文人工盲评说明](../../eval/emotional_expression_diagnostics/HUMAN_REVIEW.md) 与本轮 [盲评补充情境](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/BLIND_REVIEW_CONTEXT.md)，两位评分者分别校准、独立填写并保留原始分歧。当前没有可用于估算表达适配效应、pilot 评分方差或确认集功效/样本量的人工数据；不以回复长度、token 数或探针正确率代替这些量。

用户已确认暂不安排两位人工评分者，先交付本轮 pilot。人工评分、基于方差确定确认集规模和效果确认均记为未完成；本轮到此封存，不自动扩样。

## 6. H0–H6 当前证据边界

以下名称和判定口径逐项对应源研究文档，工程机制结论与用户投诉的线上根因分别陈述。

- **H0 输入接线或陈旧状态**：合成生产代码链的接线缺口为 `supported`，最小候选已修复并通过对应回归；用户真实反馈是否由该缺口或陈旧状态造成仍为 `inconclusive`。未将最新状态强加给已经受理的历史重试。
- **H1 参数语义不明确**：`inconclusive`。真实探针 B0 为 11/12、B1/C 各为 12/12，但主要测量提取与目标带比较，唯一差异在一次 unknown_fields；没有人工表达评分。不能确认定义补充有实质收益，也不能据探针高分排除正式表达中的语义理解或利用问题。
- **H2 投影丢失强度或趋势**：同档 intensity 对子在当前生产 renderer 下成为相同可见输入的结构事实为 `supported`；新增信息是否改善真实表达仍为 `not_tested`。没有修改生产 renderer，也没有把假响应差异当作投影收益。
- **H3 人格或表达指令过度压制**：`inconclusive`。D-example 已有真实生成，但缺少独立人工配对评分；E-style 未执行，生产 Persona 未按实验条件修改。当前未证明示例或“克制但可辨”能带来真实改善。
- **H4 生成/校验/交付削弱**：`inconclusive`。合成测试证明可关联初稿、修订与 Validator 原因，但本次真实 pilot 是无 Validator 的原始回放，尚无真实阶段配对样本可判断削弱是否发生。
- **H5 端点与生成模式影响**：`not_tested`。本轮已取得 `deepseek-flash` 响应身份及稳定指纹，但只使用一个端点与固定模式。身份事实不能证明模式效应，也不能认定关闭思考或其他供应商会更自然。
- **H6 表达能力/后训练不足**：`not_tested`。H0–H5 尚未完成实测排查，当前没有支持切换模型、SFT/LoRA/DPO 或其他后训练的效果证据。

## 7. 尚未达到的完成条件

P0 已完成代码路径和合成 HTTP 接线审计，但缺真实投诉样本与历史请求身份；P1 已完成隔离工具和离线流程验证。P2 首轮付费 pilot 采样与有限探针统计已完成，人工评分及基于评分选择待确认假设尚未完成；P3 独立人工确认未执行。P4 已形成最小接线代码候选，P5 仅完成相关工程回归，尚未完成候选发布验证。

仍需保留原表达门禁：九类条件、每类至少 30 对、独立 fresh-state soak、critical 零调用探针及相关 QQ/GA 事实与动作回归。离线替换输入、fake、模型辅助评分或旁路实验都不能替代这些条件。本次未部署、未重启运行实例、未向 QQ 发送消息，未宣称情绪表达改善、自主能力支持或产品发布通过。

## 8. Cursor 接入与最终集成复核

已定位 Fable 最初不可用的三个独立原因：原生 Node 未继承现有 Windows 系统代理，导致实际 CLI 仅返回 23 个模型且缺少 Fable；Windows 不支持 CLI 的 OS sandbox；隐藏参数 `--exclude-workspace-context` 被服务端拒绝。没有修改系统代理或账户隐私政策。

可复现入口、源码隔离审计和成功记录见 [Cursor 接入与复现](../../eval/emotional_expression_diagnostics/CURSOR_TROUBLESHOOTING.md)。最后从独立新进程运行只读预检，再次确认两模型均可选，结果保存在 `cursor-final-preflight-20260914T132700Z/cursor-preflight.json`。

适配器通过 [Cursor 官方代理配置](https://cursor.com/docs/cli/reference/configuration) 在子进程继承现有代理，并在临时配置中使用 HTTP/1.1 SSE。实际 `agent models` 随后返回 223 个模型，包含 `claude-fable-5-1-high`。Windows 子进程使用 allowlist 模式、空工具列表与全部工具 deny；新建盘根隔离 workspace、home/config/data/cache，检查祖先自动加载路径，移除不受支持的隐藏参数。正常认证仍由官方 CLI 读取，未复制或输出凭据。

Fable 的隔离合成评审已实际完成：`cursor-fable-isolated-smoke-20260914T132215Z/aux-ratings.jsonl` 记录 process exit 0、结构化评分、无工具事件及 workspace 未写入。请求 canonical 为 `claude-fable-5-1`，CLI variant 为 `claude-fable-5-1-high`，selected label 为 `Claude Fable 5.1 300K High No Thinking`。相对证据根目录为 `eval/reports/emotional_expression_diagnostics/`。

CLI 选定标签尚不能核验供应商实际 serving model；`actual_model=null`、`identity_status=unverified`、`fallback=unknown`。因此该记录证明指定型号的 Cursor 调用已恢复，仍不计为身份核验通过的正式 Fable 评审。模型辅助评分不能填补两位独立人工评分的缺口。

Kimi K3 也完成一次独立合成评审，证据为 `cursor-kimi-isolated-smoke-20260914T132339Z/aux-ratings.jsonl`；CLI variant 为 `kimi-k3-high`，重点为中文语境贴合与自然度。两模型分别使用新会话，不读取对方评分。两次成功调用仅是辅助接入 smoke，没有把最小样本的分数当作 QQ 效果结论。Cursor 超额付费关闭已获用户确认；后续通过只读 `aiserver.v1.DashboardService/GetHardLimit` 获得明确的 `noUsageBasedAllowed=true`，封存后批次启动前最近核验时间为 13:59:07 UTC。所有调用限现有套餐额度，没有修改费用设置。

冻结前材料评审共调用两模型各 3 次，6 次均返回结构化内容，其中 1 条保留旧适配器后处理错误记录，正常完成状态为 5 条，没有重发该请求。记录见 [材料审阅汇总](../../eval/reports/emotional_expression_diagnostics/private/cursor-prefreeze-20260914/review-summary.json)。独立代码审阅结合辅助意见，补充主体来源、目标带闭区间和已确认原因的定义；未采纳删除已提供趋势、只给一个条件添加推导信息的建议。最终探针文案由 Fable 审过，Kimi 对应的是三项澄清前版本；材料版本和意见均未覆盖。

针对封存后的真实 pilot，按请求前规则选出 12 条样本，即每个场景族固定变体、首次重复的 B0/B1 各一条，并独立打乱顺序。最多 24 次辅助评审，实际 **13 次调用**，完成 **12 份 Kimi 评分**；Fable 第一次调用约 46 秒后以 `0xC0000409` 异常退出，stderr 为空，未返回结构化结果或 usage。Fable 因此按首次故障停止规则停用，余下 11 次未发，没有重试、改选或替代模型。它此前的成功调用证明最初可用性问题已修复，本次进程稳定性问题的根因尚未查明；缺少 usage 不能当作零额度消耗。

完整结果见 [封存后辅助评审汇总](../../eval/reports/emotional_expression_diagnostics/private/qq-live-pilot-20260914/postseal-cursor-reviews-20260914T135934Z/qualitative-summary.json)。Kimi 对这 12 条的中文贴合度均为 5，自然度均值 4.417、人格 4.833、适配 4.75、可辨性 4.0；仅为单一辅助评审者的描述性分数，没有按条件估计效应。两条被标记为可能虚构环境事实，引用分别为“窗边那盆有点蔫的植物”和“外面好像有风，降温了”；原始引用与分数已保留，尚未经人工裁定。机制、生理和过度表达未标记，不等于这些问题被证明不存在。

Kimi 返回用量为 input 46628、output 11750、cacheRead 28、cacheWrite 0，批次墙钟约 523.7 秒；Fable 失败请求用量未知。两模型实际服务身份仍全部 `unverified`，正式核验评审数为 0；封存后有效双模型配对数为 0，无法估计二者分歧。辅助结果没有导入人工评分表，也不改变 `inconclusive` 和 `release_eligible=false`。

真实 pilot 前的一次集成回归命令覆盖诊断核心、Cursor 适配器、HTTP 证据、QQ 接线、共享内稳态与原表达评测：

```powershell
.venv\Scripts\python.exe -m pytest -q eval/emotional_expression_diagnostics/test_core.py tests/test_expression_cursor_review.py tests/test_expression_http_evidence.py tests/test_qq_expression_wiring.py tests/test_shared_qq_homeostasis.py eval/homeostatic_expression
```

结果为 **132 passed**，有两条既有 Starlette/httpx/AnyIO 弃用警告；相关修改文件 Ruff 检查通过。此结果是上述集合的一次联合执行，不与前文重叠测试相加，也不表示整个脏工作区已完成全仓回归。

随后补充可复现 CLI 入口测试，单独重跑 Cursor 适配器测试为 **35 passed**，该模块与测试 Ruff 检查通过。

真实 pilot 封存后的最终联合回归已纳入新增 `tests/test_expression_live_safety.py`，结果为 **159 passed**，有两条既有 Starlette/AnyIO 弃用警告；相关文件 Ruff 检查通过。这是最终联合执行的总数，不与历史 132、35、19 等重叠结果相加；它仍不是整仓脏工作区的全量回归或发布 PASS。

```powershell
.venv\Scripts\python.exe -m pytest -q eval/emotional_expression_diagnostics/test_core.py tests/test_expression_live_safety.py tests/test_expression_cursor_review.py tests/test_expression_http_evidence.py tests/test_qq_expression_wiring.py tests/test_shared_qq_homeostasis.py eval/homeostatic_expression
```
