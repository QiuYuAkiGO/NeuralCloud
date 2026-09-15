# QQ 发布后交付审计

`postrelease_audit.py` 是离线读取工具，不是运行时组件。将此单文件复制到服务器的审计目录，使用已部署版本的 Python；`--release-root` 明确指定该版本源码。不要为了运行审计修改生产源码。

```sh
/opt/neuralcloud/current/.venv/bin/python -I /var/lib/neuralcloud/audits/postrelease_audit.py \
  --release-root /opt/neuralcloud/current \
  --database /ACTUAL/QQ/OBSERVATION.sqlite3 \
  --since '2026-09-15T00:00:00+00:00' \
  --prompt-version qq-group-turn-taking-v15 \
  --prompt-version qq-private-turn-taking-v4 \
  --output /var/lib/neuralcloud/audits/qq-brief-first50-UNIQUE.json
```

运行前替换数据库路径、实际激活时间和唯一输出文件名；输出目录须已存在。版本号使用此次发布的真实群聊/私聊版本。可以重复传入 `--group-id` 缩小会话范围。

## 统计口径

- 默认收集指定版本在激活后首次实际送达的前 50 轮，按首条已送达消息时间排序。同一决定的多个分段计一轮，未发送草稿不计。
- 单轮首段在激活之前的跨界回复不纳入本次 cohort。完整回执、部分前缀、已送达全部分段但缺完整回执分别列出，不能相互代替。
- 复用部署版本的冻结决定、回执哈希、来源范围、分段顺序和完整回执一致性检查。失败记录进入 `issues`，不计入交付数。
- 数据库仅以 SQLite `mode=ro`、`query_only`、事务快照读取；默认最多检查 1,000 个候选决定、10 秒。可设置 `--max-candidates`（最多 10,000）与 `--max-seconds`（最多 30）。

## 输出与退出码

stdout 只输出汇总计数。私有 JSON 文件保留实际交付正文、正文 SHA256、回执与证据引用、完整性问题及风格指标；常见凭证格式会被遮盖，风格指标仍根据原始正文计算。风格指标只是可检查的字符串规则，不能代替人工判断事实、语气或首句是否答题。

每轮还提供 `current_request`（冻结决定中的 `effective_query`，脱敏后最多 2,000 字）、其原字段 SHA256、脱敏与截断标志、`query_source`、`addressing`、`new_observation_record_ids` 和 `reply_plan.intent/detail_level`，用于复核首句是否答题及普通回复篇幅。计划缺失时 `reply_plan_available=false`，默认 `intent=other`、`detail_level=brief`。SHA256 对应冻结的 effective_query，不代表额外读取了完整原始消息；已有 `derived_query_truncated` 或本次长度裁剪都会令 `current_request_truncated=true`，不能把截断请求当作完整语义证据。

`verified_turns < 50` 表示尚不足 50 轮，不能声称完成首批验证。`first_cohort_complete` 只有达到目标数且没有完整性问题时为 true。输出文件采用独占创建，已有文件不会覆盖；每次运行使用新的文件名。

- 退出 0：读取成功，可能仍在等待更多真实交付。
- 退出 2：完整性检查失败或工具未能完成；stdout/stderr 不打印正文、凭证或数据库原始异常。

当前生产版本尚未发布时不要把已有历史决策或模拟评测当作发布后的交付。这个工具不发消息、不调用模型、不修改生产状态。
