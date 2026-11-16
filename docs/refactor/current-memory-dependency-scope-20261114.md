# 新记忆服务及当前使用方：保留范围核对

> 后续进度：已于 2026-11-16 完成一轮现场核对和源码补齐，见[交接记录](production-source-sync-20261116.md)。以下保留 11 月 14 日历史审计语境。

日期：2026-11-14 UTC。**本轮只读核对，不删除、不归档、不改运行代码、不部署。** 先前按“仅保留 memory 包”进行的删除与归档已全部撤回，4,125 个文件与清理前提交逐一核对哈希一致。

## 1. 当前结论与证据边界

用户确认的范围是：**保留新记忆服务，以及目前实际仍在依赖它的相关代码和文档**。这里的“相关”包括实际消费者、消费者所需的运行依赖、接入契约、测试、部署和恢复配方；不等于只留下 Python wheel，也不等于把所有带有 memory 字样的旧模块留下。

本轮完成本地源码、构建配置、跨仓库调用和历史部署记录核对。**未完成今天的生产启用状态核验**：远程设备列表为空，尚无可用部署机连接。已向用户询问只读连接或固定发布版本；没有读取生产凭据、私密消息或数据库正文。

- NeuralCloud 业务基线：`ab421b125be0dc632eef51ea2744fa22a75bbf98`；审计检查点：`4fcd8c335b4ee5ea32a84a6430955a027c443db6`。
- MistyAI 本地基线：`37e093595b512ca139dfda0d70fc43eda5df207d`。
- 本地路径是源码快照，不能直接等同于线上加载路径。机器可读结果见 [dependency-audit.json](code-cleanup-audit-20261114/dependency-audit.json)。

**最重要的发现：本地新记忆代码不是已知部署链的完整源码。** 10 月 28 日上下文补丁引用 `memory-stack.mjs`、`continuity.mjs`、`context-budget.mjs`，三个文件均不在当前 MistyAI 本地实施快照的 resident 目录中。补丁还包含 `memory.workflow_mode`、`workflow_bindings`、`verifyMemoryStack`。因此之前 1,919 行“新记忆服务 + 插件”只是本地代码下界，不能作为“全部实际启用的记忆链路”的体量或删除边界。

## 2. 已确认的依赖链

```text
NeuralCloud/src/neuralcloud
  CLI → API → MemoryStore → contracts / ownership / SQLite
  管理命令 → archive / temporary_views / maintenance → MemoryStore
  显式旧数据迁移 → migration → MemoryStore（不导入旧 Harness）

MistyAI/compatibility/harness/resident/launch.mjs
  配置 neuralcloud_plugin 路径与哈希 → 加载 NC 记忆插件
  ├─ ServiceClient → MistyAI role / executor 状态接口
  └─ MemoryClient → NeuralCloud 记忆接口
       ↓
  composition → ResidentRuntime + dsh-misty.installIntegration
       ├─ bindMemory → memory_recall / memory_read
       ├─ DurableIngress.stageMemory → DurableOutbox → /v1/ingest
       ├─ SENT 发送回执 → self_expression → DurableOutbox
       └─ 内容读取事实 → observation → DurableOutbox

手机插件（需确认实际挂载）
  start(receive) → 宿主 runtime.lifecycle.receive → ingress / memory

部署补丁中的增量（本地未完整同步，需核实实际加载）
  memory-stack / continuity / context-budget / memory workflow
```

源码锚点：

- NC `plugins/dsh-neuralcloud/src/index.mjs:38/110/146`：MemoryClient、Outbox、工具绑定。
- MA `compatibility/harness/resident/launch.mjs:106/151`：校验并加载插件；同一 NC ServiceClient 同时服务于状态接口。
- MA `compatibility/harness/resident/composition.mjs:47/58`：构造持久 Outbox，绑定记忆接口。
- MA `plugins/dsh-misty/src/index.mjs:87/108/121/249/285`：入站转记忆、drain、工具绑定、发送与内容事实入队。
- MA `compatibility/harness/resident/runtime.mjs:119/120`：会话游标与持久输入收集；队列是否 pending/blocked 还影响自主调度。
- NC `plugins/mobile-chat/channel.mjs:60/105`：接收回调和来源事件。它不直接 import MemoryClient；**若挂载在上述 resident，则是间接使用方**。不能因为缺少 memory import 就判定可删，也不能只凭插件存在判定已挂载。

这里的 MA 是另一个仓库，不需要把它的整套代码复制到 NC。需要保留的是可验证的跨仓库契约和实际依赖关系。

## 3. 代码保留/待核对表

以下“保留”是本轮处置建议，不宣称今天已逐文件验证线上加载。

| 范围 | 证据 | 本轮判定 |
| --- | --- | --- |
| NC `src/neuralcloud/**` | 新 CLI/构建；19 条包内相对 import；API 和迁移均不导入旧运行时 | **保留**。在线与管理/恢复能力均属于新记忆服务 |
| NC `plugins/dsh-neuralcloud/**` | MA launcher 按配置路径/hash 加载，调用 MemoryClient、ServiceClient、Outbox、bindMemory | **完整保留**。不能删掉被状态客户端复用的 ServiceClient |
| NC `contracts/resident/**`、导出工具、pyproject/uv.lock、当前 CI | API/DTO 的生成与验证入口；不是旧协议副本 | **保留** |
| NC `tests/resident/**`、记忆插件测试 | 新服务的数据完整性、恢复、Outbox 契约保护 | **保留**。不进入阅读代码量，不代表应删除 |
| NC `tools/benchmark_resident.py` | 直接读取新 MemoryStore，独立管理/开发工具 | **配套保留候选**，不是线上必需；不运行性能优化 |
| MA `compatibility/harness/resident/**`、`plugins/dsh-misty/**` 及其锁文件/测试 | 已确认是新记忆的直接调用者和持久化拥有者 | **属于依赖保留范围**；留在 MA，核对部署增量后确定完整文件集合 |
| QQ/OneBot、内容工具、状态/审批/投递模块 | 是上述消费方的能力和运行依赖；消息与观察写入新记忆 | **实际启用者连同必要依赖保留**。不能因为不是记忆实现就裁掉 |
| NC `plugins/mobile-chat/**` | 独立 npm 入口，提供 source 事件；文档示例接入 resident；本地 MA 标准 launcher 未显示该插件挂载 | **暂保留，待查部署挂载和消费游标路径**。手机文字渠道不等于 MA 的 mobile MCP 工具 |
| NC `tools/deploy/misty_oral_v2/**` | 已记录部署到包含记忆工具的 MA 插件；带 hash 和发布/回滚脚本 | **暂保留**。表达模块无直接记忆调用，但属于已记录的消费者发布增量，不先行删配方 |
| `docs/harness/evidence/context-budget-20261028/source.patch` 及其回执 | 含本地缺失的记忆工作流绑定/连续性/压缩模块引用 | **按源码恢复证据保留，不能按普通旧文档批量归档后失去入口** |
| NC 根 `harness/**`、`gui/**`、`infra/**` | 新 memory 包无静态依赖；旧部署记录记载退役，但旧脚本仍存在执行入口 | **退役候选，尚非本轮删除许可清单**。需排除实际部署引用和额外 writer |
| NC `tools/qqbot/**` | 旧 `harness/qq/official_client.py` 仍引用；新 MA resident 使用另一条 QQ 接入路径 | **待查运行入口**。名称相似不代表新旧桥接可互删 |
| 根 start/stop/launch、旧工具、旧 schema/presets | 主要指向旧 GUI/runtime；不是新 memory wheel 的依赖 | **逐入口待核对**；从 active 服务/计划任务反查后再确定退役 |
| `eval/**` 与旧测试 | 大部分是旧系统/研究；少量可能是运行补丁的唯一回归样本 | **按能力逐组归属**；被保留消费方需要的测试留下，历史研究文档可归档，业务代码先核实 |

不能采用“保留依赖新记忆的文件，其余全部删”的字符串过滤。消费者依赖的发送账本、审批、持久会话和工具模块即使不直接调用 memory，也可能是消费方正常运行的必要条件。反过来，两个模块同处一个仓库也不能证明存在必要依赖。

## 4. 相关文档的当前保留范围

**当前阅读入口必须保留/修订**：根 README、新记忆运行/开发说明、当前简化需求与清理范围报告、记忆插件接入说明、实际使用的手机插件说明。README 的“尚未切换”陈述已与后续部署记录冲突，后续应改为基于核验时间的说明，不能再引用为当前运行事实。

**在依赖核验完成前保留为发布/恢复依据**：

- `docs/refactor/misty-harness-rollout/05-persona-state-archive.md`：新记忆的迁移、归档和临时视图边界。
- 同目录 `06-production-cutover.md`、`07-production-operations.md`、`08-onebot-production.md`、`09-autonomy-repair.md` 及直接引用的 JSON 回执：新服务启用、读写拥有者、通道接入与恢复记录。
- `docs/refactor/context-budget-compression-20261028.md` 及 `docs/harness/evidence/context-budget-20261028/**`：consumer 的后续运行增量，不能用 10 月 25 日快照替代。
- `docs/refactor/misty-oral-v2-deployment-20261028.md`、对应证据和 `tools/deploy/misty_oral_v2/README.md`：在记忆消费方上的后续发布配方。
- `plugins/mobile-chat/README.md`：挂载与 source/游标契约；是否进入当前主文档要由实际启用状态确认。
- `docs/refactor/autonomous-action-memory-plan-20261028.md`：**待实施设计**，保留明确状态，不能算已运行功能；其中关于远端版本差异的核查记录有边界价值。

其它历史调研/旧部署文档可以进入归档候选，但先保存原路径、基线 SHA、相对资源及引用关系。源码补丁、恢复配方和当前消费者的验收证据不是普通过期说明，不能只按日期决定归档。

## 5. 现场核对尚缺什么

需要一次受限只读核对，输出以下信息即可，不需要密码、令牌、完整配置、消息正文或数据库内容：

1. `neuralcloud-memory.service`、`misty-harness.service`、`misty-state.service` 的实际 ActiveState/MainPID、WorkingDirectory、FragmentPath/DropInPaths，以及 ExecStart 中的可执行文件、模块/脚本和配置**路径**；同时核对是否有其它记忆服务/旧 writer 启用。
2. 实际配置中 memory endpoint（去掉 URL 凭据/query）、`neuralcloud_plugin` 路径/hash、workflow mode/bindings、渠道/手机插件挂载**名称与路径**。不导出 token、密钥或模型凭据。
3. 从上述路径解析真实 release 根；确认 memory Python 模块来自哪里；采集实际加载源文件的 SHA-256、Git commit/dirty 状态（若有 Git 元数据），以及本地缺失 workflow/continuity 模块的文件清单。配置里声明的 hash 须与文件实际 hash 比对。
4. 对手机插件和旧 QQ 桥接：查实际启动命令/动态 import/插件注册，不向渠道发送测试消息，也不通过模型请求验证启用。
5. 对状态文件只取新鲜度、队列计数、服务/插件版本等诊断字段；如需要证明当前确有新记忆调用，只读取脱敏请求计数或最新调用时间，不读取聊天内容、不写任何状态。
6. 只读核对定时任务、timer、恢复配方对拟删除文件的引用。停用服务与恢复所需源码分开标记，避免删除唯一回滚依赖。

完成后按“路径 → 实际调用方 → 当前启用证据/时间 → 依赖集合 → 文档/测试 → 处置”生成最终清单。当前所有缺少现场证据的条目保持待核对，不删除。

## 6. 下一步边界

本轮已完成静态依赖与历史部署对照，发现 **当前记忆消费者的源码快照与部署记录存在明确差异**。今日线上状态仍待连接核验；不能声称完整范围已经确定，也不能继续以此前约 2,000 行估计最终保留代码量。

在现场证据补齐前，保留完整新记忆服务和消费者相关候选代码/文档，暂停全部删除与归档。没有修改业务文件，没有推送新清理提交。

## 7. 进度保存与接续约束

用户已要求将当前工作进度推送到分支；本次保存范围为实施清单、全仓/实现代码统计、复现工具、既有测试证据及本报告，不包含任何业务代码删除或归档。

- 当前分支：`cleanup/resident-memory-only`。名称沿用此前工作分支，但实际变更仅为调研资料；不得据分支名称恢复“只保留 memory 包”的错误范围。
- 用户不希望为本次核验在服务器安装 MCP 或其它常驻组件。后续优先考虑现有 SSH/受保护的环境凭据，或由用户在现有 SSH 会话运行经审阅的只读采集命令并提供脱敏结果；尚未选择连接方式，也未收到凭据。
- 接续顺序：取得第 5 节的实际运行证据 → 补齐部署增量与源码差异 → 确定消费者及其传递依赖 → 生成文件级保留/归档/删除清单 → 再实施经确认范围的清理。
- 之前的 110,880 行全仓实现代码统计与 1,919 行本地记忆服务/插件统计仍是各自固定口径，不代表最终实际启用范围。不得把历史运行记录写成今日现场核验结果。
