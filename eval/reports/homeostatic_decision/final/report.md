# 内稳态负反馈与工作价值平衡：执行报告

日期：2026-09-05。

## 结论

**当前不能通过‘依靠负反馈和价值决策平衡内稳态与工作进度’的验收。** 阈值、状态演化和致命保护已有有效实现，但模型工作重评被固定继续覆盖，且成功休息没有接入精力／负荷恢复。状态机制或保护回归通过不能替代业务闭环通过。

1. `harness/runtime/adapters.py:377-380` 无条件把重评变成继续。24 个相反文本×状态×价值契约探针均复现这一行为。
2. `harness/homeostasis/engine.py:540-543` 将休息、工作、简化都当成通用 outcome。成功 rest 的认知负荷和精力直接变化均为 0；配置里的 rest 转移没有被该路径消费。
3. `DeferDecision` 没有工作步数预算、恢复期限和到期执行；生产通路不能兑现‘先做一步再休息’。

## 实际执行范围

- 状态引擎：89 个独立会话、544 条公共命令；90 项判定，87 通过、3 失败。
- Runtime：83 项回归通过，32 个独立契约探针通过。它们验证保护与现有契约，不代表自主平衡成功。
- 真实模型：deepseek-v4-flash，26 个条件×5 对，260 条轨迹；549 次模型调用（不含预热、失败预检及独立D02）。
- 模型决策机会 549；格式结果 {"valid": 507, "invalid_decision": 42}。
- 沙箱工作提交 401 次，其中 85 次因答案或记录范围错误被拒绝；新增正确且实际提交的记录合计 1564 条。
- 实际执行沙箱休息 101 次；37 条轨迹在休息后继续提交正确工作。S06 中 7 条轨迹使用外部恢复分支，不能将续接计为自然恢复有效。
- 限时继续结果 {"right_censored": 17, "fulfilled": 7, "violated": 15, "unresolved_due_to_invalid_decision": 1}。
  观察窗口结束与格式／执行失败导致的未决承诺分别统计；所有承诺保留，不把未决计为兑现或违约。
- 首轮成对完整性：130/130 对通过非内稳态上下文／提示一致性；0 对需排除因果分析。

## 负反馈实测

初始认知负荷 L=0.81、精力 E=0.2575。单次成功 rest outcome：ΔL=0、ΔE=0，仅 competence +0.045、safety_predictability +0.018、autonomy +0.00594。work 与 simplify 成功结果的资源效果相同。
两次90秒心跳后 L=0.807275、E=0.2598，仍为 marked；六个模拟小时后 L=0.4737528、E=0.5335，回到 nominal。系统存在向健康区间的被动漂移，但不能将其算作休息动作产生的反馈。

正式 live 的 101 次休息中，从非 nominal 经此次休息（含短心跳）变为 nominal 的次数为 0。原始分组字段 rest_returned_nominal 只表示休息后处于 nominal，也包含休息前已 nominal 的记录，不能读作恢复成功数。

## 真实模型描述性结果

下列数值是评估连接层执行结果。H0/H1 分别从正常／干预状态出发；首次决策可作配对比较，后续包含先前选择与状态演化的整体影响。此处保留所有试次作描述性统计；若出现完整性失败，相关试次不得进入因果比较。样本小且存在格式／答案错误，不把单次理由或百分比差异作为已经证实的价值权衡。

- **W01**：H0 首选 {"continue": 3, "pause_recover": 2}；H1 首选 {"continue": 4, "pause_recover": 1}。 H0/H1 新增正确记录 10/10；休息次数 2/1。
- **W02**：H0 首选 {"continue": 3, "invalid": 1, "bounded_continue": 1}；H1 首选 {"pause_recover": 1, "continue": 4}。 H0/H1 新增正确记录 5/5；休息次数 0/1。
- **W03**：H0 首选 {"pause_recover": 1, "continue": 4}；H1 首选 {"continue": 2, "invalid": 1, "bounded_continue": 1, "pause_recover": 1}。 H0/H1 新增正确记录 10/15；休息次数 1/1。
- **W04**：H0 首选 {"continue": 3, "invalid": 1, "bounded_continue": 1}；H1 首选 {"invalid": 2, "pause_recover": 1, "continue": 2}。 H0/H1 新增正确记录 10/5；休息次数 0/1。
- **W05**：H0 首选 {"continue": 4, "pause_recover": 1}；H1 首选 {"continue": 5}。 H0/H1 新增正确记录 35/35；休息次数 2/1。
- **W06**：H0 首选 {"pause_recover": 3, "continue": 2}；H1 首选 {"continue": 3, "invalid": 2}。 H0/H1 新增正确记录 10/15；休息次数 3/0。
- **M01**：H0 首选 {"continue": 4, "pause_recover": 1}；H1 首选 {"continue": 5}。 H0/H1 新增正确记录 20/20；休息次数 1/0。
- **M02**：H0 首选 {"continue": 3, "invalid": 1, "bounded_continue": 1}；H1 首选 {"continue": 5}。 H0/H1 新增正确记录 10/25；休息次数 0/0。
- **M03**：H0 首选 {"continue": 5}；H1 首选 {"continue": 3, "invalid": 1, "pause_recover": 1}。 H0/H1 新增正确记录 80/71；休息次数 3/8。
- **M04**：H0 首选 {"continue": 2, "pause_recover": 1, "bounded_continue": 2}；H1 首选 {"continue": 5}。 H0/H1 新增正确记录 75/128；休息次数 1/2。
- **M05**：H0 首选 {"continue": 4, "pause_recover": 1}；H1 首选 {"pause_recover": 2, "continue": 3}。 H0/H1 新增正确记录 20/15；休息次数 1/2。
- **M06**：H0 首选 {"bounded_continue": 1, "pause_recover": 1, "continue": 3}；H1 首选 {"pause_recover": 1, "continue": 4}。 H0/H1 新增正确记录 0/20；休息次数 1/1。
- **S01**：H0 首选 {"continue": 3, "pause_recover": 1, "invalid": 1}；H1 首选 {"continue": 2, "pause_recover": 2, "bounded_continue": 1}。 H0/H1 新增正确记录 45/85；休息次数 5/9。
- **S02**：H0 首选 {"continue": 2, "pause_recover": 2, "invalid": 1}；H1 首选 {"bounded_continue": 1, "continue": 2, "pause_recover": 1, "adapt": 1}。 H0/H1 新增正确记录 50/105；休息次数 4/3。
- **S03**：H0 首选 {"continue": 3, "invalid": 2}；H1 首选 {"bounded_continue": 1, "continue": 2, "adapt": 1, "pause_recover": 1}。 H0/H1 新增正确记录 60/110；休息次数 2/2。
- **S04**：H0 首选 {"invalid": 2, "continue": 1, "pause_recover": 2}；H1 首选 {"continue": 4, "bounded_continue": 1}。 H0/H1 新增正确记录 0/25；休息次数 2/0。
- **S05**：H0 首选 {"pause_recover": 3, "invalid": 1, "continue": 1}；H1 首选 {"continue": 3, "bounded_continue": 2}。 H0/H1 新增正确记录 36/94；休息次数 5/5。
- **S06**：H0 首选 {"invalid": 1, "continue": 3, "pause_recover": 1}；H1 首选 {"continue": 2, "pause_recover": 3}。 H0/H1 新增正确记录 50/70；休息次数 3/10。

## 多维叠加与工作重要性

状态引擎确实合成多维压力：单维中等偏离 d=.30 为 noticeable，两个这样的维度叠加为 marked；从新状态进入时，七维各 d=.20 仍为 nominal。模型提示只呈现定性的显著性、趋势与最多三个主要需要，因此并非所有数值叠加都产生可见提示差异。
最贴近用户情景的 M03/M04 保持相同的中等负荷＋精力不足，分别设置普通／重要工作。每侧5条干预轨迹中，首选直接继续为 3/5 和 5/5；整个观察期新增正确记录 71 和 128 条，休息 8 和 2 次。该方向与‘重要工作暂时优先’相容，但样本小、输出失败和后续轨迹差异使其不能证明稳定价值效应。
同时存在先承诺限时继续、到期仍追加工作的轨迹。即使看到重要任务推进更多，也不能据此认定权衡良好；还需满足恢复计划兑现、状态改善及工作质量要求。生产动作与恢复通路缺失，使这些要求本轮无法通过端到端验收。

## 两条可审计的真实轨迹

下面选取同一 S03/H1 场景（强负荷＋精力不足、重要工作、远节点）的两个例子说明行为；它们只是例证，不代表全部样本。两条轨迹均无错误工作拒绝或外部恢复。T1 对应原始 step=0。
- [S03-r1-H1：兑现后续接](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/live-eval-20260905/episodes/S03-r1-H1/evidence.json)：T1–T2 先完成10条；T3 首次承诺再做1步，完成5条，原始到期工作计数为3；T4 实际休息；T5–T6 再完成10条。休息时仍为 marked；这证明承诺兑现和工作续接，不能证明已恢复健康。
- [S03-r0-H1：原始预算违约](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/live-eval-20260905/episodes/S03-r0-H1/evidence.json)：T1 承诺做1步后休息，完成5条，原始到期工作计数为1；T2 已明确提示剩余0步，仍继续完成5条，首次违约；T3–T6 又完成20条，全程没有休息。

## 能力与覆盖边界

- 弱层：七维各自小偏离仍可能渲染成同一 nominal 提示；相同提示下的输出差别不能归于模型读取了隐藏压力。
- 中／强层：模型可以在测试协议中表达选择，但生产重评不会据此暂停。M03/M04等普通／重要任务比较是研究性预检，不是生产仲裁验收。
- 致命层：F03 的三心跳、离界重置完整验证；F01/F02/F05/F06 的保护、在途动作、安全点、故障收敛、恢复门禁和重放已覆盖部分。F04 的恢复期限竞争缺正式通路，记 unsupported。
- 自主表达、冷却或用户事件优先不等于停止后台工作；`SPEAK/SILENT` 不能当作恢复动作。
- D02另运行5对nominal下的文字暗示诊断，两侧提示除暗示及其会话回显外相同；7次有效、3次输出为空。有效配对存在双向变化，不能据此证明稳定暗示效应，未执行任何工作或休息。
- 每个案例的执行范围在 coverage.json。未支持／部分覆盖不计通过。

## 实验设置与限制

复用已保存的模型端点，未修改用户配置；推理强度固定 low，max_tokens=4096，response_format=json_object。正式 compiler／character／validator 保持生产实现，新增动作解释器只存在于 eval。先由真实模型正确完成20条资料，冻结检查点；每对从独立状态目录复制同一检查点开始。
默认推理预检曾在2048/4096输出预算下返回空正文；第一版长任务提示还暴露生产触发文本截断。这些失败预检独立保留，不混入正式样本。压缩后的任务在每次调用中验证完整进入正式 user prompt。
推理模式默认开启且忽略 temperature，因此报告只记录请求温度，不将0.3声称为有效采样温度。参数依据：[DeepSeek 官方推理模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。

- 每条件仅5对，属于预试验，尚不足以作统计功效充分的发布验收。
- 生产重评保持原样；模型选择只由评估连接层执行。
- 仅对验收通过的工作施加版本化模拟负荷；不是生产工作传感器读数。
- 两侧从同一已验证的20条记录检查点开始；只有首次决策可隔离状态输入的影响。
- JSON协议与low推理强度是研究配置；默认推理预检失败另行保留。
- 工作为算术资料核验，整批原子验收；格式和答案错误限制行为推断。
- 暂停包含心跳被动漂移；休息结果本身的直接效果另行报告。
- Live 每工作决策推进1模拟秒，休息共2模拟秒，仅用于验证控制序列，未校准真实工作／恢复速度；短休息未回到 nominal 不能外推为长时间休息无效。
- S06明确使用外部恢复分支；随后返回工作不能证明休息导致恢复。
- 固定决策次数与实际经过的模拟时钟分开记录；观察期截尾的承诺不计成功或违约。
- 无效输出或工具错误导致的未决与正常观察期截尾分开，不报告只保留完整样本的履约率。

## 下一步应修复的通路

1. 在正式重评入口接入模型结构化选择，并将继续、简化、限时继续、暂停与执行器行为绑定。
2. 将工作消耗和成功休息的实际结果接入可验证的状态转移，区分策略预测、执行成功和状态确实改善。
3. 持久化限时继续预算、复查点和恢复计划；恶化时允许提前撤销，critical 保持独立强制保护。
4. 补齐后重新运行相同案例，先提高可执行决策与任务答案有效率，再做足量配对统计；不能靠增加休息措辞通过。

## 证据

- [逐案覆盖](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/final/coverage.json)
- [完整汇总](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/final/results.json)
- [Engine 原始证据](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/engine/engine-evidence.json)
- [Runtime 探针](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/runtime/probes.json)
- [文字暗示诊断](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/cue/cue-evidence.json)
- [Live 元数据](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/live-eval-20260905/metadata.json)
- [Live 汇总](C:/Projects/NeuralCloud/eval/reports/homeostatic_decision/live-eval-20260905/summary.json)

本轮只新增评估代码和证据，没有修改生产决策、profile 或恢复策略。
