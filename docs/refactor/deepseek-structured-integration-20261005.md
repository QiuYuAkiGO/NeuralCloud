# DeepSeek 结构化输出接入定稿

2026-10-05，按用户决定暂缓 OpenAI 评测，以 **DeepSeek Flash、`reasoning.effort=none`、Responses JSON Schema** 作为结构化决策的定稿方案，已接入 `codex/grounded-agency-rebuild-plan` 分支的现有代码。此前 CLM 与 DeepSeek 的 [64 条冻结评测](clm-deepseek-benchmark-20261005.md) 是选择依据：Flash none 为 62/64；不把该合成数据集分数视为生产准确率承诺。

本轮完成代码接入和隔离验证，未部署到 Linux 生产服务器，也未更换在线服务配置或重启服务。没有发送 QQ 消息、执行工具动作或写入业务人物记忆。OpenAI 的原有部分数据保留，但没有接入生产调用路径。

## 接入行为

既有 `OpenAICompatClient` 在官方 DeepSeek Flash 后端收到 JSON Schema 格式请求时，改用 `POST https://api.deepseek.com/responses`。其他后端、普通角色正文和没有类型定义的旧 JSON helper 继续原有路径。已有 `deepseek-v4-flash` 主模型配置的类型化决策请求映射为实测使用的 `deepseek-flash`；计量及请求日志记录实际请求模型与端点。

| 入口 | 传给服务端的实际类型 |
|---|---|
| 生产决策、生产情绪/记忆评估 | `ProductionChoice`、`ProductionAppraisal` |
| QQ 回复时机 | `_GateDecision` / `_ProductionGateDecision` |
| 能力、目标、网页步骤 | `CapabilityChoice`、`GoalChoice`、`WebStepChoice` |
| 告示板、日记 | 各自的 `_Expression` |
| Grounded Agency | 已有 `ExperimentalDecision` union |
| 终端决策 | 已有 `_DECISION` union |

请求采用 temperature 0、无思考、非流式完整响应。调用方已有的 max_tokens 与 deadline 保留；未指定输出上限时使用 4096 tokens。支持已有 Routing 主模型与 QQ 包装器，不把角色决策转发给独立 aux。API 只生成决策对象，Runtime 原来的权限、能力、证据、记忆修订与执行检查继续生效。后续的[缓存布局优化](deepseek-cache-optimization-20261005.md)将生产实时状态移至末尾，保持其 system 角色，并让固定规则与人格先于动态输入。

DeepSeek Responses 文档明确支持 `text.format` 的 JSON Schema；本实现发送 `type/name/schema`，没有把工具调用的 Beta strict 规则套到 Responses 文本格式上，也没有删减项目 Schema 中的约束。[官方 Responses 文档](https://api-docs.deepseek.com/api/create-response/)

## 验证与失败处理

服务端结构约束之后，客户端先确认完整成功状态，再解析整个 JSON 对象并按原 Schema 校验，随后由原 Pydantic 类型和业务验证器继续检查。拒绝、工具调用、截断、失败、重复 JSON 键、非有限数值、额外字段或未知枚举都会失败；不从 Markdown/解释文字中提取片段，不把错误修复成可执行决定，不降级重发到 JSON Mode。

HTTP 分发仍在原有生产计量边界内，provider usage 在校验前落入计量记录，失败也不丢失已经发生的调用成本。Responses 的 input/output/cache/reasoning token 映射回现有 `TokenUsage`；生产原始账本保留 Responses 字段路径。日志不写入密钥、正文或思考内容；用户显式启用的诊断 observer 延续原来的可见文本证据协议。

新增 `jsonschema>=4.23` 为运行依赖，因此部署新版本时应按更新后的 `pyproject.toml` 安装依赖。无需安装 OpenAI SDK。

真实 API 冒烟测试通过 **10/10** 项目 Schema：生产决策、生产评估、QQ gate、能力、目标、网页、告示板、日记、Grounded Agency、终端。全部输出通过 Schema 和原 Pydantic 校验，reasoning_tokens 均为 0。单次完整调用约 0.415–0.908 秒；这是连通与复杂 Schema 兼容性检查，不是新的 p95 基准。使用项目现有 HTTP 客户端并继承环境代理设置，与前轮禁用环境代理的专用基准脚本不同。

原始合成输入、完整 Schema、可见预测、用量、时间和源码哈希见 [集成实测证据](../harness/evidence/deepseek-integration-20261005/project-smoke.json)。该探针只要求等待或不行动，用于验证契约，不能替代带真实授权与业务证据的上线验收。

自动化回归覆盖客户端与辅助路由、生产调用计量、失败计量、超时、QQ gate 与大段历史、能力/目标/网页、告示板/日记、Grounded Agency 和终端流程。新增测试特别覆盖 HTTP 调用边界、主/aux 隔离、JSON Schema 错误不重试、拒绝/截断拒收，以及实际生产 `_ask` 的原始用量保存。最终测试结果见下方验证记录。

## 配置与回滚

推荐主端点配置如下，密钥继续使用项目已有的本地配置或环境变量：

```dotenv
NC_LLM_BASE_URL=https://api.deepseek.com/v1
NC_LLM_MODEL=deepseek-flash
NC_DEEPSEEK_STRUCTURED_OUTPUTS=true
NC_DEEPSEEK_STRUCTURED_MAX_OUTPUT_TOKENS=4096
NC_DEEPSEEK_CACHE_FRIENDLY_INPUTS=true
```

当已有 GUI/部署配置为官方 `deepseek-v4-flash` 时，同样自动启用这些类型化请求；无需修改密钥文件。功能默认启用，但只匹配官方 HTTPS 主机、标准端口、根路径或 `/v1`，不向相似域名、代理兼容端点、其他模型或本地 Ollama 注入 Responses 参数。

回滚协议切换可设置 `NC_DEEPSEEK_STRUCTURED_OUTPUTS=false`，随后按项目正常流程重新加载配置/服务；已接入入口恢复以前的 JSON Mode，研究/终端路径恢复原调用方式。这个开关不是删除现有业务校验。当前执行期间没有自动更改用户本地 `.env`、GUI 配置或远端配置。

只回滚输入布局可设置 `NC_DEEPSEEK_CACHE_FRIENDLY_INPUTS=false`，保留 Responses JSON Schema；该开关不控制供应商自动缓存。

可重复执行真实接口探针，输出目录必须是新目录：

```powershell
& .venv/Scripts/python.exe tools/production_acceptance/deepseek_project_smoke.py `
  --config data/gui_settings.json `
  --output H:/NeuralCloud-evals/clm-20261005/results-deepseek-project-integration-next
```

## 代码入口

- [Responses 映射与校验](../../harness/llm/deepseek_structured.py)。
- [现有 LLM 传输与计量边界](../../harness/llm/client.py)。
- [配置](../../harness/config.py)与[配置示例](../../.env.example)。
- [新增回归](../../tests/test_deepseek_structured.py)与[真实 API 探针](../../tools/production_acceptance/deepseek_project_smoke.py)。

## 验证记录

最终一次相关回归共 **563 passed**，耗时 35.62 秒；只有既有 FastAPI/Starlette 依赖弃用警告。新增及修改的 Python 源码通过 Ruff 检查；diff 按仓库已有 CRLF 行尾口径检查通过。没有宣称仓库所有测试或 Linux 在线服务验收已经通过。本轮未提交或推送。
