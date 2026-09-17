# 中文回复人工评审：候选与参数核验

核验日期：2026-09-16。机器可读来源、请求参数和逐模型限制保存在 `catalog.py`，采样前由工具写入冻结清单。文档预检没有发送生成请求；账户可见性由 `preflight` 的只读 `/models` 查询另行记录。

## 入选依据的边界

20 个候选沿用已批准清单，覆盖七家；保留最新代际、较早对照及固定快照。模型名称或厂商的能力介绍，不等于已经证明其中文角色聊天质量。

[Arena 中文榜](https://arena.ai/leaderboard/text/chinese)核验时显示 2026-09-13 的榜单，包含本清单多家模型或同家族不同推理档位。这是独立人类偏好证据，但覆盖通用中文任务，且很多条目使用高思考量；不能把排名搬到本次低思考量、两个角色场景，也不能把同家族视为完全同快照。清单只将可对应的条目标为 `human_preference`，不复制排名或宣称全部候选“广泛好评”。

`official_model`、`official_release`、`official_capability_claim` 分别表示厂商型号资料、发布日期资料和厂商自述；`gateway_api` / `gateway_protocol` 表示 API易对于自己接口的说明。历史对照的选择理由写在 `selection_reason`。未核实发布日期为 `null`，名称中的快照时间单列 `version_date`，不冒充正式发布时间。

## 请求冻结决策

- OpenAI 使用 Chat Completions。Astra 的最低合法档位为 `low`；Sol 支持 `none`；4o 为非推理历史快照。Astra/Sol 使用 `max_completion_tokens=8192`，4o 使用 `max_tokens=8192`，不传 temperature/top_p。[OpenAI 最新模型指南](https://developers.openai.com/api/docs/guides/latest-model)、[Sol 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- Claude 使用原生 Messages，保留完全相同的 system/user 内容，仅改变协议封装；请求头采用 `x-api-key` 和 `anthropic-version: 2023-06-01`。Fable 5.1 不允许禁用思考，使用 adaptive + low；Opus 5 禁用思考并显式设置合法的默认 high effort；Opus 4.6 禁用思考。[API易原生协议](https://docs.apiyi.com/api-capabilities/claude-effort-thinking)、[Claude 思考说明](https://platform.claude.com/docs/en/build-with-claude/thinking)、[effort 说明](https://platform.claude.com/docs/en/build-with-claude/effort)
- DeepSeek 使用 `reasoning_effort=none`；Qwen3.8 使用 API易明确列出的相同开关；Qwen3.7 Plus 使用 `enable_thinking=false`；Instruct-2507 本身只提供非思考模式，不加开关。[DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/)、[API易 Qwen3.8](https://docs.apiyi.com/api-capabilities/qwen-3-8/chat-completions)、[Qwen 官方接口](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-api-via-openai-chat-completions)、[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507)
- Kimi K3 必须思考，使用 `reasoning_effort=low`；K2.6 使用原生 `thinking.type=disabled`；K2.5 根据 API易对阿里云转发通道的明确说明使用 `enable_thinking=false`。[K3 指南](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)、[K2.6 指南](https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart)、[API易 K2.5](https://docs.apiyi.com/api-capabilities/kimi-k2-5)
- GLM-5.3 不接受禁用思考，显式 enabled + low；GLM-4.7/4.5 使用 disabled。[GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)、[GLM 思考接口](https://docs.bigmodel.cn/cn/guide/capabilities/thinking)
- MiniMax M3 使用 disabled；M2.7/M2.5 的官方文档明确不能关闭思考，也没有列出合法的 effort 调节参数。因此保留唯一有文档依据的默认模式，不能称为“低档”。这项可比性例外随清单和报告保留。三者使用 `max_completion_tokens=8192` 和 `reasoning_split=true`；后者只分离显示，不减少思考。[MiniMax 接口说明](https://platform.minimax.io/docs/api-reference/text-openai-api)、[参数参考](https://platform.minimax.io/docs/api-reference/text-chat-openai)

## 可比性与可用性限制

8192 是请求里的生成上限，不是可保证的最终正文长度；各接口是否把思考计入同一上限并不一致。Qwen3.8 的 API易文档尤其明确 `max_tokens` 只限制正文。不得为截断样本自动续写。

API易明确提供 Chat Completions 兼容入口，但只读目录和厂商文档无法独立验证每次转发是否保留全部扩展参数、是否保留原厂权重，以及具体调度到哪条线路。候选清单把这类局限与确定的请求参数分开保存；不通过额外生成探针验证，也不遇错改参重试。原始请求、响应、用量及返回型号共同作为审计依据。思考模式用请求配置描述，不能只凭无 reasoning 字段宣称“实际无思考”。

Kimi 官方目录称 K2.5 原生服务已于 2026-08-31 退役，API易仍明确提供阿里云通道；两者不是同一个可用性承诺。保留历史候选并记录风险，不自动换成别的 Kimi 型号。[Kimi 型号目录](https://platform.kimi.ai/docs/models)

DeepSeek 旧更新日志曾宣布 9 月 14 日后将 V4 Pro 重定向到 V4.1 Flash，但核验时最新价格页已经说明应用户要求继续提供 V4 Pro 0813。保留批准的 `deepseek-v4-pro-0813` 日期化 ID，并记录官方页面冲突；不擅自改用移动别名。API易实际后端身份依然只能保存线索。[最新价格页](https://api-docs.deepseek.com/quick_start/pricing/)、[旧更新日志](https://api-docs.deepseek.com/updates/)

V3-0324 没有明确的独立目录项，`deepseek-v3` 也没有被证明固定指向它，因此排除。模型数上限不等于必须凑齐三个。

## 人工评分前的解释约束

每个模型只有两个独立首轮，评审为单人偏好采样。最终报告须保留场景原文、参数差异、失败/截断及型号不确定性；不推出中文能力总排名。金额只有在网关用量或账单有可核实依据时才填写，不能将原厂标价直接当作 API易实付账单。
