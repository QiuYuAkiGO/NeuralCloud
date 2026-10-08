# QQ 回复声称未连接服务器：诊断记录

日期：2026-10-08，时间说明使用北京时间。

结论：服务器只读连接可用；QQ 普通回复链路没有接入服务器能力状态与执行回执，回复规划阶段在缺少依据时先断言“没有服务器监控能力”，正文随后沿用该错误前提。上一轮验收覆盖 goal 到服务器的执行路径，没有覆盖 QQ 普通聊天到 goal/服务器的入口。

## 线上证据

- 09:02:04 用户询问“现在能看到你服务器上的情况了嘛”。09:02:28 已送达回复：“看不到的，我没有连到服务器那边，只能看到你发给我的话”。
- 09:03 检查 Runtime：服务器及自主能力开关均为 true，`server.observe.v1` 为 healthy / `server_ssh_available`。总体 degraded 源于另一项 `server.execute.v1` 尚未验证，不能解读为只读 SSH 断开。
- 服务器命令账本保留 08:41:44 goal 执行 `uptime` 的成功回执；本轮 QQ 对话没有发起新的服务器命令，也没有对应连接失败记录。
- 09:05:13 使用当前配置、主机公钥和独立 `misty-observe` 账号进行新的只读 SSH 探测，`uptime` 退出码为 0。此项是诊断探测，不计作 Misty 新的自主动作。

## 信息缺失与错误传播

1. 实际 `PrepareQQTurn` 命令包含 `qq_web_capabilities`；`ResumeQQResponse` 包含 `runtime_web_context`。两者均没有服务器能力上下文。
2. 实际正文调用的编译上下文及全部 8 条 prefix messages 中，均没有 `server.observe.v1`、`server.execute.v1`、服务器健康状态或此前 uptime 回执。
3. gate 的 `reply_plan.must_address` 已写入：“如实说明自己看不到服务器运行状态，只能通过对话感知；不编造监控或运维能力。”它是本轮模型生成的回应计划，不是 Runtime 返回的权限或连通性判断。
4. 正文沿用了这个计划；校验阶段返回 pass、零 violations，未检测出能力事实矛盾。

部署源码核对与当前工作区一致的结构性缺口：`harness/runtime/qq_pipeline.py` 注入 Web 能力并执行 Web 计划，`harness/cognitive/service.py` 转交 Web/目标续接上下文，`harness/qq/decision.py` 构造 Web/视觉相关输入；当前没有对等的服务器状态输入或普通 QQ 请求触发服务器计划的流程。`runtime_goal_context` 用于既有 goal 选择 QQ 表达后的续接，不能视为普通 QQ 提问自动创建/执行服务器 goal。

## 修复方向

应先把由 Runtime 提供的服务器能力摘要接入 QQ gate 与正文：明确已配置、已启用、最近验证时间、本轮是否有新回执、可查询的固定状态命令及删除/写入禁止。不得仅根据工具缺席或总体 degraded 推断“没有连接”。

若希望在 QQ 提问后实际查询服务器，还需增加受控执行入口：将允许的状态查询交给 Runtime/goal，取得本轮回执后再生成正文。继续使用现有独立账号、命令白名单、限额和 SSH 强制命令；不扩大群成员的操作授权，也不向群聊泄漏私有目标、凭据或服务器敏感内容。

回归至少覆盖：已启用但本轮未查询、查询成功、连接失败、只读 healthy/execute unverified，以及禁用删除。仅修改人格记忆或把拒绝句替换为“已连接”，无法修复实际工具入口缺失。

脱敏证据见 [diagnosis.json](../harness/evidence/qq-server-awareness-20261008/diagnosis.json)。本轮未修改运行配置、未重启服务、未代发 QQ 消息，也未部署修复。
