# QQ 记忆主体约束热修复（2026-10-08）

冻结来源任务 `11b1e901744eaf04146aec99af8e3ef894dfad8227bfe565fac926e504e9f657` 只有一条消息、一个真实作者及一条独立证据。8 次尝试的 16 次物理响应均 HTTP 200，但第一条印象的 subject_refs 越过作者枚举：9 次第二个元素、7 次第一个元素。最终 skipped，未保存 proposal。历史响应未留存，不能断言错误主体具体是谁或是否缺失前缀。

代码增加当前窗口 allowed_subject_refs、independent_subject_refs 和 allowed_impression_kinds 提示。证据无法支持两位主体时排除 relationship；Schema 补齐关系最少两人、主体唯一、topic/group 空主体，与现有业务校验对齐。旧印象保留原内容，仅在模型上下文附加当前窗口可更新性和历史证据角色标记，不修改存储或来源。

现有一次 Schema 反馈明确要求删去无依据的印象、不得换人成立，并保持全部来源恰好归入 episodes；impressions=[] 合法。不裁剪、替换、自动接受模型结果；不增加调用或任务尝试上限，不改变 deadline。旧失败任务不自动重排或重放。

群聊、私聊、重试边界、记忆提交与结构化客户端共 197 项测试通过，Ruff 和 diff 检查通过。模拟模型验证单主体越界后可以在第二次生成保留经历而不形成印象；双主体有效关系仍可通过。测试不代表自然模型失败率已经下降。

生产发布以实际 current-evidence-v2 为底，只替换 harness/memory/qq_assimilation.py，保留其他发布修改。内稳态继续 shadow，24 小时计时以重启后持久化连续计数为准。

[脱敏诊断](../harness/evidence/subject-constraints-hotfix-20261008/diagnosis.json)。部署结果另见同目录 deployment.json。


## 经批准的空间清理

盘点显示历史备份约 203.5 GiB，离线评测约 33.3 GiB，系统 Ollama 模型约 42.4 GiB。用户明确批准删除列出的 7 个 10 月 5 日/6 日旧快照。在本次压缩停写备份逐文件校验完成后，仅删除获批的准确路径；保留 10 月 7 日备份及本次备份，未删除模型、评测或在线状态。

清理时可用空间从 8,247,205,888 字节升至 80,025,559,040 字节，实际增加约 66.85 GiB。清理列表及结果见 approved-cleanup.json，盘点见 space-audit.json。


## 上线结果

北京时间 22:39 完成 `20261008-subject-constraints-v1` 激活，六服务 active，Runtime RUNNING，无降级，QQ 连接正常。首批自然记忆生成 4 成功、0 错误，其中 3 个新任务 applied；skipped 保持 619。历史失败任务没有重排。

实际 manifest revision 5（沿用部署前配置）、profile qq-social-drive-v1@3、shadow 保持。配置和注意力策略文件部署前后核验一致。22:40 样本 continuous_seconds=68.317326，起点约北京时间 2026-10-08 22:39:13.544；最早次日同刻满足连续 24 小时，仍须依据持久化计数复核。

由于服务器空间不足以放置全量未压缩快照，采用包含 WAL 的停写原始文件压缩归档：29,672 文件逐项 SHA-256 与归档内容核对、归档前后源哈希一致、1157 个数据库 quick_check 通过。备份路径 `/var/backups/neuralcloud/20261008-before-subject-constraints`，4,547,485,850 字节，SHA-256 `076871c24808725cc4f61a28d382d6f5b067715f308d3b5a8716369fbedc83de`。恢复必须停止全部 writer，按 manifest 完整恢复数据库及配套 WAL；此为原始归档，非标准 export_snapshot 格式。
