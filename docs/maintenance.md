# 维护与修改指南

本文为人和 AI 提供源码导航及维护边界。它描述当前实现，不授予执行部署、发送消息或修改线上数据的权限。

## 修改从哪里开始

改变请求字段或响应字段，先读 [contracts.py](../src/neuralcloud/contracts.py) 和 [api.py](../src/neuralcloud/api.py)，再查 [插件客户端](../plugins/dsh-neuralcloud/src/index.mjs)。改变记录生命周期，先读 [storage.py](../src/neuralcloud/storage.py) 的 ingest、correct、sources 和 changes 处理，再检查 retrieval 与 archive 对记录状态的使用。

改变查询结果，先区分基础 recall、Retriever 和 MemoryWorkflow 哪条路径被配置启用。工作流代码与 [规则文件](../src/neuralcloud/workflows/memory-v1.json) 配套，不能只改规则名而不确认代码消费者。

旧 Python 运行时及其专用调用链已于 2026-11-17 删除。后续修改应围绕保留的新服务、插件和工具展开，不重新引入 harness/gui/infra 依赖。

## 归档不是普通记忆接口

[archive.py](../src/neuralcloud/archive.py) 的 ArchiveManager 管理归档状态、验证和恢复授权。detach 要求批次处于 VERIFIED_OFF_HOST，并重新检查记录修订；它把记录标为 ARCHIVED_OFFLINE 并从在线索引移除。evict 当前直接拒绝，错误为 LOCAL_EVICTION_BLOCKED_PENDING_DEPLOYMENT_COPY_INVENTORY，不能将 detach 描述为已清除全部本机副本。

[maintenance.py](../src/neuralcloud/maintenance.py) 提供 enqueue、tick、status、checkpoint、retry，显式要求数据库、角色、root 和 policy；部分操作还需要 request 或 key-file。[temporary_views.py](../src/neuralcloud/temporary_views.py) 是临时受限读取实现，[migration.py](../src/neuralcloud/migration.py) 是显式旧库导入入口。这些能力不在普通角色 HTTP 路由内。

归档依赖 archive extra。维护 CLI 也会取得数据库所有权，因此是否能在现有服务运行时打开数据库必须按源码锁行为判断；不能为执行命令而自动停止现有服务。

## 人与 AI 如何使用这套文档

先确认本次操作针对哪个仓库、分支、目录和服务，再阅读对应源码入口。文档中的本地实现说明不证明远端运行相同版本；线上核对需取得实际加载路径和文件内容。不要把旧测试统计、旧发布名称或文档标题当成现行事实。

代码变更后同步更新相关说明；涉及字段则同步生成契约。描述尚未验证的行为时写明限制，不写“测试通过”或“已上线”。本轮用户要求暂不运行测试，应仅做静态检查；以后明确开展测试时，默认 Python 范围见 pyproject.toml，JavaScript 范围见各插件 package.json。

评测包的导入前缀是 `workspace_assets.eval`。数据、日志、跟踪和构建产物集中在 workspace_assets，本机兼容 junction 仍可能被旧工具使用；不要递归删除链接指向的数据。

旧项目文档已删除。保留的机器可读证据、契约、依赖清单、脚本与第三方许可证不作为现行使用指南；历史资产中的路径和哈希可能对应已删除文档，这是历史出处，不应伪造为本轮重新生成的结果。

## 旧运行时退役调研（2026-11-16 历史核查）

以下为删除前的核查记录；本地执行结果见文末。服务状态仅代表当时的只读观察，本轮未再次连接服务器。

目的：移除 NeuralCloud 旧 Python 角色运行时及其配套调用，仅保留记忆服务和必要接入。本节依据当前本地源码、MistyAI 调用代码与本次服务器只读核查；未执行删除、测试、重启或部署。结论是：三个旧目录可以作为完整退役批次移除，不需要为旧工具和旧测试无限期保留它们，但不能把所有同名服务和工具目录一并删除。

### 现场边界

服务器的 neuralcloud-cognitive、neuralcloud-homeostasis、neuralcloud-runtime、neuralcloud-gui、neuralcloud-board、neuralcloud-qq-direct 均为 disabled / inactive、MainPID=0。它们的 ExecStart 仍指向 `/opt/neuralcloud/current` 下的 `python -m harness.deploy`，属于尚未移除的旧服务单元。8770、8771、8772、8790 未发现监听；本次 /proc 命令行扫描没有发现以 harness.*、gui.* 或 infra.* 为启动模块的进程。该扫描不是对所有动态导入和未来定时任务的绝对证明。

仍在运行的 neuralcloud-memory 和 neuralcloud-evidence 使用 `/opt/misty/releases/resident-20261028-memory-workflow-v4/NeuralCloud` 的新包；neuralcloud-semantic 使用 `/opt/misty/semantic/.venv` 的 neuralcloud.vector_index。对已检查的新包源码和语义服务安装包做 AST 检查，未发现旧三个顶层包的绝对导入。

misty-harness 是另一套运行时：实际启动 `/opt/misty/releases/resident-unified-compression-20261104/MistyAI/compatibility/harness/resident/launch-paused-napcat.mjs`。它导入同目录 mobile-chat.mjs，并从配置动态加载 NeuralCloud 的 plugins/dsh-neuralcloud/src/index.mjs。因此不能把 MistyAI 的 compatibility/harness 当成这里的旧 Python harness 删除。

### 应保留的最小调用链

- `src/neuralcloud`：记忆 API、存储、检索、证据与语义索引，以及显式维护模块；其中 migration 读取旧库格式但不导入旧运行时，不能因名字含迁移就删除。
- `plugins/dsh-neuralcloud`：MistyAI 动态加载的记忆客户端、发送队列、工具绑定及契约；保留其测试代码。
- `contracts/resident`、`tools/export_resident_contracts.py`、`tests/resident`、`.github/workflows/ci.yml` 和 Python 项目元数据：现行生成、构建与验证链。本轮只读这些入口，未执行 CI 命令。
- `tools/deploy/frp_runner.py`：线上 neuralcloud-frp 正在运行 `/opt/neuralcloud/tooling/frp_runner.py`，远端文件 SHA-256 与本地一致。必须从旧部署工具清理批次中排除。
- `tools/deploy/install-ingress.sh`：暂留为入口设施的维护配方，后续按手机路由拆分旧 GUI 配置；不是因它位于 deploy 就判为旧角色运行时。
- 线上数据库、凭据、索引、模型和消息服务，以及 MistyAI 的状态、通知、手机和 JavaScript 宿主实现，不属于本轮源码退役对象。

### 可成组退役的内容

核心是 `harness/`（397 个跟踪文件）、`gui/`（40 个）和 `infra/`（17 个）。AST 发现 532 个外部 Python 文件直接导入其中至少一个包；这个数按文件去重，不代表 532 个线上调用方，主要是旧测试、工具、评测和证据源码快照。

与核心一起处理根 conftest.py、dev.ps1、launch.ps1、launch.json、start/stop 系列启动器、旧 launcher、自检、QQ、Board、Docker 和旧模型启动配套。旧 deploy 服务模板、旧 presets、非 resident 测试及 workspace_assets/eval 中对应评测应按调用链成组退役；不能只删除 import 语句让旧功能表面可启动。

评测和历史样本里有不依赖旧运行时的独立实验，不能把“目录中存在旧导入”扩大为对全部本地数据的删除许可。待删除的是退役代码及其专用配套；数据库、私有记录和模型资产另外核对。

### 三个必须解除的耦合

**发布哈希清单。** 当前宿主的 memory_stack_manifest 锁定 87 条 NeuralCloud 文件路径，包括 19 条 src 路径、37 条契约路径、11 个工具、测试和 README；未绑定 harness/gui/infra。部分 contracts/embodiment 和 production-body-schema-lock 虽不是当前记忆 API 的导入依赖，仍属于启动校验依赖。MistyAI 的 memory-stack.mjs 会逐文件校验路径和哈希。此前本地 README 重写未部署到这些远端路径，因此不影响当前进程；后续部署不能直接覆盖冻结目录，需要在新发布中明确重建清单及配置绑定。

**共用入口。** 活动 Caddy 配置有两处代理到 8786（手机服务），另有一处指向已不监听的 8790。删除旧 GUI 路由不等于停止整个 neuralcloud-proxy 或 neuralcloud-frp；手机入口仍需保留。这轮没有修改路由。

**遗留 systemd 关系。** 旧 Runtime 仍被旧 GUI / QQ Direct 的 RequiredBy 引用，旧 Cognitive 被旧 Runtime 引用，旧服务还列在 neuralcloud.target 的 WantedBy 中。后续服务器退役要一起移除对应 target 关系及单元，不能仅以 disabled 认定文件已经无关。未发现名为 misty/neuralcloud 的 timer 单元，neuralcloud-backup.service 也未安装；本地备份模板是旧配套，不应假装它正在保护新数据库。

### 另外处理的项目

`plugins/mobile-chat` 未在本次活动启动入口中看到被使用；活动入口使用 MistyAI 同目录的 mobile-chat.mjs。它不是保留旧 Python 运行时的理由，但是否退役独立发布的插件，还需单独确认外部包消费者，不能由单台服务器的启动路径断定全局无调用。

新检索的模型下载、评测、seed_identity 和索引探针工具，以及当前口语、手机、会话压缩发布工具，不应按“tools 全部是旧代码”删除。已由线上清单锁定的工具先保留；其余按任务和实际调用方分离。历史证据代码不属于运行路径，但也不应计作当前业务消费者。

### 后续实施顺序

1. 固定保留链和线上绑定清单，确认现有工作区未提交的目录整理、文档重写与待退役变更的边界。
2. 删除旧核心与专用启动、配置、工具和验证链；保留新链，再静态扫描剩余源码、配置、脚本的旧导入和旧路径，逐项消除真实残留。
3. 单独迁移哈希清单中的非运行必需文件；保持冻结远端目录原样，避免对正在运行的发布使用同步删除。
4. 在明确安排服务器退役时，清理旧服务单元、target 依赖和 8790 路由，保留 8786 手机代理和 FRP。退役数据库、模型或历史记录不包含在此源码操作中。
5. 本轮按要求不运行测试。以后进入验证阶段时，对新记忆服务和实际手机调用链安排必要检查；未验证前不宣称已完成运行回归。

本地逐文件调研清单保存于被忽略的 workspace_assets/legacy-runtime-retirement-inventory.json；它区分保留、受发布清单约束、旧运行时删除候选、旧配套候选与待单独确认项，不是可直接执行的删除脚本。

## 本地旧运行时清理结果（2026-11-17）

已执行固定清单删除，共移除 1,610 个文件：包括 1,226 个当时由 Git 跟踪的源码、配置及配套文件，另有旧运行时目录内的缓存等文件。`harness/`、`gui/`、`infra/`、`presets/`、根目录 `deploy/`、旧启动器和根 conftest.py 已删除，空目录一并移除。随后又备份并清理 644 个已无对应源码的 Python/pytest 缓存文件；累计删除 2,254 个文件。

删除前已将实际工作区内容备份到 `C:/Users/22850/ProjectArchives/legacy-runtime-local-20261116-234059/removed-files.zip`，ZIP 完整性检查通过。脚本逐项校验文件 SHA-256、路径和保留清单后删除，没有递归跟随目录链接。前一日的自动审批拒绝没有造成删除；本次在用户再次明确授权后执行成功。

新记忆服务、记忆插件、resident 契约与验证代码、FRP、入口安装配方、独立手机插件、现行口语与会话压缩工具均保留。发布清单约束的文件未删除；README 仅为同步本地清理现状更新了文字，其余 90 个受保护文件内容哈希与清理前一致。未来部署仍需重新审核发布清单，不能把本地目录同步删除到远端冻结发布。

历史评测数据、报告、数据库、模型和日志不在本次代码删除范围。依赖旧运行时的评测 Python 代码随调用链移除；不依赖它的独立工具保留，不能把保留的数据目录当成仍可运行的完整旧评测包。

验证仅包括文件存在性、受保护文件哈希、剩余 Python AST 语法与旧包导入检查、文档链接和 Git 差异格式检查。没有运行测试、评测或构建，没有连接或修改线上服务，也没有提交或推送。

本机结果记录为 `workspace_assets/local-runtime-cleanup-result.json`，删除计划和备份索引保存在同目录。清理脚本是针对删除前快照的一次性脚本，已执行，不应再次运行；回滚需从外部 ZIP 或 Git 历史按需恢复。
