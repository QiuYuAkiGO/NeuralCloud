# 目标与心理内稳态调节

`/goals` 是现有共享 Misty 的目标控制页。本地对话、留言板和 QQ 沿用同一认知与记忆；页面通过 GUI 服务端代理访问已有 Runtime，不创建第二个认知实例，不把控制 token 交给浏览器。

首版关注心理内稳态代理与表达：用户给出目标，Misty 结合当前状态和可用能力安排步骤、等待或调整。持续方向是保持可持续的参与；一次调节尝试完成不等于状态已经改善。电力、充电站和物理补能不属于本轮范围。

## 启用共享运行时

目标调节默认关闭。生产配置 `presets/runtime/shared-misty-regulation-v1.json` 在现有共享人格、记忆和心理状态上启用 `goal_regulation`，并将留言板与 QQ 交给共享能力执行器。使用已配置好的真实模型、渠道凭据及 `data/shared-misty-launch.json` 启动：

```powershell
.\start.ps1 -Mode live -ManifestPath presets/runtime/shared-misty-regulation-v1.json
```

此配置允许已经接通且通过健康检查的留言板读写和 QQ 读写；QQ 继续遵守已有会话与发送权限。桌面能力保持关闭，只有另外完成身体绑定和授权后才能加入。生产目标调节要求启用共享能力，不与研究专用的 grounded-agency 目标模式混用。

配置默认每小时最多进行 12 次目标评估，两次重评至少间隔 60 秒，连续 3 次无新进展后等待新依据。这里统计的是目标决策评估；各渠道现有的表达生成、执行期限与冷却仍独立适用。活动默认每 300 秒复查，状态需要在目标区间保持至少 180 秒才可记录稳定。修改配置后需重启相应服务；普通页面刷新不会使新配置生效。

单次目标决策给模型最多 25 秒，完整能力机会仍沿用 60 秒期限。模型在活动创建前超时或拒绝也记入无进展次数；重启不会清空次数或小时预算。每次步骤重新读取能力目录；只有普通心跳排队时仍可提出一次目标评估，用户输入与实际事件继续优先。运行时兼容服务启动时的平铺快照及心跳回执中的包装快照。180 秒稳定窗口要求连续新鲜观测，超过 90 秒的观察缺口重新计时。

2026-09-12 修复了两处真实决策阻塞：DeepSeek v4 的两次返回将 4096 token 全部用于内部推理，未生成 JSON；一次 QQ 目录在约 25 秒模型调用结束前已经过期。目标决策现在只对官方 DeepSeek v4 flash/pro 按次传入 `reasoning_effort=low`，保留思考模式、4096 token 上限及原期限。提案根据模型调用前的目录快照校验，真正执行及桌面最终执行守卫仍重新获取并检查可用性、有效期、权限绑定与目标观察。

模型不能用 `wait/error` 或心理快照证明步骤完成，未执行的必要步骤会在认知阶段明确拒绝提前完成；Runtime 继续独立复核。Schema 拒绝日志只保存脱敏字段位置和错误类型，目标结果仅投影允许的固定错误码。真实只读模型对照的两个提案都通过解析，本次耗时为默认推理 19.234 秒、低推理 9.406 秒；这两个诊断调用未执行身体动作，也不证明长期稳定或调节有效。见 [模型对照](milestones/evidence/goal-repair-model-probe-2026-09-12.json) 和 [维护部署](milestones/evidence/goal-repair-deployment-2026-09-12.json)。原 8 小时观察未重启，维护产生的失败样本与断档继续保留。

## 使用界面

从对话页进入“目标与调节”，或在现有 GUI 地址后打开 `/goals`。

- 填写目标和逐行完成标准，提交后查看当前目标、步骤与结果证据。
- 用户目标可暂停、恢复或取消；常驻的心理平衡方向只提供暂停和恢复。操作都通过 Runtime 记录，已发生的行动、未知结果和证据继续保留；取消不会撤销已经发生的外部行为。
- 持续方向、当前调节、等待原因、下次复查与每小时目标评估预算单独显示。
- 页面每 5 秒只读刷新一次，后台页每 15 秒刷新。刷新页面本身不创建机会或执行身体动作。
- 功能未启用或 Runtime 不可达时停用写控制，并显示当前状态。失败或超时后，保留相同输入重试沿用原 `request_id`；页面不自动重发写请求。

## 事实、自评和漂移

复查显示行动前后的维度数值，并将来源分为三类：

- **事实证据**：独立观察、回读、真实交付回执或已验证关联回应。证据引用以纯文本显示，不执行其中的 HTML、脚本或链接。
- **自我评价**：模型对变化的描述及 appraisal 引用。只有自评时明确标记，不据此推断客观改善。
- **时间漂移**：内稳态随时间向基线变化的部分；不能把它全部归因于写板、发送或等待。

维度数值增加不总是改善，例如认知负荷过高时下降才可能接近目标区间。因此页面同时显示 Runtime 复查结论：改善、无变化、恶化、稳定、仅自评或待确认。情绪标签和这些数值是功能代理，不是主观感受存在的证明。

Board 回读证明表达已经保存；它本身不证明舒缓。QQ 真实发送证明尝试联系，关联回应证明发生了互动，不能自动等同于支持或被理解。桌面操作成功也不自动证明心理需要得到满足。

新目标模式下，QQ 关联回应不沿用旧模式的固定社交恢复分值。事件解释和自我报告均记录类型并按服务端低权重处理；中性自评后的自然恢复不会标成“仅有自评”。动作结束后仍可继续复查效果，允许记录已尝试但没有改善。

## GUI 与 Runtime 接口

GUI 使用现有 `RuntimeGateway` 的服务端 bearer 认证，仅代理以下接口；跨来源写请求被拒绝。

- `GET /api/goals` → `GET /v1/goals`。
- `POST /api/goals` → `POST /v1/goals`，输入 `{statement, success_criteria: [string], request_id}`。
- `POST /api/goals/{id}/{pause|resume|cancel}` → Runtime 同名路径，输入 `{request_id}`。目标标识由页面编码；不允许其他任意操作或路径。

快照包含 `enabled`、`standing_goal`、`goals`、`active_episode`、`episodes`、`evaluations`、`budget` 和 `next_review_at`。目标与调节记录使用 `id/status/statement`；步骤带完成标准、状态、调用关联和证据引用。复查包含 `before/after`、偏离、`outcome`、`sources.fact/self_report/drift` 和证据引用。页面不修改原始判断，也不将缺失字段补成成功。

## 验证边界

`tests/test_goal_regulation_gui.py` 使用模拟 Runtime 传输，核对服务端认证、请求标识保留、同源保护、动作白名单、Runtime 错误透传，以及不可信目标/证据的纯文本渲染和反馈来源分离。它验证控制界面与代理，不证明真实模型调节效果、QQ 无人辅助运行或长期稳定性。

正式验收需分别保留行动是否发生、复查是否完成、指标是否变化和变化归因；等待、无变化、负向反馈、未知结果、用户抢占及重启恢复不能从报告中删去。真实通道与真实模型样本必须另行记录。

## 对照评估与在线观察

离线成对评估使用真实 `HomeostasisEngine`、SQLite 持久化及 `ApplyAppraisal`；启用、关闭两侧冻结相同初始状态、profile 和外部事件序列，只改变是否接收脚本化调节活动与 appraisal。运行到一个新的输出目录：

```powershell
.\.venv\Scripts\python.exe -m eval.regulation.run_eval --out-dir eval/reports/regulation-engine-01
```

默认样例包含合成压力、一次模拟表达、中性自评、重复调用和等待；中性自评不预设表达带来收益。`--scenario` 可读自定义合成 JSON，`--profile` 可选择已有 profile。报告独立列出按真实引擎状态计算的区间偏离积分、首次回到区间的时间、活动/等待/重复尝试及实际重复状态效果，并分开外部合成事实、自评、事件解读、心跳漂移和耦合。模型、回执和时钟明确标为模拟；即使设置负向 appraisal 后数值下降，也只能证明该输入经过引擎后的影响，不能证明真实模型调节有效。

在线工具只观察已经运行的 Runtime，不启动服务、触发动作、发送消息或注入心跳。先运行只读预检；令牌只从现有私有文件或 `NC_CONTROL_TOKEN` 读取：

```powershell
.\.venv\Scripts\python.exe tools/verify_regulation.py --token-file C:\absolute\private\control-token.txt --out-dir eval/reports/regulation-preflight-01
.\.venv\Scripts\python.exe tools/verify_regulation.py --mode monitor --token-file C:\absolute\private\control-token.txt --out-dir eval/reports/regulation-observation-01
```

`--runtime-url` 默认为已有配置或 `http://127.0.0.1:8770`。监控默认按不超过 30 秒的间隔连续观察真实 8 小时，仅 GET `/v1/goals` 与 `/v1/homeostasis/snapshot`。公共 `samples.jsonl` 和 `report.json` 只保存数值、状态、计数和标识哈希，不保存目标、聊天、自评正文、原始证据引用或凭据。

`--duration-seconds` 可用于短时诊断；少于 8 小时、假时钟、观测缺口、错误或功能停用都标为 `incomplete`。完成 8 小时但没有新增带证据引用的活动标为 `unverified`；观察到新增活动标为 `observed`，仍不等于证明调节有效。历史活动不计作本轮新增，监控退出或中断保存已采样结果。两个只读端点没有提供的事实/自评/漂移/耦合贡献量和无效重复副作用数量保留 `null`，不从总变化或成功次数臆算。

这些报告与正式无人辅助验收分开；`release_eligible` 始终为 `false`。查看真实效果仍需独立身体证据、模型选择记录和完整因果路径。
