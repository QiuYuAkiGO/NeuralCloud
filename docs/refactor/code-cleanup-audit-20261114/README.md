# 2026-11-14 清理审计证据

固定提交、范围与解释见[实施清单](../code-cleanup-implementation-checklist-20261114.md)。本目录不是清理实现或生产验收证据。

批次 0 补充：[全仓体量与边界报告](../batch-0-neuralcloud-size-and-boundaries-20261114.md)；`count_repository.py` 对固定 Git tree 统计，`repository-size.json` 保存完整逐文件 SHA、行数、分类与汇总。统计不含本轮新增文档和工具；分类表示代码位置/用途，不表示已经获得删除资格。

阅读实现口径：`count_implementation.py` / `implementation-size.json` 排除测试、文档、评测、验收/基准脚本、生成声明、注释/docstring/空行，保留业务实现和运行工具。结果为 518 文件 / 110,880 行；其中运行实现 99,549 行、启动与配套工具 11,331 行，新记忆服务和两个插件 2,094 行。

- `inventory.json`：两仓库当前 Python 包的物理行数、最长函数及全部异常捕获位置，由 Python AST 扫描。行数含空行/注释；不是圈复杂度。未对历史代码做全量人工审核。
- `fault-probe.mjs` / `fault-probe.json`：使用内存 Response 和临时 SQLite 复现错误混合；不调用 HTTP。后续错误收敛后输出应改变，不能把错误旧行为写成永久验收预期。
- `neuralcloud-pytest.txt` / `mistyai-pytest.txt`：各自 `.venv/bin/python -m pytest -q`，28 / 29 passed。
- `neuralcloud-plugin.txt`：NC `npm --prefix plugins/dsh-neuralcloud test`，4 passed。
- `mistyai-harness.txt`：MA `node compatibility/patch-qq.mjs` 后，设置 `NEURALCLOUD_PLUGIN` 为本轮 NC 插件路径，运行 `npm --prefix compatibility/harness test`，39 passed。
- `tcp-integration.txt`：MA 下 `../NeuralCloud/.venv/bin/python compatibility/run_services_test.py --neuralcloud-root ../NeuralCloud`，4 passed，真实外部动作数 0。
- `checks.json`：静态检查、契约检查和 wheel 内容清单。

环境：Python 3.12.14、Node 24.19.0，依赖按各自锁文件安装。CI 的 Node 为 24.15.0，当前本地结果不替代该版本 CI。默认沙箱的本地通信测试未正常完成，以上最终日志来自允许本地通信的重跑；旧 master 的探查结果不包含在此基线中。

复跑前：NC `uv sync --locked --extra dev --extra archive`；MA `uv sync --locked --extra test`；MA `compatibility/harness` 下 `npm ci --ignore-scripts --no-audit --no-fund`。两仓库应为清单注明的 SHA，重新安装 Node 依赖后须重新执行锁定 QQ 补丁。
