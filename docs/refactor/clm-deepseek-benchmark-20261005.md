# CLM 与 DeepSeek Structured Outputs 实机评测

2026 年 10 月 5 日，在 `codex/grounded-agency-rebuild-plan` 分支完成 Linux CPU、Windows RTX 5090 和 DeepSeek 官方 API 的真实评测。结论是：**5090 上的 CLM 已具备低延迟运行条件，但本次中文场景的正确性不足；DeepSeek Responses API 的 JSON Schema 输出更适合进入 NeuralCloud 的下一轮接入验证，简单判断优先关闭思考。**

这是隔离评测结果，不是生产替换验收。没有更改生产模型配置、重启生产服务、发送 QQ 消息或写入人物记忆。原始报告、请求载荷、预测、用量、失败和文件哈希保存在[证据目录](../harness/evidence/clm-deepseek-20261005/summary.json)。

## 评测对象与方法

CLM 是 `Contrastive-LM/CLM-v0.1-8B`，不是 OpenAI 产品。使用未修改的官方 Engine、投影头、问题协议和 FastAPI 服务。编码器使用 Transformers 的 Qwen3-8B、BF16、SDPA、最后一个非 padding token 的归一化表示；未运行 vLLM，所以这些数字不能称为 vLLM 服务性能。官方要求同一编码器与 pooling 配套使用。[CLM 固定版本说明](https://github.com/Contrastive-LM/CLM/tree/bb42c6c5bf914fd449bed2f6ca65be80602cb1f7)

| 项目 | Linux 服务器 | 本地机器 |
|---|---|---|
| 计算设备 | Ryzen 7 7840HS，8 核 16 线程，无 NVIDIA GPU | Ryzen 9 9950X，RTX 5090 32 GB |
| 系统 | Ubuntu，Linux 6.8 | Windows 11 |
| PyTorch | 2.8.0 CPU | 2.8.0 CUDA 12.8 |
| Transformers | 4.57.6 | 4.57.6 |
| 推理约束 | CPU 线程 2，batch 1，编码器串行 | CPU 线程 2，batch 1，编码器串行 |
| CLM 向量缓存 | 32 MiB | 32 MiB |

Linux 使用低优先级进程，保留其他服务；本地保留桌面应用。CPU 的结果是受限资源下的可用性基线，并非整颗 CPU 的峰值。测试服务仅监听 loopback，评测结束后退出。原有本地 Python/PyTorch 环境保持不变，5090 依赖安装在独立目录。

固定版本如下；两台机器的权重和官方源码文件哈希完全相同，见[模型清单](../harness/evidence/clm-deepseek-20261005/model-manifest.json)。

- CLM 源码：`bb42c6c5bf914fd449bed2f6ca65be80602cb1f7`。
- Qwen3-8B：`b968826d9c46dd6066d109eabc6255188de91218`。
- CLM 头：`e939398d4556fcd9400c76fa8c5a513202f42b0a`。

DeepSeek 使用官方 `https://api.deepseek.com/responses`、`deepseek-flash`、`text.format.type=json_schema`、temperature 0，分别设置 `reasoning.effort=none` 和 `low`。none 的输出上限为 128 tokens，low 为 1024 tokens，后者包含思考 token。使用 HTTPS 直连，不继承系统代理，不自动重试，也不修复输出。官方文档确认此接口支持 JSON Schema，且思考 token 计入输出上限。[Responses API](https://api-docs.deepseek.com/api/create-response/)

输入为推理前冻结的 [64 条中文合成用例](../../tools/production_acceptance/clm_cases_20261005.json)，SHA-256 为 `76eae06c9395582ed9bd3e355753e3811dfd47513a9cc5a1dd8cffbcd752f573`。标签由评测脚本作者制定，未经独立人工审核；它是诊断集，不是生产流量样本或可靠性证明。标签和类别名不进入模型请求，没有依据模型结果改标签。

CLM 的 choice 取 argmax，noul 以 0.5 为阈值，score 以最高概率等级计算分类正确率，并另存期望分数的绝对误差。DeepSeek 输出相同候选键、布尔值或等级序号。两者回答同一决策问题，未要求 DeepSeek 模拟 CLM 概率。

## 功能结果

| 类别 | 样本数 | CLM CPU | CLM 5090 | DeepSeek none | DeepSeek low |
|---|---:|---:|---:|---:|---:|
| 回复时机 | 12 | 4 | 4 | 11 | 11 |
| 证据关系 | 12 | 8 | 8 | 12 | 11 |
| 主体归属 | 8 | 6 | 6 | 8 | 8 |
| 工具选择 | 8 | 3 | 3 | 8 | 8 |
| 记忆候选检索 | 8 | 2 | 2 | 7 | 7 |
| 布尔判断 | 8 | 7 | 7 | 8 | 8 |
| 等级评分 | 8 | 2 | 2 | 8 | 8 |
| **合计** | **64** | **32，50.0%** | **32，50.0%** | **62，96.9%** | **61，95.3%** |
| 完整且结构可用的输出 | 64 | 64 | 64 | 64 | 63 |

来源：[CPU](../harness/evidence/clm-deepseek-20261005/clm-cpu.json)、[5090](../harness/evidence/clm-deepseek-20261005/clm-5090.json)、[DeepSeek none](../harness/evidence/clm-deepseek-20261005/deepseek-none.json)、[DeepSeek low](../harness/evidence/clm-deepseek-20261005/deepseek-low.json)。

CLM 的主要问题具有业务意义：4 条期望安静和 3 条期望延后的用例全部选择立即回复；4 条“证据不足”的用例全部被判成支持或否定；检索经常把可用候选排在 none 之后。这些错误会影响打扰控制、证据边界和记忆使用，不能只通过提高置信度阈值认定可上线。

CPU 与 GPU 的预测在 63/64 条上相同，唯一差异是 `tool-02`：CPU 选 none，GPU 选 web，正确标签为 memory，两者都错。两端采用相同 BF16 精度，但没有位级一致性保证。反转 12 条 choice 用例的候选顺序，两端均保持原预测。

DeepSeek 的两种设置都在 `retrieval-06` 选择 none，而未选含真实送达回执的候选 c。另一条 `gate-04` 是标签边界问题：用户要求“发完三段前先别回复”，冻结标签为 wait，模型选 defer；现有候选描述允许这种解释。原始分数保留该分歧，后续数据集应先明确 wait/defer 的互斥边界。

low 额外在 `evidence-04` 耗尽 1024 个输出 token，全部用于思考，返回 `incomplete/max_output_tokens` 且没有正文。该请求计为失败，不用重试覆盖。其余 63 条完成输出均通过 Schema 校验；这次截断不是已完成 JSON 违反 Schema。增加思考在这批简单任务上未带来正确率收益。

## 响应速度

下面是固定 3 个候选动作、状态发生变化的连续调用。CLM 预热候选向量，DeepSeek 每次生成新的 JSON；性能窗口中的状态文本、问题和候选逐条核对一致。数字是客户端收到完整响应的耗时，单位毫秒。

| 方案 | 新状态 p50 | 新状态 p95 | 并发 2 的 p95 | 并发 2 的成功请求每秒 |
|---|---:|---:|---:|---:|
| CLM CPU | 1663.22 | 1724.38 | 3414.89 | 0.59 |
| CLM 5090 | 33.84 | 34.57 | 68.99 | 26.79 |
| DeepSeek none | 570.78 | 1070.05 | 777.26 | 3.22 |
| DeepSeek low | 1025.66 | 1419.59 | 1261.23 | 2.19 |

每个新状态阶段 16 次，并发阶段 16 次、客户端并发 2；CLM 编码器串行，因此并发耗时包含排队。分位数采用 nearest-rank。16 次样本的 p95/p99 等于或接近最大值，只能作短窗口观察。DeepSeek 包含公网和供应商调度时间；CLM 是本机 HTTP loopback，这不是纯模型算力对比。

| CLM 缓存状态 | CPU p50 / p95 | 5090 p50 / p95 | 次数 |
|---|---:|---:|---:|
| 清空状态和动作缓存后重新计算 | 3941.61 / 3987.60 | 123.08 / 127.22 | 10 |
| 状态和动作全部命中向量缓存 | 41.82 / 42.84 | 1.39 / 1.65 | 30 |

CPU 完整命中时，服务端 `X-CLM-Latency-Ms` 约 0.4 ms，客户端约 42 ms，差额位于服务端计时之外的 HTTP 路径，原因未进一步定位。不能将 42 ms 都归为模型计算，也不能用完整缓存命中的 1.65 ms 代表新状态推理。

DeepSeek 重复完全相同请求 30 次时，none p50/p95 为 569.40/895.99 ms，low 为 987.79/1330.43 ms。它仍需生成输出；供应商自动 KV 缓存不同于 CLM 已有状态向量的直接复用，没有把它标成“完整结果缓存命中”。

在 64 条异构功能用例中，DeepSeek none 的完整响应 p50/p95 为 803.31/1388.02 ms，首个输出文本 delta 的 p50 为 725.90 ms；low 的成功请求 p50/p95 为 1227.09/2410.40 ms，首个文本 delta p50 为 1187.84 ms。low 的截断请求另列，耗时约 5.98 秒。收到流事件或思考内容不算收到可消费的决策对象。

性能原始记录：[none](../harness/evidence/clm-deepseek-20261005/deepseek-none-performance.json)、[low](../harness/evidence/clm-deepseek-20261005/deepseek-low-performance.json)。最终性能窗口在外网模型下载完成后执行；较早探索窗口保留在本地工作目录，不混入这张表。

## 输入长度与资源

以下重放相同状态、问题和候选；CLM 清空缓存。token 数为 Qwen 编码器实际状态加问题的长度，DeepSeek 使用不同 tokenizer，完整请求的实际 token 用量保存在其报告中。**每个长度只测一次，不是 p95。**

| Qwen 状态加问题 tokens | CLM CPU | CLM 5090 | DeepSeek none | DeepSeek low |
|---|---:|---:|---:|---:|
| 123 | 5.969 秒 | 0.148 秒 | 1.003 秒 | 0.808 秒 |
| 507 | 17.953 秒 | 0.163 秒 | 0.834 秒 | 1.236 秒 |
| 1019 | 38.761 秒 | 0.238 秒 | 0.689 秒 | 1.588 秒 |
| 2043 | 93.871 秒 | 0.460 秒 | 0.690 秒 | 1.161 秒 |

长文本由无关背景加同一末尾状态构成，只说明该输入下的耗时，不证明长上下文决策质量。[DeepSeek 同状态重放记录](../harness/evidence/clm-deepseek-20261005/deepseek-length.json)

CLM CPU 进程峰值 RSS 约 15.01 GiB。5090 的 PyTorch 峰值显存分配约 15.57 GiB、保留约 15.65 GiB，进程峰值 RSS 约 3.46 GiB；显存数字不包含桌面和其他进程。BF16 模型能够装入这张 32 GB 显卡。

首次真实推理 CPU 约 6.03 秒，5090 约 15.50 秒；GPU 稳态 34 ms 不能代替首次运行预算。模型文件哈希校验会预热 OS 文件缓存，CPU 权重还可能延迟触页，因此记录的模型加载时间不是完整冷启动指标。常驻服务应完成预热后再宣告 ready。

## Structured Outputs 与正文质量

7 项 JSON Schema 探针全部通过：enum、嵌套对象及禁止额外字段、数值范围、integer、数组长度、字符串 pattern、可空 anyOf。探针故意让文本要求与部分约束冲突，实际输出仍满足验证器。非法 Schema 返回 HTTP 400。这验证了本次用到的规则，不能外推为支持任意 JSON Schema。

另用[8 条冻结正文用例](../../tools/production_acceptance/deepseek_reply_cases_20261005.json)，分别比较 Responses 的 json_schema 与 json_object。两组均 8/8 结构可用、7/8 动作符合冻结标签；CLM 不生成正文，因此没有虚构 CLM 的文字回复质量分数。[完整正文及规则探针](../harness/evidence/clm-deepseek-20261005/deepseek-reply-contract.json)

人工阅读发现，两组均能完成简单计算、无文件时索取材料、区分第三方引用、回答有证据的猫名，并在静默场景输出空 speech。仍有三点需要改进：

- 延后回复场景，两组都选 wait 而非 defer。该补充提示没有给出两类动作的完整定义，需要先规范协议语义。
- 对“打算明天完成报告”的证据，json_schema 回复“还没有……说明报告目前尚未完成”；它把计划推成了过度确定的事实。json_object 也以“还没有”起句。结构约束没有消除这一问题。
- 用户要求只聊一句时，两组都给出两句话，风格约束还需要单独验收。

这些是按预设 rubric 的小样本文本检查，不是独立盲评。此次没有证据表明仅从 json_object 切换到 json_schema 就能提高语义质量；它的实测价值是输出协议的约束能力。

## 补充诊断与接口恢复

CLM 在原始结果出现问题后追加 4 组中英文对照：账单分流、潮汐常识、猫名提取、保持静默。训练头英文 4/4，中文 2/4；猫名和静默的中文版本失败。去掉投影头的 `clm-raw` 在原 64 条用例上为 23/64。该事后诊断未参与主表，不能凭 4 组对照断言语言是唯一原因，也未完成与 vLLM 的数值等价验证。[补充记录](../harness/evidence/clm-deepseek-20261005/clm-diagnostics.json)

CLM 两端均通过 8 项接口检查：错误凭据、空问题、未知模型、非法温度、超长输入拒绝、编码器故障、故障恢复、rank 接口。超长拒绝由本次适配器实施；官方默认服务允许截断，本评测明确不静默截断。这是测试适配器的行为，不能误认为所有 CLM 部署都如此。

DeepSeek 最初一次调用因余额不足返回 402；用户充值后恢复。低思考评测期间发生 Windows 报告文件占用，已添加原子替换重试；从已保存记录继续，未重跑或删除截断用例。报告保存了中断和继续记录。

## 对 NeuralCloud 的建议

1. **优先验证 DeepSeek Responses JSON Schema 与 none。** 保留客户端 Schema 校验、完整响应状态检查、时间与 token 上限，以及失败回退；不要把合法结构当成事实正确或动作已执行。
2. **CLM 5090 保留为局部实验。** 硬件和延迟可行，但应先建立独立审核的中文数据、明确 wait/defer、比较模板与候选描述、再考虑训练头。当前回复时机和检索表现不支持直接替换生产 gate。
3. **现有 Linux CPU 不承担长状态实时判断。** 在本次 2 线程条件下，千 token 输入已超过普通回复 30 秒的目标；它适合接口验证、离线工作或调用独立 GPU 服务。
4. **扩大独立测试后再定阈值。** 本轮 64 条合成数据未覆盖真实多轮、说话人噪声、线上并发、人物长期状态、vLLM、量化和真实 QQ 交付，也不代表 95% 或 99% 的生产可靠性。

## 复现与交付

评测代码在 `tools/production_acceptance`：`clm_benchmark.py`、`clm_eval_cases.py`、`clm_diagnostics.py`、`deepseek_structured_benchmark.py`、`deepseek_reply_contract_benchmark.py`、`deepseek_length_benchmark.py`。本地模型、独立环境和完整运行产物保留在 `H:\NeuralCloud-evals\clm-20261005`；Linux 的隔离目录为 `/home/qiuyu/neuralcloud-evals/clm-20261005`。原有分支修改仍保存在切分支时建立的 stash 中，本轮没有提交或推送。

CLM 脚本要求固定版本的官方 `clm` 模块位于 `--vendor` 指定目录，以及按模型清单下载并校验的 `encoder`、`head` 子目录。运行示例：

```powershell
H:\NeuralCloud-evals\clm-20261005\venv\Scripts\python.exe tools/production_acceptance/clm_benchmark.py --models H:/NeuralCloud-evals/clm-20261005/models --vendor H:/NeuralCloud-evals/clm-20261005/vendor --cases tools/production_acceptance/clm_cases_20261005.json --output H:/NeuralCloud-evals/clm-rerun --device cuda --threads 2
```

DeepSeek 可读取项目已有的官方端点配置，或使用 `DEEPSEEK_API_KEY` 环境变量；密钥不写入报告。每次新测试使用新输出目录。重新执行会产生 API 用量：

```powershell
H:\NeuralCloud-evals\clm-20261005\venv\Scripts\python.exe tools/production_acceptance/deepseek_structured_benchmark.py --config data/gui_settings.json --cases tools/production_acceptance/clm_cases_20261005.json --output H:/NeuralCloud-evals/deepseek-rerun --effort none --performance
```

独立环境的依赖检查通过，9 项评测器回归测试通过，覆盖 pooling、缓存、超长拒绝、失败统计、Schema、SSE、正文与思考分离，以及 Windows 原子写入重试。全部新增 Python 工具通过 Ruff 检查。业务代码没有变更，因此未运行整套业务回归来代替真实模型评测。
