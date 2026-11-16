# 文件级保留清单（2026-11-16）

状态：用于继续清理讨论的逐文件保留建议；本次不执行删除、归档、部署或服务器变更。

## 范围与判读

- 基线：NeuralCloud `9f33e57e`（cleanup/resident-memory-only），MistyAI `0f8ec93`（codex/restore-production-source-20261116）；现场依据是同日已完成的核查与两个仓库的 `production-source-sync-20261116.json`，本次未重新宣称服务状态实时不变。
- 每条路径均相对其所属仓库，逐文件写明一句保留原因；不以目录通配符代替文件清单，不要求把 MistyAI 文件搬入 NeuralCloud。
- “运行与接口”表示当前服务和消费者所需源码及契约接线，不代表每个辅助模块均已观察到生产执行；“管理、验证与构建”“契约与测试”“文档与恢复证据”同样属于保留范围，但不等于线上常驻依赖。
- “暂保留待核实”表示不能证明可以删除，尚未认定为最终必需；其中未跟踪的部署配方只保留在本地，未随源码补齐提交。
- 未列出的旧 harness/gui/infra、旧测试、启动脚本和历史资料仍待逐入口核查；**不在清单中不等于获得删除许可**，本清单不是反向删除脚本输入。
- 数据库、聊天与记忆正文、私有配置、凭据、模型权重、虚拟环境和 node_modules 不属于 Git 源文件保留清单，仍按运行与备份要求保全，绝不因未列入而删除或提交。
- 已知 MA 测试失败、契约漂移和旧 CI 配对必须保留证据并后续修复，不能通过删测试或删契约使检查变绿。
- `NeuralCloud/docs/refactor/file-retention-list-20261116.md`：记录经核查的文件级清理边界与每个文件的保留理由。

共列出 283 个已有文件：NeuralCloud 146 个，MistyAI 137 个；另保留本清单自身。

## NeuralCloud

### 运行与接口（15 个）

- `plugins/dsh-neuralcloud/src/contracts.d.ts`：向 JS 调用方提供拥有者导出的 TypeScript 契约。
- `plugins/dsh-neuralcloud/src/index.mjs`：提供被 Harness 复用的 ServiceClient、记忆召回工具和持久 Outbox。
- `src/neuralcloud/__init__.py`：声明独立记忆包身份并提供 Python 包入口。
- `src/neuralcloud/__main__.py`：提供记忆初始化、服务启动及工作流配置的命令入口。
- `src/neuralcloud/api.py`：暴露受认证的记忆写入、召回、更正接口并维持管理能力隔离。
- `src/neuralcloud/contracts.py`：定义记忆事件、回执、召回证据及哈希规范的权威类型。
- `src/neuralcloud/evidence_index.py`：维护绑定来源修订的可重建证据索引。
- `src/neuralcloud/ownership.py`：以操作系统锁确保同一记忆库只有一个正常写入拥有者。
- `src/neuralcloud/rebuild_batch.py`：实现生产证据服务的单并发抽取、预算预留和断点恢复。
- `src/neuralcloud/retrieval.py`：组合身份、关键词和语义证据完成有界召回。
- `src/neuralcloud/semantic.py`：访问本地语义服务并禁止远程模型兜底。
- `src/neuralcloud/storage.py`：维护权威 SQLite 记忆、事务、幂等回执和修订失效。
- `src/neuralcloud/vector_index.py`：实现独立语义 worker 与可重建向量索引。
- `src/neuralcloud/workflow.py`：统一准备阶段与显式召回工具使用的有界证据工作流。
- `src/neuralcloud/workflows/memory-v1.json`：定义 memory-v1 工作流节点和预算，作为包内资源随运行代码分发。

### 管理、验证与构建（25 个）

- `.cursor/rules/github-cli.mdc`：落实用户指定的 GitHub CLI 优先拉取和失败回退约定。
- `.gitattributes`：固定 Git 文本和换行处理以稳定源码比较。
- `.github/workflows/ci.yml`：保留 resident 构建与验收门禁入口，防止清理后丢失自动验证流程。
- `.gitignore`：阻止该目录范围内的凭据、运行数据及生成产物进入版本库。
- `plugins/dsh-neuralcloud/package.json`：声明 plugins/dsh-neuralcloud 的依赖、导出和命令入口。
- `pyproject.toml`：声明 Python 包、运行依赖、命令入口和测试构建规则。
- `src/neuralcloud/archive.py`：执行带签名、加密和恢复验证门禁的记忆归档。
- `src/neuralcloud/calibration.py`：对证据抽取结果离线校准并按修订检查激活策略。
- `src/neuralcloud/maintenance.py`：编排有界归档任务、重试和维护状态。
- `src/neuralcloud/migration.py`：从已核验旧数据库快照显式、断点续传地导入记忆。
- `src/neuralcloud/rebuild.py`：提供有预算限制的离线抽取试验和索引重建入口。
- `src/neuralcloud/temporary_views.py`：维护可撤销且自动过期的加密管理视图。
- `tools/benchmark_memory_workflow.py`：度量记忆工作流并执行隔离生成重放。
- `tools/benchmark_resident.py`：测量独立进程启动与有界查询而不输出记忆正文。
- `tools/build_retrieval_evaluation.py`：生成隔离的来源可核验检索评测集。
- `tools/download_retrieval_models.py`：获取语义检索所需的公开模型文件。
- `tools/download_retrieval_modelscope.py`：从固定镜像按哈希下载检索模型。
- `tools/evaluate_local_retrieval.py`：对冻结语料运行本地嵌入检索评测。
- `tools/export_resident_contracts.py`：从拥有者类型导出 Schema 和 TypeScript 并检测契约漂移。
- `tools/prepare_retrieval_int8.py`：生成 CPU 检索所需的量化模型产物。
- `tools/probe_retrieval_http.py`：以只读 HTTP 探针核对检索接口和延迟。
- `tools/probe_semantic_lifecycle.py`：在隔离数据上验证语义服务生命周期。
- `tools/seed_identity.py`：从经审核证据清单执行离线身份初始化。
- `tools/watch_retrieval.py`：以不保存聊天正文的聚合指标观察发布后的检索。
- `uv.lock`：固定 Python 依赖解析结果以保留发布环境的重建依据。

### 契约与测试（40 个）

- `contracts/resident/v1/ChannelContext.v1.schema.json`：定义 ChannelContext 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ContentPerception.v1.schema.json`：定义 ContentPerception 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Correction.v1.schema.json`：定义 Correction 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DocumentPerception.v1.schema.json`：定义 DocumentPerception 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DurableReceipt.v1.schema.json`：定义 DurableReceipt 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/EvidenceAssessment.v1.schema.json`：定义 EvidenceAssessment 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/EvidenceReference.v1.schema.json`：定义 EvidenceReference 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/IdentityEvidence.v1.schema.json`：定义 IdentityEvidence 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/LegacyProvenance.v1.schema.json`：定义 LegacyProvenance 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/MissingReceipt.v1.schema.json`：定义 MissingReceipt 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Recall.v1.schema.json`：定义 Recall 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RecallConstraints.v1.schema.json`：定义 RecallConstraints 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RecallDiagnostics.v1.schema.json`：定义 RecallDiagnostics 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RecallResult.v1.schema.json`：定义 RecallResult 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RecallSubject.v1.schema.json`：定义 RecallSubject 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RecordView.v1.schema.json`：定义 RecordView 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RestoreGrant.v1.schema.json`：定义 RestoreGrant 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/RetrievalEvidence.v1.schema.json`：定义 RetrievalEvidence 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/SourceEvent.v1.schema.json`：定义 SourceEvent 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/SourceEventV2.v1.schema.json`：定义 SourceEventV2 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/SourceInfo.v1.schema.json`：定义 SourceInfo 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/SourceInfoV2.v1.schema.json`：定义 SourceInfoV2 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/SupersessionReference.v1.schema.json`：定义 SupersessionReference 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/TextRange.v1.schema.json`：定义 TextRange 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/WorkflowEvidence.v1.schema.json`：定义 WorkflowEvidence 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/openapi.v1.json`：描述本仓库 resident HTTP API 以供消费者及契约漂移检查。
- `plugins/dsh-neuralcloud/test/outbox.test.mjs`：验证持久出站队列、回执和断网缓存限制，为后续修改保留回归边界。
- `plugins/dsh-neuralcloud/test/workflow.test.mjs`：验证准备阶段与显式召回共享的证据工作流，为后续修改保留回归边界。
- `tests/resident/conftest.py`：提供 resident 测试共享夹具并隔离真实运行状态。
- `tests/resident/pytest.ini`：固定 resident 测试发现与隔离范围，避免混入旧测试链。
- `tests/resident/test_archive.py`：验证加密归档与授权恢复边界，为后续修改保留回归边界。
- `tests/resident/test_calibration.py`：验证校准产物及修订绑定，为后续修改保留回归边界。
- `tests/resident/test_maintenance.py`：验证有界归档维护与恢复，为后续修改保留回归边界。
- `tests/resident/test_memory.py`：验证记忆持久化、幂等和更正语义，为后续修改保留回归边界。
- `tests/resident/test_migration.py`：验证迁移幂等、错误输入及断点恢复，为后续修改保留回归边界。
- `tests/resident/test_rebuild_batch.py`：验证证据重建预算与暂停恢复，为后续修改保留回归边界。
- `tests/resident/test_retrieval_v2.py`：验证身份与混合检索的来源约束，为后续修改保留回归边界。
- `tests/resident/test_temporary_views.py`：验证临时授权视图的过期、撤销和清理失败，为后续修改保留回归边界。
- `tests/resident/test_vector_lifecycle.py`：验证向量索引重建与服务生命周期，为后续修改保留回归边界。
- `tests/resident/test_workflow.py`：验证准备阶段与显式召回共享的证据工作流，为后续修改保留回归边界。

### 文档与恢复证据（51 个）

- `README.md`：提供当前服务职责、运行入口和现场恢复记录的阅读入口。
- `docs/harness/evidence/context-budget-20261028/deployment.json`：保留上下文预算发布的现场回执与版本对应关系。
- `docs/harness/evidence/context-budget-20261028/source.patch`：保留上下文预算与压缩发布的可恢复源码补丁。
- `docs/refactor/README.md`：保留“Grounded Agency 全面重构调研”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/autonomous-action-memory-plan-20261028.md`：保留“自主行动经历记忆：实施计划与待办”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/code-cleanup-audit-20261114/README.md`：保留“2026-11-14 清理审计证据”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/code-cleanup-audit-20261114/checks.json`：保存清理审计时各验证命令的执行结论。
- `docs/refactor/code-cleanup-audit-20261114/count_implementation.py`：复现实现代码统计的固定口径。
- `docs/refactor/code-cleanup-audit-20261114/count_repository.py`：复现全仓文件统计的固定口径。
- `docs/refactor/code-cleanup-audit-20261114/dependency-audit.json`：保存清理前消费者依赖核对与待现场确认的条目。
- `docs/refactor/code-cleanup-audit-20261114/fault-probe.json`：保存记忆插件错误分类探针的原始结果。
- `docs/refactor/code-cleanup-audit-20261114/fault-probe.mjs`：复现记忆插件的非法响应和错误重试语义。
- `docs/refactor/code-cleanup-audit-20261114/implementation-size.json`：保留 implementation-size.json 的基线统计以区分全仓体量与当前实现范围。
- `docs/refactor/code-cleanup-audit-20261114/inventory.json`：保留 inventory.json 的基线统计以区分全仓体量与当前实现范围。
- `docs/refactor/code-cleanup-audit-20261114/mistyai-harness.txt`：保存 mistyai-harness 的历史测试输出以核对审计结论。
- `docs/refactor/code-cleanup-audit-20261114/mistyai-pytest.txt`：保存 mistyai-pytest 的历史测试输出以核对审计结论。
- `docs/refactor/code-cleanup-audit-20261114/neuralcloud-plugin.txt`：保存 neuralcloud-plugin 的历史测试输出以核对审计结论。
- `docs/refactor/code-cleanup-audit-20261114/neuralcloud-pytest.txt`：保存 neuralcloud-pytest 的历史测试输出以核对审计结论。
- `docs/refactor/code-cleanup-audit-20261114/repository-size.json`：保留 repository-size.json 的基线统计以区分全仓体量与当前实现范围。
- `docs/refactor/code-cleanup-audit-20261114/tcp-integration.txt`：保存 tcp-integration 的历史测试输出以核对审计结论。
- `docs/refactor/code-cleanup-implementation-checklist-20261114.md`：保留“代码清理实施清单：以 24 小时可理解为验收目标”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/context-budget-compression-20261028.md`：保留“按上下文预算自动压缩：部署记录”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/current-memory-dependency-scope-20261114.md`：保留“新记忆服务及当前使用方：保留范围核对”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/00-remote-progress-sync.md`：保留“G0 远端进度同步报告”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/01-production-observations.json`：保存首次生产核查观察以解释迁移前状态。
- `docs/refactor/misty-harness-rollout/03-isolated-implementation.md`：保留“隔离实施与验收记录（2026-10-25）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/04-managed-harness.md`：保留“常驻 Harness 编排与恢复检查点”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/05-persona-state-archive.md`：保留“人格、旧状态与归档维护：隔离验收记录”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/06-production-cutover.md`：保留“生产切换记录：2026-10-25”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/07-production-operations.md`：保留“Misty resident 运维入口”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/08-onebot-production.md`：保留“OneBot 生产接入与真实收发验收（2026-10-25）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/09-autonomy-repair.md`：保留“主动调度修复与验证”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/baseline-manifest.json`：记录切换前源码和环境基线以定位恢复版本。
- `docs/refactor/misty-harness-rollout/decisions.md`：保留“已确认实施决定”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/implementation-checkpoints.md`：保留“Implementation checkpoints”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/progress.json`：保存历史实施阶段与未完成门禁以防把计划误认为验收。
- `docs/refactor/misty-harness-rollout/validation-20261025.json`：保存 20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-autonomy-fix-20261025.json`：保存 autonomy-fix-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-group104-auto-20261025.json`：保存 group104-auto-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-onebot-production-20261025.json`：保存 onebot-production-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-production-20261025.json`：保存 production-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-oral-v2-deployment-20261028.md`：保留“Misty 口语表达 v2 部署”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/production-source-sync-20261116.json`：记录逐文件现场来源、原始哈希和 Git 规范化哈希以验证恢复真实性。
- `docs/refactor/production-source-sync-20261116.md`：保留“生产源码补齐交接（2026-11-16）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/simplification-and-error-handling-20261114.md`：保留“MistyAI / NeuralCloud：代码瘦身与错误处理收敛需求”的设计、边界或交接依据以解释当前实现。
- `tools/deploy/misty_oral_v2/README.md`：说明表达规则发布与回滚的前置条件。
- `tools/deploy/misty_oral_v2/deploy.py`：复现表达规则的发布步骤和回滚路径。
- `tools/deploy/misty_oral_v2/hashes.json`：验证表达规则发布输入的完整性。
- `tools/deploy/misty_oral_v2/oral-expression.mjs`：保留该历史发布的表达规则源码以重建基线。
- `tools/deploy/misty_oral_v2/oral-expression.test.mjs`：验证历史表达规则补丁的预期行为。
- `tools/deploy/misty_oral_v2/source.patch`：重建表达规则发布对消费者代码的增量。

### 暂保留待核实（15 个）

- `.cursor/rules/shared-mind.mdc`：保留历史单实例约束供迁移核对，其中旧 Cognitive/GUI 表述需改写后才可作为当前规约。
- `plugins/mobile-chat/README.md`：说明独立手机插件的接入契约，待与现场 resident 适配确认关系。
- `plugins/mobile-chat/channel.mjs`：保存独立手机渠道的接收与 source 事件实现，现场未证实直接挂载。
- `plugins/mobile-chat/invited.mjs`：提供受邀请群聊客户端，尚需核对外部使用方。
- `plugins/mobile-chat/join.mjs`：提供邀请入群命令入口，尚需核对外部使用方。
- `plugins/mobile-chat/package.json`：定义独立手机插件导出和依赖，删除前需确认包使用者。
- `tools/deploy/unified_compression/README.md`〔未跟踪〕：保存已运行统一压缩版本的部署、回滚与已知异常说明。
- `tools/deploy/unified_compression/capability-catalog.mjs`〔未跟踪〕：保存按需能力目录的部署输入以比对现场实现。
- `tools/deploy/unified_compression/failed-drive.mjs`〔未跟踪〕：保存自主任务失败门禁的部署输入以比对现场实现。
- `tools/deploy/unified_compression/fix_app_turn.py`〔未跟踪〕：保存手机轮次修复配方，须核对当前适用版本。
- `tools/deploy/unified_compression/install_catalog.py`〔未跟踪〕：保存能力目录的发布安装步骤，须核对当前可重入性。
- `tools/deploy/unified_compression/install_drive_guard.py`〔未跟踪〕：保存自主任务门禁的安装步骤，须核对当前可重入性。
- `tools/deploy/unified_compression/recover_exhausted.py`〔未跟踪〕：保存耗尽摘要重试会话的维护恢复工具，须核对操作门禁。
- `tools/deploy/unified_compression/source.patch`〔未跟踪〕：保存统一压缩最终补丁以从先前版本恢复该发布。
- `tools/deploy/unified_compression/stage.py`〔未跟踪〕：记录统一压缩发布的初始 staging 配方。

## MistyAI

### 运行与接口（38 个）

- `compatibility/harness/resident/basic-tools/index.mjs`：安装调度、待办、存储、网页与手机提醒等当前组合依赖。
- `compatibility/harness/resident/capabilities.mjs`：向 Harness 绑定内容读取和经配置准入的外部能力。
- `compatibility/harness/resident/commitment-memory.mjs`：把手机承诺的证据组织为可写入记忆的事件。
- `compatibility/harness/resident/composition.mjs`：装配锁定 Harness、Misty 插件、记忆客户端和常驻运行时。
- `compatibility/harness/resident/context-budget.mjs`：计算上下文预算及触发压缩的阈值。
- `compatibility/harness/resident/continuity.mjs`：生成并验证跨会话延续摘要，阻止空会话替代失败摘要。
- `compatibility/harness/resident/document-text.mjs`：提供有界文档读取和文本证据提取。
- `compatibility/harness/resident/failed-drive.mjs`：识别失败或待核对自主任务以限制错误重放。
- `compatibility/harness/resident/group-channel.mjs`：实现独立手机群聊通道的轮询和交付接线。
- `compatibility/harness/resident/images.mjs`：将图片转为 Harness 原生附件并执行来源及大小约束。
- `compatibility/harness/resident/launch-paused-napcat.mjs`：作为现场 ExecStart 指定的入口启动当前 Harness 和手机通道。
- `compatibility/harness/resident/market-android.mjs`：绑定已审查的 Android MCP 能力和设备访问边界。
- `compatibility/harness/resident/memory-stack.mjs`：核验记忆插件、工作流和发布清单的版本与哈希。
- `compatibility/harness/resident/mobile-chat.mjs`：把实际手机消息接入持久输入、记忆及回复通道。
- `compatibility/harness/resident/onebot.mjs`：维护 OneBot 消息身份、去重和显式投递适配。
- `compatibility/harness/resident/phone-media.mjs`：将图片交付任务接入幂等手机媒体 worker。
- `compatibility/harness/resident/phone-reminders.mjs`：实现当前基础工具依赖的手机提醒接口。
- `compatibility/harness/resident/private-contact.mjs`：维护手机私聊机会、安静时段和联系策略。
- `compatibility/harness/resident/runtime.mjs`：持有持久会话、串行调度、输入屏障和投递恢复状态。
- `compatibility/harness/resident/session-maintenance.mjs`：在渠道之上统一执行会话维护并隔离单会话失败。
- `compatibility/harness/vendor/dsh-onebot-eb449d46.tgz`：提供锁文件指定的 OneBot 本地依赖包以重装当前渠道适配。
- `plugins/dsh-misty/src/capability-catalog.mjs`：提供按需能力发现及权限过滤。
- `plugins/dsh-misty/src/contracts.d.ts`：向 JS 调用方提供拥有者导出的 TypeScript 契约。
- `plugins/dsh-misty/src/index.mjs`：将状态、审批、入站记忆和投递账本绑定到 Harness 工具生命周期。
- `plugins/dsh-misty/src/oral-expression.mjs`：提供当前记忆消费者使用的表达规则。
- `plugins/dsh-misty/src/recall-input.mjs`：规范可信说话人与召回目标以防跨主体检索混淆。
- `services/chat/chat_images.py`：将图片附件独立存储并以 image ID 接入消息。
- `services/chat/commitments.py`：保存私聊承诺、提醒和回执的可追溯证据。
- `services/chat/group.py`：实现与私聊历史隔离的共享群组接口。
- `services/chat/media/worker.py`：执行图片准备和幂等媒体交付任务。
- `services/chat/server.py`：维护手机消息持久化、私聊上下文和幂等收发。
- `src/mistyai/__init__.py`：声明 MistyAI 顶层包使状态服务和导出工具可导入。
- `src/mistyai/integration/__init__.py`：声明常驻状态与 Harness 接口包。
- `src/mistyai/integration/main.py`：加载角色配置并提供状态服务初始化和启动入口。
- `src/mistyai/integration/service.py`：提供角色、执行器和操作员边界的状态 HTTP 接口。
- `src/mistyai/state/__init__.py`：声明独立常驻状态包而不引入旧身体运行时。
- `src/mistyai/state/contracts.py`：定义状态反馈、活动、审批、投递的权威数据契约。
- `src/mistyai/state/runtime.py`：以事务拥有角色状态、活动机会、审批和投递账本。

### 管理、验证与构建（25 个）

- `.gitattributes`：固定 Git 文本和换行处理以稳定源码比较。
- `.github/workflows/ci.yml`：保留 resident 构建与验收门禁入口，后续修正已知配对和安装差异。
- `.gitignore`：阻止该目录范围内的凭据、运行数据及生成产物进入版本库。
- `compatibility/harness/.gitignore`：阻止该目录范围内的凭据、运行数据及生成产物进入版本库。
- `compatibility/harness/package-lock.json`：固定 compatibility/harness 的依赖解析版本以复现安装。
- `compatibility/harness/package.json`：声明 compatibility/harness 的依赖、导出和命令入口。
- `compatibility/harness/resident/basic-tools/package-lock.json`：固定 compatibility/harness/resident/basic-tools 的依赖解析版本以复现安装。
- `compatibility/harness/resident/basic-tools/package.json`：声明 compatibility/harness/resident/basic-tools 的依赖、导出和命令入口。
- `compatibility/harness/resident/ingress-maintenance.mjs`：提供持久输入的离线核对、隔离与恢复操作。
- `compatibility/harness/resident/launch-chat.mjs`：保留手机接入版本的组合入口以核对当前入口与恢复差异。
- `compatibility/harness/resident/launch.mjs`：作为通用启动和验收模块的导入入口维持发布兼容。
- `compatibility/harness/resident/memory-acceptance.mjs`：提供记忆栈维护验收入口以核对发布绑定。
- `compatibility/harness/resident/memory-replay.mjs`：使用重放验证记忆与 Harness 组合行为。
- `compatibility/harness/resident/operator-notifier.mjs`：向操作员传递审批通知并保留含糊发送结果。
- `compatibility/patch-qq.mjs`：对锁定 QQ SDK 应用显式发送和持久接收补丁。
- `plugins/dsh-misty/package.json`：声明 plugins/dsh-misty 的依赖、导出和命令入口。
- `pyproject.toml`：声明 Python 包、运行依赖、命令入口和测试构建规则。
- `src/mistyai/integration/deployment.py`：执行不会启停生产服务的只读发布门禁检查。
- `src/mistyai/integration/migration.py`：从冻结输入显式迁移角色身份与状态。
- `src/mistyai/integration/operator.py`：提供不把管理凭据暴露给角色的操作员控制入口。
- `src/mistyai/integration/recovery.py`：保全新增状态并将核验后的恢复包还原到空目录。
- `src/mistyai/state/maintenance.py`：提供显式状态库升级和有界历史保留维护。
- `tools/export_resident_contracts.py`：从拥有者类型导出 Schema 和 TypeScript 并检测契约漂移。
- `tools/recover_encrypted_snapshot.py`：验证加密部署恢复包并支持显式导出。
- `uv.lock`：固定 Python 依赖解析结果以保留发布环境的重建依据。

### 契约与测试（51 个）

- `compatibility/harness/integration/services.test.mjs`：验证真实 TCP 状态服务、记忆服务与 Harness 组合，为后续修改保留回归边界。
- `compatibility/harness/test/agent.test.mjs`：验证真实 Harness 工具编排与状态集成，为后续修改保留回归边界。
- `compatibility/harness/test/capabilities.test.mjs`：验证内容读取与 Android 能力准入，为后续修改保留回归边界。
- `compatibility/harness/test/context-budget.test.mjs`：验证上下文预算和失败摘要处置，为后续修改保留回归边界。
- `compatibility/harness/test/continuity.test.mjs`：验证跨会话摘要、恢复及失败不重计费，为后续修改保留回归边界。
- `compatibility/harness/test/fixtures/persist-crash.mjs`：构造子进程中断场景以验证持久化屏障。
- `compatibility/harness/test/fixtures/resident-checkpoint.mjs`：构造进程退出检查点以验证常驻会话恢复。
- `compatibility/harness/test/fixtures/resident-support.mjs`：为常驻集成测试提供合成模型与持久会话夹具。
- `compatibility/harness/test/fixtures/vision.png`：提供原生图片处理测试所需的固定图片样本。
- `compatibility/harness/test/image-pragmatics.test.mjs`：验证图片理解规则与角色上下文装配，为后续修改保留回归边界。
- `compatibility/harness/test/images.test.mjs`：验证原生图片附件及来源限制，为后续修改保留回归边界。
- `compatibility/harness/test/launch.test.mjs`：验证启动配置与组合门禁，为后续修改保留回归边界。
- `compatibility/harness/test/memory-stack.test.mjs`：验证记忆发布栈哈希和依赖校验，为后续修改保留回归边界。
- `compatibility/harness/test/misty.test.mjs`：验证Misty 工具审查与投递约束，为后续修改保留回归边界。
- `compatibility/harness/test/onebot.test.mjs`：验证OneBot 通道的接收与发送语义，为后续修改保留回归边界。
- `compatibility/harness/test/oral-expression.test.mjs`：验证表达规则的绑定与输出约束，为后续修改保留回归边界。
- `compatibility/harness/test/persistence.test.mjs`：验证会话持久化屏障与崩溃恢复，为后续修改保留回归边界。
- `compatibility/harness/test/private-contact.test.mjs`：验证手机私聊联系策略，为后续修改保留回归边界。
- `compatibility/harness/test/qq-lifecycle.test.mjs`：验证QQ 输入接收、会话切换和持久化生命周期，为后续修改保留回归边界。
- `compatibility/harness/test/qq.test.mjs`：验证QQ SDK 补丁和显式发送约束，为后续修改保留回归边界。
- `compatibility/harness/test/recall-input.test.mjs`：验证可信说话人和显式召回目标的区分，为后续修改保留回归边界。
- `compatibility/harness/test/resident.test.mjs`：验证常驻单实例、输入账本及自主调度，为后续修改保留回归边界。
- `compatibility/harness/test/unified-maintenance.test.mjs`：验证统一压缩不被无关任务阻塞及单会话故障隔离，为后续修改保留回归边界。
- `compatibility/run_services_test.py`：启动隔离的实际 TCP 服务以验证双仓协议和持久化组合。
- `contracts/resident/v1/ActivityRequest.v1.schema.json`：定义 ActivityRequest 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ActivityResult.v1.schema.json`：定义 ActivityResult 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ActivityView.v1.schema.json`：定义 ActivityView 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ApprovalDecision.v1.schema.json`：定义 ApprovalDecision 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ApprovalRequest.v1.schema.json`：定义 ApprovalRequest 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/AutonomyStatus.v1.schema.json`：定义 AutonomyStatus 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Claim.v1.schema.json`：定义 Claim 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/ContextFrame.v1.schema.json`：定义 ContextFrame 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DeliveryReceipt.v1.schema.json`：定义 DeliveryReceipt 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DeliveryRequest.v1.schema.json`：定义 DeliveryRequest 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DriveResult.v1.schema.json`：定义 DriveResult 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/DriveView.v1.schema.json`：定义 DriveView 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Feedback.v1.schema.json`：定义 Feedback 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Persona.v1.schema.json`：定义 Persona 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/Reconciliation.v1.schema.json`：定义 Reconciliation 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/StateSnapshot.v1.schema.json`：定义 StateSnapshot 的跨组件数据结构，避免消费者自行猜测字段。
- `contracts/resident/v1/openapi.v1.json`：描述本仓库 resident HTTP API 以供消费者及契约漂移检查。
- `tests/resident/conftest.py`：提供 resident 测试共享夹具并隔离真实运行状态。
- `tests/resident/pytest.ini`：固定 resident 测试发现与隔离范围，避免混入旧测试链。
- `tests/resident/test_autonomy_status.py`：验证自主调度与状态可观测性，为后续修改保留回归边界。
- `tests/resident/test_conversation_policy.py`：验证对话策略及权限边界，为后续修改保留回归边界。
- `tests/resident/test_deployment.py`：验证生产发布门禁，为后续修改保留回归边界。
- `tests/resident/test_migration.py`：验证迁移幂等、错误输入及断点恢复，为后续修改保留回归边界。
- `tests/resident/test_private_contact.py`：验证私聊机会竞争和幂等交付，为后续修改保留回归边界。
- `tests/resident/test_recovery.py`：验证恢复包校验与新增状态保全，为后续修改保留回归边界。
- `tests/resident/test_retention.py`：验证状态保留与显式 schema 迁移，为后续修改保留回归边界。
- `tests/resident/test_state.py`：验证状态事务、审批和投递账本，为后续修改保留回归边界。

### 文档与恢复证据（23 个）

- `README.md`：提供当前服务职责、运行入口和现场恢复记录的阅读入口。
- `docs/refactor/misty-harness-rollout/00-remote-progress-sync.md`：保留“G0 远端进度同步报告”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/03-isolated-implementation.md`：保留“隔离实施与验收记录（2026-10-25）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/04-managed-harness.md`：保留“常驻 Harness 编排与恢复检查点”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/05-persona-state-archive.md`：保留“人格、旧状态与归档维护：隔离验收记录”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/06-production-cutover.md`：保留“生产切换记录：2026-10-25”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/07-production-operations.md`：保留“Misty resident 运维入口”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/08-onebot-production.md`：保留“OneBot 生产接入与真实收发验收（2026-10-25）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/09-autonomy-repair.md`：保留“主动调度修复与验证”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/11-conversation-policy-20261026.md`：保留“对话额度、自主活动与自然收尾修复”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/12-native-images-20261026.md`：保留“Native image context — 2026-10-26”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/baseline-manifest.json`：记录切换前源码和环境基线以定位恢复版本。
- `docs/refactor/misty-harness-rollout/decisions.md`：保留“已确认实施决定”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/implementation-checkpoints.md`：保留“Implementation checkpoints”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/misty-harness-rollout/progress.json`：保存历史实施阶段与未完成门禁以防把计划误认为验收。
- `docs/refactor/misty-harness-rollout/validation-20261025.json`：保存 20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-autonomy-fix-20261025.json`：保存 autonomy-fix-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-group104-auto-20261025.json`：保存 group104-auto-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-onebot-production-20261025.json`：保存 onebot-production-20261025 批次的测试和现场验收边界。
- `docs/refactor/misty-harness-rollout/validation-production-20261025.json`：保存 production-20261025 批次的测试和现场验收边界。
- `docs/refactor/production-source-sync-20261116.json`：记录逐文件现场来源、原始哈希和 Git 规范化哈希以验证恢复真实性。
- `docs/refactor/production-source-sync-20261116.md`：保留“生产源码补齐交接（2026-11-16）”的设计、边界或交接依据以解释当前实现。
- `docs/refactor/simplification-and-error-handling-20261114.md`：保留“MistyAI / NeuralCloud：代码瘦身与错误处理收敛需求”的设计、边界或交接依据以解释当前实现。

## 待闭合的核查

1. 核对旧服务、timer、cron、恢复配方与额外 writer 的文件引用，再建立独立删除候选表。
2. 核对独立 mobile-chat npm 插件的外部使用者，以及本地未跟踪部署配方是否需要正式入库。
3. 核对语义 worker 安装副本、模型和 Android/MCP 外部进程依赖；这些运行资产不能从当前仓库清单自动推断可重建。
4. 修正 MA 现有测试和契约漂移后再进行组合验收，保留原始失败证据。

## 后续清理证据（本清单之后新增）

- `NeuralCloud/docs/refactor/cleanup-safe-generated-20261116.md`：记录首批清理依据、保留证据与索引恢复方法。
- `NeuralCloud/docs/refactor/cleanup-safe-generated-20261116.json`：保存逐文件清理路径和哈希以支持核对与恢复。

- `NeuralCloud/tools/cleanup/remove-verified-pytest-output.ps1`：按固定清单校验路径和哈希后逐文件删除历史测试产物。
- `NeuralCloud/docs/refactor/cleanup-safe-generated-20261116-execution.json`：记录实际删除数量、目标残留核验和执行范围。
