# 中文角色回复人工评审

独立研究工具，固定 20 个模型和两个项目合成场景，计划 40 次客户端生成请求。
不是生产角色流水线，不会发送 QQ 消息、写角色记忆、调用辅助模型或模型裁判。

## 本轮约束

- 每个模型两个场景各一次，总量硬上限 42；错误、超时、截断及中断后不确定的请求都占槽位。
- 发送前 SQLite 原子占位；HTTP 零重试、不跟随重定向，恢复运行不重新发送已占用槽位。
- 使用根目录 `apiyi-api-key.txt`，不从环境变量自动回退。凭据与研究结果已加入 Git 忽略规则。
- `preflight` 只 GET 模型目录，`dry-run` 完全离线。目录收录不等于已证明生成权限或后端身份。
- 只有 `run --live` 发送正式生成请求。不要新建运行目录来绕过本轮次数限制。
- 各模型优先关闭思考；强制思考时选择最低支持档位，无法调节的模型保留必需默认模式并注明。
- 不设置金额上限。保存接口返回的 token 用量；没有可核实的费率/账单就不编造人民币费用。

默认输出：`data/evaluations/chinese-reply-review-20260916/`。

## 操作

在工程根目录运行：

```powershell
.venv\Scripts\python.exe -m eval.chinese_reply_review prepare
.venv\Scripts\python.exe -m eval.chinese_reply_review preflight
.venv\Scripts\python.exe -m eval.chinese_reply_review dry-run
.venv\Scripts\python.exe -m pytest -q eval/chinese_reply_review
.venv\Scripts\python.exe -m eval.chinese_reply_review run --live
.venv\Scripts\python.exe -m eval.chinese_reply_review serve
```

评分入口默认 `http://127.0.0.1:8795`。服务仅暴露白名单评分资源，不把工程根目录当作网站目录。
评分页只包含随机编号、场景、人格和正文，不发送型号、映射、思考内容、耗时或费用。
同一冻结样本包可恢复草稿；模型可能在正文中自报身份，评分者应记录该情况，不改写模型原文。

先阅读非测试校准例子，再对中文自然度、贴题接话、角色一致性、情绪分寸和总体喜欢程度分别打 1–5 分。
不因篇幅更长、情绪更强或更亲密就自动给高分；立场选择哪一边本身不决定质量。
两个首轮均没有对话历史。比赛场景没有“不要给建议”的额外要求。

页面提交会封存真实人工评分并生成揭盲报告。也可导出评分文件后离线导入：

```powershell
.venv\Scripts\python.exe -m eval.chinese_reply_review import-ratings --ratings <评分文件.json>
.venv\Scripts\python.exe -m eval.chinese_reply_review analyze
.venv\Scripts\python.exe -m eval.chinese_reply_review status
```

评分原件不能被覆盖；修订另存。技术失败不是中文质量低分。
没有人工评分时报告状态为等待人工审核，不伪造打分。
报告仅描述一位评审对两个场景中的实际样本偏好，不给总体中文能力排名、显著性或发布通过结论。

## 证据布局

- `manifest.json`：固定人格、场景、模型参数、来源及内容哈希。
- `private/requests.json`：预先冻结的实际请求正文和哈希，无鉴权头。
- `private/ledger.sqlite3`：权威次数账本；先占位后发送，未知请求永不自动释放。
- `private/results.jsonl`：可重建的结果导出，仅包含可见正文与白名单元数据。
- `private/preflight.json`：目录预检结果和本地 key 指纹，不含 key 文本。
- `review/`：匿名评分材料；映射与评分封存位于 `private/`，不静态暴露。

官方材料与人类偏好证据参见同目录 `candidate-research.md`。候选的历史口碑、官方自述与实际采样结果分开记录。
