# 05 · 里程碑、验收标准与技术栈

> 本文定义建设顺序（M0–M5）、V1 量化验收标准、WSL2 适配后的技术栈，以及面向实体机器人的扩展路径。

---

## 1. 六个里程碑

### M0 — Digital Body

**目标**：模型能看到 Ubuntu 桌面，并稳定控制鼠标键盘。

交付：

- Docker（WSL2 backend）内的 Ubuntu + Xvfb + XFCE 桌面环境，noVNC 人工观察窗；
- Observation Backend V0（screenshot + 窗口信息 + 时间）；
- Action Backend V0（PyAutoGUI/xdotool 风格的统一 `GUIAction` 接口）；
- 动作执行结果校验（执行前后截图 diff / 窗口状态比对）。

出口条件：基础 GUI 动作成功率 >95%。此阶段甚至不需要人格。

### M1 — Computer Agent

**目标**：AI 可自主完成 GUI 小任务（打开文件、整理目录、在本地 Wiki 查资料并写笔记）。

交付：

- Computer-Use Controller 三层：Planner / Grounder / Executor + Verifier；
- 任务定义格式（含强制 `success_criteria`）与外部验证器；
- 引入 OSWorld 类任务作为外部 benchmark 基线。

出口条件：一组预定义桌面任务可端到端完成并由外部证据验证；无限循环可检测。

### M2 — Cognitive Harness

**目标**：从 GUI automation agent 变为 persistent character agent。

交付：

- 六层状态/记忆体系落地（PostgreSQL + pgvector）：Persona Core、Episodic、Semantic、Relationship、L/M/S Psychological State、World State；
- 混合检索 + 反向检索；Memory Writer 候选-证据链管线；Dual Memory（事实层 + 角色视角层）；
- Context Compiler（预算管理 + provenance 追踪）；
- 门控 Subagent V1 三个：Memory Curator、Social/Intent Interpreter、Experience Evaluator；
- 在线推理 9 阶段链路与 Fast/Deep Path 分流（见 [02-inference-flow.md](02-inference-flow.md)）。

出口条件：角色在多轮对话中表现出可观测的人格一致性；每条上下文可追踪来源；Persona Core 非授权修改为 0。

### M3 — Autonomous Life

**目标**：没人指挥时自主活动。

交付：

- Autonomous Drive System（curiosity/social/creation/organization/learning/entertainment）；
- Curiosity Queue 与 Goal Manager（目标可生成/暂停/失败/完成）；
- IDLE 循环：Observe → Update World Model → Check Goals → Check Curiosity → Check Social Context → Potential Action。

出口条件：AI 可在离线小世界中连续自主活动，产生非重复、有目的的行为序列；所有自主目标带 success_criteria 并经外部验证。

### M4 — Experience Learning

**目标**：正式产生后训练数据。

交付：

- 五类数据导出管线（Observation / Action / Cognitive / Outcome / Correction）；
- Chosen/Rejected 偏好对构造器；独立外部 evaluator；
- 数据集版本管理（与 model/prompt/schema 版本绑定）。

出口条件：可从 trajectory 自动导出可用于 SFT/DPO 的数据集；失败轨迹完整保留。

### M5 — Long-running Experiment

**目标**：持续运行，观察演化。

交付：

- 24–72h 连续运行能力（崩溃自恢复、快照回滚）；
- Web dashboard：实时观察 AI 活动、记忆增长、目标状态、人格状态；
- Harness schema 演进报告（从真实失败中提炼的新状态变量建议）。

出口条件：连续运行达标；产出的数据与失败分析能够指导 Harness V2 的具体修改——**这个阶段的数据会反过来告诉我们 Harness V2 究竟应该增加什么。**

---

## 2. V1 验收标准（量化）

第一版不用"感觉它像真人"作为验收标准：

| 指标 | 初期目标 |
| --- | --- |
| GUI action 成功率 | >95% 基础动作 |
| 环境 rollback | 100% 可恢复 |
| trajectory 完整率 | >99.9% |
| Memory 可追溯率 | 100% 有 evidence |
| Context 可解释率 | 100% 能追踪来源 |
| Persona Core 非授权修改 | 0 |
| Host escape（越出隔离环境） | 0 |
| GUI infinite loop | 能检测并中断 |
| autonomous goal | 可生成/暂停/失败/完成 |
| memory contradiction | 能发现而非直接覆盖 |
| 相同 snapshot 重放 | 可比较模型差异 |
| 连续运行 | 第一目标 24–72 h |

---

## 3. 技术栈（Windows + WSL2 适配版）

| 层 | 选型（V0/V1） | 适配说明 |
| --- | --- | --- |
| Host | **Windows + WSL2** | 原方案为纯 Linux 宿主机 |
| 虚拟化（数字身体） | **Docker Desktop（WSL2 backend）**；V1+ 评估 WSL2 嵌套 KVM / Hyper-V VM | 原方案 KVM/QEMU + libvirt，降级为容器起步，接口抽象保证可回迁 |
| Guest 桌面 | **Ubuntu LTS + Xvfb + XFCE**（不用 Wayland） | 与原方案一致，Xorg 会话确定性最好 |
| GUI Action | **PyAutoGUI / xdotool 抽象层**（XTEST 注入） | 与 OSWorld 接口同构 |
| Harness 语言 | **Python**（研发速度优先；实时组件未来可重写 Rust/C++） | 与原方案一致 |
| Service API | FastAPI（gRPC 备选） | 与原方案一致 |
| State DB | **PostgreSQL** | 与原方案一致 |
| Vector | **pgvector**（Qdrant 备选） | 与原方案一致 |
| Object storage | 本地文件系统；后期 MinIO | 与原方案一致 |
| 模型运行时 | **llama.cpp / vLLM（WSL2 CUDA）/ TensorRT-LLM**，经 HTTP API 暴露 | 原方案同机部署；适配为 Windows 本机 GPU + WSL2 服务化 |
| Embedding | 独立小 embedding model | 与原方案一致 |
| 辅助小模型 | 1–3B（Memory Curator 等） | 微模型当 memory daemon，不当最终角色 |
| 主角色模型 | 后训练后的主 RP 模型，建议 **30B-A3B 级 MoE**（详见 §4） | 与原方案一致 |
| Trace format | JSONL + DB metadata | 与原方案一致 |
| 可视化 | Web dashboard | 与原方案一致 |
| 版本控制 | Git | — |
| 实验元数据 | model / prompt / schema / 快照版本全部固定记录 | 与原方案一致 |

---

## 4. 主模型选型依据

来自模型规模-角色扮演研究的核心结论（选型背景，不影响 Harness 架构）：

- 角色扮演存在 scaling law 但远弱于数学/代码，且增长不稳定；**3B→14B 的提升经常比 32B→72B 明显得多**。
- 概括：3B 在模仿"人格的输出"；30B 在模拟"人格的状态"；80B+ 开始模拟"人格产生行为的原因"。
- **角色专训收益对小模型巨大**：专门训练的 7/8B ≥ 通用 70B 的结果在多个 benchmark 反复出现。
- 推理能力 ≠ 角色能力：CoT 经常降低角色扮演质量；理想状态是"有足够强的隐式推理，但不让显式理性分析覆盖人物本身"。
- **MoE 改变部署结论**：30B-A3B（激活约 3B）在消费级/边缘硬件上兼具容量与速度，是高质量角色模型的甜点区。

工程结论：**30B 主模型 + 显式人物状态 + 长期记忆 + 关系状态 + persona-aware 后训练**，大概率优于"裸 100B + 一张超长角色卡"。

---

## 5. 代码仓库组织（建议）

```text
NeuralCloud/
├── docs/harness/            # 本文档集
├── harness/
│   ├── core/                # SituationState / ContextPackage / 数据模型
│   ├── memory/              # 六层记忆 / Memory Writer / 检索
│   ├── compiler/            # Context Compiler
│   ├── router/              # Complexity Gate / Fast-Deep 分流
│   ├── subagents/           # Curator / Interpreter / Evaluator
│   └── validator/           # Response Validator
├── body/
│   ├── perception/          # Observation Backend（screenshot / a11y / fs）
│   ├── action/              # Action Backend（GUIAction 执行与校验）
│   └── env/                 # 容器/VM 定义与构建
├── runtime/
│   ├── drives/              # Drive System / Curiosity Queue / Goal Manager
│   └── risk/                # Risk Engine / 权限门
├── infra/
│   ├── trace/               # Trajectory Recorder
│   ├── snapshot/            # Snapshot Manager
│   └── db/                  # PostgreSQL / pgvector schema 与迁移
├── eval/                    # 外部 evaluator / benchmark（OSWorld 类）
└── datapipeline/            # 五类数据导出 / SFT-DPO 数据集构造
```

该目录已给实体机器人留出空间：未来新增 `body/perception/camera`、`body/action/vla` 等后端，Harness 体系不变。

---

## 6. 附录：实体机器人扩展路径（远期参考）

> 本附录仅为架构预留方向，**不属于当前建设范围**。

### 6.1 平移路径

| 数字身体时代 | 实体机器人时代 |
| --- | --- |
| Screenshot / a11y / fs events | Camera / microphone / tactile skin |
| Mouse / keyboard（GUIAction） | VLA / Skill System / 机械执行器 |
| World State（桌面世界） | World State（物理世界） |
| Risk Engine（数字权限） | 独立 Safety Controller（物理限力/急停） |
| 六层记忆 / Persona / Drive / Harness | **原样保留** |

### 6.2 算力参考（延迟目标：1.5s 内开始回复）

- 甜点配置：**30B-A3B MoE 主模型 + 1–3B 认知辅助**，Jetson AGX Thor 级（128GB 统一内存、273GB/s、最高约 130W）单请求实测约 60–85 tok/s，可行且推荐；
- 开发期：本机 RTX 5090 级 GPU 验证架构远比购置 Thor 经济（Qwen3-30B-A3B Q4_K 短上下文 decode 可达约 350 tok/s）；
- 关键手段：ASR/RAG/状态装配与用户说话时间重叠；LLM generation 与 TTS 流式重叠；Fast Path P50 首次发声 600–900ms、P95 <1.5s。

### 6.3 实体化的六套系统

Character Brain（人格/语言/社会判断/长期记忆）、Perception Brain（视觉/声音/空间）、Task/VLA System（意图→任务→技能）、Real-time Motion System（WBC/平衡/IK/MPC）、Distributed Nervous System（电机/编码器/手/触觉皮肤）、Independent Safety System（急停/限力/碰撞/电气安全）。前三层适合 GPU/神经网络；后三层必须是实时控制器 + MCU/FPGA + deterministic software——**即使 30B 模型卡死 5 秒，机器人也不能摔倒**。

---

## 7. 与其他文档的关系

- 原则与总架构：见 [00-overview.md](00-overview.md)。
- 各里程碑涉及的 Harness 内部机制：见 [01-cognitive-harness.md](01-cognitive-harness.md)、[02-inference-flow.md](02-inference-flow.md)。
- M0/M1 对应数字身体建设：见 [03-digital-body.md](03-digital-body.md)。
- M3/M4/M5 对应自主与数据：见 [04-autonomy-and-data.md](04-autonomy-and-data.md)。
- 记忆子系统 V1（M2 记忆部分）操作手册：见 [06-memory-subsystem-usage.md](06-memory-subsystem-usage.md)。
