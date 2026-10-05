# CLM 接入评估 · 2026-10-05

状态：**初始架构评估已完成，生产接入尚未实施。** 下文保留首次评估的工作范围；同日后续已完成 [CLM 双机与 DeepSeek Structured Outputs 实机评测](clm-deepseek-benchmark-20261005.md)，以该报告补充和修订模型效果与部署建议。

## 1. 对象、基线与结论

本次 CLM 指 Contrastive Language Model，目标权重为 `Contrastive-LM/CLM-v0.1-8B`，不是 OpenAI 产品，也不是 OpenAI Structured Outputs。用户提供的《CLM 托管 API 监控》对话用于定位目标；技术事实重新核对官方模型卡与源码。

- NeuralCloud：`codex/grounded-agency-rebuild-plan`，`e6951ed`。
- CLM：官方仓库 main 在本次 `git ls-remote` 返回 `bb42c6c5bf914fd449bed2f6ca65be80602cb1f7`；读取该提交的 engine、embedder、schema 对应实现及客户端、服务端。上线应额外锁定权重哈希、编码器 revision、pooling、dtype、服务依赖和问题模板，不能仅记录 `clm-latest`。
- 本次本机只读查询：RTX 5090，显存总量 32607 MiB、查询时空闲 29804 MiB。这是瞬时设备信息，不是 CLM 装载或吞吐实测。
- 未下载模型权重、安装依赖、启停服务、调用真实判定模型、发送 QQ 消息或写入人物记忆；未改业务源码。

**建议：作为独立的有限候选判定/排序 provider 接入，先做影子评测。首批目标是已授权记忆候选重排，以及现有 QQ gate 的旁路对照。通过评测后，才考虑拆分并替换 gate 的有限选择部分。主角色生成、经历解释、证据裁决及交付权威继续保留。**

首发是单人 QQ 私聊、无语音和桌面。不能用群聊高频旁观、游戏循环或语音抢话的假想收益，代替这个首发负载的实际收益。[首发范围](production-launch-scope-20260929.md)

## 2. 已确认能力及历史对话的修正

CLM 使用冻结 Qwen3-8B 编码器及投影头，对状态与候选分别编码。参考部署由编码服务和 CLM 服务组成，支持 TypeSafe 风格的 `Choice`、`Score`、`Noul`。状态/候选缓存提供加速机会；作者的最高加速结果不等于 Misty 的端到端延迟。[官方仓库](https://github.com/Contrastive-LM/CLM)

官方模型卡明确：它只评分已有候选，概率相对于候选集合；参考头依赖 Qwen3-8B 的 last-token pooling。编码基座不能替换为项目现有通用 embedding 模型；微调 verifier 的基准数字也不能归给原始 checkpoint。模型卡当前仍显示无 Hugging Face Inference Provider。本次未找到可核验的现成按量托管 API，但这不是对全网不存在服务的证明。[模型卡](https://huggingface.co/Contrastive-LM/CLM-v0.1-8B)

需要修正的预期：

- 约 75 MB 指投影头，不是完整部署规模；还需要 8B 编码基座。
- “接口类似 Jev”不代表中文社交判断质量、概率校准或置信阈值可直接复用。
- 不生成正文，也不生成开放式动作参数、理由或证据引用。固定标签、候选 ID 和分数适合它；完整 `ProductionChoice` 不适合直接替换。
- “几十毫秒”和“不需要生成 token”都不代表零成本、零排队或每轮只有一次编码。

## 3. 源码发现的集成约束

### 3.1 分数和候选设计

CLM `confidence` 是最高概率减去其余概率的平均值。它不是独立的正确率估计。`Choice` 的非空描述直接作为候选文本；状态与问题说明拼接成 state 输入。因此候选描述质量、数量、重复选项、温度和模板版本都会影响分布。[schema.py](https://github.com/Contrastive-LM/CLM/blob/bb42c6c5bf914fd449bed2f6ca65be80602cb1f7/src/clm/schema.py)

集成应使用描述完整、彼此区分的候选，并保留 `none_of_above`/升级路径；即使存在弃权候选，也必须通过标注集校准分数和 margin 阈值。不能因为某个候选得分最高就认定它适用。模型错误、低分、输入超限和过期分别记录，不能一律伪装成角色主动选择 `wait`。

### 3.2 输入长度和多问题成本

参考 embedder 默认 `max_tokens=2048`，请求带 `truncate_prompt_tokens`，默认 HTTP timeout 为 300 秒。它按未缓存的文本分批请求编码器；缺少编码器 token 用量时会返回零。[embedder.py](https://github.com/Contrastive-LM/CLM/blob/bb42c6c5bf914fd449bed2f6ca65be80602cb1f7/src/clm/embedder.py)

NeuralCloud 必须在发送前用匹配 tokenizer 检查实际“state + instructions”及每个候选长度，编译有预算的专用状态投影；不把完整人格、历史和记忆大 JSON 直接交给默认截断。身份、否定、更正、受众和当前问题均不能悄悄丢失。超限走显式降级；扩容需同步配置编码端与 CLM 端并重新测显存。

多个不同问题各自形成 state 文本，可能重复编码同一份上下文。批处理能摊薄成本，但不能假定追加问题免费。engine 的 `input_tokens` 累加缓存未命中编码量，`billing_units` 为问题数，`output_tokens=0`。[engine.py](https://github.com/Contrastive-LM/CLM/blob/bb42c6c5bf914fd449bed2f6ca65be80602cb1f7/src/clm/engine.py)

### 3.3 外部服务和就绪检查

服务提供 `/v1/systemone`、`/v1/rank`、`/v1/models`、`/health`，认证可选；`/health` 将服务存活 `ok` 和编码器状态 `embedder` 分开返回。[server.py](https://github.com/Contrastive-LM/CLM/blob/bb42c6c5bf914fd449bed2f6ca65be80602cb1f7/src/clm/server.py)

生产 readiness 应检查两者、目标模型和一次小型合成探针，不能只检查 `ok=true`。远程部署需鉴权与受保护的传输，编码端仅内网可达。客户端期限、服务端排队和编码超时需要共同约束；客户端超时不证明后端计算已经取消。

## 4. 与当前分支的具体契合度

| 接入位置 | 适合程度 | 限定范围 |
| --- | --- | --- |
| 已经过权限、撤销和时效过滤的记忆候选重排 | 高，需验证中文召回 | 只调整排序；不增加可信度、生成证据或直接登记实际采用 |
| QQ `speak/wait`、表达风格等有限选择 | 中，值得影子对照 | 当前 gate 有生成职责，不能直接替换整个 DTO |
| 已存在且参数冻结的合法工具/任务候选选择 | 中 | 不能补出路径、查询词、命令参数，也不能批准服务器修改 |
| 自主活动中是否继续某个已有计划 | 后期评估 | 会影响人格行为；先测与主角色决策的一致性和实际结果 |
| 情绪解释、认识修订、回复正文、开放式规划 | 不适合整体替换 | 包含自由文本、证据跨度和跨字段依赖 |
| 身份、权限、删除批准、交付成功、身体事实 | 不作为权威 | 由现有确定性合同和回执决定 |

### QQ gate 不是一个布尔接口

[`_GateDecision`](../../harness/qq/decision.py) 包含 `action`，也包含 `reason`、寻址来源/引文、`reply_style_reason`、`reply_plan`、`web_plan`；生产扩展还有 `used_memory_ids`。正文生成和 [`checkpoint_gate`](../../harness/qq/prepared_turn.py) 持久化并消费这些字段。

所以不能把 CLM 标签包成旧 gate，给其余字段填默认文案，就声称与原来行为等价。需要拆出版本化的有限判定合同；计划及自由文本继续由生成路径承担，缺失字段明确标识不适用。自动说明只能标为程序路由诊断，不能伪装成模型推理理由。

尤其是 `used_memory_ids`：排序靠前或被编码不等于角色采用该记忆。CLM 输入可记作实际传输曝光，筛选结果记作候选选择；不得批量升级为 decision-use。

### 不会自动消除 QQ 串行延迟

[`prepare` 路径](../../harness/cognitive/qq_turn.py) 在 gate 前先运行 [`appraise_qq`](../../harness/cognitive/production.py)，用于来源绑定的经历解释与认识更新，再更新记忆视图，然后运行 gate 和后续正文生成。

只替换 gate 能节省该阶段，不能同时宣称省掉 appraisal、正文、校验和实际交付。若把 CLM 放到 appraisal 前，则改变了它能看到的认识版本；需要专项证明不会依据过时认识作选择，且不会因暂不发言丢弃合法经历学习。

历史模拟 QQ 复测为 10 个请求、7 个成功回复、24 次 HTTP 请求，成功回复 p95 约 55.17 秒；它使用真实旧模型和模拟 QQ、隔离新库，不能视为当前线上基线或 CLM 对照。[历史结果](production-qq-latency-10-retest-results.json)

## 5. 建议的接入结构

```text
现有 Runtime / Cognitive
    → 冻结身份、受众、来源清单、epoch 与任务状态
    → 编译有 token 预算的判定输入
    → DecisionProvider（CLM，后续可有独立 Jev adapter）
    → 检查候选 ID、分数、模型版本、期限、输入版本
    → 影子记录 / 候选排序 / 经批准上线的局部路由
    → 现有角色生成、准备与提交、授权与交付
```

建议新增的组件（尚未创建）：

- `harness/decision/contracts.py`：`DecisionRequest/DecisionResult`，含 request ID、schema/template/model 版本、state/candidate 哈希、候选映射、结果状态和原始用量。
- `harness/decision/clm.py`：独立 HTTP adapter，不塞入 `OpenAICompatClient.complete_chat_result`，不假造聊天 finish reason 或 assistant 正文。优先用 `/v1/systemone` 的 Choice 保留稳定 ID 和用量。
- `harness/decision/context.py`：从已获授权的冻结视图编译简短文本，记录来源清单、实际 token 长度与编译策略。
- `harness/decision/policy.py`：任务级阈值、弃权与回退；每个 provider 独立校准。组合结果不得产生跨问题矛盾。
- `tools/production_acceptance/clm_decisions.py`：隔离回放、基线对照、扰动与延迟报告。

建议开关为 `off | shadow | on`，默认 `off`，这些是待实现配置，不是现有环境变量。shadow 不影响回复、记忆或身体动作，但真实计算仍需记录与限额；用离线隔离配额做早期回放，避免挤占在线自主预算。

结果提交前保留现有期限、Runtime epoch、受众、来源撤销和模型/模板版本检查。网络失败或低置信只在剩余预算和期限足够时调用原生成判定；否则显式结束为技术失败/降级，不能默认为发送或自然等待。

## 6. 用量和身体记账

[`production_attempt`](../../harness/production/metering.py) 是现有实际调用预约/持久记账边界。CLM adapter 应使用同一机制，每次 NeuralCloud 的实际 CLM 请求先预约，结果无效、超时或被版本检查拒绝仍记录尝试。后续主模型回退是另一真实尝试，不可隐式重试而不计量。

现行生产自主模型尝试合计每滚动小时最多 12 次。[生产配置](../../harness/production/config.py) CLM 接入不会自动豁免这个限制。不能因为它快就每秒或每个心跳调用；额外频率属于单独的机制变更。输入完全相同的应用结果复用需绑定版本和权限；CLM 内部编码缓存命中仍是一次外部判定请求。

还需区分三种量：服务报告的缓存未命中编码 tokens、判定请求/问题数、真实 GPU 和货币成本。不能把 `billing_units` 当美元，也不能把输出 token 为零理解为免费。

现有 [`flush_usage`](../../harness/production/runtime.py) 所在结算路径已识别 `input_tokens/output_tokens`，但只有总量大于零才走 token 结算，否则按一次调用结算。因此全缓存命中会走 call 路径；该分支行为必须纳入身体成本回归。上游缺失用量被补零的问题需在自托管服务补充来源标记/编码遥测，否则只保留服务报告值及“不足以证明实际零用量”的状态。计算开销估算不能写成服务商测量事实。

## 7. 部署选择与成本

建议先用本机 RTX 5090 做隔离验证，通过后再决定生产位置。硬件已存在，不代表 CUDA、vLLM、WSL/Linux 环境、并发显存或长期在线可用性已验证。参考头依赖匹配编码器，不优先引入量化或第三方移植，以免混淆首轮问题来源。

粗算 8B 参数在 BF16 下仅权重约 16 GB（十进制），另需框架、激活、批处理、缓存和投影头空间；这是容量估算，不是实测显存。32 GB 卡有尝试空间，应从短输入、低并发起测，并限制其他模型竞争。

| 部署方式 | 本项目判断 |
| --- | --- |
| 现有本地 GPU 自托管 | 最适合首轮验证；主机休眠、网络和负载波动可能妨碍 Linux 生产服务 |
| 生产服务器附近租 GPU、自托管 | 可改善在线性和链路可控性；低频单人聊天可能不抵常驻成本 |
| 购买现成 CLM API | 本次未核实可用供应商，不作为首发依赖；将来验证型号、版本、用量、期限和数据处理后可适配 |

成本应按实际报价计算，不给未经核验的“CLM 每百万 token 单价”：

- 云端常驻月费约为 `730 × GPU 实例小时价 + 存储/网络/运维`。
- 本地增量电费约为 `平均增量功率(kW) × 开机小时 × 电价`，另计显卡机会成本。
- 混合每轮模型成本约为 `CLM 成本 + 回退概率 × 原判定成本 + 保留的正文/解释成本`。

设可省去的原 gate 耗时为 G，CLM 端到端耗时为 C，回退比例为 r；在相同后续工作量的简化均值模型中，收益需满足 `C + rG < G`。p95 不能用均值或各阶段分位数直接相加，必须测完整请求。若旧 gate 仍因生成计划被调用，则额外 CLM 未必降低延迟。

## 8. 验收方案和推进次序

第一阶段：冻结服务代码、编码器、头、模板和候选描述；实现独立 adapter、状态编译、配额记账与离线评测。先测合成/明确授权的去标识数据，无实际动作和认识写入。

建议建立至少 300 条人工标注的首轮中文诊断集（工程建议，不是达到 99% 可靠性的统计证明），按完整会话/场景划分开发集、阈值校准集和未参与调参的测试集。覆盖直接提问、无需回复、延后、歧义、反讽/否定、本人更正、跨轮指代、私有证据、已撤销来源、身体未知、工具不适用和输入过长；不得用原 LLM 输出直接充当正确答案。

比较当前 gate、CLM 原始头，以及可选 `clm-raw` 消融；有合法可用 Jev 端点时再添加对照，不能把 CLM 开放接口等价当成 Jev 效果。中文模板和中英对照单独统计，不能凭 Qwen 基座推定中文角色判断可靠。

报告至少包括：

- speak/wait 分类混淆矩阵，漏答直接提问、误打扰、错误工具路由分别统计；人物偏好及身体状态变化的行为敏感性。
- 分数分桶正确率、Brier/ECE 等校准诊断、不同阈值下覆盖率与错误率；候选顺序、同义改写、重复干扰项和 none-of-above 的稳定性。
- 记忆重排 recall@k/nDCG，以及否定、更正、反证的保留率；不能只优化表面相关性。
- 端到端冷/热缓存 p50/p95/p99、输入长度分桶、并发 1/2、回退比例、GPU 显存和实际调用数。首轮可把单次热态判定 p95 ≤500 ms 作为优化目标，是否合理须以首测修订，不是模型承诺。
- 超时、编码器断连、模型不存在、响应非法/候选不匹配、源撤销、暂停、进程恢复的行为；不得产生额外交付或丢失用量。

第二阶段：有限 shadow 对照，保留原选择，检查是否有稳定可替代的子任务。影子结果不直接变成 memory-use 或角色认识。

第三阶段：仅对通过验收的场景启用候选重排/局部 gate；一键关闭恢复原路径，保留新尝试和审计记录。角色主体选择若从主 LLM 转到 CLM，应作为明确的行为机制版本变更来评审。

整条 QQ 链仍需满足已定目标：普通回复成功率目标 99%、单人试点底线 95%、成功回复实际交付 p95 ≤30 秒，等待/拒绝/错误另列；有限评测结果不能替代长期观察。

## 9. 与首发交接的关系

CLM 适合作为可插拔的优化工作包，与现有首发修复并行规划；本次不启动其他 agent 或实施工作。不能以其替代尚未完成的用户任务接线、MistyAI 发布配对、迁移回归失败修复、批准链和真实交付验收。[首发交接](production-launch-handoff-20260930.md)

当前决策是 **Go：隔离原型/测评；No-Go：直接替换生产 gate 或把它当唯一认知模型**。下一步具体产物应是可关闭的 DecisionProvider 原型与冻结评测集，而非修改线上主模型配置。
