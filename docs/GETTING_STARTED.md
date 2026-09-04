# NeuralCloud 新手入门：它如何让角色“记得、判断并像自己一样说话”

> 这是一份面向第一次接触本项目的入门说明。目标不是把每个类都罗列一遍，而是先建立一张清楚的“心智地图”：项目解决什么问题、各模块为什么存在、一条消息如何走完整条链路，以及代码目前真正运行到了哪一步。

## 1. 先用一句话理解项目

NeuralCloud 是一个**角色智能体运行时**。大语言模型负责“演出角色”，项目中的 Harness 则负责在演出前后完成记忆、状态、检索、分流、约束、校验和审计。

可以把普通聊天模型想成一位临场发挥的演员：它会说话，但容易忘记以前发生的事，也可能突然性格漂移。

NeuralCloud 更像一整套剧组：

- `Persona Core` 是角色设定，不允许演员随意改剧本。
- 记忆系统是场记，保存发生过的事情和逐渐形成的认识。
- `Context Parser` 是信息整理员，先把用户的话拆成结构化线索。
- `Complexity Gate` 是分诊台，判断这句话要快速处理还是认真分析。
- `Social Interpreter` 是角色顾问，只给判断，不替角色说台词。
- `Context Compiler` 是提词器，只把当前最有用的信息送给主模型。
- `Character Model` 是演员，专心生成符合角色口吻的回复。
- `Response Validator` 是出场前审片，发现越界就要求局部重演。
- `Runtime Supervisor` 是长期运行时的值班导演，负责事件、优先级、暂停、恢复、动作和故障处理。

项目最核心的设计思想是：

> Harness 负责锚定角色、选择信息、划定边界；主模型只负责把角色演出来。

## 2. 当前项目做到哪里了

先分清“统一双工程入口”“NeuralCloud 低层入口”和“兼容单进程入口”，阅读代码会轻松很多。

### 2.1 Windows 默认路径：MistyAI + NeuralCloud

Windows 11 的公开开发入口是根目录 `dev.ps1`。它先检查环境和端口，再部署并启动以下拓扑：

```text
Windows 11
├── NeuralCloud（Windows Python 3.12）
│   ├── GUI Runtime facade（8766）
│   ├── Runtime Supervisor / REST + SSE（8770）
│   ├── Cognitive 服务（8771）
│   └── Homeostasis 七维确定性引擎（8772）
├── WSL2 Ubuntu 24.04
│   └── MistyAI Body Service（systemd，8787）
└── Docker Desktop（Linux containers）
    ├── MistyAI desktop（含 noVNC 6080）
    ├── MistyAI agent
    └── loopback-gateway
```

NeuralCloud 四个服务只监听本机。Runtime 固定使用 `grounded-mistyai-v1.json` 和真实 `mistyai` provider；MistyAI 身份、身体实例或必需能力协商失败都会中止启动，不会悄悄回退到 simulator。`-Mode demo` 只把 NeuralCloud 的语言模型切换为 MockLLM，不会模拟身体。

### 2.2 低层与兼容入口

`start.ps1` / `stop.ps1` 保留为 NeuralCloud 四进程的低层启停器，适合已经准备好环境后的单工程调试。它们不会安装依赖、部署或启停 MistyAI，也不具备统一入口的前置检查和事务式清理。

`python -m gui.server` 与 `start.sh` 仍可启动旧的进程内 `HarnessChatService`，适合 Cognitive Pipeline 隔离调试；它们不会启动 Runtime、Homeostasis 或 MistyAI。日常双工程开发使用 `dev.ps1`。

## 3. Windows 统一启动入口

首版统一入口固定支持 Windows 11、WSL2 Ubuntu 24.04 和 Docker Desktop。NeuralCloud 在 Windows Python 3.12 中运行，MistyAI Body Service 在 WSL systemd 中运行，MistyAI 容器使用 Docker Desktop 的 Linux engine。

默认假设两个仓库互为同级目录：

```text
<parent>/
├── MistyAI/
└── NeuralCloud/
```

### 3.1 第一次启动

在 NeuralCloud 根目录打开 PowerShell 5.1 或更高版本，先执行只读检查，再启动：

```powershell
.\dev.ps1 doctor
.\dev.ps1 up
```

如果 MistyAI 不在默认位置，显式指定仓库和 WSL 发行版：

```powershell
.\dev.ps1 up -MistyAIPath D:\src\MistyAI -Distro Ubuntu-24.04
```

启动成功后可访问：

- GUI：<http://127.0.0.1:8766>
- 运维页：<http://127.0.0.1:8766/ops>
- MistyAI noVNC：<http://127.0.0.1:6080>

### 3.2 命令

```powershell
.\dev.ps1 up                 # 检查、修复、部署并启动两个工程
.\dev.ps1 down               # 先停 NeuralCloud，再停 MistyAI
.\dev.ps1 restart            # 完整停止后重新启动
.\dev.ps1 status             # 查看组件、端口与健康状态
.\dev.ps1 doctor             # 只读检查，不安装、不启动
.\dev.ps1 install            # 准备依赖和部署环境，不启动服务
.\dev.ps1 logs -Service all  # 汇总查看服务日志
```

`down` 只停止这套启动器能够确认归属的 NeuralCloud 进程、MistyAI units 和 Compose 服务；不会关闭共享的 Docker Desktop 或 WSL，也不会删除数据库、evidence 或 Docker volumes。

### 3.3 前置检查与半自动安装

统一入口会检查三层要求：

- Windows：Windows 11、Git、Python 3.12、硬件虚拟化、WSL2、Ubuntu 24.04、Docker Desktop、Linux engine 和 Docker Compose。
- WSL：systemd、Python 3.12、uv 0.12.9、Docker Desktop 的 WSL integration、MistyAI 部署目录、配置、systemd units 和 token 文件权限。
- 运行时：6080、8765～8772、8787 的占用者。已有健康组件只有在身份匹配时才会复用；未知进程占用时返回配置冲突，不会擅自结束进程。

交互模式下，缺失依赖的安装或 Windows 系统功能变更会逐项说明并请求确认，可使用 `winget`、`wsl --install`、`apt` 和 `python -m pip install uv==0.12.9`。当依赖已经就绪时，用户执行 `install`/`up` 本身即授权相应的 WSL root 部署、配置和服务操作；启动器会在每类特权操作前显示脱敏的动作说明，但不会再次逐项询问。启动器不会使用“下载后直接管道执行”的远程脚本。

以下步骤需要用户在 Windows 或 Docker Desktop 中完成，启动器会保存无敏感信息的进度；完成或重启后重新运行原命令即可续跑：

- 在固件与 Windows 功能中启用虚拟化/WSL2，并按提示重启。
- 接受 Docker Desktop 首次使用协议，切换到 Linux containers。
- 在 Docker Desktop 的 **Settings → Resources → WSL Integration** 中启用 `Ubuntu-24.04`。

`-NonInteractive` 禁止自动安装缺失依赖和交互确认；缺少依赖时只输出结构化修复建议并返回退出码 10。若前置条件已经满足，显式请求的 `install`/`up` 仍会继续执行部署或启停（包括所需的 WSL root 操作）；无人值守只读预检应使用 `doctor -NonInteractive -Json`。

依赖准备完成后，启动器以 `uv sync --frozen --extra dev --extra gui` 同步 NeuralCloud。MistyAI 使用当前工作区快照和内容指纹部署到 WSL；源码或锁文件未变化时会跳过重复同步和镜像构建，部署失败时保留或恢复上一可用版本。

### 3.4 常用参数

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `-MistyAIPath <path>` | `..\MistyAI` | 指定 Windows 上的 MistyAI 工作区。 |
| `-Distro <name>` | `Ubuntu-24.04` | 指定承载 MistyAI Body Service 的 WSL 发行版。 |
| `-Mode demo\|live\|ollama` | `demo` | 选择 NeuralCloud 的 LLM 模式；不改变真实 MistyAI grounded 身体。 |
| `-NonInteractive` | 关闭 | 禁止自动安装和交互确认；前置已满足时仍执行所请求的部署或启停。 |
| `-NoOpen` | 关闭 | 启动成功后不自动打开 GUI。 |
| `-Json` | 关闭 | 输出适合脚本处理的结构化结果；其中不会包含 token。 |
| `-Service <name>` | `all` | 为 `logs` 命令选择服务；`all` 查看全部。 |

示例：

```powershell
.\dev.ps1 doctor -NonInteractive -Json
.\dev.ps1 up -Mode ollama -NoOpen
.\dev.ps1 status -Json
```

### 3.5 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 命令成功。 |
| `10` | 缺少依赖、需要手动安装或需要重启；按检查项修复后重试。 |
| `20` | MistyAI 部署、启动、健康、身份或能力协商失败。 |
| `30` | NeuralCloud 安装、启动或就绪检查失败。 |
| `40` | 配置错误、仓库错误或端口被未知进程占用。 |
| `50` | 另一个统一启动流程持有独占锁。 |

### 3.6 安全边界

- MistyAI 工作区快照包含 tracked、已修改和未被忽略的未跟踪源码，但排除 `.git`、虚拟环境、缓存、构建产物和日志；发现 `.env`、token、私钥等敏感候选文件时会拒绝部署并报告路径。
- `/etc/mistyai/body-service.token` 只在当前 PowerShell 进程内存中读取，并临时通过环境变量交给 NeuralCloud Runtime；它不会进入命令行、Windows 配置、日志、JSON 输出或 PID registry，命令结束时会被清除。
- `%LOCALAPPDATA%\NeuralCloud\launcher\` 只保存安装阶段、部署指纹和组件归属，用于重启续跑与安全清理，不保存密钥。
- 启动、回滚和停止不会清空用户仓库、SQLite、evidence 或 Docker volumes；无法确认归属的进程和服务不会被终止。

### 3.7 Linux / WSL2 单工程兼容路径

统一双工程入口当前只支持从 Windows 运行。只调试 NeuralCloud 的单进程 GUI 时仍可使用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m gui.server
```

依赖安装完成后，也可以使用：

```bash
bash start.sh
bash stop.sh
```

### 3.8 第一次应该观察什么

首次启动默认使用小雾（Misty）内置角色预设 [`presets/builtin/misty.json`](../presets/builtin/misty.json)。它来自相邻 MistyDigitalBody 的冻结设定包 v0.2.0，包含姓名、年龄、生日、身高、爱好、四项核心人格、克制的表达基线、自我外观认知与背景物品；关系、对话和长期记忆从干净状态开始，不继承旧角色经历。Vaelith 模板仍保留在预设库中供显式选择。已有 GUI 配置仍会打开用户上次选择的预设，因此要切换既有环境需在预设面板应用 `misty`。

可以先发一句简单问候，再问一个涉及往事、承诺、第三方人物或情绪暗示的问题。观察中间区域的阶段输出：

```text
ingest -> parse -> state_build -> gate
       -> [interpret，仅 Deep Path]
       -> compile -> generate -> validate
       -> reply_segment -> final
```

普通问候通常走 `fast`；复杂关系或记忆问题通常走 `deep`。`demo` 模式的回复是确定性的示范结果，重点是观察完整机制，不是评价语言模型能力。

## 4. 一条消息是怎样跑完的

用户在 GUI 中点击发送后，真实调用链如下：

```text
POST /api/chat
  -> gui.server.chat()
  -> HarnessChatService.respond()
  -> InferencePipeline.respond()
  -> 各阶段通过 Observer 推送到 WebSocket /ws
  -> GUI 实时显示阶段结果
  -> 最终回复与最新状态写入角色工作副本
```

核心编排入口是 [`harness/pipeline/pipeline.py`](../harness/pipeline/pipeline.py)。概念上它分为九个阶段；代码为了减少开销，把“读取状态、检索记忆、组装情境”合并在一次 `state_build` 观察事件里。

### 阶段 1：Gateway —— 先把字符串变成事件

入口文件：[`harness/pipeline/gateway.py`](../harness/pipeline/gateway.py)

系统内部不直接传递裸字符串，而是把输入包装成 `InteractionEvent`，附上：

- 事件 ID；
- 时间；
- 说话者；
- 会话 ID；
- 渠道和模态；
- 原始文本与证据来源。

原因很简单：长期运行的角色以后可能同时接收聊天、语音、环境感知或动作结果。统一成“事件”后，下游模块不必为每种入口重写一套逻辑。

### 阶段 2：Context Parser —— 提取线索，不替用户下结论

入口文件：[`harness/pipeline/parser.py`](../harness/pipeline/parser.py)

Parser 会识别问候、提问、时间指代、情绪线索、人物实体和“可能的社交意图”。这里故意使用“假设 + 置信度”，例如“可能是在确认承诺”，而不是武断地写成“用户就是缺乏安全感”。

实现采用规则优先：简单文本直接用规则处理；确有必要时再调用辅助小模型；模型返回的 JSON 不合规时会回退到规则结果。当前各辅助模块对网络异常的处理还不完全一致，因此 `live` 模式仍应保证辅助端点稳定可达。

### 阶段 3～5：状态读取、记忆检索和 SituationState

入口文件：[`harness/pipeline/state_builder.py`](../harness/pipeline/state_builder.py)

这里会并行做两件事：

1. 读取角色当前的 Persona、关系、心理和世界状态；
2. 根据用户这句话检索相关事件与语义记忆。

两边完成后，组装成统一的 `SituationState`。它可以理解为这一轮推理的“案情卷宗”：谁触发了什么、角色现在是什么状态、找到了哪些往事、有哪些待判断假设。

并行执行不是装饰。状态读取和检索彼此独立，同时做可以缩短响应等待时间。

### 阶段 6：Complexity Gate —— Fast 还是 Deep

入口文件：[`harness/router/gate.py`](../harness/router/gate.py)

每句话都做深度分析会慢，也会浪费模型调用。Gate 因此把输入分成两类：

- `fast`：问候、简单闲聊、普通事实问题和日常安排；
- `deep`：共享往事、承诺检查、价值冲突、关系试探、情绪暗示、人名或陌生概念。

Gate 使用“规则评分”和“模型判断”两个裁判，采用偏保守的 OR 逻辑：任一裁判认为需要深入，就走 `deep`；只有双方都认为简单，才走 `fast`。这种策略宁可多分析一次，也尽量避免漏掉重要输入。

### 阶段 6.5：Social Interpreter —— 只做顾问，不抢台词

入口文件：[`harness/subagents/interpreter.py`](../harness/subagents/interpreter.py)

只有 `deep` 路径会调用它。它结合当前话语和检索到的记忆，输出：

- 若干可能意图及置信度；
- 推荐的回应姿态；
- 可能产生的关系变化；
- 不可越过的表达红线。

它不能直接生成角色回复。这能防止“分析模型”把心理学术语原样说给用户，也能让主模型继续保持角色口吻。

### 阶段 7：Context Compiler —— 在有限预算里制作提词卡

入口文件：[`harness/compiler/compiler.py`](../harness/compiler/compiler.py)

记忆越多，不代表把所有内容塞进提示词越好。Compiler 在默认约 3000 token 的预算中，按优先级选择：

```text
人格锚点和红线
  -> 当前用户输入
  -> 心理 / 关系 / 世界状态
  -> Social Interpreter 的结构化建议
  -> 最相关的少量记忆
  -> 最近对话窗口
```

每段内容都保留来源 ID 和入选原因，这就是 provenance（来源追踪）。以后发现模型说错时，可以反查是哪个记忆、状态或约束进入了上下文。

如果没有检索到相关记忆，Compiler 会明确加入“禁止编造共同经历”的约束。

### 阶段 8：Character Model —— 只负责演出

入口文件：[`harness/llm/character.py`](../harness/llm/character.py)

主模型接收编译后的上下文，生成角色台词。它不直接访问数据库，也不负责决定该检索什么、能不能修改 Persona，因而职责很纯粹。

项目还支持分段连说协议。模型可以用 `[CONTINUE]` 表示还想继续，用 `[END]` 表示自然收尾；流水线会去重并限制最多回复段数，避免无限续写。

当前没有 token 级流式输出。GUI 展示的是“阶段级”和“回复分段级”推送。

### 阶段 9：Response Validator —— 先检查，再发送

入口文件：[`harness/validator/validator.py`](../harness/validator/validator.py)

Validator 检查回复是否：

- 违反人格红线；
- 不合角色地直接道歉或直接表达关心；
- 把内部心理分析说了出来；
- 在记忆为空时编造共同往事。

发现问题后，它不会自己改写整段回复，而是给主模型一条局部修订指令。默认最多重生成两次。`deep` 路径必检，`fast` 路径按配置抽样检查；GUI 为了便于观察，将采样率设为 100%。

### 最后：写回会话与记忆

回复通过后，流水线会把用户输入和角色回复加入近期会话；在普通 `respond()` 路径中，还会把两边写成 episodic event。

用户事件会进入 Dual Memory 和 Curator 流程；角色回复只记录事实，不再次生成角色视角或触发 Curator，避免自我循环放大。

## 5. 记忆系统为什么不是一个向量库

入口目录：[`harness/memory/`](../harness/memory/)

向量相似度只能回答“文本像不像”，无法独立回答：这是谁的记忆、什么时候发生、是否重要、是否与当前关系有关、是否符合角色价值观。因此项目把记忆和状态拆成六层。

### 5.1 六层记忆与状态

1. **Persona Core：角色是谁**  
   姓名、核心特质、价值观、说话方式和知识边界。首次写入后锁定；普通更新和删除会被拒绝并记录审计。这样角色可以成长，但不能偷偷换人。

2. **Episodic Memory：发生过什么**  
   带时间、参与者、经过、结果和证据的具体事件。它像日记，而不是概括。

3. **Semantic Memory：已经形成什么认识**  
   例如“用户偏好猫”。它不能由模型一句话直接写入，必须由候选结论积累多条事件证据后固化。

4. **Relationship State：和某个人的关系如何**  
   亲密度、信任、依恋、近期紧张等独立数值。关系不是普通事件的一段文字，因此单独维护。

5. **Psychological State：角色现在处于什么状态**  
   分为 long / mid / short。短期心情带 TTL，到期后失效，避免“一次生气，永远生气”。

6. **World State：角色眼前的现实**  
   当前时间、渠道、用户是否在线、活动窗口等。默认聊天模式只有轻量感知接口，未来可替换成数字身体的真实感知后端。

此外还有 **Candidate Memory**。它不是第七层正式记忆，而是“待核实区”：模型提出的长期结论先放这里，积累证据和置信度后才升级为 Semantic Memory。

### 5.2 Dual Memory：事实与角色看法分开存

入口文件：[`harness/memory/dual.py`](../harness/memory/dual.py)

同一个事件同时保存两部分：

```text
objective_fact                 客观发生了什么
persona_conditioned_insight    这个角色认为它意味着什么
```

例如，客观事实可能是“用户说不用管了”；角色视角可能是“这更像气话，而非真实拒绝”。分开保存后，系统既不会把角色猜测冒充事实，又能保留角色独有的理解方式。

### 5.3 混合检索：像查案，不只是搜相似句子

入口目录：[`harness/memory/retrieval/`](../harness/memory/retrieval/)

检索大致分两步：

1. Planner 从问题中提取时间范围、人物、事件类型和渠道等检索计划；
2. Hybrid Retriever 建候选池，再用六类信号综合重排。

默认信号包括语义相似、实体匹配、时间新近度、事件重要性、关系相关度和人格相关度。检索结果不仅返回分数，还返回 `why_retrieved`，方便解释“为什么想起这件事”。

如果用户记错时间或渠道导致查不到，检索器会适当放宽范围，而不是直接宣布没有记忆。系统还提供冲突检索，用于主动查找与当前推断相矛盾的证据。

### 5.4 Memory Writer：先做候选，再形成长期认识

入口目录：[`harness/memory/writer/`](../harness/memory/writer/)

写入长期知识的路径是：

```text
新事件
  -> Curator 提出结构化操作
  -> Candidate Memory
  -> 新证据到来，置信度上升
  -> 达到阈值
  -> Semantic Memory
```

Curator 只能提出受限的 `ADD / UPDATE / DELETE / NO_OP` 操作，Writer 再校验引用和证据。即使模型输出不合规，存储层也不会让它直接污染长期记忆。

### 5.5 事件记忆的生命周期：记住不等于永久保存

入口文件：[`harness/memory/tier.py`](../harness/memory/tier.py)

每条新事件先进入 `short`：

- 普通问候的 uniqueness 很低，很快到期；
- 承诺、冲突、强情绪和关键人物事件保留更久；
- 被频繁检索命中的事件可从 `short` 升到 `mid`，再升到 `long`；
- 到期时由规则或辅助小模型决定升级还是标记为 `forgotten`。

`forgotten` 是“退出检索池”，不是物理删除。原始记录和审计仍然存在，便于研究和追责。

## 6. 各代码模块的职责与原理

下面按目录介绍。第一次阅读不需要把每个文件都看完，先抓住每个模块的输入、输出和边界。

### `harness/core/`：全系统共同语言

- [`models.py`](../harness/core/models.py) 定义记忆侧对象，如事件、Persona、关系、心理、世界和检索结果。
- [`inference.py`](../harness/core/inference.py) 定义推理侧对象，如 ParsedInteraction、SituationState、ModelContext 和 FinalResponse。
- [`ids.py`](../harness/core/ids.py) 统一生成不同类型的 ID。
- [`exceptions.py`](../harness/core/exceptions.py) 定义证据缺失、Persona 锁定等领域错误。

这些对象由 Pydantic 校验。模块之间尽量传结构化对象，不传含义模糊的自然语言“小作文”。这相当于先统一零件规格，再允许各模块独立替换。

### `harness/config.py`：把策略参数集中起来

检索权重、上下文预算、分流阈值、修订次数、记忆 TTL 和模型端点都集中在 [`harness/config.py`](../harness/config.py)。环境变量使用 `NC_` 前缀，并自动读取根目录 `.env`。

集中配置的好处是：实验时可以调参数，而不用在调用链各处寻找魔法数字。

### `harness/pipeline/`：一轮对话的总装线

这里负责把 Gateway、Parser、State Builder、Gate、Interpreter、Compiler、Character Model 和 Validator 串成一次完整推理，并维护最近会话窗口和连说协议。

如果要理解“点发送后到底发生了什么”，第一站就看 [`pipeline.py`](../harness/pipeline/pipeline.py) 的 `InferencePipeline.respond()`。

### `harness/memory/`：长期一致性的基础

`MemoryService` 是对外门面，统一提供 `record_event()`、`query()`、`get_state()` 和状态更新方法。它内部再调用 Dual Memory、检索器、Writer、Curator、Tier 和底层 Stores。

外部代码通常应该依赖 `MemoryService`，而不是绕过它直接写某张表；否则可能跳过证据校验、Persona 锁和审计。

### `harness/memory/store/`：存储可替换层

[`protocols.py`](../harness/memory/store/protocols.py) 定义存储协议；[`inmemory.py`](../harness/memory/store/inmemory.py) 用于 demo 和测试；[`postgres.py`](../harness/memory/store/postgres.py) 对接 PostgreSQL + pgvector。

上层只认协议，因此检索、Writer 和 Pipeline 不必知道数据最终放在字典还是数据库里。

注意：当前 GUI 的 `_swap_runtime()` 明确使用 `build_inmemory_service()`。即使启动了 PostgreSQL，GUI 也不会自动切换数据库；PostgreSQL 后端目前需要由调用方显式构建，主要由集成测试和自定义部署使用。

### `harness/llm/`：主模型、辅助模型和 Embedding 的隔离层

- `complete_chat*` 用于主角色生成；
- `complete_json` 用于 Parser、Gate、Interpreter、Validator、Planner 和 Curator 等结构化辅助任务；
- `RoutingLLMClient` 可以把两类调用路由到不同端点；
- Embedding 客户端负责语义向量，远端不可用时可降级为本地确定性 MockEmbedding。

这样可以让大模型专心演角色，把结构化判断交给更便宜的小模型。所有真实端点都采用 OpenAI 兼容 HTTP 接口。

### `harness/contracts/`：跨服务命令的“快递单”

长期运行时不能只传一个字典。这里定义命令 envelope、请求哈希、幂等键、截止时间、版本向量、Prepare/Commit 结果和交付证据。

它解决的是分布式系统中的现实问题：请求重试会不会执行两次、结果回来时是否已经过期、写入时读到的状态是否还是原版本。

### `harness/runtime/`：让角色长期在线的调度内核

Supervisor 不负责具体扮演，而是负责：

- 接收并排序用户、环境、心跳、动作结果和控制事件；
- 定时唤醒与空闲自驱；
- 管理生命周期和单实例锁；
- 协调认知域、内稳态域、回复投递和外部动作；
- 在失败后根据 journal 和 checkpoint 恢复；
- 通过 watchdog 发现依赖异常或重复失败；
- 暴露带本地令牌认证的 REST + SSE 控制面。

它更像“操作系统内核”，`InferencePipeline` 则像其中一个提供认知能力的服务。

### `harness/research/`：实验配置的身份信息

`ExperimentManifest` 固定实验修订、心跳周期等运行参数。Supervisor 将 manifest revision 写入事件和 journal，确保实验结果可以追溯到当时使用的配置。

### `infra/trace/`：观察和审计

- stage trace 记录每个推理阶段的时间和摘要；
- CallLog 在成功的顶层调用结束后，保存一份完整结构化调用链；
- GUI 通过 Observer 把同一批阶段结果推送到 WebSocket。

Trace 回答“这一轮内部发生了什么”，Runtime Journal 回答“长期运行时按什么顺序接受、处理、提交和恢复事件”。两者用途不同。

### `gui/`：展示层和开发控制台

[`gui/server.py`](../gui/server.py) 提供 FastAPI、REST、WebSocket 和静态页面；[`gui/service.py`](../gui/service.py) 组装 LLM、Embedding、MemoryService 和 Pipeline；[`gui/observer.py`](../gui/observer.py) 把阶段事件线程安全地广播到浏览器。

预设相关代码会把内置模板复制成 `data/presets/` 下的可写工作副本。对话后保存的是工作副本，恢复出厂设置时再从 `presets/builtin/` 重建。

### `infra/db/`：可选 PostgreSQL 基础设施

包含 pgvector Docker Compose、Alembic 配置和数据库迁移。它用于持久化 Memory Store，不是运行 demo GUI 的前置条件。

### `eval/` 与 `tests/`：回答“它真的更好吗、没有坏吗”

- `tests/` 验证模块契约、分流、检索、Persona 锁、Writer、GUI 和 Runtime 恢复等行为；
- `eval/scenarios/` 验证跨模块场景；
- `eval/rp/` 对比原始模型与 Harness 全链路的角色扮演表现；
- `eval/bench_retrieval.py` 生成检索性能基线。

## 7. 完整 Runtime Supervisor 怎样长期运行

这一节讲的是 `harness/runtime/` 的机制，不是当前 GUI 的默认启动方式。

### 7.1 生命周期

```text
BOOTING -> RECOVERING -> RUNNING
              |           |
              v           v
            PAUSED <- PAUSING
              |
              v
           RUNNING

任何关键故障 -> FAULTED -> RECOVERING
正常退出     -> STOPPING -> STOPPED
```

启动时 Supervisor 会先拿单实例锁、读取 journal/checkpoint、检查认知与内稳态服务、对账未完成命令和外部动作，再决定进入 `RUNNING` 还是保守地停在 `PAUSED`。

### 7.2 主事件循环

所有输入先变成 `RuntimeEvent`，获得严格递增的接收序号并写入 journal，然后进入优先级队列。控制事件优先级最高，其次是动作结果、用户消息、重要环境变化、普通感知、心跳、空闲事件和遥测。

队列压力过大时，可合并的低价值感知会合并，可丢弃的低优先级事件会降载；用户消息和控制事件受到保护。

### 7.3 用户消息的 Prepare → Deliver → Commit

长期运行模式不会让认知服务生成后立即写回，而是采用三步事务：

```text
1. PrepareResponse
   只生成可重放的回复，不修改 Session 和 Memory

2. Deliver
   Supervisor 实际把回复交给渠道，并获得成功或失败证据

3. CommitInteraction
   只有拿到交付证据后，认知域才写回会话与记忆
```

这样可以避免一种常见故障：数据库已经记住“我回复过”，但进程恰好在真正发送前崩溃，用户其实什么都没收到。

每个命令都有幂等键、请求哈希、截止时间和期望版本。重试同一个命令不会重复提交；同一幂等键若携带不同内容会被判定为冲突。

### 7.4 心跳和空闲自驱

Scheduler 到期后产生 heartbeat。内稳态服务先接收心跳；当队列为空且没有前台动作时，它可以提出 policy，再由 Supervisor 生成低优先级 `IDLE` 事件。Cognitive 先用辅助模型进行纯文本 `SILENT` / `SPEAK` 判定并写入独立 `.txt` 日志；只有 `SPEAK` 才调用角色主模型。判定前后若收到用户消息，本次主动表达直接取消。

当前 `PassiveHomeostasisAdapter` 只保存单调版本和预设 policy，不代表完整的情绪、需求和驱力模型已经实现。

### 7.5 暂停、动作与恢复

暂停不是粗暴杀进程。Supervisor 会停止调度、请求前台动作到安全点、排空认知维护任务，然后进入 `PAUSED`。暂停期间的重要感知被摘要保存；恢复时生成 `ResumeObservation`，告诉角色“离线期间发生了什么”。

外部动作由 `ActionCoordinator` 管理。高风险动作可以等待批准；用户消息到达时可以请求当前动作协作式抢占；崩溃后结果不明确的动作会进入 `UNKNOWN`，要求研究者确认，而不是盲目重做。

## 8. 数据最终写到哪里

默认 GUI 模式下：

- `data/gui_settings.json`：GUI 模式、模型端点、角色名和调参配置；
- `data/presets/*.json`：角色预设的可写工作副本、记忆和对话记录；
- `traces/calls/YYYYMMDD/*.json`：GUI 强制开启的成功调用全链路记录；
- `logs/gui.out.log`、`logs/gui.err.log`：使用一键脚本后台启动时的进程日志。

直接构造纯内存 `MemoryService` 时，进程结束后数据会消失，除非调用方自行做快照。使用 PostgreSQL Store 时，领域数据由数据库持久化。

Runtime Supervisor 的 journal、checkpoint、锁文件和适配器状态写到启动时配置的 `state_dir`。统一启动器自身的安装阶段、部署指纹和组件归属保存在 `%LOCALAPPDATA%\NeuralCloud\launcher\`；这是编排元数据，不包含 MistyAI token。

## 9. 切换真实模型

先复制示例配置：

```powershell
Copy-Item .env.example .env
```

或在 Linux / WSL2 中：

```bash
cp .env.example .env
```

示例配置使用 OpenAI 兼容接口，并推荐把角色主模型、辅助小模型和 Embedding 分开。以 Ollama 为例，需要在外部安装并启动 Ollama，再准备相应模型：

```bash
ollama pull huihui_ai/qwen3-abliterated:30b-a3b-instruct-2507-q4_K_M
ollama create neuralcloud-main -f infra/llm/Modelfile.character
ollama pull qwen3:4b-instruct
ollama pull bge-m3
```

然后在 GUI 顶部设置中选择 `ollama` 或 `live`：

- `demo`：MockLLM，完全离线，适合看机制和跑测试；
- `ollama`：本地 Ollama 的便捷模式；
- `live`：任意 OpenAI 兼容服务，可分别填写主模型和辅助模型端点。

辅助模型应选择能稳定输出 JSON 的 instruct 模型。结构化输出解析失败时，相应模块通常会回退到规则；但当前网络异常并未在所有辅助模块中统一降级，所以辅助端点断连仍可能让该轮失败。Embedding 不可用时会降级为 MockEmbedding，主角色生成端点不可用也会直接导致回复失败。

## 10. 可选 PostgreSQL + pgvector

只有在需要测试持久化 Store 时才需要这一节：

```bash
docker compose -f infra/db/docker-compose.yml up -d
python -m pip install -e ".[dev,postgres]"
alembic -c infra/db/alembic.ini upgrade head
```

然后由代码显式创建 PostgreSQL Stores 并传给 `MemoryService`。再次强调：启动数据库不会让当前 GUI 自动从 in-memory 切换到 PostgreSQL。

## 11. 测试与最小代码示例

运行全部测试：

```bash
pytest
```

只看推理、记忆或 Runtime：

```bash
pytest tests/test_pipeline.py
pytest tests/test_retrieval.py tests/test_writer.py tests/test_tier.py
pytest tests/test_runtime_supervisor.py
```

不启动 GUI，直接调用流水线：

```python
from harness.memory.service import build_inmemory_service
from harness.pipeline import InferencePipeline
from gui.demo_seed import bootstrap_demo

memory = build_inmemory_service()
bootstrap_demo(memory)

pipeline = InferencePipeline(memory)
result = pipeline.respond("早上好", session_id="sess_demo")

print(result.path)      # fast 或 deep
print(result.segments)  # 分段回复
print(result.text)      # 合并后的最终回复
```

这个例子使用 MockLLM，因此不需要真实模型。生产或实验代码中应在完成后调用 `memory.close()`，等待后台 tier 任务并释放线程池和 HTTP 客户端。

## 12. 推荐的源码阅读顺序

第一次阅读建议按一条真实请求向下走：

1. [`gui/server.py`](../gui/server.py)：HTTP 请求从哪里进入；
2. [`gui/service.py`](../gui/service.py)：GUI 如何组装各组件；
3. [`harness/pipeline/pipeline.py`](../harness/pipeline/pipeline.py)：一轮对话如何编排；
4. [`harness/pipeline/state_builder.py`](../harness/pipeline/state_builder.py)：状态和记忆如何汇合；
5. [`harness/memory/service.py`](../harness/memory/service.py)：记忆门面如何读写；
6. [`harness/memory/retrieval/hybrid.py`](../harness/memory/retrieval/hybrid.py)：记忆如何排序；
7. [`harness/compiler/compiler.py`](../harness/compiler/compiler.py)：最终上下文如何组成；
8. [`harness/validator/validator.py`](../harness/validator/validator.py)：回复如何被拦截和修订；
9. [`harness/runtime/supervisor.py`](../harness/runtime/supervisor.py)：最后再看长期事件循环与恢复。

需要查实现细节时，再读 [`TECHNICAL.md`](TECHNICAL.md)；需要理解设计依据时，读 [`docs/harness/`](harness/) 下的专题文档。

## 13. 常见误解与排查

### “装好项目后，为什么回复很固定？”

很可能仍在 `demo` 模式。它使用 MockLLM，目的在于稳定演示链路和测试行为。切换真实模型需要启动兼容端点，并在 GUI 设置中选择 `ollama` 或 `live`。

### “为什么我启动 PostgreSQL 后，GUI 里还是内存数据？”

因为当前 GUI 明确构建 `InMemoryStores`。数据库是可替换后端，不是自动发现的全局服务。

### “GUI 为什么不是逐字流式输出？”

当前只实现阶段级 WebSocket 推送和回复分段推送，`CharacterModel` 的 token 流式参数仍保留给后续里程碑。

### “为什么 Fast Path 也出现 Validator？”

通用默认配置会按 `validator_sample_rate` 抽样；GUI 为方便调试，将其设置为 1.0，所以每轮都能看到校验结果。

### “为什么记忆查不到，但数据还在文件或数据库里？”

它可能已被标记为 `forgotten`。遗忘只让记录退出检索池，不会直接删除审计数据。

### “`dev.ps1` 提示需要安装或重启？”

先运行 `.\dev.ps1 doctor` 查看每个检查项的当前值、要求和修复建议。退出码 10 表示环境尚未就绪，不代表源码错误；完成 Docker Desktop 首次设置、WSL integration 或 Windows 重启后，重新运行原命令即可从保存的阶段继续。

### “Docker 已安装，为什么仍说 WSL integration 不可用？”

确认 Docker Desktop 正在运行 Linux engine，并在 **Settings → Resources → WSL Integration** 中启用启动命令所选的发行版。随后在该发行版内确认 `docker version` 和 `docker compose version` 可用，再运行 `.\dev.ps1 doctor`。

### “为什么拒绝现有的 `/opt/mistyai`？”

启动器只会替换带有自身部署标记的 `/opt/mistyai`，不会静默接管手工部署、符号链接或来源不明的目录。先在 WSL 中检查并把现有目录备份或移到你明确选择的位置，再重新运行；如果 `/opt/mistyai` 缺失但存在带标记的 `/opt/mistyai.previous`，先执行 `sudo mv /opt/mistyai.previous /opt/mistyai` 恢复上次被中断的部署。不要为未知目录手工伪造启动器标记。

### “端口冲突或另一个启动流程正在运行？”

退出码 40 通常表示 6080、8765～8772 或 8787 被无法确认归属的进程占用。用 `.\dev.ps1 status` 查看占用者，关闭对应程序后重试；启动器不会杀死未知进程。退出码 50 表示已有 `dev.ps1` 持有独占锁，应等待该命令完成，而不是同时启动第二套环境。

### “MistyAI 或 NeuralCloud 没有就绪？”

先执行 `.\dev.ps1 logs -Service all`。退出码 20 表示 MistyAI 部署、systemd/Compose、body health、身份或能力协商失败；退出码 30 表示 NeuralCloud 四进程或最终 Runtime/Embodiment 就绪检查失败。MistyAI 失败不会启动 NeuralCloud；NeuralCloud 失败时，启动器会清理本轮创建的 NeuralCloud 进程，并只在 MistyAI 原先未运行时回收本轮启动的 MistyAI 组件。

### “`start.ps1` 执行失败时看哪里？”

它是仅启动 NeuralCloud 的低层入口。检查 `logs/gui.err.log`，并确认虚拟环境已创建、依赖已安装、8766 端口未被其他程序占用；也可以直接运行 `python -m gui.server` 查看前台错误。需要自动检查 MistyAI、WSL2、Docker 和依赖时，应改用 `.\dev.ps1 up`。

## 14. 最后记住这三件事

1. **主模型不是整个系统。** 它只负责角色表达，记忆、判断、约束和写回都由 Harness 管理。
2. **记忆不是聊天记录堆积。** 事件、角色视角、候选认识、语义事实和遗忘生命周期彼此分开，并保留证据链。
3. **GUI、长期 Runtime 与 MistyAI 身体是分层协作的。** `dev.ps1` 负责把三层严格地部署、协商并拉起；`start.ps1` / `stop.ps1` 只负责 NeuralCloud 的低层四进程启停。

带着这三点再看源码，绝大多数目录和类都会自然地找到自己的位置。
