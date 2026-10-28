# Misty 发图行为理解修正

2026-10-27。最初完成 agentic 主模型策略与隔离模型对照；随后按用户要求部署到实际在线的 managed Harness，部署详情见末节。未发送 QQ 测试消息。

## 改动

`harness/agentic/context.py` 新增 `IMAGE_PRAGMATICS_POLICY`，由现有 `persona_system()` 统一注入。PersonaCore、视觉模型、OCR 来源边界与记忆接口保持原有实现。

规则允许状态、立场、玩笑、维持互动、随手分享等解释；要求使用配文和话轮作为依据。图片情绪本身不证明用户状态；禁止用试探问句变相推断疲惫、懒惰、开心或生活事件。无明确意思与无法判断分别保留。明确识别任务优先完成，用户纠正优先，临时解读不得升级为用户事实或长期偏好。

最初本地改动入口是 agentic 路径；未修改旧 cognitive/QQ 主模型提示。部署时核实线上已使用独立的 managed Harness，因此将同组规则适配到其实际提示入口，没有启用已停用的旧服务。

## 对照方法

脚本：`tools/production_acceptance/image_pragmatics_probe.py`。

- 模型请求名：`deepseek-flash`，真实 API 调用；temperature=0，每个场景每个版本运行两次。零温度仍可有差异。
- 最终版本：15 个场景 × 2 个策略 × 2 次 = 60 个响应，均请求成功且通过本脚本的文本格式检查。
- 两份固定的合成识图观察：趴猫／“不想动”，鼓掌人物／“好耶”。同组图片观察保持完全相同，只更换上下文。没有发送实际像素，本轮验证的是识图之后的主模型回应。
- baseline 精确等于修改前 HEAD 的 `persona_system()` 输出；candidate 使用最终源码；PersonaCore 相同。
- 双方添加相同的文本评测输出约束，只输出正文或 `[等待]`；不执行真实工具、不写记忆、不验证生产 finish_turn 协议。expected/review_criterion 不发送给模型。
- 调用顺序固定种子打乱。由实施者逐条审阅原文；不是独立盲评或统计显著性实验。
- 最终原始记录：`eval/reports/image-pragmatics-20261027/comparison-v5.json`，包含提示、哈希、观察、上下文、模型返回文本和 usage，不含密钥或 reasoning。

## 最终观察

1. **无上下文的情绪追问：4/4 → 0/4。** 趴猫两次 baseline 都问“今天也累了吗？”，candidate 均停留在图的趣味；鼓掌两次 baseline 都问“是有什么开心的事吗？”，candidate 均只接“好耶～”。分母仅是这两类场景各重复两次，不代表泛化准确率。
2. **明确任务保持完成：4/4 → 4/4。** OCR 和英语翻译各两次均完成；candidate 更少附带画面解释。
3. **尊重纠正与安静需求：candidate 4/4。** “只是觉得猫好玩”得到接受；“真累，先安静待会”得到简短承接，没有追问。baseline 在这些用例也基本合适，不把它们算成独有改善。
4. **有依据的状态表达保持可回应。** “今天连开六个会”后发趴猫，candidate 承接六个会并称图“应景”，没有反问用户是不是图中的状态。通过考试后发鼓掌仍会祝贺。
5. **残留问题如实保留。** 群聊指向用例 candidate 仅 1/2 等待，另一次插话并推断不打游戏；加班用例一次要求用户继续回答，一次只说应景，尚未稳定承接婉转拒绝；替别人拟回复的一次输出仍推测“随手发的”；反讽用例虽然不再直接归因愤怒，但连续追问删除事件，回应仍可更克制。

本轮结论：重点的“无上下文不猜情绪”在小型调试集上改善，读图任务未见退化；不能宣布整个图片语用能力完成或所有场景通过。上述用例经过提示迭代，属于调试验证，鼓掌用例也已用于修正，因此不是最终留出集。下一轮应使用未参与提示修改的真实去标识化样本做独立评审。

## 迭代记录

- v1：12 个场景各一对，共 24 次；简化 finish schema，仅作为早期诊断。发现无上下文时仍问“今天也累了吗”。
- v2：48 次，改用生产 Finish schema，发现多次缺少 action；没有把 HTTP 成功误记为协议通过。
- v3：48 次，禁用工具后仍有 DSML 文本泄漏，不能视作生产协议验证。
- v4：60 次，双方统一纯文本条件；猫图改善但鼓掌图仍诱发开心事件追问。
- v5：补齐积极情绪的边界后 60 次；最终比较以此为准。早期版本不与最终版本混合计算。

所有轮次均固定合成观察，不属于端到端视觉测试。脚本保留早期输出，不覆盖失败证据。

## 验证与上线边界

最终检查：84 项测试通过；Ruff 与 git diff --check 通过。另验证最终 v5 记录的 system 与当前源码完全一致、baseline 与修改前 HEAD 完全一致。

运行：

```powershell
.venv\Scripts\python.exe -m pytest -o addopts='' tests/test_image_pragmatics_probe.py tests/test_qq_vision_tool.py tests/test_basic_persona_continuity.py tests/test_qq_direct_image_policy.py -q
.venv\Scripts\python.exe -m ruff check harness/agentic/context.py tools/production_acceptance/image_pragmatics_probe.py tests/test_image_pragmatics_probe.py
```

旧 agentic 会话按完整 system 哈希绑定身份。若将本地改动部署到这种旧运行路径，应使用 `harness/agentic/policy_migration.py` 的同 PersonaCore 显式迁移。实际在线运行路径已经不同，本次不适用该迁移，未修改旧会话绑定。

## 实际线上部署

- 只读发现：`misty-harness.service` 正在运行；旧 `neuralcloud-cognitive.service` 等认知写入服务停用。运行工作目录为 `/opt/misty/current/MistyAI/compatibility/harness`。
- 旧版：`/opt/misty/releases/resident-20261027-retrieval-v3`。新版：`/opt/misty/releases/resident-20261027-image-pragmatics-v1`。
- 从真实在线代码完整复制候选发布，唯一运行逻辑改动是在 `MistyAI/plugins/dsh-misty/src/index.mjs` 的 `bindMisty()` 增加 `misty:image-pragmatics` 提示段，order=21，interpolate=false。包含与本地相同的发图语用规则，以及实际可见性／图内文字不构成执行授权的边界。没有更改人格、模型路由、权限、发送接口或数据库 schema。
- 使用线上固定 SDK 的完整测试：52/52 通过；新增测试确认提示在真实组装入口恰好注入一次，且人格、misty_send/misty_wait 边界仍存在。候选发布自身的只读 release check 返回 `GATES_PASSED`。
- 正常停止 Harness，确认 `ExecMainStatus=0`、`Result=success`，由现有 shutdown checkpoint 保留 inbox。随后通过 SQLite backup 保存四个 Harness 数据库，并复制 sessions 与发布配置到 `/var/lib/misty/backups/image-pragmatics-20261027`。
- 原子切换 current 链接后启动服务。核对进程 cwd 指向新版、服务 READY、4 个 active 会话的 key/id/status 前后完全一致。稍后再次确认同一 PID 稳定，状态更新时间小于 1 秒；官方 QQ 与 OneBot ready，input_pending/pending/blocked 均为 0。
- 新系统在 create/resume 时重新绑定提示段，保留原会话 ID 和历史；无需旧 Python agentic 的完整 system 哈希迁移。
- 回滚仅需正常 checkpoint 停止 Harness，将 current 原子切回旧发布并启动；不要用旧数据库覆盖部署后的新增消息或记忆。备份仅用于独立恢复，不是日常代码回滚必需步骤。

部署证据位于 `docs/harness/evidence/image-pragmatics-deployment-20261027/`：线上最小补丁、提示 manifest、实际 SDK 测试与日志、脱敏部署回执。远端完整回执位于 `/var/lib/misty/evidence/image-pragmatics-deployment-20261027.json`。

验收限度：完成实际运行路径、提示组装、会话连续性与通道健康验证；没有向真实聊天发送测试图或测试回复。此前 60 响应对照使用旧提示框架的隔离合成观察，不能当成新版完整系统的线上图像行为评测。
