# 首发开发交接 · 2026-09-30

## 接手入口与当前状态

用户要求先 commit & push，回家后再继续开发。本次只保存当前实现、验证和交接，不继续扩展功能，不部署。下一次收到继续开发指令后，从本文的待办开始。

- 仓库：`C:\Projects\NeuralCloud`；分支：`codex/grounded-agency-rebuild-plan`。
- 远端：`https://github.com/QiuYuAkiGO/NeuralCloud.git`。
- 本批修改的父提交：`801990684b2ce6ee769477cc511efb917e7313de`，已包含首发范围、实施计划和验收清单。
- 本文与本批代码在同一提交中保存；可用 `git log -1 -- docs/refactor/production-launch-handoff-20260930.md` 找到交接提交。
- 当前是 **WP0 + WP1 的部分实现和基础设施**，不是工作包全部完成，也不是可上线版本。
- 没有运行真实 QQ、真实模型、远端服务器修改或线上切换。本地 TCP 测试使用合成身体夹具。

先阅读：[已确定首发范围](production-launch-scope-20260929.md)、[实施顺序](production-launch-implementation-20260929.md)、[验收用例](production-launch-acceptance-20260929.md)。这些文档是完整合同，下面只是接手摘要。

## 已确定的产品边界

- 首发仅 QQ 私聊 `2285027817`，暂停其他群聊/私聊，禁止旧写入端并行运行。
- 首发包括文本、图片理解、网页、文件接收/处理/回传及普通主动聊天；不包括语音、桌面控制、用户定时任务或 AstrBot 插件兼容。
- 服务器修改均需该账号通过 QQ 明确批准；删除要特别展示目标、范围和可恢复性。Web 不能成为批准旁路。
- 长任务先确认受理、后台执行、最后发送结果；受理不等于成功。普通聊天不应被后台任务占住。
- 自主活动/目标主要由 Web 管理；暂停自主活动仍保留普通聊天和用户任务。内部状态可通过 QQ 询问，但不主动暴露；普通主动聊天、批准请求、任务受理/结果属于已确定例外。
- 支持记忆查询、纠正和具体认识退出召回，保留审计。无效提案最多自动重新评估一次，然后在 Web 待处理；不自动重发不确定消息或重放服务器副作用。
- 首发不设金额上限；已有自主调用、前台并发、目标/步骤/无进展约束仍保留。试运行后至少观察七天再扩量或退役旧路径。
- 普通文本成功率目标 99%、试运行底线 95%，成功回复 p95 不超过 30 秒。错误、受理、等待、拒绝不冒充普通回复成功。

## 本批已实现

1. **私聊配置和动态授权**：Direct/Session 支持零群、有私聊的配置；启用时不能没有任何受众。接收、历史查询、队列处理、发送重新检查当前配置和会话范围；零群不查询群名单。
2. **耐久用户任务基础**：`harness/production/user_tasks.py` 提供 SQLite 任务/通知记录、来源事件幂等、建议队列上限 4、后台并发 1、900 秒超时。任务状态和交付状态分离，重启遇到执行中任务标为 uncertain，不自动重跑。Runtime heartbeat 驱动，独立后台执行。
3. **固定任务通知基础**：`harness/qq/task_notifications.py` 只支持任务受理/结果用途，复用 Direct actor 和发送账本。发布时核对 Runtime 中的真实任务/通知，最终发送再核对当前授权；私聊历史证明交付，丢 ACK 不自动重发，有回执可只读恢复核对。通知不占普通聊天冷却。
4. **自检准确性**：普通回复必须对应同一事件、完成决策和实际交付；日记必须有本次指定 entry_id 的 committed 记录，不能凭当天已有日记或历史计数判成功。
5. **首发指标工具**：`tools/production_acceptance/launch_metrics.py` 使用冻结请求 ID/分母，保留缺失、受理、错误、部分交付等失败；计划内等待/拒绝/工具单列，按 nearest-rank 计算 p95，保存输入哈希，不覆盖旧报告。
6. **发布双仓检查**：CI 要求完整的 MistyAI 生产提交 SHA，不允许跨仓合同/进程门禁静默跳过；校验双仓干净提交、生产合同与锁文件哈希。`contracts/production-body-schema-lock.json` 独立锁定七个生产 schema，保留旧研究夹具。
7. **依赖和静态基线**：修复此前 21 项 Ruff 问题；补齐 uv.lock 中 pyproject 已声明但缺失的 jsonschema 依赖。CI 用 uv 0.12.10 和 `uv sync --locked`。

**接线限制**：`UserTaskRunner.register/accept` 目前是内部入口，默认没有注册任何真实业务 handler，也没有自然语言转交路径。测试里的合成 handler 成功不代表网页/文件/服务器用户任务已可用。批准用途、持久等待批准/恢复、Web 任务详情和通知身体用量记账尚未完成；不能把本批称为完整长任务功能。

## 验证结果与未解决失败

隔离环境：`.tmp/launch-venv`，Python 3.12，dev/gui/qq extras。没有修改用户原有 `.venv`。

2026-09-30 收尾重新执行：

- Ruff：`harness tests gui eval infra tools/ci tools/production_acceptance/launch_metrics.py tools/selfcheck/probes_production.py` 全部通过。
- `tests/test_launch_foundation.py tests/test_production_user_tasks.py tests/test_release_pair.py tests/test_launch_metrics.py`：**59 passed, 1 skipped**，5.86 秒。跳过项需要显式设置 `MISTYAI_PRODUCTION_ROOT`。
- `tests/test_cross_process_mistyai.py` 对本机 `127.0.0.1:64610` 合成服务执行：**2 passed**，2.28 秒；覆盖生产快照、rest 幂等、记账鉴权与幂等及原有动作适配。
- 先前显式设置本地身体生产目录执行 release-pair 与身体适配测试：**17 passed**；只证明本地候选合同匹配，不证明已发布提交有效。
- 完整回归使用短目录 `.tmp/lf2`，`pytest -q -x --tb=short`：**2877 passed, 17 skipped, 1 failed**，耗时 45913.05 秒；遇到首个失败停止，因此未跑完整套件。
- 失败：`tests/test_production_memory_migration.py::test_benchmark_uses_actual_production_messages_and_receipts_on_copy`，在 `tools/benchmark_production_memory.py:119` 抛出 `RuntimeError("benchmark_did_not_reach_real_model_boundary")`。根因尚未定位，不能宣称基线问题或本批已通过全部回归。
- 完整输出留在本地 `.tmp/launch-pytest.log`，不纳入 Git。此前长临时路径导致 Windows FileNotFoundError；短路径重跑对应 `tests/body_belief/test_runner_interventions.py` 已通过 5 项。后续 Windows 测试继续使用短 `--basetemp`，不要重用仍运行测试的目录。
- 两项已见依赖弃用警告来自 Starlette/httpx 和 anyio BlockingPortal。

新增/修改测试覆盖事件幂等、配置撤销、任务与普通聊天并行、并发领取、伪造通知用途、发送前撤权、已知回执恢复、丢 ACK 不重发、取消尚未执行的 claim、存储错误隔离，以及指标分母/协议检查。

## 发布阻塞与下一步

1. **先处理上述完整回归失败**：单独复现记忆迁移基准，判定是否因 Runtime 新接线、环境或已有代码导致；修复后做相关回归，再决定是否续跑全量，避免重复启动十余小时的测试。
2. **复核 WP1 生命周期边界**：`accept` 已校验 QQ 来源及当前配置，但还需检查全局暂停/关闭竞态是否应显式要求 supervisor RUNNING。`verify_sent` 私聊分支原有固定 history count 20，可能未使用恢复传入的 history_count；先核对再改。CI 静态检查尚未列入新的 launch_metrics 文件（本地已经检查）。
3. **完成真实用户任务接线**：设计可信的 handler 注册和普通回合到任务的移交；接收确认必须发生在持久化后。需要补任务查询/详情、取消/恢复定义、通知记账及真实通路验收，不能开放任意模型标签执行任务。
4. **推进 WP2**：所有服务器修改走固定方案、QQ 唯一批准、删除特别提示、持久批准状态和超时。服务通知不能直接接受任意审批正文；需绑定真实、不可变的方案和批准身份。
5. **随后按计划推进 WP3/WP4/WP5/WP6**：记忆管理/自主暂停、网页图片文件、部署与备份、模型筛选，最后联合验收与单写切换。默认值仍是实施建议，不新增用户承诺。
6. **MistyAI 发布配对仍阻塞**：本地 `C:\Projects\MistyAI-embodied-production` 的生产改动未提交，当前基线 HEAD 为 `309c59a771528f20de431f32c14faded4bfd3eb3`。本批只读取/测试该候选，没有修改或代为提交其源码。需由对应工作完成真实生产提交并推送，再配置 GitHub 变量 `MISTYAI_PRODUCTION_REVISION` 为完整 40 位 SHA，确认私库读取凭据 `MISTYAI_REPO_TOKEN`。当前不能宣称远端 CI 绿色；未配置时新门禁会按设计失败。
7. **回滚仍待设计/验收**：旧代码对新增任务/发送记录的兼容不能假定成立，属于 WP5。不得因内部测试通过直接部署或重放 uncertain 副作用。

## 工作区与资源收尾

- 本地合成 uvicorn 服务本次验证后停止，不需保留常驻进程；下次跨进程验证需重新启动夹具。
- `.tmp/launch-body-venv` 是身体独立测试环境；`.tmp/launch-body.log` 与 `.tmp/launch-body-state1` 是合成测试证据，不是线上数据。
- 预先存在的 `.mtest*`、`.mrecover*`、`.midlefence*`、`.pytest-tmp-*`、`.tmp-cache-*`、`.tmp-ga04-*` 和 `data/` 保留、不提交、不删除。不要 `git add .` 把它们纳入。
- 不需要读取 API key 文件、远端登录文件或真实运行凭据来继续本批本地验证。
- 本交接写入仓库文档，供后续模型直接读取；没有改动用户全局记忆。
