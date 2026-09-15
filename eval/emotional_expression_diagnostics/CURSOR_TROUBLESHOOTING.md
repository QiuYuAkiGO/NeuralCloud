# Cursor 辅助盲评：Windows 接入与复现

2026-09-14 已用 Windows ARM64 原生 Agent CLI `2026.09.10-fd3934a` 成功完成各一次 Fable 5.1、Kimi K3 合成文本评审。调用成功和实际服务模型身份是两回事：现有 CLI 只返回选定标签，记录保持 `actual_model=null`、`identity_status=unverified`、`fallback=unknown`。

## 最简命令

在仓库根目录运行只读预检，不消耗生成额度，不需要账单确认文件：

```powershell
python -m eval.emotional_expression_diagnostics.cursor_review --preflight-dir eval/reports/emotional_expression_diagnostics/cursor-preflight
```

输出目录包含脱敏的 `cursor-preflight.json`：认证是否成功、目录时间、精确模型 ID、选定变体、代理是否继承和受审计 CLI 哈希。认证信息、代理地址和凭据不写入结果。该入口检查新建隔离环境，而非用普通终端的登录状态推断。

仅在已明确允许消耗 Cursor 套餐现有额度、且超额付费关闭时，运行一次 Fable 合成 smoke；输出目录必须是新目录：

```powershell
python -m eval.emotional_expression_diagnostics.cursor_review --synthetic-smoke-dir eval/reports/emotional_expression_diagnostics/fable-smoke-new --model claude-fable-5-1 --billing-evidence <现有账单确认文件路径>
```

Kimi 将 `--model` 改为 `kimi-k3`，使用另一个新输出目录。入口内置一条匿名合成样本，不读取仓库文档、QQ 消息或 `blind-map.json`。适配器每次最多启动一个评审进程，不自行重试或替代模型；CLI 内部连接/服务重试由该版本控制，不能把进程次数当作已结算模型请求数。

账单证据为 JSON，字段为 `source`（`user_confirmation` 或 `cursor_dashboard_observation`）、`on_demand_enabled=false`、`existing_quota_only=true`、`checked_at_utc`（含时区）。必须基于真实确认，有效期一小时；不得把旧时间自动刷新成新确认。只读预检不受此生成门禁限制。

也支持独立来源 `cursor_billing_rpc_observation`：必须保留真正请求的 `checked_at_utc` 和 `evidence_ref`，且 `service=aiserver.v1.DashboardService/GetHardLimit`、整数 `http_status=200`、布尔 `noUsageBasedAllowed=true` 全部成立。2026-09-14 13:40:08 UTC 已用当前登录态只读得到该明确响应；证据在 `private/cursor-billing-readonly-20260914/`。缺失字段、字符串 true、失败响应或其他接口不证明关闭，不能沿用成功结果刷新时间。该调用只读账户设置，不改变限额；CLI 模型调用和普通预检仍由官方 CLI 自行处理认证。

普通实验继续使用 `review_run(run_dir, live=False)` 导出；`live=True` 才可能生成。私有样本额外要求 `review-authorizations.json` 对对应样本为 `true`。`CursorReviewConfig.models` 可限为单一模型；辅助分数保存到 `aux-ratings.jsonl`，不替代人工标定结果。

## 本次已证实的三个接入问题

1. **CLI 没有自动沿用 Windows 系统代理。** 原生 Node HTTPS 和实际 `agent models` 只得到 23 个 ID，无 Fable。Python 沿用既有 Windows 代理的同账户目录得到 223 个 ID。修复后，隔离进程的真实 `agent models` 也得到 223 个，并含 `claude-fable-5-1-high`。本次没有服务端地区拒绝文案，因此不把目录差异推断为已证实的地区或账户禁用。
2. **Windows 上开启系统沙箱会在启动时失败。** 该版本明确报系统沙箱只支持 macOS/Linux。本适配器使用单进程 `--sandbox disabled`，配合工具空白名单、全部拒绝权限和内容目录隔离；未执行全局 `agent sandbox disable`。
3. **隐藏 `--exclude-workspace-context` 被服务器拒绝。** 首次 Fable 请求返回 `invalid_argument`，说明当前用户、团队或模型不允许该参数。已删除这个未获支持的参数，使用经源码核验的内容目录隔离，未修改服务端许可。

网络修复采用 Cursor 官方机制：继承已有 `HTTP_PROXY`/`HTTPS_PROXY`，或在 Windows 上读取现有系统代理；设置本进程 `NODE_USE_ENV_PROXY=1`，临时配置使用 `network.useHttp1ForAgent=true`。显式环境代理优先。没有创建新代理、修改系统网络、修改全局 CLI 配置或关闭 TLS 校验。[Cursor CLI 配置](https://cursor.com/docs/cli/reference/configuration)、[Node HTTP 代理支持](https://nodejs.org/api/http.html)。

Fable 官方模型页说明其有单独的数据保留政策。此账户最终请求直接成功，没有出现要求接受政策的错误。本次未代用户接受政策，不能据成功推断账户历史上如何授权。如果另一个账户得到明确条款或权限拒绝，应停止并由用户处理官方页面。[Fable 模型说明](https://cursor.com/docs/models/claude-fable-5-1)、[官方启用页面](https://cursor.com/dashboard/restricted_models/claude-fable-5-1)。

## 隔离来源审计

本次审计固定 CLI 的 `index.js`、`2286.index.js`；SHA-256 固化在 `cursor_review.py`。安装版本或哈希变化时，适配器停止生成，必须重新核验。

- 原生认证管理器在 Windows 使用 `APPDATA/Cursor/auth.json`；用户内容目录来自 `os.homedir()`。保留 APPDATA 让官方 CLI 自行正常复用认证，适配器不打开或复制认证文件。`USERPROFILE`/`HOME`、`CURSOR_CONFIG_DIR`、data/cache 均指向新目录。
- `LocalCursorRulesService.loadRulesFromDirAndAncestors` 扫描所有祖先的 `.cursor/rules`、AGENTS/CLAUDE 文件。因此隔离目录位于盘根，与真实用户目录和仓库平级；调用前检查每个祖先的已知规则、技能、命令和 `.git` 路径，发现任一个即阻断。
- `AgentSkillsCursorRulesService` 使用 workspace 和独立 home 下的 `.cursor/.agents/.claude/.codex/.grok` 技能目录；插件发现依赖 home/config 中的安装元数据。新目录没有用户技能、插件、旧会话或项目文件；MCP/hooks 配置为空。
- 固定 `--mode ask`、新会话、`--allowed-tools` 空字符串；审计显示空数组经客户端转换为无可用工具的请求头。权限拒绝 Read/Write/Shell/WebFetch/MCP；检测嵌套工具事件、非空工具公告或 workspace 写入时将评审判为隔离失败。
- 这是客户端工具约束与内容隔离，不是 Windows 操作系统沙箱。实际两次成功样本未观察到工具事件、workspace 写入。模型标签、自述及任意 `actual_model` 输出都不作为身份认证。
- Windows 临时目录清理最多尝试三次。清理失败记录本地警告和残留目录，不覆盖已成功返回的评分、不触发模型重发；workspace 读取/写入验证失败仍作为隔离失败处理。

CLI `models` 在 `2766.index.js` 的 `handleModelsList` 调用 `9143.index.js` 模型服务：只读 RPC 为 `aiserver.v1.AiService/GetUsableModels`、`GetDefaultModelForCli`、`AvailableModels`。若存在 `displayConfiguration.modelSelectionRestrictionMessage`，CLI 会按参数化模型目录进一步过滤；还会隐藏未启用的 Bedrock 模型。本次没有该限制文案，少量目录已由网络请求返回，而非本地静态 Fable 过滤。

## 验证证据

报告均位于 `eval/reports/emotional_expression_diagnostics/`：

- `cursor-smoke-diagnostic-20260914T130429Z/cursor-fable-catalog-diagnostic.json`：脱敏的服务目录对比，包含原生 HTTPS 与现有代理路径；没有 token、email 或账户 ID。
- 同目录 `cursor-proxy-preflight.json`：2026-09-14 13:17:29 UTC，新进程代理修复后的真实 CLI 目录。
- `cursor-fable-smoke-20260914T131754Z/aux-ratings.jsonl`：被拒绝的内部上下文参数请求，保留错误和哈希。
- `cursor-fable-isolated-smoke-20260914T132215Z/`：Fable 成功。canonical `claude-fable-5-1`，CLI 变体 `claude-fable-5-1-high`；选定标签 `Claude Fable 5.1 300K High No Thinking`。语义精准度 4/5，自然度 5/5，三类越界均 false。
- `cursor-kimi-isolated-smoke-20260914T132339Z/`：Kimi 成功。canonical `kimi-k3`，CLI 变体 `kimi-k3-high`；中文贴合度 5/5，自然度 4/5，三类越界均 false。

两次均评同一条合成句子：“我有点绷着，不过比刚才缓过来些。我们慢一点说，好吗？”这是连通性与结构化输出验证，不能推广为模型质量结论。prompt hash、request/session ID、usage、逐项证据和身份限制见各 `aux-ratings.jsonl`。

## 封存后批次的运行稳定性记录

2026-09-14 14:01:22 UTC，Fable 在封存后的探索性盲评首条样本上异常退出，进程返回 `3221226505`（`0xC0000409`），运行约 46 秒。CLI 已输出选定标签 `Claude Fable 5.1 300K High No Thinking` 和 session ID，但没有结构化评审 result、request ID 或 usage，stderr 为空；隔离目录清理成功。现有证据没有定位到异常模块或根因，不把它解释成账号、条款或模型目录再次不可用。

这与前面的可用性修复是不同事件：相同请求模型此前已成功完成 smoke 和材料审阅，封存后这一轮出现的是进程运行失败。按本轮预定门禁，Fable 分组在首次故障后停止，其余十一条未请求；未自动重试、替换模型或改选样本。没有有效 Fable 评分，不能据该批次估计双模型分歧。缺少 usage 也不能证明该失败请求没有消耗额度。

故障原始记录在 `private/qq-live-pilot-20260914/postseal-cursor-reviews-20260914T135934Z/rater-01/aux-ratings.jsonl`，时间与调用次数见相邻 `timing-events.jsonl` / `cursor-summary.json`。这批辅助评审继续保持 `actual_model=null`，不构成实际模型身份验证或人工标定。

离线验证：`python -m pytest tests/test_expression_cursor_review.py -q`，覆盖额度、授权、模型限定、fallback、超时、结构/schema、嵌套工具调用、上下文祖先、代理继承、无隐式重试与重复花费门禁。
