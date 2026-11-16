# 生产源码补齐交接（2026-11-16）

状态：已完成本轮实际运行源码及配套文件恢复；不是新部署，也不是清理删除验收。

## 来源与边界

- NeuralCloud 分支：`cleanup/resident-memory-only`，以 `b375016b` 清理交接为基线。
- MistyAI 分支：`codex/restore-production-source-20261116`，以 `37e0935` 为基线；使用独立 worktree，保留原 main 工作区未提交修改。
- 通过用户指定的本地登录文件进行密码认证，禁用 SSH key/agent。凭据、运行配置、数据库、消息、模型权重、node_modules 和备份源码未加入提交。
- 逐文件来源和服务器原始 SHA-256 见同目录 `production-source-sync-20261116.json`。`lf_sha256` 用于 Git 换行规范化后的复核，`base_git_content_sha256` 对应本轮基线 Git blob。清单包含校验后无需变更的文件。
- NC 校验 75 个文件；MA 校验 104 个文件。各仓库分别维护其源码，不将 MA 代码复制进 NC。

## 实际运行链

- `neuralcloud-memory` / `neuralcloud-evidence`：`/opt/misty/releases/resident-20261028-memory-workflow-v4/NeuralCloud`，进程 PYTHONPATH 指向该目录的 `src`。
- `misty-harness`：`/opt/misty/releases/resident-unified-compression-20261104/MistyAI/compatibility/harness/resident/launch-paused-napcat.mjs`；包含 memory-stack、continuity、context-budget、session-maintenance、basic-tools、mobile/group 和 Android 适配。
- `misty-state`：`/opt/misty/releases/resident-20261027-image-pragmatics-v1/MistyAI`。state/integration 源码来自这个实际版本，而不是 Harness 副本。
- `misty-chat`：`/opt/misty/releases/resident-open-contact-20261103-4d3c9b0c9a57/mobile-server`，四个 Python 文件映射回 MA `services/chat/`；媒体 worker 从 `/opt/misty-chat/media/worker.py` 映射到 `services/chat/media/worker.py`。
- `neuralcloud-semantic` 运行 `neuralcloud.vector_index`，独立环境 `/opt/misty/semantic/.venv`；本轮恢复 NC 中的源码，不复制该环境与模型。
- memory workflow 为 enabled；配置中的 NC 插件、memory-stack manifest 和当前 Harness package-lock 哈希均符合实际文件。manifest 列出的 283 个文件全部匹配；该旧清单不覆盖所有后续 Harness 增量，本轮另行记录实际源码哈希。
- 旧 Runtime、Cognitive、Homeostasis、GUI、QQ Direct、Board 均 inactive/disabled。恢复配方和额外 writer 尚未完成逐文件排除，因此不形成删除许可。

## 补齐内容

NC 恢复检索、语义索引、证据重建、校准、工作流 JSON、API/契约、记忆插件、测试、管理脚本和构建依赖。此前本地缺失的 8 个 Python 模块均已补回，4 个不同的核心文件按现场版本恢复。

MA 恢复实际消费者及其源码依赖、锁文件、vendored OneBot 包、手机服务、状态代码和已有测试。线上保存的临时备份 `.before-*` / `.basic-attempt*` 不作为主源码引入。恢复文件保持现场字节，不通过更改业务代码或测试预期来隐藏验收差异。

## 本地验证

环境：Windows，Python 3.12（uv 隔离工具环境），Node 24.15.0。Python 测试使用显式依赖环境，未把它描述为整套生产锁文件复现。

- NC Python：80 passed；记忆插件：7 passed；Ruff 通过；27 个导出契约检查通过。
- MA Python：29 passed / 1 failed。`test_retention.py::test_v1_requires_explicit_retention_migration` 仍期望 schema 2，现场维护代码升级到 schema 3。
- MA Harness：57 passed / 5 failed。失败位置：capabilities 的内容读取断言和 Android mock 上下文、context-budget 的失败摘要归档计数、continuity 的错误码预期、misty 的未审查工具错误断言。未对全部失败归因，不能声称只是陈旧测试或生产功能通过。
- MA 导出契约检查发现 `TextRange.v1.schema.json` drift；保留现场契约，不自动重生成覆盖证据。
- NC staged diff whitespace 检查通过；MA 两个现场文件（failed-drive、market-android）保留末尾空行，staged diff --check 报告该格式问题；恢复文件扫描未发现登录密码、私钥块或常见 GitHub/OpenAI token 格式。扫描不是完整安全审计。
- 未执行真实模型、QQ/手机发送、生产数据库迁移或服务重启；未执行 TCP 组合验收和完整 wheel/生产依赖环境构建。

Harness 本地安装步骤（在 MA 根目录）：

```sh
npm ci --prefix compatibility/harness --ignore-scripts --no-audit --no-fund
npm ci --prefix compatibility/harness/resident/basic-tools --legacy-peer-deps --ignore-scripts --no-audit --no-fund
node compatibility/patch-qq.mjs
# NEURALCLOUD_PLUGIN 指向本轮 NC 插件的绝对路径
npm --prefix compatibility/harness test
```

basic-tools 锁文件按 legacy peer 安装方式可复现；普通 npm ci 会报告 peer 条目缺失。手机测试还需 Pillow。现有 CI 的旧配对 SHA、安装步骤和格式/契约门禁尚未更新，本次保存的是有已知失败的现场源码基线，不能当作可直接发布版本。

## 后续

先基于本轮双仓源码核对失败语义、修正契约/测试/CI 配对，再补全传递依赖、回滚引用和文件级保留清单。继续保持不删除、不归档生产唯一源码；不得用本地工作区直接覆盖运行服务器。
