# DeepSeek Structured Outputs 输入缓存优化

2026-10-05，分支 `codex/grounded-agency-rebuild-plan`。已实现输入布局优化并完成真实 DeepSeek API 对照。在三组各 8 轮的合成生产决策中，优化后预热阶段之后的加权命中率为 **92.69%**；包括各组前两轮的全部 24 次优化请求为 **92.48%**。这些结果证明本次输入布局能够达到 90%+，不代表所有生产会话或首次请求都能达到该比例。

## 实现与边界

原来的生产调用把每轮变化的身体、预算和情绪状态放在第一个 system 消息里。即使后面的人格、规则和 Schema 完全相同，变化的开头也会打断其前缀复用。生产 user JSON 又按字母顺序排列，使当前事件早于人格。

现在官方 DeepSeek Flash 的类型化请求默认采用以下布局：

1. 固定生产规则在前；生产 `_ask` 不再重复注入相同规则。
2. 生产 user JSON 先放人格、动作定义、固定任务说明，再放已准入记忆、历史与证据，最后放实时状态、当前事件和引用列表。只调整顶层字段顺序，嵌套内容、数组顺序和未知字段全部保留。
3. 当前生产状态仍为 **system** 消息，移到输入末尾，并在每次调用时重新读取。
4. JSON Schema 由 Responses `text.format` 完整提供，生产 `_ask` 不再把同一完整 Schema 再复制到 system 正文。Schema 对象键按确定性顺序序列化，约束和数组不删改。

没有添加填充文字，没有冻结时钟、预算、情绪或授权，没有为了命中而继续保留已撤回记忆。记忆准入、manifest、曝光计量、执行授权、超时和输出校验仍经过现有路径。其他类型化入口受益于生产状态后置；本轮字段重排与去除重复 Schema 针对生产 `_ask`，下面的数字仅来自 `ProductionChoice`，不是全项目各入口的加权结果。

实现见 [production.py](../../harness/cognitive/production.py)、[DeepSeek 适配](../../harness/llm/deepseek_structured.py)、[客户端](../../harness/llm/client.py)。

## 真实 API 对照

使用项目 HTTP 客户端、官方 `deepseek-flash`、`reasoning=none`、原始 `ProductionChoice` Schema，继承环境网络配置。人格读取当轮仓库内置 Misty，其他内容全部为合成输入。每个场景基线与优化各 8 次，轮间间隔 2 秒，各窗口前两次单列为预热；不额外发送隐藏预热请求。基线指旧提示布局，传输和 Schema 校验与优化组一致。

三种场景均每轮更新事件、时间、身体快照和预算；历史增长场景逐轮追加观察；记忆撤回场景从第 5 轮移除猫名记忆及其引用。不同场景使用独立会话时间和事件/来源编号，同一场景两种布局使用相同业务字段。没有执行动作或写入业务记忆。

命中率采用 API usage 的实际输入缓存字段，经现有 `TokenUsage` 归一化后计算 `sum(cache_hit_tokens) / sum(prompt_tokens)`。以下每格为去除前两轮之后的 6 次请求，不平均单次百分比，所有样本缓存字段覆盖率为 100%。

| 场景 | 基线命中率 | 优化后命中率 | 基线未命中 tokens | 优化后未命中 tokens |
|---|---:|---:|---:|---:|
| 持续状态变化 | 4.51% | **92.88%** | 65,118 | 2,886 |
| 历史逐轮增长 | 4.45% | **92.30%** | 65,928 | 3,184 |
| 记忆撤回 | 4.52% | **92.92%** | 64,962 | 2,858 |
| 合计，18 次/组 | 4.49% | **92.69%** | 196,008 | 8,928 |

包含预热阶段的全部调用也保留如下，不能只展示稳定窗口：

| 指标 | 基线，24 次 | 优化，24 次 |
|---|---:|---:|
| 输入 tokens | 273,464 | 162,776 |
| 命中 tokens | 12,288 | 150,528 |
| 未命中 tokens | 261,176 | 12,248 |
| 加权命中率 | 4.49% | **92.48%** |
| 完整调用 p50，最近秩口径 | 846 ms | 907 ms |
| Schema 与合成等待要求均通过 | 24/24 | 24/24 |

本窗口输入总量减少 **40.48%**，未命中输入减少 **95.31%**。持续状态变化场景单次输入从 11,365 降到 6,753 tokens。延迟 p50 没有改善；本次小样本顺序运行不能证明稳定加速，也未测量 TTFT 或线上 p95。48 条请求只检验合成等待约束和 Schema，不作为新的通用准确率分数。

### 预热、探索轮和复现限制

服务端缓存不能强制清空。本次正式对照复用了探索轮已经形成的部分固定前缀，因此“全部 24 次”也不是完全冷启动成绩。探索轮持续状态变化的优化窗口中，首个请求命中 512/6,601 tokens，全部 8 次为 **82.41%**，后 6 次为 **93.08%**；预热成本确实可能把短会话总命中率拉低到 90% 以下。

探索轮的后两个场景复用了第一个场景的时间和事件编号，导致基线出现不适合作为独立场景对照的高命中。该轮全部 48 次及脚本原样保留为探索证据，未混入上表。之后使用独立场景时间和事件编号重新执行上述 48 次正式对照。两个实验之间仓库人格内容发生变化，故只在各自实验内部进行配对比较，不把两轮输入 token 数直接相减。

证据目录：[deepseek-cache-20261005](../harness/evidence/deepseek-cache-20261005/)。其中：

- [正式对照原始记录](../harness/evidence/deepseek-cache-20261005/isolated-report.json)保留逐次完整合成输入、可见输出、用量、耗时、窗口汇总与源码哈希。
- [探索轮原始记录](../harness/evidence/deepseek-cache-20261005/exploratory-report.json)保留初次预热及场景复用问题。
- `benchmark-isolated.py.txt` 与 `benchmark-exploratory.py.txt` 是各自运行时脚本快照；`production-before.py.txt` 是优化前生产构造代码。
- [验证摘要](../harness/evidence/deepseek-cache-20261005/validation.json)记录回归与汇总核验。

当前可重复运行的[评测工具](../../tools/production_acceptance/deepseek_cache_benchmark.py)另已改为每次启动采用新的会话基准时间，并写入报告，避免重复执行固定历史时间而高估基线。其业务场景、配对方式和模型调用不变；历史成绩对应上述脚本快照，没有用新脚本哈希覆盖旧记录。

```powershell
.venv/Scripts/python.exe tools/production_acceptance/deepseek_cache_benchmark.py `
  --config data/gui_settings.json `
  --output H:/NeuralCloud-evals/clm-20261005/results-deepseek-cache-next `
  --turns 8 --interval 2
```

此命令调用付费 API，输出目录必须不存在。首次请求仍可能命中服务端已有固定前缀，不能因为新建目录就标记成冷缓存。

## 配置、观测与验收

`NC_DEEPSEEK_CACHE_FRIENDLY_INPUTS=true` 默认启用上述布局。设为 `false` 可以恢复旧生产提示布局，保留 Responses JSON Schema 接入；它不是供应商缓存开关。未修改密钥、本地 GUI 配置或远端运行配置，未部署、重启服务或推送代码。

上线后应按实际请求的阶段、请求/响应模型、缓存字段覆盖率和输入 token 总量观察，不能把本报告的生产决策命中率套到 QQ 正文、validator 或所有辅助调用。已有报表工具可直接使用：

```powershell
.venv/Scripts/python.exe tools/qq_cache_report.py --requests traces/model_requests.jsonl
```

达到 90% 的实际条件是服务端已保存的可复用前缀占本轮输入约九成以上。人格或工具定义变化、检索结果更换、长篇新输入、历史窗口换代、缓存过期都会降低比例；应忠实更新这些内容。DeepSeek 缓存自动启用、需要时间落盘且属于尽力而为；Responses 的 `prompt_cache_key` / `prompt_cache_retention` 不受支持，不能通过传入开关保证命中。[官方缓存指南](https://api-docs.deepseek.com/guides/kv_cache/)、[Responses 指南](https://api-docs.deepseek.com/guides/responses_api/)

最终相关回归 **201 passed**（15 个测试文件），覆盖原生出站布局、主路由与回滚、字段完整性、撤回与授权刷新、证据/manifest、分发/计量、QQ deadline、已有历史缓存与 usage 计量。两条既有 Starlette/httpx 弃用警告。修改的 Python 文件 Ruff 与 `git -c core.whitespace=cr-at-eol diff --check` 通过。
