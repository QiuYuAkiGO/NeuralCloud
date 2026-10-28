# Qwen3.8-27B Memory Encoder 实施计划

日期：2026-10-19。用途：在另一台配备 RTX 5090 的电脑独立执行。

## 1. 目标与固定方案

交付完成训练、可以实际调用的记忆编码器和配套读取模块，让 Qwen3.8-27B 分别接收对话和记忆，用于跨会话、长期连续对话。

- 回答主干：`Qwen/Qwen3.8-27B`，revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`。
- 记忆来源：NeuralCloud 自研系统、Hy-Memory；每次选择一种来源，共用编码器。
- 成品：模型权重、Python SDK、独立 HTTP 服务、NeuralCloud 可选客户端、训练和验收工具。
- 新增 Transformers 运行时；保留现有 GGUF / llama.cpp 服务，不切换生产流量。
- 资源：单张桌面 RTX 5090 32GB；冻结量化主干，仅训练新增模块。
- 数据：合成中文数据和隔离测试库，不使用真实用户对话，不依赖付费模型生成训练数据。

模型具有 64 层混合注意力、隐藏维度 5120，不能照搬此前 4B 模型的层号和显存预算。[官方配置](https://huggingface.co/Qwen/Qwen3.8-27B/blob/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0/config.json)

## 2. 输入接口与双来源适配

流程：记忆来源 → 权限与版本校验 → MemoryBundle → Memory Encoder → memory_states → Qwen 读取模块 → 回答。

`POST /v1/memory/chat` 接受如下 JSON：

```json
{
  "messages": [{"role": "user", "content": "我们上次决定采用什么训练方案？"}],
  "memory": {"provider": "neuralcloud", "query": "记忆编码器的训练方案", "limit": 8},
  "max_new_tokens": 256
}
```

`provider` 支持 `neuralcloud` 和 `hy_memory`。身份和权限由服务端认证上下文确定，返回回答、模型版本、实际编码清单及用量。

- `MemorySource.read(query, context) -> MemoryBundle`
- `MemoryEncoder.encode(bundle) -> EncodedMemory(states, mask, manifest)`
- 模型前向新增 `memory_states`、`memory_mask`。

MemoryBundle 使用版本化 JSON，保留记录 ID、版本、主体或未绑定称谓、内容类型、有效时间和证据引用。SDK 和训练工具支持直接读取；在线服务通过适配器构造。

NeuralCloud 复用 `ProductionMemoryStore.recall()`、`validate_references()`，保持证据、解释、转述、否定和争议状态的区别。Hy-Memory 固定 [v1.0.4](https://github.com/Tencent-Hunyuan/Hy-Memory/releases/tag/v1.0.4)，commit `688878171e9d98ade3871fecf7eca603c7c43a76`，通过独立环境的 SDK 读取。`user_id` 表示存储范围，不直接当成主体；缺失信息标记未知，缺少版本时用内容指纹。适配器不增加记忆写入器。

manifest 记录实际编码的内容、记录版本和槽位，不将检索命中等同于模型读取，也不能充当写入证据。每轮重新读取、校验和编码；KV 缓存和 DeltaNet 状态不跨请求复用。

## 3. 结构与训练

编码器采用 `google-bert/bert-base-chinese`，revision `8f23c25b06e129b6c986331a13d8d025a92cf0ea`；冻结词嵌入和前 8 层，训练后 4 层。输出从 768 投影到 512 维，接两层可训练 Resampler。每块最多 128 个 BERT tokens，生成 32 个槽位；最多 16 块，即 512 × 512 的记忆张量。分块保留主体、类型、时间和定位；超过预算明确记录，不丢弃限定或反证来保留半条记录。

加载 Qwen 文本分支，在第 16、32、48、64 层后增加 512 维、8 头的门控交叉注意力。tanh 门控零初始化；空记忆跳过。主干采用 NF4 4-bit、双重量化、BF16 计算，必要的状态计算保持原生高精度。冻结主干参数，保留损失向编码器传播的计算图。[量化依据](https://huggingface.co/docs/transformers/quantization/bitsandbytes)

- 数据集：训练 20,000、验证 2,000、测试 2,000；人物、故事、模板族分组隔离。
- 覆盖事实、多主体、偏好更新、时间、转述、否定、任务、干扰、未知和记忆中的指令文本。
- 包含问题不变、记忆与答案同步变化的反事实配对。
- 先简单事实后复杂情境；仅回答位置计算交叉熵。
- micro-batch 1，梯度累积 8，学习率 1e-4，梯度裁剪 1.0，最多 3 epochs，验证集选检查点。
- 非重入梯度检查点，训练关闭生成缓存；对话加答案最多 2,048 tokens，记忆另计。
- 比较无记忆、原文记忆、同数据文本记忆 LoRA 基线；同证据，报告实际资源差异。

## 4. 执行与验收

### A：环境和协议

Ubuntu 24.04 x86-64（Windows 使用 WSL2），单张 5090，至少 64GB RAM、200GB 可用磁盘。起始版本：Python 3.12、PyTorch 2.14.1+cu130、Transformers 5.20.1、bitsandbytes 0.50.2、Accelerate 1.15.0。组合需要实测，通过后生成完整锁文件。

先完成适配器、JSON 契约、合成数据生成器和 CPU 测试。

### B：单卡验证

检查权重完整加载、空记忆一致性、真实梯度和参数更新、检查点恢复；测试 512 / 1,024 / 2,048 tokens 及最大槽位；验证缓存生成与完整前向一致。

峰值显存不超过 28 GiB，无中断梯度和非有限损失。未通过即停止正式训练，输出资源报告，不自动更换主干。27B 的 BF16 权重约需 54GB，GGUF 推理成功不证明新增模块训练能够装入单卡。

### C：训练和质量

- 事实正确率 ≥90%；反事实配对成功率 ≥90%；信息不足正确弃答率 ≥90%。
- 综合正确率不低于表现更好的文本记忆基线超过 5 个百分点。
- 越权进入编码器的记录数为 0；更正、删除和权限变化全部通过。
- 两个真实来源分别完成读取、编码、生成的端到端验收，区分检索、适配和读取错误。
- 回放 10,000 条事件、逻辑跨度 90 天，验证跨会话、更新、失效和重启；不将回放当成运行 90 天的稳定性证明。
- 记录首字时间、完整时间、峰值显存、训练吞吐；实测前不承诺生产延迟。

## 5. 交付和跨机入口

交付配套权重及主干 revision / 量化 / tokenizer 配置，双来源适配、SDK、HTTP、可选客户端，生成器、固定测试集、基线和验收报告，依赖锁、校验值、许可证及操作说明。

CLI：`python -m harness.memory_encoder`，子命令 `doctor`、`prepare-data`、`profile`、`train`、`evaluate`、`serve`。按此顺序执行；路径通过配置或环境变量传入。

相关尚未跟踪的评测代码和合成夹具按明确清单打包，不带真实记忆、密钥和虚拟环境。编码器与读取模块配套发布，更换主干后重新验收。

本文已保存。实际完成情况与执行命令见同目录的 `memory-encoder-handoff.md`；未完成 GPU 训练和验收前，不将工程实现称为已训练成品。
