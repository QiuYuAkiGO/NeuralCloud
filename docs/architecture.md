# 架构与源码导航

本文基于当前源码静态阅读；入口为 [项目首页](../README.md)。

## 目录怎样分工

`src/neuralcloud` 是默认安装包。`pyproject.toml` 只从 `src` 查找 `neuralcloud*`，命令 `neuralcloud-memory` 指向 `neuralcloud.__main__:main`。

`plugins/dsh-neuralcloud` 是记忆服务的 JavaScript 客户端与工具适配层；`plugins/mobile-chat` 把手机会话接入 Harness。它们有独立的 package.json，不随 Python wheel 安装。

旧 `harness`、`gui`、`infra`、`presets`、根目录 `deploy` 及旧启动器已从本地删除。`tools` 保留新检索、契约生成、FRP/入口设施及独立实验和现行 MistyAI 配套工具。`tools/deploy` 与已删除的根目录 `deploy` 不同；受发布清单约束的旧契约仍保留。详见 [清理结果与边界](maintenance.md#本地旧运行时清理结果2026-11-17)。

`contracts` 保存生成的接口契约；`tests` 保存验证代码；`workspace_assets/eval` 保存评测代码和数据。`workspace_assets` 还集中存放本机 data、logs、traces、build、dist。根目录同名隐藏 junction 是兼容路径，不是数据副本；这是本机布局，并非新克隆自动具备的设施。

## 一条记录怎样写入

[api.py](../src/neuralcloud/api.py) 的 `/v1/ingest` 解析 [contracts.py](../src/neuralcloud/contracts.py) 的 SourceEvent 或 SourceEventV2，交给 [MemoryStore.ingest](../src/neuralcloud/storage.py)。存储先核对 character_id，再在事务内检查来源事件是否已经处理、写入正文与修订、建立来源关系和搜索索引，最后生成持久回执。

幂等键由 source_instance 与 event_id 组成；复用同一键而内容变化会被拒绝，不能用重复发送覆盖旧记录。更正必须通过 Correction 提供 record_id 和 expected_revision。更正和撤回会处理依赖该记录的派生内容；修改这一部分应连同来源关系和 changes 流一起阅读。

[ownership.py](../src/neuralcloud/ownership.py) 持有操作系统文件锁；[storage.py](../src/neuralcloud/storage.py) 使用 WAL、FULL 同步及进程内锁。普通启动要求数据库已初始化，角色、数据库版本和索引版本匹配；不会自动创建或迁移生产数据库。

## 一次查询怎样返回

未开启新检索时，HTTP 查询调用 MemoryStore.recall。`NC_RETRIEVAL_V2=1` 时，[入口](../src/neuralcloud/__main__.py) 构造 [Retriever](../src/neuralcloud/retrieval.py)，组合关键词、显式身份索引和可选语义服务。

[semantic.py](../src/neuralcloud/semantic.py) 只接受本机 HTTP 语义服务，检查令牌、响应大小、READY 状态和 database_id；它不负责自动下载或启动模型。[vector_index.py](../src/neuralcloud/vector_index.py)、[rebuild.py](../src/neuralcloud/rebuild.py) 与 [rebuild_batch.py](../src/neuralcloud/rebuild_batch.py) 是继续查索引实现的入口。

同时开启 `NC_MEMORY_WORKFLOW=1` 时，入口把 Retriever 包装为 [MemoryWorkflow](../src/neuralcloud/workflow.py)，并接入 [EvidenceIndex](../src/neuralcloud/evidence_index.py)。工作流根据问题、目标和约束选择检索方式，给出 DIRECT、INFERRED、CONFLICT、INCOMPLETE 等证据判断。规则在 [memory-v1.json](../src/neuralcloud/workflows/memory-v1.json)。未查到证据不等于已经证实不存在。

## 旧运行时已退役

本地旧多服务启动器和根 conftest.py 已删除；当前 Python 服务入口为 `neuralcloud-memory`。`tests/resident` 和记忆插件测试继续保留，旧运行时测试随实现移除。

仍被保留的历史样本和报告不是可执行的旧运行时。需要恢复旧源码时使用 Git 历史或本次清理的项目外备份。删除本地源码没有停止、更新或删除远端服务与冻结发布目录。
