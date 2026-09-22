# Misty 全量自检

由 AI 全自动执行的调用路径自检：外部脚本核对各条路径是否真正触发并留下证据，Runtime 心跳则做被动健康投影。不注入 debug 事件，不向 QQ 群发送测试消息，不停止 8766。

## 边界

- 生产 `control_mode` 是 `study_runtime`：禁止 `/v1/debug/events`、禁止读取日记正文、禁止伪造 `QQ_TURN` / `CAPABILITY_OPPORTUNITY`。
- `/v1/qq/decisions` 绑定 watcher ledger；生产 QQ 联网只能由真实群消息触发。隔离实例用合成 `QQ_TURN` 或 `prepare_web_context(channel="qq")` 覆盖接线。
- 隔离实例必须显式 `NC_CONTROL_MODE=development_debug` 与 `NC_ISOLATED_DEVELOPMENT=1`。共享服务不可用时报告 `unavailable`，不做进程内脑回退。
- QQ 发送账本 `qq_watch_sends` 在 Tier0/Tier1 前后计数必须一致。

## 分层

| 层 | 执行者 | 做什么 |
|----|--------|--------|
| Tier0 | `tools/self_check.py` 只读 | 端口、health、catalog、近窗口 journal/sqlite 证据、QQ watcher 心跳、最近真实 QQ 回合联网采证 |
| Tier1 | 脚本在隔离 owner 上跑 | 真模型（若 GUI 为 live）或脚本化认知：chat、chat+web、QQ web、日记、Goal、内稳态、记忆提交 |
| Tier2 | 脚本对生产自动触发 | web / 日记邀请 / Goal；每项每日最多一次；Goal 结束即 cancel；QQ 只被动采证 |
| Runtime | `SelfCheckController` | 心跳后台被动探针，写 journal 与 `state_dir/self-check/latest.json`，GUI 只读展示 |

Cursor Agent 通过 `.cursor/skills/misty-selfcheck/SKILL.md` 一键跑脚本、读报告、按 triage 表继续排查。可用 `/loop` 定时。不要用 Cursor Automation（云端访问不到本机 127.0.0.1）。

## 状态

`pass` · `fail` · `skipped(disabled)` · `skipped(not_configured)` · `skipped(already_ran_today)` · `skipped(quota_guard)` · `unavailable` · `no_recent_sample`

任一 `fail` 或 `unavailable` 使脚本退出码为 1。`no_recent_sample` 不是失败：生产上最近窗口没有真实 QQ 联网样本时如实记录。

## 路径矩阵

| 路径 | 触发 | 证据 | 层 |
|------|------|------|----|
| 基础设施 | 端口 + `/v1/health` | lifecycle=RUNNING，8770/8771/8772/8790 可达 | T0, Runtime |
| 聊天回复 | `POST /v1/messages` 或隔离 `submit_user_message` | `processing_completed`，`decision_kind=reply` | T1, T2 |
| 聊天联网 | 明确要求查资料的消息 | journal `web_request_completed`，`web/calls.sqlite3` round completed | T1, T2 |
| QQ 联网 | 隔离 `prepare_web_context(channel=qq)`；生产被动采证最近 `qq_turn` | `runtime_web_context.sources` 或 journal 关联 `web_request_completed` | T0/T2 被动, T1 合成 |
| 日记 | `POST /v1/diary/invite`，`request_id=selfcheck:<day>` | `GET /v1/diary` today_count 增加或 turns committed | T1, T2 |
| Goal | `POST /v1/goals` 观察类小目标 | journal `production_goal_submitted`，结束后 cancel | T1, T2 |
| 能力/Board/QQ 身体 | catalog + 近窗口 `capability-calls.sqlite3`；Board 在线时委托 `verify_capabilities.py --mode board` | owners/allowed/ready；机会行 | T0, T1 Board |
| Homeostasis | snapshot + heartbeat | `updated_at` 新鲜；隔离心跳后 revision 前进 | T0, T1 |
| 情绪 | cognitive/affect 声明 | 未配置 aux 时 `skipped(not_configured)` | T0, T1 |
| Voice/Server/桌面 | `/v1/voice` `/v1/server/status` catalog | 未启用 `skipped(disabled)` | T0 |
| QQ 通道 | 8766 监听、watcher 心跳年龄、发送计数快照 | 只读 | T0 |

`expression.diary.write.v1` 尚未接入共享 catalog；自主日记默认 `autonomous_enabled=false`。自检走用户邀请路径，并把该事实写入报告。

## Tier2 安全阈值

- 每条主动路径每日最多 1 次，ledger 在 `logs/self-check/ledger.json`。
- 联网：`/v1/web/status.usage.requests` 必须低于日限额 80%。
- Goal：`finally` 中 `POST /v1/goals/{id}/cancel`。
- 日记：复用 `DiaryController` 日限额与幂等 `request_id`。
- 零 QQ 发送。`--no-active` 关闭全部生产主动触发。

## 命令

```powershell
.\.venv\Scripts\python.exe tools\self_check.py --tier all
.\.venv\Scripts\python.exe tools\self_check.py --tier 0
.\.venv\Scripts\python.exe tools\self_check.py --tier 2 --no-active
```

报告写到 `logs/self-check/<UTC时间戳>/report.json` 与 `report.md`。

Runtime 内置：`NC_SELF_CHECK_CONFIG` 指向 JSON（`enabled`、`interval_seconds` 默认 3600、`evidence_window_hours` 默认 24、`retention_days`）。`GET /v1/selfcheck`、`POST /v1/selfcheck:run` 需要 DIAGNOSTIC（`study_runtime` 可用）。GUI：`/selfcheck`。

## Triage

| 失败路径 | 下一步 |
|----------|--------|
| 端口/health unavailable | 不要动 8766。按 `launch.json` shared profile 用 `start.ps1` 启动，等 `/v1/health` |
| 聊天无 reply | `GET /v1/monitor/journal?record_type=processing_completed`；看 Cognitive 8771 |
| 联网无 `web_request_completed` | `GET /v1/web/status`；只读 `web/calls.sqlite3`；确认 conversation_enabled |
| QQ 联网 no_recent_sample | 等真实群消息；隔离层应仍能跑 `prepare_web_context(channel=qq)` |
| 日记 409/未 committed | `GET /v1/diary`；`diary-turns.sqlite3` |
| Goal 未出现 | `GET /v1/goals`；journal `production_goal_submitted` |
| 能力 catalog 空 | `GET /v1/capabilities`；manifest `autonomous_capabilities` |
| 内稳态不新鲜 | 8772 `/v1/health`；homeostasis snapshot `updated_at` |
| QQ 发送计数变化 | 立即停止自检；核对 `qq_watch_sends`；自检不得发消息 |

## 清理

- 取消自检创建的 Goal；日记原文保留不删。
- 隔离临时目录用完删除；生产人格、内稳态、GUI 配置指纹必须不变。

## 首次 dry-run

2026-09-17 对本机 `launch.json` 的 shared profile 执行：

```powershell
.\.venv\Scripts\python.exe tools\self_check.py --tier all
```

当时 8770 / 8771 / 8772 / 8790 / 8766 均未监听。退出码 1。报告目录：`logs/self-check/20260917T142335Z/`。

| 状态 | 数量 | 含义 |
|------|------|------|
| `unavailable` | 29 | 共享 Runtime/Cognitive/Homeostasis/GUI 未在线，HTTP 与 journal 探针无法取证 |
| `skipped(not_configured)` | 4 | QQ watcher 文件不存在；GUI `mode=demo` 故 Tier1 live owner 跳过 |
| `pass` | 1 | `isolated.safety`：未改生产指纹、未产生 QQ 发送 |

判定在这次 dry-run 中修正过：

- Runtime 连不上时，journal 与 emotion 记 `unavailable`，不再写成 `no_recent_sample` 或 `skipped(not_configured)`。
- 缺少 `capability-calls.sqlite3` 记 `skipped(not_configured)`。
- QQ 联网隔离探针必须留下约 30 秒 web 窗口（总截止 ≥60s）。20 秒截止会被 `prepare_web_context` 跳过搜索。

隔离接线由 `tests/test_self_check_tool.py` 的脚本化认知覆盖：chat、chat+web、qq.web、日记、Goal、内稳态、记忆提交均为 `pass`。生产在线后再跑一次 `--tier all` 才能采到真实 journal / sqlite 样本。

`expression.diary.write.v1` 仍未接入 catalog；自主日记默认关闭。自检只走用户邀请路径。
