# Memory Encoder 跨机执行说明

批准的设计见 2026-10-19-memory-encoder-qwen38-plan.md。当前交付为可测试的工程实现和训练工具，尚无完成训练并验收的 Qwen3.8-27B 适配权重。CPU 小模型测试、合成回放和随机初始化参数均不是最终模型。

## 当前实现

- JSON / SDK 契约、NeuralCloud 只读适配、独立进程 Hy-Memory 读取桥。
- 中文 BERT、整条记录预算、Resampler、第 16/32/48/64 层门控读取。
- 小型真实 BERT + Qwen 混合注意力测试，覆盖梯度检查点、缓存和恢复。
- GPU 显存门禁、训练恢复、无记忆 / 原文 / 文本 LoRA 对照评测。
- 验收通过后启动的 HTTP 服务、显式选用的 Python 客户端。
- 可重现的 20,000/2,000/2,000 样本、10,000 事件回放和源码打包。

生产路由、GGUF 和线上记忆未修改。本机为 Windows ARM64、无 CUDA；数值测试在独立 x64 CPU 环境运行，GPU 环境需另验。

本次验证记录：77 项新功能与相关记忆回归通过；固定 Qwen/BERT tokenizer 的 66 组模板与预算检查通过。真实 NeuralCloud 存储回放完成 10,000 事件、90 天逻辑跨度、两次重启、8,953 次失效检查、9,230 次范围检查。Hy SDK 桥接尚未执行真实库与 27B 联合验收，GPU 训练和最终质量门槛仍待执行。机器可读结果见 tools/memory_encoder/cpu-verification.json。

## 5090 机器执行顺序

Ubuntu 24.04 x86-64（Windows 使用 WSL2），Python 3.12、CUDA 驱动、32GB 显存、64GB RAM、200GB 可用磁盘。训练需要可用的整张 GPU；工具不会停止既有推理服务。

在仓库或交付包根目录执行：

~~~bash
python3.12 -m venv .venv-memory
source .venv-memory/bin/activate
python -m pip install --upgrade pip
python -m pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cu130
python -m pip install -r tools/memory_encoder/requirements-gpu.txt
python -m pip install -e '.[dev]'
python -m harness.memory_encoder doctor --out data/memory-encoder/doctor-gpu.json
python -m pytest tests/test_memory_encoder_contracts.py tests/test_memory_encoder_modeling.py -q
python -m harness.memory_encoder prepare-data --out data/memory-encoder/synthetic-v1
python -m harness.memory_encoder profile --out data/memory-encoder/profile-01
~~~

已有数据目录不能重复生成。profile 下载固定 revision 原始 HF 权重，使用 NF4 双重量化，不读取 GGUF。检查 profile.json 的 passed=true、每个长度 peak_reserved_gib≤28。失败即停止并保留报告，不自动降低主干型号。

~~~bash
python -m pip freeze --exclude-editable > data/memory-encoder/requirements-gpu.lock.txt
python -m harness.memory_encoder train --data data/memory-encoder/synthetic-v1 --profile data/memory-encoder/profile-01/profile.json --out data/memory-encoder/native
python -m harness.memory_encoder train --data data/memory-encoder/synthetic-v1 --profile data/memory-encoder/profile-01/profile.json --arm text --out data/memory-encoder/text-lora
python -m harness.memory_encoder evaluate --data data/memory-encoder/synthetic-v1 --checkpoint data/memory-encoder/native/best --text-lora data/memory-encoder/text-lora/best --out data/memory-encoder/evaluation
python -m harness.memory_encoder replay --data data/memory-encoder/synthetic-v1 --out data/memory-encoder/replay-01
~~~

默认每 100 次优化器更新验证并保存 last；恢复时在原 train 命令增加 --resume data/memory-encoder/native/last。best 为验证损失最低的配套权重。输出目录不能被无恢复参数的训练覆盖。验证集选模型，测试集只验收。

原生对话加答案预算为 2,048 tokens；文本对照为容纳同一份原文证据允许 4,096 tokens。超预算报错，不截断记忆。质量报告采用合成答案包含与错误答案排除规则，不是开放域语义评分，不证明真实用户长期效果。

## 两个真实来源的连接与验收

复制 tools/memory_encoder/service.example.json 到私有配置目录，修改路径、owner/preset 和服务端身份映射。NeuralCloud 以 SQLite mode=ro 打开身份匹配的既有数据库，不初始化或迁移。

Hy 使用独立环境和固定 checkout：

~~~bash
git clone https://github.com/Tencent-Hunyuan/Hy-Memory.git ../Hy-Memory
git -C ../Hy-Memory checkout 688878171e9d98ade3871fecf7eca603c7c43a76
python3.12 -m venv ../hy-venv
../hy-venv/bin/python -m pip install -e ../Hy-Memory
~~~

私有 Hy 配置外层为以下结构，sdk_config 填写该部署实际向量库、缓存、图存储和 embedder：

~~~json
{"mode":"pro","sdk_config":{}}
~~~

SDK 固定 pro 模式和 chat/legacy 搜索，不触发 LLM query rewrite。桥接器强制关闭搜索后台去重删除、访问计数回写、搜索历史记录和 pipeline trace，并禁用 coding 路径。嵌入的计算位置和费用取决于原部署，合成测试使用本地嵌入服务。SDK 的基础设施仍可能维护缓存或自身日志；桥接器不调用记忆 add/update/delete。公开 get() 允许返回记录后，从固定版本 SDK 存储对象补齐 session_id 和有效时间；版本不符拒绝运行。

Hy user_id 必须按权限范围隔离，例如各私聊和群聊独立分区；不能把所有对话放同一分区后依靠模型过滤。hy_agent_ids 必须非空；session 过滤可收紧范围。无时区的 Hy 时间按配置 timezone 解释。

source-check 只接受 audience 以 synthetic: 开头的测试身份。先在两个隔离库准备事实、更新、删除、越权夹具，再创建 JSONL 案例：

~~~json
{"id":"nc-fact","principal":"synthetic-test-client","kind":"fact","messages":[{"role":"user","content":"小林喜欢什么？"}],"memory":{"provider":"neuralcloud","query":"小林 喜欢","limit":8},"required_records":["fixture-current"],"forbidden_records":["fixture-private"],"expected_answer":"摄影","forbidden_answers":["游泳"]}
~~~

每个来源须包含 fact/update/delete/scope 四类。update/delete 需 invalidated_bundle，即更改前适配器读出的完整 MemoryBundle，检查器必须确认它已失效。scope 须列 forbidden_records。夹具准备和变更只作用于隔离测试库，不提供在线写入能力。

~~~bash
python -m harness.memory_encoder source-check --service-config /absolute/path/to/synthetic-service.json --cases /absolute/path/to/synthetic-source-cases.jsonl --checkpoint data/memory-encoder/native/best --out data/memory-encoder/source-check
python -m harness.memory_encoder accept --checkpoint data/memory-encoder/native/best --profile data/memory-encoder/profile-01/profile.json --evaluation data/memory-encoder/evaluation/evaluation.json --sources data/memory-encoder/source-check/sources.json --replay data/memory-encoder/replay-01/replay.json
~~~

accept 将对应架构、数据集和适配器哈希的通过报告收入权重目录。缺少真实来源、LoRA 对照、长历史回放或 GPU 报告不会批准发布。

## 服务与 SDK

在进程环境设置 token_env 指定的令牌，不将令牌写入仓库或源码包。每个令牌绑定服务端 principal / audience / Hy 分区，HTTP JSON 不能覆盖权限。

~~~bash
python -m harness.memory_encoder serve --service-config /absolute/path/to/service.json --checkpoint data/memory-encoder/native/best --host 127.0.0.1 --port 8091
~~~

~~~python
import os
from harness.memory_encoder.client import MemoryChatClient

with MemoryChatClient("http://127.0.0.1:8091",
                      api_key=os.environ["NC_MEMORY_TEST_TOKEN"],
                      provider="neuralcloud") as client:
    result = client.chat([{"role":"user","content":"我们上次决定的方案是什么？"}])
    print(result["answer"])
~~~

离线 SDK 可解析 MemoryBundle JSON，再调用已加载的 model.encoder.encode(bundle)，返回 states、mask、manifest。离线调用方负责授权；HTTP 服务通过来源适配器建立授权边界。

v1 单请求串行、非流式；每轮重读和编码，不跨轮复用 KV 或 DeltaNet 状态。生成前后验证版本，变化时返回 409，不返回过时答案。manifest 只证明编码输入，不能证明引用正确，也不替代自研写入链证据。

## 打包、版本和许可证

~~~bash
python -m harness.memory_encoder bundle --out dist/memory-encoder-handoff.zip
~~~

源码包按白名单收录源码、测试、文档、既有合成评测代码和文件哈希，排除 data、私有配置、密钥、虚拟环境和权重。训练完成后另行转移 best、其 evidence、GPU 依赖锁和所需模型缓存；主干 revision 与适配器成对保留。

原始主干和 BERT 从固定 revision 下载，不重新分发在源码包内。Qwen3.8、BERT、Hy-Memory 上游使用 Apache-2.0，分发上游权重或代码时保留许可证和通知。其他依赖以其各自许可证为准：

- https://huggingface.co/Qwen/Qwen3.8-27B
- https://huggingface.co/google-bert/bert-base-chinese
- https://github.com/Tencent-Hunyuan/Hy-Memory/blob/688878171e9d98ade3871fecf7eca603c7c43a76/LICENSE

质量和显存门槛尚待 5090 实测。90 天测试为合成逻辑时间回放，不证明实际连续运行 90 天，也不保证无损保留任意长度的全部历史。
