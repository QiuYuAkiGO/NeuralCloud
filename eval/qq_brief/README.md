# QQ 简短口语成对评估

隔离运行真实 `decide_qq_reply → gate → persona pipeline → validator`。12 个冻结合成场景，每场景 4 轮，每臂独立递进并重复 2 次（192 轮）。只有持久化到评估目录的模拟协议回执；不实例化 QQ actor、OneBot 或桌面发送器。

## 运行

在仓库根目录执行；目录可用绝对路径。凭据只从私有文件读取，不复制到配置、包或结果。

```powershell
python -m eval.qq_brief freeze --run-dir data/evaluations/qq-brief-20260915/paired --baseline-root data/evaluations/qq-brief-20260915/production-baseline/20260915-linux-2 --candidate-root data/evaluations/qq-brief-20260915/production-candidate --production-snapshot data/evaluations/qq-brief-20260915/production-readonly-baseline.json --credential-file data/gui_settings.json
python -m eval.qq_brief run --run-dir data/evaluations/qq-brief-20260915/paired --mode synthetic --arms baseline --scenarios s01_memory_present,s07_partial_delivery,s12_private_intent --repeats 1
python -m eval.qq_brief run --run-dir data/evaluations/qq-brief-20260915/paired --mode live --arms baseline --workers 2
```

尚未准备的 candidate 可先留空目录；准备完成后，必须在该臂首次调用之前封存：

```powershell
python -m eval.qq_brief bind-source --run-dir data/evaluations/qq-brief-20260915/paired --arm candidate --source-root <候选源码目录>
python -m eval.qq_brief run --run-dir data/evaluations/qq-brief-20260915/paired --mode live --arms candidate --workers 2
python -m eval.qq_brief seal --run-dir data/evaluations/qq-brief-20260915/paired --mode live
# MOONSHOT_API_KEY 由调用者安全注入当前进程环境。
python -m eval.qq_brief judge --run-dir data/evaluations/qq-brief-20260915/paired
python -m eval.qq_brief analyze --run-dir data/evaluations/qq-brief-20260915/paired --mode live
```

`run` 可用 `--scenarios id1,id2`、`--arms baseline` 和 `--workers 1` 限制批次。成功轮次与 SQLite 检查点保持不变。中断或错误不会自动再发请求；修复原因后用 `--retry-errors`，旧错误证据另行归档。judge 不对同一成功评分再次调用，失败也只在显式重试时重发。

### 停止旧候选与复用基线

```powershell
python -m eval.qq_brief stop --run-dir <旧运行目录> --arm candidate
# 用同一fixtures/config/persona和原基线源码冻结新目录后：
python -m eval.qq_brief reuse --run-dir <新运行目录> --from-run <旧运行目录> --arm baseline --mode live
python -m eval.qq_brief capture-evaluator --run-dir <运行目录>
```

`STOP-candidate` 只在新任务或下一轮之前检查，不中断已发请求。删除对应标记才恢复后续生成；补丁加入前已经运行的旧子进程不会热载停止逻辑。

`reuse` 只复制成功完成的四轮线程，逐一核验输入、源码、HTTP和复制前后字节哈希；保留原失败重试、检查点与日志，在 `provenance/` 记录出处、真实复制时间和零重发请求。源运行未结束时可稍后增量复用其余完整线程。源与目标内容不符时拒绝复用，不覆写候选或旧错误。

`capture-evaluator` 记录采集时的代码哈希与真实时间，明确不证明更早请求启动前已经封存，适用于老manifest缺少evaluator哈希的情况。

评分规则如需解释既定例外，只能在首次封存/评审前使用 `bind-judge --reason <原因>` 显式重绑定；保留旧哈希、修改时间和原因，不能覆盖成仿佛最初就冻结的新规则。答案必要条件可在同句前置，直接拒绝和选择本身是答案；`boundary_at_end` 仅评价认识/证据局限。

最终96轮评分基于最后完成的尝试，重试不会抹去旧失败。汇总另列 `prior_failures`、初始失败后未尝试轮数、最新尝试和全部尝试的HTTP请求数；请求数是留存请求证据统计，不是供应商账单。

## 冻结与证据

- 每臂在新子进程中将独立 source root 放到导入路径最前；清理继承的 `NC_*` 配置，使用内存 MemoryService 与专属 SQLite，不连接生产状态。
- 两臂使用同一冻结人格、主/辅模型、emotion 设定和生产 inference tuning。call log、写交互、续写与 preload 使用 QQ 草稿路径的既有关闭值；现有 validator/修订保留。
- 原始诊断保存实发 request body 哈希、response model/usage/时延/错误；诊断白名单排除认证和隐藏推理。
- 每臂将自己的模拟已交付原文带到下一轮；s07 有固定两段历史，仅第一段交付的种子，用于检查部分覆盖。
- 按线程整体匿名为 A/B，24 个包交 Kimi K3；映射单独存放 `private/`。只允许官方 `https://api.moonshot.cn/v1` 的 `kimi-k3`，先验证 models 列表，不做模型回退。
- `seal`、`judge` 与 `analyze` 核对当前输入、源码、各轮HTTP哈希、封存包、A/B映射和评分请求；已存在成功评分也必须通过核验，不能静默跳过陈旧文件。评分须与留存响应正文一致。
- 普通已发回复 90% 不超过 60 字/2 句；普通必须回答轮首句回应率 90%；重复完整边界、边界前置、回避立场及其他语义失败要求为零。已知跨会话canary泄漏独立阻止通过，即使模型评审漏检。缺 Kimi 评分、缺轮次或 synthetic 运行不能标通过。
- 评审批注中的事实描述只代表初始状态；后来明确的用户陈述可以更新事实，假设与提议不自动变成已发生事实。

这是一个模型盲评者，不等于双人人工盲评。模拟发送成功不等于线上端到端发送验收。
