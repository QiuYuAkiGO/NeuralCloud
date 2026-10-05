# JSON 模型调用迁移到 DeepSeek：范围与可行性

核查日期：2026-10-05。线上 release：`20261005-qq-parser-recovery-v2`。本轮读取源码、线上配置和无正文的请求元数据，并复核已有真实 API 实验；没有修改线上路由、重启服务或新增付费模型调用。

结论：文本 JSON 场景具备迁移条件，现有 DeepSeek Responses 适配可复用；但仅修改 `aux_llm_*` 无法覆盖全部入口。建议新增独立结构化调用通道，以 `deepseek-flash`、`reasoning.effort=none`、Responses `text.format=json_schema` 为默认方案。包含最终正文的 JSON 与图像输入分别验收。

## 当前实际路由

- 主模型：本地 `qwen3.8-27b`。`complete_chat*` 默认跟随主模型，QQ gate 即使返回 JSON，也走此路径。
- 通用辅助模型：`qwen3:4b-instruct-2507-q4_K_M`。`RoutingLLMClient.complete_json` 走 aux。
- 情绪：manifest 为 `emotion_mode=on`，独立配置使用官方 `deepseek-v4-flash`；近期请求元数据中 `emotion_appraisal` / `emotion_guidance` 确认使用该端点。当前适配器把 Schema 放进提示词，然后调用 JSON Mode。
- QQ 记忆整理：独立端点配置为官方 `deepseek-v4-flash`，控制文件的整理与召回均启用。配置解析函数的默认开关不能代替运行时控制文件。
- 日级记忆修订：独立 Kimi K3 配置，当前禁用。
- validator 与 LLM 检索规划当前禁用；协议 QQ 图片分析当前为 `off`。代码中存在入口不等于每轮都调用。
- 普通正文有 DeepSeek 备用端点，但备用机制不等于常驻结构化路由。

相关源码：[客户端路由](../../harness/llm/client.py)、[QQ 决策](../../harness/qq/decision.py)、[情绪独立端点](../../harness/emotion/endpoint.py)、[服务配置覆盖](../../harness/cognitive/service.py)。配置证据见 [脱敏核查记录](../harness/evidence/json-routing-assessment-20261005.json)。

## 场景清单

以下按业务用途合并列出，包含可选/研究入口；不把日志序列化、数据库 JSON、普通 HTTP JSON 接口误算为模型结构化输出。

| 场景 | 当前调用位置/路由 | 迁移判断与必要工作 |
|---|---|---|
| 上下文解析、fast/deep 复杂度判断 | `pipeline/parser.py`、`router/gate.py` → aux | 优先迁移；为旧提示定义独立输出 Schema。保留规则优先和恢复逻辑，固定内部指令不必新增外部请求 |
| 社交意图解释 | `subagents/interpreter.py` → aux | 可迁移；保留假设、置信度、事实与心理推断的区分 |
| 检索计划 | `memory/retrieval/planner.py` → aux | 可迁移，当前 LLM 开关关闭；不因换端点而启用 |
| 回复校验 | `validator/validator.py` → aux，超上下文时转 main | 可迁移，当前关闭；需同步调整 Qwen 分词与 8192 上下文导致的主模型转发逻辑 |
| 记忆候选、分层、人格视角 | `memory/writer/curator.py`、`memory/tier.py`、`memory/dual.py` → aux | 可迁移；补输出 Schema，保留原证据校验、规则回退和写入权限 |
| QQ 回复时机、回应计划、联网计划 | `qq/decision.py` → main | 第一优先级；已有 `_GateDecision` / `_ProductionGateDecision`，可使用现有 Responses 适配。正文继续单独生成 |
| 能力、目标、网页下一步 | `cognitive/capabilities.py`、`goals.py`、`web.py` → main | 可迁移；已有类型；执行仍由 Runtime 验证授权、参数、引用和剩余预算 |
| 生产选择、情绪/记忆提议 | `cognitive/production.py` → main | Schema 已接入；`ProductionChoice.text` 可包含最终正文。若要求 Qwen 继续写正文，需把选择与文字生成拆开 |
| 留言板、日记 | `cognitive/board.py`、`diary.py` → main | Schema 已接入；JSON 同时含行动和最终文字，整体迁移会把这些文字也交给 DeepSeek |
| Grounded Agency、终端 | `cognitive/grounded_agency.py`、`basic_terminal.py` → main | 已有 union Schema 与 DeepSeek 冒烟证据；是否执行仍取决于原有能力和授权 |
| 持续目标/计划、主体信念、身体因果判断 | `agency/planner.py`、`social_world/observations.py`、`body_belief/appraisal.py` → 注入客户端的 chat 调用 | 可迁移；目前主要把 Schema 写进提示词，需显式接入结构化入口；逐一验证引用和提议的业务含义 |
| 情绪评估与演绎 | `emotion/service.py`、`emotion/endpoint.py` → 独立 DeepSeek | 已使用 DeepSeek；后续重点是从 JSON Mode 升级为 Responses Schema，保留 8/4 秒等现有阶段时限 |
| QQ 记忆整理 | `memory/qq_assimilation.py` → 独立 DeepSeek | 已使用 DeepSeek；统一非思考与 Schema 路径，保留归属、source_ids 动态约束和已存任务的恢复语义 |
| 日级记忆清洗、修订、审查 | `memory/revision/provider.py` → 独立 Kimi 客户端 | 可做独立适配；当前直接发送 Chat Completions `json_schema/strict`，不能假定只换 URL 就能复用。需保留队列、检查点、重试与计费 |
| QQ 图片分析、旧桌面定位 | `qq/direct_images.py`、`qq/grounding.py` → 多模态客户端 | DeepSeek Flash 官方支持视觉，但项目现有 Responses 映射仅接受文本。需增加图像转换，并独立检验 OCR、身份归属和坐标正确性；当前协议图片分析关闭 |

排除项：`cognitive/autonomous.py` 的旧自主评估解析 `silent/speak` 首行及正文，不是 JSON；向量 embedding、OCR 工具本身、角色纯文本回复也不能仅因使用 HTTP JSON 载荷而归入本次迁移。

## 为什么改辅助配置不够

1. 方法路由按 `complete_json` 与 `complete_chat_result` 区分，而非按业务结果是否 JSON 区分。QQ gate、目标等都在主模型路径。
2. `decision_format()` / `main_settings()` 只检查主客户端；主模型为 Qwen 时，会选择 JSON Mode，无法自动使用另一个 DeepSeek 的 Schema 接口。
3. 旧 `complete_json` 在未指定 `response_format` 时不会自动启用 JSON Mode；只换模型仍可能只靠提示词约束。
4. aux 客户端构建无条件注入 Ollama 的 `keep_alive/options`。迁移应按 provider 构建请求，不能依赖外部 API 忽略这些参数。
5. validator 的超上下文转主模型规则、aux 预加载和旧 512-token 默认限制都带有本地模型假设。要按用途设置输出预算，避免思考或截断耗尽 JSON 正文预算。
6. 情绪、QQ 记忆、日级修订分别建立独立客户端，中央 main/aux 调整不能自动覆盖它们。

推荐接口为显式 `complete_structured(messages, schema, purpose, deadline, ...)`。`purpose` 决定预算与记录标签；供应商返回后依次检查完成状态、严格 JSON、Schema/Pydantic 和业务证据。保留现有 HTTP 分发计量、故障计费、总 deadline、权限和持久化边界。正文生成继续使用专门的文本入口。

## 已有实测能支持什么

- [10 个项目 Schema 的实际 API 冒烟](deepseek-structured-integration-20261005.md)：10/10 通过原 Schema 与 Pydantic 校验，完整调用约 0.415–0.908 秒。合成输入只要求等待或不行动；不等于复杂行动质量通过。
- [64 条冻结合成决策](clm-deepseek-benchmark-20261005.md)：DeepSeek none 结构有效 64/64，按冻结标签 62/64；low 63/64 结构有效、61/64 标签符合，一次思考耗尽预算没有正文。它支持先用 none，但不是生产准确率承诺。
- [输入缓存对照](deepseek-cache-optimization-20261005.md)：ProductionChoice 优化组 24 次加权命中率 92.48%，预热后 92.69%；p50 没有改善。这个结果不能套用到所有 JSON 场景。
- 刚完成的 QQ 隔离验证中，Qwen gate 约 6.55 秒。它和上述 DeepSeek 小输入冒烟的输入不同，不能据此声称迁移后必然节省约 6 秒。

本轮未新增模型请求。下一步应使用同一组冻结输入对比当前通道和 DeepSeek：既测 wait/no_action，也测 speak、合法动作、无授权、错误/撤回引用、私聊作用域、长历史、截断、429 与超时。统计结构合格率、语义合格率、完整耗时 p50/p95、输入/输出/cache tokens 和每个有效结果成本。将故障计入结果，不能以重试后的成功覆盖第一次失败。

## 官方接口与费用

截至本次核查，官方建议模型名 `deepseek-flash`，对应 V4.1-Flash；旧 `deepseek-v4-flash` 仍接受但转由新模型服务。Responses 支持 `text.format=json_schema`，`reasoning.effort=none` 关闭思考。JSON Mode 仅解决 JSON 格式，官方仍提示可能空白或因输出上限截断。[Responses API](https://api-docs.deepseek.com/api/create-response/)、[JSON Mode](https://api-docs.deepseek.com/guides/json_mode/)、[模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

Flash 每百万 tokens 的官方人民币价格：

| 类型 | 空闲时段 | 高峰时段 |
|---|---:|---:|
| 输入缓存命中 | ¥0.02 | ¥0.04 |
| 输入未命中 | ¥1 | ¥2 |
| 输出 | ¥4 | ¥8 |

按每次 4,000 输入、300 输出 tokens 的假设计算：

| 每 1,000 次请求 | 空闲时段 | 高峰时段 |
|---|---:|---:|
| 输入全部未命中 | ¥5.20 | ¥10.40 |
| 输入 90% 命中 | ¥1.672 | ¥3.344 |

这是指定 token 数的算例，不是线上账单预测；一条用户消息可能产生多次决策、情绪和记忆调用，长历史与重试另增费用。当前日志抽样不足以推算全项目日调用量；应按实际用途记账。缓存由供应商尽力提供，不能保证命中比例。[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[缓存说明](https://api-docs.deepseek.com/guides/kv_cache/)

## 推荐实施顺序

1. 新建独立结构化通道；先接 QQ gate 与仍实际调用的旧 JSON helpers。保留 Qwen 正文和既有规则恢复，针对本次私聊做隔离对照。
2. 将已使用 DeepSeek 的情绪、QQ 记忆统一到相同的 Schema/计费/时限协议；逐步接目标、能力、网页、终端与研究入口。
3. 单独决定包含最终文字的 JSON 是否整体迁移；如保留 Qwen 文风，则拆分决策与写作。日级修订和视觉走各自的专项验证。

替代模型能降低本地小模型的结构负担，但不能代替刚修复的错误状态处理，也不能保证事实正确、引用有效或每次公网调用都更快。预计主要收益是更一致的结构协议和对本地推理资源的释放；实际收益需由同输入对照确认。
