---
name: misty-selfcheck
description: Run Misty's full path self-check (diary, Goal, chat/QQ web search, capabilities) with tools/self_check.py, then triage failures from the JSON report. Use when the user asks to 自检, self-check, verify Misty call paths, or loop a health check.
---

# Misty 全量自检

对本地共享 Misty 跑调用路径自检。不要停止 8766（QQ Agent）。不要向 QQ 群发测试消息。不要调用 `/v1/debug/events`。

## 步骤

1. 端口：`8770` Runtime、`8771` Cognitive、`8772` Homeostasis、`8790` GUI。若未监听，用仓库根目录 `start.ps1` 按 `launch.json` 的 shared profile 启动，轮询 `GET http://127.0.0.1:8770/v1/health`（Bearer 来自 `NC_CONTROL_TOKEN_FILE`）。绝不为腾 GUI 端口去停 8766。
2. 运行：

```powershell
.\.venv\Scripts\python.exe tools\self_check.py --tier all
```

只读：`--tier 0`。关闭生产主动触发：`--tier 2 --no-active`。
3. 读输出中的 `folder`，打开 `report.json`。对每个 `fail` / `unavailable` 按 `docs/SELF_CHECK.md` 的 triage 表继续查 `/v1/monitor/journal`、只读 sqlite、`logs/`。
4. 用中文表汇报：路径、层、状态、证据、下一步。`no_recent_sample` 不是失败。

## 禁止

- 停止或重启 8766 去“腾端口”
- 往 QQ 群发自检消息
- 注入 heartbeat / `CAPABILITY_OPPORTUNITY` / `QQ_TURN` 到生产
- 在共享服务不可用时回退到进程内脑

定时：把本 skill 与 `/loop` 组合，例如每 60 分钟跑 `--tier 0`。
