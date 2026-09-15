# Misty 历史回复 Kimi K3 评估

本目录保留2026-09-15报告的导出、调用、校验和渲染流程。生产服务只读；评分使用官方中国区 `kimi-k3`，密钥只由 `MOONSHOT_API_KEY` 进入调用进程。

## 本次产物

- [完整报告](C:/Projects/NeuralCloud/eval/reports/misty-humanization-20260915.md)
- [可筛选HTML](C:/Projects/NeuralCloud/eval/reports/misty-humanization-20260915.html)
- [逐条评分](C:/Projects/NeuralCloud/data/evaluations/misty-humanization-20260915/judgments.json)
- [独立核验](C:/Projects/NeuralCloud/data/evaluations/misty-humanization-20260915/independent_verification.json)

## 模块

- `export_history.py`：只读SQLite/JSON/JSONL，按稳定ID合并正式状态和Linux API增量，区分模型归属及交付状态；写UTF-8/LF语料与实际文件SHA256。
- `fetch_linux_history.py`：从本机LinuxDeployment/access.json读取管理凭证，分页只读调用GUI API，不导出凭证。`--since` 与 `--output` 可配置。
- `prepare_judgments.py`：脱敏、封存逐条输入、量表和结构化Kimi请求。初次禁止覆盖；`--append` 仅添加新来源ID，`--refresh-context` 明确提交变化语境的复评并记录supersession。
- `kimi_worker.py`：只执行run-dir/jobs中的`kimi-k3`任务；保存final content、usage、HTTP/模型身份，不保存内部推理文本。run-dir/STOP存在时完成队列后退出。
- `prepare_quote_repairs.py`：原评审引文不连续逐字时，另调用Kimi摘取正确片段。保持原评分、理由和响应不变。
- `analyze_judgments.py`：验证封存输入、模型、完成状态、评分、ID覆盖及引文，显式应用复评和quote repair，生成统计与证据。
- `prepare_synthesis.py` / `review_synthesis.py`：本次跨历史复审及来源核对。
- `build_markdown_report.py` / `build_report_view.py`：本次报告文字、Canvas与HTML。

跨历史复审的范围文字、R0014/R0018编辑修订和Markdown结论针对本次固定语料。新评估可复用导出、调用和统计流程；需要重新审阅定性报告及改写，不能把本次按ID绑定的编辑规则套到新语料。

## 复核现有报告（不调用模型）

在 `C:\Projects\NeuralCloud` 中运行：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
.venv\Scripts\python.exe eval\misty_humanization\analyze_judgments.py --run-dir data\evaluations\misty-humanization-20260915
.venv\Scripts\python.exe eval\misty_humanization\review_synthesis.py --run-dir data\evaluations\misty-humanization-20260915
.venv\Scripts\python.exe eval\misty_humanization\build_markdown_report.py
.venv\Scripts\python.exe eval\misty_humanization\build_report_view.py
```

## 新一轮数据导出与调用

使用一个新的私有run-dir，保留本次封存文件。先核对新快照路径和增量起点，再运行类似：

```powershell
.venv\Scripts\python.exe eval\misty_humanization\fetch_linux_history.py --since '<快照与增量重叠起点UTC>' --output '<新run-dir>\linux-api'
.venv\Scripts\python.exe eval\misty_humanization\export_history.py --state-root '<正式快照>\data\state' --state-root '<新run-dir>\linux-api' --output-dir '<新run-dir>'
.venv\Scripts\python.exe eval\misty_humanization\prepare_judgments.py --run-dir '<新run-dir>'
```

在单独终端启动worker；它会等待后续复评/引文修复任务。安全输入密钥，不将真实值写进脚本或命令历史：

```powershell
$taskKimiSecret = Read-Host 'Kimi API key' -AsSecureString
$env:MOONSHOT_API_KEY = [System.Net.NetworkCredential]::new('', $taskKimiSecret).Password
try {
    .venv\Scripts\python.exe eval\misty_humanization\kimi_worker.py --run-dir '<新run-dir>'
} finally {
    Remove-Item Env:MOONSHOT_API_KEY -ErrorAction SilentlyContinue
}
```

结果齐全后运行引文核验及analyze；有缺失/失败时先检查原响应，不把HTTP成功或已有文件当作完整评分。完成全部任务后，在该run-dir写入一个名为`STOP`的普通文件让worker退出。

## 证据口径

`request_sha256`是历史worker字段名，其实际含义是**磁盘job JSON字节哈希**，不是HTTP传输字节哈希；统计证据中已称`job_sha256`。重试前的失败调用可能已计费，worker只保留末次usage；analyzer用unknown_previous_attempts标明未知尝试。本次该值为0。

主分母仅包含明确模型归属、且有交付回执的QQ整轮/已交付部分。网页运维、看板独白、交付未确认和归属不明分别保留。完整五维才能进入综合分的聚合；null维度不填默认值。缺少回执不等于未发送，缺少可见历史不等于模型撒谎。

原始会话、管理API快照、冻结请求和原始模型响应位于Git忽略的 `data/evaluations/misty-humanization-*/`。报告呈现的评分与Kimi输出一致；人为事实校正仅发生在明确标注的改写建议中。
