# Misty 表达内化研究：下一阶段参考记录

记录日期：2026-10-09。状态：已完成机制研究与隔离原型，未通过生产发布验收。研究期间线上版本为 `20261009-json-repair-v2`，表达内化开关保持 `off`，本研究没有真实 QQ 投递。

先阅读 [交互报告](report.html)，再按需要查看 [实验汇总](summary.json)、[原宿主重放](original-replay.json)、[原型重放](scoped-replay.json) 和 [验证记录](verification.json)。HTML 自包含，可从本地直接打开，不依赖先前的 localhost 服务。

## 已获得的结论

1. 内容与内部约束分成字段仍不足以隔离影响。固定原话、证据和候选，仅切换合理/偏移规划，原候选有 14/48 对最终放行或拦截结果翻转。移除规划后，48 对完全相同的模型请求仍有 1 对翻转，保留为模型服务的残余波动。
2. 此次单句提示修补没有解决问题。偏移规划下，正常回复被拦截从原候选的 11/24 变为 12/24；不能据此推广为所有提示词调整无效。
3. 正文需要有用的内容规划。64 次同检查点回放中，保留规划组流程完成 32/32，移除组为 31/32；移除组出现空正文，并在配色用例中重新提及无关 OCR 缺口。流程完成不是自然度或事实质量证明。
4. 校验输入应直接呈现原话与证据。单独移除规划会漏检真正漏答；显式呈现原话后，又暴露宿主不接受工具补充证据引用的问题。
5. 隔离原型保留用户触发行绑定，允许同一受众已准入的工具/记忆证据补充说明。对显式呈现原话的 48 份相同首份报告，有效判定正确从 43/48 变为 46/48，校验未完成从 3 次变为 0 次。仍有一个情绪主语反转用例在两次重复中被放行。

推荐方向是保留正文的内容规划，校验独立依据原话和证据，并同步修正宿主引用约定。不要直接移除全部规划，不要把内部状态、工具失败或不确定字段自动变成正文义务。

## 实验口径与边界

- 主试验：24 条固定候选 × 2 种规划 × 2 次重复 × 3 种输入配置，共 288 次。
- 原话呈现后续对照：24 条候选 × 2 种规划 × 2 种配置，共 96 次。它是在主试验后追加的开发对照，不是独立留出集。
- 正文回放：32 个捕获检查点 × 2 种输入配置，共 64 次。工具选择和返回已冻结，没有重新测真实识图。
- 宿主规则重放：384 份相同首份模型报告，在原宿主和隔离原型各检查一次，共 768 次本地校验，无新增模型调用。此口径不包含模型重试，与完整运行的最终判定分开统计。
- `semantic_review_unavailable` 是校验未完成。保护性拦截错误回复不能计为正确校验；未评分样本保留在原定分母中。
- 场景与标签由实施者预先编写，重复和正反例相关。没有独立人工盲评，不能宣称自然度提升百分比、生产故障率、统计显著性或生产 P95 达标。
- 试验使用冻结的 v6 开发候选，与后来更新的线上源码存在差异。归档源码只用于复现，不是待部署 release。

## Git 中保留的内容

- `summary.json`：三组实验的冻结清单、场景、逐条结果、正文与模型报告，以及分项统计。
- `original-replay.json` / `scoped-replay.json`：全部 384 份首报告的双版本校验结果及边界检查。
- `scoped-support.patch`：宿主原型的唯一源码差异；生产代码未应用此补丁。
- `replay-inputs.json`：384 次校验对应的原始输入，按内容去重为 96 项；只含本轮构造场景及其证据，没有运行凭据。
- `frozen-runtime-v6.tar.gz`：冻结清单中的 397 个源码/资源文件。只包含 `harness`、`infra`、`gui`、`presets`、`contracts` 下清单指定的文件，不含 `.git`、虚拟环境、运行数据或登录文件。
- `archive-manifest.json`：上述证据文件及冻结源码的 SHA-256。目录内 `.gitattributes` 禁止证据文件换行转换，保持跨平台校验一致。
- `verification.json`：原实验输入、评测器、397 个源码文件的校验，原始 HTTP 归档的本地位置与 SHA-256，及研究时的测试结果。
- `../../../../eval/reply_source_authority/` 与相关测试：隔离干预、输入投影、分析、报告生成、宿主原型和离线重放工具。

完整 HTTP 请求/响应归档和正文检查点仍保留在 `data/source-authority-20261009/`，没有纳入本次 Git 提交。Git 中的汇总与最小输入足以重放宿主校验及重建报告；重新执行全部模型调用、审计原始 HTTP 或重建正文检查点仍需要原始归档和单独配置的模型访问。

## 从 Git 离线复核

在已有项目 Python 开发环境中，从仓库根目录运行。以下 PowerShell 命令不访问模型、不发送 QQ；输出目录必须是新目录。先执行恢复工具，它会核对归档与全部源文件的 SHA-256。

```powershell
$record = 'docs/harness/evidence/reply-source-authority-20261009'
$replay = 'data/source-authority-from-git'
python -m eval.reply_source_authority.restore_record --record $record --output "$replay/original"
python -m eval.reply_source_authority.report_replay --source "$replay/original" --summary "$record/summary.json" --inputs-bundle "$record/replay-inputs.json" --output "$replay/original-replay.json"
python -m eval.reply_source_authority.scoped_support --source "$replay/original" --target "$replay/scoped"
python -m eval.reply_source_authority.report_replay --source "$replay/scoped" --summary "$record/summary.json" --inputs-bundle "$record/replay-inputs.json" --output "$replay/scoped-replay.json"
python -m eval.reply_source_authority.report --summary "$record/summary.json" --verification "$record/verification.json" --output "$replay/report.html"
```

预期：原宿主 384 份报告中有效判定正确 285 份、未完成 67 份；原型正确 336 份、未完成 0 份。后者仍包含错误判断，不能视作全量通过。两份重放结果应分别与 Git 中对应 JSON 完全一致；不要用新结果覆盖历史记录。

项目测试：`python -m pytest -q tests/test_reply_source_authority.py tests/test_reply_uncertainty_research.py`。研究阶段记录为 10 项通过；归档时增加输入完整性测试，以本次提交的运行结果为准。

## 下一阶段先解决什么

1. 在当前线上源码基线重新合并并验证任务视图与引用约定，保留作用域、授权、缓存版本及恢复幂等约束。
2. 专门覆盖说话者、被指对象、否定与因果，解决“这不是你的错”被回应成“你别自责”的原意反转；不通过修改情绪计算掩盖问题。
3. 保留正文规划对相关性的帮助，进一步试验按当前问题选择信息，避免把未知角色、换装、OCR、记忆缺口扩散成全局免责声明。
4. 执行冻结的 24 组 × 3 轮 × 2 次完整对照，补真实图片工具链、失败恢复及最近八轮送达历史；开展匿名人工成对盲评。
5. 通过离线发布门槛后，再按已有授权会话进行 `observe` / `enforce`。事实越界、必要限定遗漏或原意新增回归时不启用候选。不要用现有裁判的通过标签直接训练偏好模型。

## 论文依据与前一阶段

- [Sufficient Context，ICLR 2025](https://arxiv.org/abs/2411.06037)：按当前问题判断证据是否足够，区分信息不足与没有利用已有信息。
- [CRITIC，ICLR 2024](https://arxiv.org/abs/2305.11738)：借用外部反馈与真实执行证据的思路，没有复现其多轮工具纠错。
- [Large Language Models Cannot Self-Correct Reasoning Yet，ICLR 2024](https://arxiv.org/abs/2310.01798)：注意外部反馈、正确答案泄漏和调用预算对纠错实验的影响，不推广为所有自检无效。
- [Faithfully Express Intrinsic Uncertainty，EMNLP 2024](https://aclanthology.org/2024.emnlp-main.443/)：同时检查过度保留和保留不足；本研究没有估计内部置信或复现校准算法。
- [上一轮论文与实验报告](../reply-uncertainty-research-20261009/report.html) 与 [初始实现、兼容和验收方案](../../QQ_REPLY_INTERNALIZATION.md)。
