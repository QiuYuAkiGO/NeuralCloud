# 果蝇连接组动力学：代码现状评估与分阶段实施方案

> 状态：**Planned / 静态代码评估完成；功能未实现、实验未执行、未部署**。
> 核对日期：2026-09-22（Asia/Singapore）。
> 目标仓库与分支：`QiuYuAkiGO/NeuralCloud` / `codex/grounded-agency`。
> 代码基线：`e268778bbd4df36b6472fc786bef034fcb69790c`，提交说明为 `docs(runtime): document recovery rollout and scaling evidence`。
> 本文编号 09 是文档编号；下述 **CD-01～CD-05 是可选实验旁支，不代表原 GA-01～GA-08 已通过研究验收，也不是其新增前置条件**。
> 本次提交仅包含方案与研究导航，不改变源码、运行配置、生产服务或模型调用。

## 1. 结论与范围

**建议做一个有明确退出条件的小型实验，不建议立即把全果蝇脑加入正式 Misty。** 当前最合适的研究问题是：在相同输入、训练和计算预算下，果蝇连接组子图是否比简单规则、通用循环网络更适合维持短时感知状态、减少重复关注并保留重要变化？

第一项交付应是**离线回放与只读 shadow 注意力建议**，而不是 QQ 发言决策、人格生成、桌面操作或 humanoid 行走。后续可以把经过验证的信号接到低风险的数字形象朝向，但该身体能力尚不能从本分支的接口中确认存在。

需要修正此前讨论中的三个前提：

1. **NeuralCloud 已经具有持续运行架构。** Runtime、独立 Homeostasis、心跳、感知守卫、持续目标和异步情绪处理均已有相应代码；连接组不是使系统“从无到有获得连续性”的必要部件。[C1][C2][C3][C4]
2. **状态历史依赖不是果蝇连接组专属优势。** 一个普通循环网络、带迟滞的规则控制器也可以保持状态。本项目必须检验生物拓扑的额外收益，不能把行为变化或随机性当作成功。
3. **连接图不是可直接执行的完整生物脑。** 本方案把连接组视为结构先验；动力学、输入编码、读出与训练仍需要人工定义。它不提供意识、潜意识、主观疲劳或人格连续性的证据。[R1][R2]

代码审查范围包括本文列出的 Runtime、manifest、Homeostasis、Embodiment、依赖与测试片段，以及研究路线文档。**没有执行完整仓库测试、连接组下载、模型训练、服务器实测，也没有核对 MistyAI / MistyDigitalBody 的当前分支。** 仓库 HEAD 不等于线上已部署版本，历史进度文档中的测试数字不作为本次验收结果。

## 2. 外部研究能支持什么

FlyWire 的成年果蝇脑图谱报告了 139,255 个神经元和超过五千万个化学突触，并提供连接与细胞注释。这里的成年全脑图谱不应与包含腹神经索等部分的完整成年中枢神经系统混称；静态连接数据本身也没有给定运行模型所需的全部动态参数。[R1]

FlyGM 的 `arXiv:2602.17997v3`（2026-06-14）把连接组用于图控制器，并在 `flybody` / MuJoCo 果蝇身体上研究运动控制。其训练包含专家轨迹模仿学习和 PPO，并比较重连图、随机图及其他网络。**这支持“可以研究结构先验”，不支持“无需训练即可获得行为”，也不能直接外推为桌面注意力或人体控制优势。** 本文不把该工作称为已在 NeuralCloud 复现。[R2]

因此，首版采用较易检查的稀疏泄漏率网络，而非以生物真实性为卖点的全脑 LIF。将来采用 LIF 时，膜时间常数、阈值、延迟、可塑性与递质作用假设必须分别记录和验证；不能仅凭递质名称自动宣称某条连接的真实兴奋或抑制作用已确定。

## 3. 现有代码与接入判断

下表中的“已有”仅指固定基线中所检查的实现或接口，不表示线上性能已经验收。

| 代码位置 | 已检查的事实 | 对方案的约束 |
| --- | --- | --- |
| `harness/runtime/scheduler.py` | `RealTimeScheduler` 默认心跳 60 秒，以单调时钟推进；错过心跳不补发历史脉冲 | 保留原调度器；数值积分时钟不能变成第二个自主决策调度器 |
| `harness/runtime/perception.py` | `PerceptionGuard` 默认间隔 5 秒，抑制语义重复观测，处理过期与故障恢复；暂停期有聚合缓冲 | 优先利用已授权观测；不能把 10 Hz 数值迭代宣传成 10 Hz 新视觉输入 |
| `harness/runtime/supervisor.py` | `RuntimeIngress.accept` 先持久化事件；用户抢占记账在发布前发生；随后等待 `_publish` 与队列写入 | 监听器只做有界投递，不得在回调内运行仿真、等待模型或写大文件 |
| `harness/runtime/models.py` / `queue.py` | 控制、动作回执、用户事件优先；感知按 source 合并；`DIAGNOSTIC` 属于控制事件 | 不改受保护事件优先级；不把每个神经步写成感知事件或控制诊断 |
| `harness/homeostasis/models.py` / `service.py` | 七维状态有固定字段；服务在 `SharedDomainLease` 下构造引擎 | 实验层只读已提交快照，不成为第二个 Homeostasis 写入者 |
| `harness/runtime/affect.py` | `AffectWorker` 按 scope 合并任务，限制待处理数；在 grounded-agency 研究条件下禁用 | 不重复制造情绪通道；借鉴隔离思路而非复制其模型调用 |
| `harness/runtime/capabilities.py` | 一个持久、模型拥有的能力机会；绑定 generation、activity_revision、期限、实例和允许能力 | 后续建议只能进入现有仲裁，不能越过模型选择直接执行 |
| `harness/embodiment/ports.py` | 已有 execute / safe-point / verify / reconcile，以及感知、资源、QQ、语音等端口 | 动作事实仍由现有执行与回执链产生；RPC 成功不等于动作成功 |
| `harness/embodiment/contracts.py` | `PerceptionSnapshot` 有时间、置信度、证据及未类型化 `extensions` | 必须新增经验证的特征投影，不能把任意 extensions 当成可信传感输入 |
| `harness/embodiment/capabilities.py` | 当前规范能力集中未声明数字形象 gaze、blink 或关节控制能力 | 不能假定 Live2D/3D 已可控制；`window.focus` 也不等于眼睛朝向 |
| `harness/research/manifest.py` | manifest 冻结且拒绝多余字段；部分 embodiment 字段如 `salience_bias` 被保留为 false | 单独设计新 condition；不要打开被现有校验器禁止的字段 |
| `.cursor/rules/shared-mind.mdc` | 正常运行只有共享认知、记忆、内稳态和 Runtime 所有者 | 数值 worker 是从属计算组件，不是另一个角色、记忆系统或 agent |
| `pyproject.toml` | 已有 NumPy；Torch 属于可选 eval 依赖 | 初期使用 CPU；不要给所有正式部署新增强制 GPU / 仿真依赖 |

代码依据与检查范围见文末 [C1]～[C14]。尤其不能把 `docs/PROJECT_PROGRESS.md` 内不同日期的历史条目拼接成当前线上状态。

### 3.1 内稳态必须使用实际字段

规范维度为 `energy_reserve`、`cognitive_load`、`safety_predictability`、`competence`、`autonomy`、`social_connection`、`novelty`。[C5]

不新增 `hunger`、`curiosity`、`stress`、`care`、`attachment` 等平行状态。可研究已有维度如何影响控制器增益、时间尺度或探索幅度，但这些映射都是**显式实验处理**，不是生物学对应或主观体验。

生产用途最多使用带 revision 的只读投影；`energy.terminal`、critical 保护、停止、权限撤销与用户输入继续由既有确定性路径处理。不能因为神经读出“觉得不重要”而抑制这些事件。[C3][C5][C6]

### 3.2 与原 Grounded Agency 实验隔离

原路线要求事实与解释分离、主模型拥有结构化选择、允许不行动、不预设社会关系结果。Supervisor 已存在 opaque interoception 及研究 binding。[C3][C12]

首版规定：**当 `manifest.grounded_agency` 非空时，禁止启用新的在线 sensorimotor condition。** 仅可在明确隔离的离线回放中开展本旁支。未来确需联合研究，应新建预注册 treatment，冻结输入变换、可见字段、对照组与行为选择权限，重新验收盲化；不能把原研究中的隐藏状态换个名字经神经建议泄漏给模型，也不能把改变后的结果继续记入旧 treatment。

## 4. 用途优先级

| 用途 | 本项目判断 | 首次可检验交付 |
| --- | --- | --- |
| 短时状态、重复刺激适应、变化显著性 | 优先试验，但现有过滤器已经覆盖部分需求 | 同一回放上的规则 / reservoir / 子图对照报告 |
| 只读内稳态到行为参数的调制 | 可与上述任务做独立消融 | 开关调制的成对轨迹，不改内稳态真值 |
| 数字形象看向目标、平滑转头 | 后续低风险闭环，先补身体接口 | 目标跟踪与暂停复位的模拟验收 |
| 可选自主机会的关注建议 | 通过离线与 shadow 门槛后再接入 | 受现有权限、期限、安静期和用户抢占限制的建议 |
| QQ 语义判定、人格、长期记忆、情绪生成 | 不作为连接组替换目标 | 维持现有模块，不改变正式发言策略 |
| 全脑 LIF、人体平衡与行走 | 单独研究课题，不进入首轮工程范围 | 需要独立生物动力学、物理环境和训练预算方案 |

“动作更多”“更随机”“看起来活着”均不是主要成功指标。若规则就能实现所需效果，工程上保留规则即可。

## 5. 建议架构：先旁路观察，再有限参与

```text
现有授权 Provider / Runtime 已接受的观测
                   │
                   ├──────────────> 原 Runtime / Cognitive / Action 路径不变
                   │
                   ▼
       受信任的特征投影 + scope / revision / freshness 检查
                   ▲
        已提交 Homeostasis 快照（只读，携带版本）
                   │
                   ▼
       有界邮箱 ──> 从属 SensorimotorWorker
                         │ rules / reservoir / fly_subgraph
                         ▼
                    AttentionAdvice
                         │
              shadow：仅独立 trace / 离线评分
                         │
              advisory：未来显式启用并通过准入
                         ▼
      Runtime 现有能力机会与模型选择 / 现有动作验证链
```

NeuralCloud 持有实验配置、状态归属和仲裁；MistyAI 继续承担已声明的身体事实与动作；MistyDigitalBody 后续只消费经过授权的形象表现指令。**本次不要求、也不假定两个外部仓库已经具备新能力。**

### 5.1 接线位置

新增 `harness/sensorimotor/` 作为数值与契约模块，新增 `harness/runtime/sensorimotor.py` 作为 Runtime 桥接层。以下路径与类型均为规划，当前基线不存在相应实现。

桥接层可使用 `RuntimeSupervisor.add_listener` 的 `event.accepted` 通知，在既有用户抢占处理之后进行最小投递。但由于发布被 ingress 等待，回调不得等待 worker、调用 LLM、阻塞 IPC 或写磁盘。投递队列满时，丢弃或合并**实验副本**并计数，绝不改变原事件的入队与持久化。[C3][C10]

`event.accepted` 不表示该事件已经完成 Homeostasis 应用。帧只能绑定当时确实已提交且可获得的快照 revision，记录其 age；不能把未来状态或尚未提交的变化填入当前帧。需要更新的内稳态时，应在领域结果确认后的只读发布点补充快照通知。

不向现有 `RuntimeEvent.payload` 注入神经内部状态，不伪造 `BODY_STATE`、`ENVIRONMENT_CHANGE` 或 `QQ_FACT` 来唤醒认知。CD-04 优先让现有能力机会准备逻辑读取有期限的建议缓存，而不是增加一个高频领域事件流。

### 5.2 特征来源与不足

首轮只处理结构化、可追溯的特征，例如观测更新间隔、已验证目标变化、数据置信度与年龄。空间目标坐标、运动强度或视觉新颖度只有在 provider 明确提供并通过验证时才可使用；没有值时置缺失掩码，不补造。

已被 `PerceptionGuard` 抑制的重复快照不会出现在下游接受事件中。因而：当前事件回放可检验稀疏变化响应，但不能独自证明对高频重复视觉刺激的适应能力。后者先用合成特征序列；确需真实重复观测时，再增加**明确授权、与正式过滤分离的只读特征采样点**，并让所有对照使用完全相同的输入。[C2]

默认不向 worker 提供聊天正文、截图、联系人身份、提示词、密钥或可执行指令。可信 source 不能仅凭 payload 内自报字符串确定，必须由 Runtime 接线和授权通道绑定。`extensions` 的新命名空间应有独立严格 schema；拒绝未知字段、NaN、Inf、越界数值、超大数组、错误版本和不可信证据引用。

### 5.3 拟议内部契约

| 拟议类型 | 必需信息 | 明确禁止 |
| --- | --- | --- |
| `SensorFrame` | schema、frame_id、来源事件 / receive_seq、scope、observed_at、记录的 dt、feature_schema、特征与 valid_mask、age / confidence、证据引用、实际快照 revision | 把缺失值解释成观测到零；把模型猜测包装成身体事实 |
| `ModulationSnapshot` | 允许读取的快照 revision、投影版本、时间与有效性；未来盲化处理需独立版本 | 写入内稳态、生成新 drive、泄漏旧研究隐藏语义 |
| `AttentionAdvice` | frame_id、scope、runtime_instance、epoch、manifest/model hash、输入 revision、expires_at、受限数值建议与原因枚举 | 任意自然语言指令、GoalProposal、QQ 发送、shell、HTTP 或桌面动作权限 |
| `ControllerCheckpoint` | 模型 / 特征版本、scope、状态、最后输入游标、随机数状态、数值后端与时钟策略 | 作为人格或长期记忆真值；恢复时重放外部副作用 |

建议不是事实、不是动作回执，也不是完成目标的证据。置信度只表示经校准的模型输出，不是神经活动的生物可信度。

### 5.4 隐私与状态作用域

共享 Homeostasis 仍是唯一权威；实验的递归状态必须按数据权限隔离。私聊、群聊与桌面环境不可共用一个未经隔离的隐状态后再跨 scope 输出。即使不保存正文，隐状态也可能携带私有输入影响。

每个 scope 有独立状态、有限 TTL 和配额；最大活跃 scope 数必须配置。超额时只停止该实验 scope，不损伤正常聊天。离线数据以会话 / 时间段切分，不把同一私聊的相邻窗口分到训练和测试。实验 trace 存受控目录并有清理规则，不进入公共日志、角色长期记忆或群聊上下文。

## 6. 动力学与连接组数据

### 6.1 先使用可解释的最小状态系统

CD-01 的规则基线采用时间衰减、迟滞与冷却；CD-02 增加通用小型 reservoir。它们解决“需要哪种状态与接口”的问题，尚未使用真实果蝇数据，必须如实标注。

CD-03 可先将真实子图作为固定稀疏算子 W，采用工程化泄漏率模型：

```text
alpha = 1 - exp(-dt / tau)
h_next = (1 - alpha) * h + alpha * tanh(g * W @ h + B @ x + C @ m + b)
y = readout(h_next)
```

这里的状态与时间常数是模型参数，不能称为复原的真实神经电活动。初轮冻结 W、输入映射与调制映射，仅离线训练小型 readout；不在正式运行中开展可塑性学习。初始状态、burn-in、dt、缺失输入处理和随机种子都需冻结。

实现先采用有界增益；例如对 tanh 状态更新约束 `||gW||_inf < 1` 可作为保守的收缩条件。该选择会限制长期记忆，需在实验中报告，而不是同时宣称任意长历史保持。仅调整谱半径不能替代数值稳定性与扰动测试。

### 6.2 数据导入门槛

只在 CD-03 引入真实数据。导入器应记录：数据来源与访问日期、release / materialization 标识、许可证与再分发限制、下载文件 SHA-256、神经元 ID 列表、注释版本、子图选择规则、边聚合与权重变换、未知注释比例、最终模型 hash。

根据与任务相关的解剖注释和输入 / 输出路径选择候选子图，不取“ID 最小的前 N 个神经元”。剪裁后应报告边界切断、孤立节点和可达性损失；若任务和解剖关联无法论证，应保留为探索性图先验，不能称为已定位的果蝇注意力回路。

神经元大整数 ID 应以精确整数或字符串保存，避免前端数值精度损失。严格区分**化学突触数**和聚合后的**神经元对边数**；突触计数作为权重是建模选择，不等于直接测得的功能突触强度。

使用稀疏邻接表示，禁止分配全脑稠密 N×N 矩阵。默认仓库只保存小型合成 fixture、导入规则和清单，不提交大数据、访问令牌或限制再分发的资产。没有实际数据与 checksum 的后端必须称为 fixture，不能标记 `fly_subgraph` 实验成功。

### 6.3 资源策略

本次没有硬件实测，不承诺全脑实时速度，也不把用户曾讨论的显卡当作当前运行服务器配置。NumPy 已在核心依赖中；初轮 CPU 即可开展合成与小规模实验。[C13]

以下仅为 CD-01 需冻结或基于开发机校准的**拟议工程预算**：小图上限 4,096 节点 / 200,000 聚合边、10 Hz 数值更新、worker P95 单步不超过 20 ms、实验总 RSS 不超过 256 MiB。单步上限与 RSS 需包含实际矩阵、状态副本和 IPC 开销；达不到时缩小实验，不自动购买或启用 GPU。

父进程投递 P99 目标小于 1 ms；统一基准下原用户路径 P95 延迟增幅目标不超过 5%。应记录硬件、输入速率、scope 数、BLAS 线程数及冷热启动，再判断是否达标。视觉采集、神经积分、建议发布、形象渲染和 LLM 调用各有独立时钟，不能混成一个毫秒级延迟承诺。

## 7. Runtime 生命周期、恢复与失败边界

| 情况 | 规定行为 |
| --- | --- |
| `off` / 旧 manifest 未声明 | 不加载数据、不创建 worker、不读写实验状态、不发起网络或新增模型请求；原路径不变 |
| `shadow` | 计算建议并写独立评估 trace；不改变领域状态、动作、QQ、目标、模型上下文或原队列排序 |
| `advisory` | 仅在未来验收后显式开启；原 Runtime 仍检查权限、期限、用户活动、能力与模型选择 |
| 暂停 / 停止 / 权限撤销 | 立即撤销建议准入并增加 epoch；禁止新增副作用；数值 worker 冻结或有界停止 |
| 恢复 / worker 重启 | 旧 epoch 建议全部失效；首轮采用显式冷启动；记录不连续，不把失去的状态伪称保留 |
| 过期观测 / 无效输入 | 标记缺失并衰减或重置；不外推伪造新的环境事实 |
| 邮箱积压 | 在实验副本内按 scope / 来源合并最新状态，记录 gap 与 drop；不阻塞原 ingress |
| 数值异常 / 超预算 / 进程死亡 | 禁用该实验后端，回到原正常路径；故障信息限频，不导致第二个 Cognitive 启动 |
| 用户新输入或条件版本变化 | 后续建议以 activity_revision / manifest / epoch 校验作废，不能用旧结果抢先发言 |
| 未知动作结果 | 保持原 execute / verify / reconcile 和未知交付规则，禁止神经层重试副作用 |

数值 worker 初期是 Runtime 管理的从属计算组件。CPU 重任务应移出主事件循环；首版影子仿真采用可终止的子进程和有界 IPC，不增加公共服务端口。即使 worker 停顿，父进程也不能等待其计算完成；停止确认可有期限，超时只回收该 worker。

错过积分 deadline 时不快速补算一长串历史刺激。固定最大补步数或直接显式 reset，记录 discontinuity；离线精确重放则使用记录的 dt 和输入顺序，不使用回放时的墙上时钟。[C1]

恢复状态属于可版本化的实验缓存，不替代 Runtime journal 或领域 store。后续需要连续恢复时，以独立原子 checkpoint 保存隐状态和随机数状态；模型、特征或 scope 不匹配即拒绝恢复。恢复不能重发旧建议或外部动作；跨数值后端只承诺经过定义的容差，不无条件承诺逐位一致。

不要把每个 tick / spike 写进 hash-chain journal，也不要每步发送 `DIAGNOSTIC`。高频数值 trace 独立限额、轮转和聚合；只把已准入建议与配置 / 故障状态转换按需记录在 Runtime 可追溯链中。父进程日志不可泄漏敏感 payload。[C3][C6]

## 8. 配置与兼容性设计

拟新增 `SensorimotorCondition`，通过独立 `sensorimotor` 字段引用；这是待实现 schema，当前 manifest 不能直接粘贴使用。

```json
{
  "sensorimotor": {
    "mode": "shadow",
    "backend": "rules",
    "config_version": "sensorimotor-v1",
    "tick_hz": 10,
    "max_nodes": 4096,
    "max_edges": 200000,
    "max_scopes": 8,
    "scope_ttl_seconds": 1800,
    "advice_ttl_seconds": 1,
    "queue_capacity": 128,
    "network_enabled": false,
    "online_learning": false
  }
}
```

此例仅示意 shadow 的配置形状，不是现有 API，也不代表子图资产已准备。连接组模式还须绑定 dataset、subgraph、feature map、readout 的实际 hash；不存在的资产不得用默认 fixture 悄悄替代。TTL 应针对建议消费频率设计，不能把过期的秒级建议挂到分钟级机会中复用。

默认 `mode=off`。配置变化需要现有授权路径和明确重启 / 激活，不热改研究条件。旧 manifest 的 hash 与恢复身份必须特别处理：**不能因为新增默认字段改变旧的 `model_dump`，就无声地改变旧 content_hash 或使既有恢复缓存失效。** CD-01 应定义版本化的规范化哈希策略；对未声明字段的旧配置保持旧规范编码，并用金样本回归。如果必须迁移，应显式记录新旧版本和重建步骤。[C3][C9]

首版禁止非 off 与 `grounded_agency` 共同启用；禁止 online learning 与 worker network。`advisory` 在相应阶段实现前必须校验失败，而非默默按 shadow 运行。不能复用被保留为 false 的 `embodiment.salience_bias` 绕过这些约束。

## 9. 对照实验与判定方式

### 9.1 需要区分的两个问题

**工程问题：** 加入短时状态控制是否改善当前任务？

**结构问题：** 改善是否确实来自果蝇连接拓扑，而非多了参数、训练、时间滤波或更大的计算预算？

| 组别 | 控制器 | 作用 |
| --- | --- | --- |
| B0 | 当前 Runtime 过滤与调度，不加新层 | 衡量额外架构成本与回归 |
| B1 | EMA / 迟滞 / 冷却规则 | 最廉价的时序基线 |
| B2 | 通用随机稀疏 reservoir | 区分循环状态本身与生物拓扑 |
| B3 | 真实、可追溯的果蝇子图 | 结构先验实验组 |
| B4 | 同节点的有向度保持重连图 | 检验特定连接布局的价值 |

B2～B4 使用相同特征、输入 / 输出节点映射、状态维度、读出类型、训练数据、训练预算和调参预算。拓扑主比较先统一无权边及归一化规则；引入突触计数或符号时另设匹配权重分布和增益的消融，不把权重差异当作拓扑收益。B4 检查入度 / 出度保持及重连充分性，不能只随机打乱节点编号，因为同构重标记没有消除结构。

B1 不必硬凑与大图相同参数量，应如实报告更低成本。另做禁用内稳态调制、每帧重置隐状态、输入历史打乱的独立消融。若 B3 只胜过 B0 而不胜过 B1/B2/B4，结论只能是新控制机制有用，不能宣称果蝇拓扑有效。

### 9.2 首轮场景

离线合成与已授权结构化回放覆盖：重复刺激后出现真正变化、相似目标竞争、背景噪声、长时间无输入、缺失 / 过期 / 乱序输入、短促重要变化、来自自身动作的反馈、资源状态变化、用户打断、暂停恢复、队列突发、worker 崩溃、跨 scope 隔离。

自身生成的动作反馈必须有来源和 generation 绑定，避免“自己动作制造刺激 → 再动作”的自激循环。测试标签由独立场景真值或盲评规则定义，不能把待比较的 B1 输出直接当作唯一正确答案。

训练、验证、测试按完整 episode / 会话 / 时间段分开；独立测试集在调参前冻结。初始探索至少使用五个独立训练随机种子和成对测试 episode；这只是最低探索设计，不是统计功效保证。报告按 episode 聚类的置信区间、全部种子、失败样本与资源成本；不能把相关的每个 tick 当作独立样本扩大显著性。

离线 shadow 只能测“拟议唤醒次数 / 注意力轨迹”，不能宣称已经节省真实 LLM 调用或证明部署效果。真实调用量与用户路径代价必须在 CD-04 的独立受控验收中测量。

### 9.3 拟议继续 / 停止门槛

下列数值是实施前应预注册的**建议门槛，尚无达成证据**；任何调整必须在测试集揭盲前完成并记录原因。

| 门槛 | 判据 |
| --- | --- |
| 安全与权限 | 所有故障与回放 fixture 中，受保护事件不经过神经过滤；shadow 无新增模型调用、外部动作或领域写入 |
| 行为不回归 | off 与旧基线、shadow 与 off 的规范化领域结果一致；原消息 / 动作回执 / Homeostasis 结果不变；额外观测日志单独比较 |
| 资源可接受 | 满足第 6.3 节冻结预算；过载与故障降级后不阻塞正常运行 |
| 工程收益 | 在独立标签的重要非保护事件召回率不低于 99% 的前提下，不必要的拟议唤醒相对 B0 至少减少 20%；同时报告漏检和延迟分布及置信区间 |
| 生物拓扑收益 | B3 相对最强低成本时序对照与 B4 的预注册主指标改善置信区间下界大于零，且具有至少 5% 的相对实用改进，并满足同一召回约束和资源预算 |
| 正式接入资格 | 离线门槛通过后仍须独立 shadow 与受控 advisory 验收；不因一段演示视频或单个种子通过而发布 |

对于“越大越好”的指标，以 B3 相对对照的改善定义正值；对于错误率 / 无效唤醒等“越小越好”的指标反向计算。主指标、配对方式和置信水平应在 CD-03 预注册，不在结果出来后择优挑指标；对照基线为零时使用预先约定的绝对改进阈值而非除零。

保护事件零漏检是有限测试要求，不是未来永不失效的保证。若没有稳定额外收益、真实特征流不足、数据许可不清、成本超限或研究条件会被污染，应暂停连接组路线，保留 B0/B1 和独立评估工具。

## 10. 分阶段实施任务

所有阶段初始状态均为 **Planned**。每轮控制在 4～12 个核心源码 / 测试文件，独立提交；文档存在不等于该阶段已实现。下列源码、测试及命令中尚未存在的名称都属于拟新增交付。

### CD-01：契约、基线与离线回放

**目标：** 不接入生产监听，不引入真实连接组，先使实验可定义、可重放、可证伪。

拟新增 `harness/sensorimotor/{__init__,contracts,config,baseline,replay}.py`，相应 `tests/test_sensorimotor_contracts.py`、`tests/test_sensorimotor_replay.py`；在 `harness/research/manifest.py` 增加默认关闭和旧 hash 兼容性校验，并为其增加专项测试。小型合成 fixture 放在新建 `eval/connectome_dynamics/fixtures/`。

交付 `SensorFrame` / `AttentionAdvice` 严格契约、B1 规则、空后端、可注入时钟与确定性回放报告。记录 B0 可比较的原系统行为；若需要构造 Runtime，必须使用仓库要求的显式 isolated development，而不是因为 MockLLM 就绕过所有权。[C11][C14]

验收：非法输入失败关闭；同一配置 / 种子 / 输入产生一致输出；旧 manifest hash 金样本保持；off 不新增任务或数据访问；禁止不支持的 advisory / 非法研究组合。**本轮不包含**子进程在线运行、数据下载、LLM API、QQ 或身体动作。

### CD-02：从属 worker 与纯 shadow 接线

**目标：** 接入真实 Runtime 事件副本，但对原业务决策无影响。

拟新增 `harness/sensorimotor/{worker,reservoir,store}.py`、`harness/runtime/sensorimotor.py`；最小修改 `supervisor.py` 的构造、事件桥接与生命周期清理，增加 `tests/test_sensorimotor_runtime.py`、`tests/test_sensorimotor_faults.py` 和 replay 对照覆盖。

交付有界邮箱、scope 状态隔离、epoch / TTL 校验、子进程停止超时、独立限额 trace 与 B2。读取已提交 Homeostasis 投影，不添加情绪、记忆或目标写入。配置关闭后不导入重型后端。

验收：CPU 卡顿、异常数值、队列满、进程死亡、暂停 / 恢复和用户突发下，原业务结果不变；受保护事件与回执继续正常处理；scope 泄漏测试通过；满足资源预算。先做隔离回放，再在明确授权的数据范围内 shadow，不能为了测验向真实 QQ 群发消息。

### CD-03：真实子图、训练与结构对照

**目标：** 首次引入实际连接组，检验它是否值得保留。

拟新增 `harness/sensorimotor/{connectome,provenance}.py`、`eval/connectome_dynamics/{import_dataset,train_readout,evaluate}.py` 和 `tests/test_connectome_import.py`、`tests/test_connectome_controls.py`。

交付冻结数据清单、子图选择说明、B3/B4、读出训练产物 hash、独立测试集报告与消融。合成 fixture 不得冒充真实数据；大资产不进入普通 git 历史。导入 API、许可证、具体材料化版本需在本阶段重新核验，本文不预设其当前可用性。

验收：数据与图构造可重现；度保持重连确实改变边；所有对照使用同一实验预算；报告失败与置信区间。未通过结构收益门槛时停止 B3 的产品集成，但可保留通用 reservoir 或规则结果。

### CD-04：有限 advisory 与独立在线验收

**目标：** 仅对已有、可选、已授权的自主能力机会提供建议，不取得发言或执行权。

拟修改 `harness/runtime/sensorimotor.py`、`harness/runtime/capabilities.py` 与 condition 校验，增加 `tests/test_sensorimotor_admission.py`、`tests/test_sensorimotor_regression.py` 和离线 / dry-run 验收记录。

现有 CapabilityController 在合法机会准备时读取缓存；必须重新检查 lifecycle、manifest / config generation、runtime_instance、scope、activity_revision、TTL、能力 allowlist、quiet period 与未知动作对账。过期、缺失或异常建议直接采用原基线，不额外触发一次“解释故障”的 LLM 调用。[C8]

第一轮不调整 QQ 收发策略，不抑制用户消息或领域事实，不改变原 GA 模型拥有选择的条件。所有真实副作用仍经过现有模型决策、授权与验证链。在线试验默认单实例、限定 scope、限时并可立即回滚；建议至少观察一个预注册的 8 小时场景窗口，但该时长不是长期稳定性证明。

验收：真实可选调用成本与用户延迟均测得，失败时恢复 off 后不丢历史、不重发未知动作，原测试回归通过。未获显式部署授权前，只提交代码和验收材料，不重启线上服务。

### CD-05：数字形象低风险闭环（可选跨仓库阶段）

**目标：** 让已验证的短时注意信号驱动可观察的朝向，而非人体肌肉或桌面输入。

先核对 MistyAI / MistyDigitalBody 对应分支与实际模型参数，再拟定如 `expression.avatar.gaze.v1` 的能力契约。**该名称目前是提议，不在已检查的规范能力集中。** 坐标系、参数范围、速度 / 加速度限制、有效期、控制 owner、优先级仲裁、暂停中立姿态和 provider 回执必须明确。

优先让确定性插值器负责平滑动作，连接组只选关注目标 / 调制低维参数；不能把平滑效果归功于神经拓扑。语音口型或其他形象控制器若共享参数，必须通过单一控制 owner 仲裁，不允许多写入者竞争。

先使用模拟 provider，再接真实形象；停机、过期、断连时回到已定义中立状态。Provider 的“已应用参数”回执只证明指令应用，若声称实际画面效果还需渲染观测证据。跨仓库同时记录双方 SHA；仅 NeuralCloud 测试通过不足以宣布此阶段完成。

## 11. 自动化验证清单

| 测试层 | 必须覆盖的案例 |
| --- | --- |
| 契约与输入 | 缺失 mask、非有限值、错误 revision、超大 payload、伪造 source、非法 evidence、未知 schema |
| 时钟与状态 | dt 一致、过期、乱序、gap、断续恢复、不同 epoch、状态 checkpoint 版本不匹配 |
| 所有权与隐私 | 默认 off、显式隔离测试、无第二领域 owner、私聊 / 群聊 scope 不串扰、scope 淘汰 |
| Runtime 接线 | 非阻塞监听、邮箱满、数值超时、worker 死亡、用户抢占、暂停 / 停止、控制事件不被降级 |
| 权限与副作用 | shadow 零新增模型 / 外部动作 / 领域写入、advice 不等于证据、未知结果不重发、能力撤销立即失效 |
| 科学有效性 | 重连度数校验、同输入读出和预算、测试集冻结、种子与失败样本完整、无未来信息泄漏 |
| 资源与可观测性 | CPU / RSS / P95 / P99、日志上限、磁盘满、恢复日志不扫描每个神经步、关闭后资源释放 |

未来最小回归命令示例（**本次未执行**）：

```bash
pytest -q tests/test_runtime_supervisor.py
# 以下测试文件在相应阶段新增后运行：
pytest -q tests/test_sensorimotor_contracts.py tests/test_sensorimotor_replay.py
pytest -q tests/test_sensorimotor_runtime.py tests/test_sensorimotor_faults.py
pytest -q tests/test_connectome_import.py tests/test_connectome_controls.py
pytest -q tests/test_sensorimotor_admission.py tests/test_sensorimotor_regression.py
```

实施者还应根据实际修改补跑 manifest / ownership / embodiment / recovery 相关回归，不用上述命令替代全部影响面检查。真实模型或身体测试只能通过独立 opt-in 配置，不在普通测试中下载全图、访问云模型或操作正式 QQ。

每阶段报告至少包含代码 SHA、配置 / 数据 / 模型 hash、测试命令、通过 / 跳过 / 失败、测试环境、是否真实 provider、资源采样方式、未验证事项与回滚结果。状态只能按证据从 Planned → Implemented → Offline-validated → Shadow-validated → Advisory-qualified 逐项更新；禁止把 Mock 测试等同于科学假设成立。

## 12. 回滚、退出与第一轮 Coding Agent 边界

部署层回滚为切回已验收的原配置并重启 / 激活，`sensorimotor.mode=off`；已运行版本必须同时支持代码级禁用和 worker 回收。保留现有 Cognitive / Homeostasis / Runtime 持久状态，不为了回滚删除共享数据库。独立实验缓存可在封存审计后清理；旧建议永不在恢复后补发。

任何保护路径回归、scope 泄漏、隐性 LLM 请求、无法解释的自激循环或资源预算持续超限，都立即停止在线实验。只有行为更花哨、没有对照收益，也应停止扩大图规模。全脑和 humanoid 工作需要单独立项，不能作为本路线默认的下一步。

**下一轮只实施 CD-01：严格契约 + 默认 off / hash 兼容 + B1 规则 + 合成回放 + 无副作用测试。** 不实现 CD-02～CD-05，不下载真实图，不修改正式提示词、不调整 QQ 发言门槛、不部署。若接口审查发现与最新 HEAD 不一致，先更新本评估的基线与差异，再实施最小改动。

本次评估的可交付结论是：**先建立可替换的短时状态实验接口，再让果蝇子图通过对照证明其价值；不让“使用生物脑”成为绕过现有连续运行、安全、所有权与研究约束的理由。**

## 13. 代码证据与外部资料

所有 C 类链接固定于本次评估 SHA；源代码行范围对应本次实际阅读片段，链接不保证未来分支行号不变。R 类资料用于研究边界，不代表本仓库已复现或其结果已迁移到 Misty。

[C1]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/scheduler.py "RealTimeScheduler：默认心跳、单调时钟与不追赶历史 deadline"
[C2]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/perception.py#L1-L200 "PerceptionGuard 与 PauseObservationBuffer"
[C3]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/supervisor.py#L150-L735 "Ingress、研究绑定、领域快照、恢复、listener 与身体终止入口"
[C4]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/affect.py#L1-L205 "按 scope 合并的 AffectWorker 与研究条件隔离"
[C5]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/homeostasis/models.py#L1-L145 "七维 Homeostasis 及受证据约束的事实类型"
[C6]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/models.py#L1-L190 "事件类型、优先级与保护边界"
[C7]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/embodiment/ports.py "现有身体端口；另核对同目录 contracts.py 1–265 与 capabilities.py 1–145"
[C8]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/capabilities.py#L1-L185 "CapabilityController 的 owner、activity_revision、generation 与失效检查"
[C9]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/research/manifest.py#L1-L200 "冻结 condition、保留字段校验和现有能力调度配置"
[C10]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/harness/runtime/queue.py#L1-L180 "事件合并、容量限制与控制槽保留"
[C11]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/.cursor/rules/shared-mind.mdc "单一共享心智与显式开发隔离规范；另核对 homeostasis/service.py 的领域 lease"
[C12]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/docs/research/grounded-agency/README.md "原 GA-01～GA-08 路线及其研究不变量"
[C13]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/pyproject.toml "Python、NumPy、可选 Torch 与测试依赖"
[C14]: https://github.com/QiuYuAkiGO/NeuralCloud/blob/e268778bbd4df36b6472fc786bef034fcb69790c/tests/test_runtime_supervisor.py#L1-L175 "现有 Runtime 测试构造方式；不是本次测试执行证据"
[R1]: https://collaborate.princeton.edu/en/publications/neuronal-wiring-diagram-of-an-adult-brain/ "Dorkenwald et al., Neuronal wiring diagram of an adult brain, Nature 634, 124–138 (2024), DOI: 10.1038/s41586-024-07558-y"
[R2]: https://arxiv.org/html/2602.17997v3 "Jin et al., Whole-Brain Connectomic Graph Model Enables Whole-Body Locomotion Control in Fruit Fly, arXiv v3, 2026-06-14；重点参照 3.2 与 4.1"
