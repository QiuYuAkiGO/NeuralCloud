# 每日运行报告

共享 Runtime 按北京时间自然日聚合一份运行报告，供 GUI `/report` 回看。报告不替代 hash-chained journal；monitor 投影默认只保留 7 天，归档 JSON 默认保留 365 天。

## 配置

可选环境变量 `NC_DAILY_REPORT_CONFIG` 指向绝对路径 JSON：

```json
{
  "timezone": "Asia/Shanghai",
  "finalize_time": "04:00",
  "retention_days": 365,
  "qq_trace_path": "/var/lib/neuralcloud/qq/direct.trace.jsonl",
  "qq_ledger_path": "/var/lib/neuralcloud/qq/watcher.sqlite3"
}
```

未配置时使用上述默认时区与 04:00 归档（晚于每日修订 03:00）。若存在 `NC_QQ_DIRECT_CONFIG` / `NC_QQ_WATCHER_CONFIG`，会尝试从中读取 trace 与 watcher ledger 路径。不新增 `deploy.json` 绑定。

心跳到点后把前一自然日写入 `runtime/reports/YYYY-MM-DD.json`。也可 `POST /v1/report/daily:finalize` 手动归档。

## 接口

认证后的 Runtime 控制面：

- `GET /v1/report/daily?day=YYYY-MM-DD`：已归档则返回归档，否则实时聚合且 `finalized=false`
- `GET /v1/report/days`
- `POST /v1/report/daily:finalize` body `{"day":"YYYY-MM-DD"}`

GUI：`http://127.0.0.1:8790/report`，代理 `/api/report/*`。

## 字段

| 章节 | 含义 |
| --- | --- |
| `uptime` | 各 lifecycle 时长、采样缺口、当前连续运行、重启估计 |
| `resources` | 各进程角色 CPU/RSS 均值峰值、主机占用、状态目录体积。`psutil` 缺失时 `available=false` |
| `homeostasis` | 七维起止/极值/净变化、salience、主导需要、episode 数、core affect 日均 |
| `intent` | marked 触发、能力机会、Board、IDLE、QQ idle/发送 |
| `pipelines` | fast/deep 占比、latency p50/p95/p99、分阶段耗时、事件类型 |
| `memory_revision` | 当日每日修订 jobs、usage、diff 条数 |
| `tokens` | monitor / QQ trace / 日修订分来源；命中率 = hit/(hit+miss)；`estimated_usd` 为估算 |
| `diary.wrote` | 是否主动写了日记，仅 true/false |
| `extras` | warning/error、动作状态、watchdog、ingest_dropped、goals |

## 日记

日记默认 `autonomous_enabled=false`。可选 `NC_DIARY_CONFIG`：

```json
{
  "autonomous_enabled": false,
  "window_local": "21:00-02:00",
  "max_entries_per_day": 1,
  "quiet_seconds": 60,
  "autonomous_model_seconds": 12
}
```

`GET /v1/diary` 返回是否写了、条数、暂停状态，不含正文。正文需要 `development_debug` 的 `GET /v1/diary/entries`。

## 未覆盖

- 每日 episodic/semantic/candidate 新增数与 `memory_ops_log` CONSOLIDATE
- 情绪导演输出分布、语音/Web 身体独立耗时
- QQ watcher LLM 错误重试细分
- 主机磁盘 IO / 模型端点网络延迟
- `DailyRevisionWorker._scheduled_day` 持久化
