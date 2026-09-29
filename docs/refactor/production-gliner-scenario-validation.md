# GLiNER2.5 来源候选接入与场景测试

2026-09-22。已将 Linux 上下载的 GLiNER2.5 接入隔离的经历摄取、来源清单与引用校验流程。当前结果支持继续研究候选片段提取，不支持让它决定人物归属、本人更正或支持／反证关系。线上主模型、配置、会话和数据库均未切换。

机器可读结果：[production-gliner-scenario-results.json](production-gliner-scenario-results.json)。原始推理输出、独立数据库、测试 JUnit 和首次启动失败记录均保留在 Linux `/home/qiuyu/neuralcloud-evals/gliner2-20260922-neuralcloud-scenes-v1`；成功模型批次为 `run-v2`。

## 已实现接线

- [gliner_bridge.py](../../tools/production_acceptance/gliner_bridge.py)：加载已经签发且当前有效的来源视图，将 GLiNER 相对 Unicode 字符位置转换为原来源位置，逐条调用实际 `EvidenceManifestStore.resolve_anchor`。它不搜索或修补引文，也不根据名字或作者猜测被描述者身份。
- [gliner_scenarios.py](../../tools/production_acceptance/gliner_scenarios.py)：离线模型 runner，实际执行 `Experience` 摄取、`prepare_views`、`issue`、候选提取与引用核验。模型只收到原文和固定 schema；金标签不进入推理。所有模型输出先独立保存，再校验和评分。
- [gliner_cases.json](../../tools/production_acceptance/gliner_cases.json)：16 个新中文合成场景、6 个目标命题与证据对；另重放上轮 QQ 合成测试中引用偏移错误的消息。
- [test_gliner_candidate_bridge.py](../../tests/test_gliner_candidate_bridge.py)：真实 SQLite 测试，覆盖裁剪、Unicode、重复原文、非法位置、非有限分数、越权、撤销、版本变化和历史不变。

原文位置核验与语义判断分开。输出的 `source_author_id` 是原话作者，不能当作提取内容的主体。标签和 confidence 始终留在 `model_interpretation`，不提高记忆可信度，不生成 `ClaimProposal`，不绕过原有认识提交约束。候选只在当前读快照有效，后续提交仍需重新校验来源。

本次没有将候选接入主模型的在线回复提示，没有发送 QQ 消息或执行身体动作，也没有把分类结果写为人物认识。独立数据库最终只有 **17 条来源经历**，没有人物判断。

## 模型和环境

复用下载的 `fastino/gliner2.5-multi-v1`，revision `a221b77a8baf4a613b8f8652661d41fa10a5641e`，约 287M 参数。权重 SHA-256 为 `c1ff4ec0bc00031c15530b8f3c33d3677f27949e6a0cb52e1247a6224b6c5395`，与下载清单一致，运行前后相同。

按[官方接口](https://github.com/fastino-ai/GLiNER2#custom-word-splitters)，用 `AutoExtractor.from_pretrained` 和适用于中文的 `word_splitter="char"` 加载本地模型。模型来源见[官方模型页](https://huggingface.co/fastino/gliner2.5-multi-v1)。中文分词选择并不保证语义准确。

Linux CPU，2 个计算线程、1 个 interop 线程、并发 1、低优先级，float32。使用 `gliner2 2.0.0`、`torch 2.8.0+cpu`、`transformers 4.57.6`、`tokenizers 0.22.2`。

复用原下载时已建立的 `model-compat`：只将 tokenizer 配置的 `extra_special_tokens` 键改为兼容当前 Transformers 的 `additional_special_tokens`；原 tokenizer、特殊 token ID 和模型权重不变。完整说明与文件哈希已随结果保存。

首次运行在 NeuralCloud 导入阶段因缺少 `pydantic_settings` 停止，**零次模型调用**。随后复制 GLiNER venv 到本轮独立目录，补齐 NeuralCloud 依赖，导入检查通过后使用新的 `run-v2` 输出目录。初始环境、失败记录和原下载环境均保留；没有更改线上 venv。

运行设置 Hugging Face 离线模式，并阻止 socket 连接。实际运行没有触发网络连接，没有外部模型请求。25 次本地前向调用由 **2 次预热 + 16 个场景 + 1 次 QQ 合成重放 + 6 个关系场景**构成，没有隐藏重试。

## 本轮实测

桥接与来源清单的 Linux 回归 **89 passed、0 failed、0 skipped**。最终 999 个候选源码文件运行后哈希一致；数据库完整性检查通过。以下延迟采用最近秩，排除导入、加载和预热，不是 QQ 端到端耗时。

- 16 个场景的模型抽取：p50 **571.378 ms**，p95 **631.240 ms**。
- 含原文摄取、来源清单签发与引用校验：p50 **581.116 ms**，p95 **640.696 ms**。
- 6 个关系分类：p50 **293.296 ms**，p95 **301.105 ms**。
- 模型加载 **10.032 s**；峰值 RSS **2.689 GiB**。加载前计算权重哈希已读取文件，因此这个加载时间不能视为冷文件缓存结果。

16 个场景返回 **44 个候选片段**，额外 QQ 合成重放返回 **3 个**。这 **47 个片段全部与原文位置一致**，没有修补或丢弃。位置正确不等于提取了足够的内容，也不等于分类正确：

- 28 个预先标注的必要命题片段中，**8 个精确提取、11 个被返回片段覆盖**；仅 **4/16** 个场景覆盖所有必要命题。
- 人物提及集合完全一致 **11/16**。有明确人名的场景为 **9/9**；没有人名的场景仅 **2/7**，其余误把代词等当作 `person`。
- 主模式标签符合 **4/16**。`direct`、否定偏好和意图之间存在语义重叠，`mixed` 和 `ambiguous` 又属于其他维度，这个分数只是本测试主标签符合率，不能命名为“自述归属准确率”。完整混淆记录保留，没有根据结果重标金标签。
- 关系分类符合 **1/6**。其中支持 **1/1**、反证 **0/1**、证据不足 **0/4**；恒猜 `insufficient` 已可取得 **4/6**，因此本轮不支持采用该分类器作为证据门禁。

抽取调用配置 `threshold=0.5`；单标签分类仍返回最高分类，其中有低于 0.5 的分数。这里没有按 0.5 接纳分类，也没有将它表述为通过门槛或校准概率。

## 关键失败和上一轮消息重放

1. “唐宁告诉我，余夏习惯在晚饭后散步”：两个人名都提取正确，但整句被判为 `direct`，没有提取陈述。人名检测不解决转述者和被描述者的区分。
2. 约定十点、实际十点二十分到场：被判为支持“该次准时到场”，将反证判为支持。
3. 按时参加报告讨论会议：被判为支持“已经完成报告”，分数约 **0.905**。高分不能阻止事实范围被扩大。

重放“刚忙完手头的事，我想放松一下，你有什么简单的主意？”时，模型准确定位了“我想放松一下”和“你有什么简单的主意”，但漏掉“刚忙完手头的事”，并将“你”提取为人名。两段陈述通过实际来源核验，说明可尝试由抽取器提供片段位置；它没有完整解决该条消息的经历理解。

## 判断与后续边界

可以保留此实验前端，为后续认知提供有来源校验的候选片段，并与原始上下文一起使用。不能把它当作人物身份绑定器、当前偏好裁决器或支持／反证判断的最终依据，也不能因为片段匹配就直接写入认识。

本轮未训练模型或调整阈值，没有测试长期学习、持续维护、线上争用或完整 QQ 延迟。GLiNER 输出 Python 结构化结果并不能修复原主模型生成情绪、记忆提案时的非法 JSON；主模型的那个阻断仍存在。

线上四份配置哈希与 QQ 复测基线一致，四服务保持 active，活动 release 仍为 `20260920-qq-memory-latency-v1`。

可重跑入口（每次使用新的输出目录和独立测试环境）：

```sh
python tools/production_acceptance/gliner_scenarios.py
python tools/production_acceptance/gliner_scenarios.py --model-path /path/to/verified/model-compat --output /path/to/new-run --execute
```

第一条仅预览，不加载模型；第二条运行离线抽取与来源核验。使用者需安装兼容的 GLiNER 本地推理依赖和 NeuralCloud 依赖。
