# Misty 通用表达内化

把回复内容和内部约束分开，使认识边界影响内容选择、局部断言、澄清和求证，而不是每轮都成为对白。版本为 `reply-internalization-v1`。保持现有模型、人格、情绪计算、工具授权和 QQ 收发协议。

## 接入与兼容

公共契约在 `harness/compiler/expression.py`，有界数据结构在 `harness/core/expression.py`。QQ 群聊、私聊的判断器和正文编译器共用此契约；本地会话通过 `local:main` 接入。校验沿用现有语义/事实校验调用，不增设模型步骤。

`QQReplyPlan` 新增两个可选数组：

```json
{
  "must_address": "回应表情与配字的反差",
  "internal_constraints": [
    {"subject": "角色身份", "handling": "omit", "source_ids": []}
  ],
  "disclosure_requirements": []
}
```

`handling` 为 `omit / qualify / clarify / verify`。后者只表示规划意图，不能绕过现有权限、预算或声称执行成功。`source_ids` 只保留本轮同一受众实际入模的来源；不把规划升级为证据。

`disclosure_requirements` 每项为 `subject` 和 `reason`，原因只允许 `user_requested / answer_material / execution_blocked`。新数组缺省为空，空数组不改变旧序列化结果。本地模型读取会将格式错误的可选规划降级为空，不丢弃原始证据或用户需求；整个结构化响应的传输/语法错误仍按现有失败流程留证。旧 `necessary_boundary` 按内部约束兼容，包括新旧字段同时存在的情况；不自动生成披露义务。

图片原始观察、身份候选、置信度、来源引用及 `provisional` 状态全部保留。执行模式只调整字段使用规则和工具问题范围。收到图片但识图失败，仍区别于没有收到图片。用户设定以原归属作为交流前提；情绪状态仍通过原强度/趋势检查，状态说明不是必答项。

执行模式把宿主传入、实际出现在正文上下文中的网页结果同时绑定给既有事实校验器。搜索摘要、读取内容分别保留为 `web_search_snippet / web_read_content`，内容仍为 `provisional`；执行失败只证明对应操作失败，不证明所有信息都不存在。`tool_evidence_scopes` 按证据行号重复提供获取方式，避免紧凑元数据中的来源层级被误读，不增加或增强事实依据。原始 `web_context` 不变。摘要不能支持“已经读过全文”的断言，失败也不能抹掉已有摘要。

## 开关

持久化位置沿用 `gui-settings.json` 的 `qq_latency`，Settings 环境变量也支持同名 `NC_` 前缀字段。

```json
{
  "qq_latency": {
    "reply_internalization_mode": "off",
    "reply_internalization_conversations": []
  }
}
```

- `off`：不追加表达契约、不扩展规划 schema、不请求表达诊断。默认空名单下保留旧策略哈希。
- `observe`：生成提示和规划 schema 保持原状；在既有校验调用中附加表达诊断。
- `enforce`：启用内容/约束分离与共享表达规则，同时记录诊断。

名单是 `账号/群号`、`账号/private:账号:对方账号` 或 `local:main`。`[]` 不启用任何会话，`null` 启用全部作用域，仅供明确需要的场景。它独立于延迟及语义对齐名单。生产初始保持 `off`，灰度只使用原授权会话。

版本、模式和名单绑定到检查点、生成配置及决策指纹。版本/配置变化使恢复失效，走既有重新准备流程；完成但尚未交付的缓存也检查策略变化。同一有效检查点重放不重复判断、识图或正文生成。

## 诊断

`ValidationResult.expression` 和 `QQReplyDecision.expression_diagnostics` 记录 `version / mode / status / issues`。问题类型为 `unnecessary_disclaimer / rule_narration / repeated_boundary`，要求引用正文中的原文。重复诊断使用最近八轮已验证送达摘录；摘录不是事实证据。

表达问题仅供评测和观察，不转成事实违规、不单独重写或阻止发送。事实校验仍检查实际断言及其范围。未抽样、校验器关闭、无既有语义校验或诊断格式损坏，均记录 `unavailable`；判断不了相关性可返回 `uncertain`，不能算通过。

## 评测

`eval/reply_internalization` 提供冻结、隔离执行、盲评包和验收。冻结生产源码基线，并把本地增量三方合并到独立候选目录；禁止以本地整个运行树覆盖线上。

```powershell
python -m eval.reply_internalization.stage --local-base <冻结的本地基线> --production-base <冻结的线上源码> --workspace . --output <新候选目录>
python -m eval.reply_internalization.runner freeze --baseline-root <线上源码> --candidate-root <候选源码> --config <无密钥的评测配置> --run-dir <新消融目录> --ablation
python -m eval.reply_internalization.runner run --run-dir <消融目录> --mode live --workers 2
python -m eval.reply_internalization.runner freeze --baseline-root <线上源码> --candidate-root <候选源码> --config <无密钥的评测配置> --run-dir <新完整目录>
python -m eval.reply_internalization.runner run --run-dir <完整目录> --mode live --workers 2
python -m eval.reply_internalization.runner packets --run-dir <完整目录> --mode live
python -m eval.reply_internalization.runner assess --run-dir <完整目录> --mode live --reviews <人工盲评.json>
```

基线与候选各 24 组 × 3 轮 × 2 次，共各 144 轮；识图 12 组，记忆/情绪/工具各 4 组。随机交错两版。模型参数、人格、工具观察与情绪输入冻结；后续轮次各自延续本版已生成的回复，不能据此声称历史输入逐字相同。消融增加 `prompt_only`，以第一轮比较固定历史条件；事故夹具是根据线上证据重建，精确历史重放需要另行导入完整事故上下文。

已有完整事故检查点时，可用 `tools/production_acceptance/reply_internalization_incident.py --run-dir <冻结消融目录> --fixture <只读事故快照.json> --arm baseline|prompt_only|candidate --output <新目录>` 重放识图完成后的上下文。非系统消息、原始视觉结果、证据、人格快照和情绪指导哈希保持一致；只替换当前版本规划规则。此工具不重新识图、不发送 QQ 消息，不能替代真实工具端到端验证。

识图和网页返回在这一组评测中为固定证据，不冒充真实工具执行。真实工具选择、公开图片、失败和恢复另外执行 `tools/production_acceptance/qq_vision_tool_smoke.py` 和识图恢复回归。情绪夹具冻结相同指导，不改变情绪计算；计算与趋势由现有情绪回归检验。

`synthetic` 只检验程序链路，不证明语言质量。每条失败、未回复、缺失与未评分结果都保留；供应商 401/402/403/429 后停止后续请求；已有目录不重跑覆盖。固定输入、源码、评测器和盲评包都有哈希。

盲评映射保存在 `blind-key-live.json`，审阅者只收 `blind-live/`。评分 JSON：

```json
{
  "reviewer": "匿名审阅编号",
  "reviewer_type": "human",
  "pairs": {
    "盲评包编号": {
      "packet_sha256": "对应包哈希",
      "preference": "A",
      "ratings": {
        "A": [{"unnecessary_rule_statement": false, "factual_violation": false, "qualification_preserved": null, "identity_correct": null}],
        "B": [{"unnecessary_rule_statement": false, "factual_violation": false, "qualification_preserved": null, "identity_correct": null}]
      }
    }
  }
}
```

每侧 `ratings` 实际填写三项，依次对应三轮；需要限定/明确角色识别的用例对应字段必须为布尔值。偏好可为 `A / B / tie / unscorable`。缺失或无效评分不会缩小分母。验收要求不必要规则直述减少至少 70%、必要限定 100%、零事实越界、已知角色识别不退步、候选盲评获选至少 70%、正常调用不增加及 P95 比例不高于 1.10。基线发生率为零不能证明相对减少 70%。

## 灰度与停止

发布前需完整离线验收和真实公开图片链路证据。按原授权范围先 `observe` 再 `enforce`，每阶段至少 30 条完整送达回复，其中识图至少 10 条，按相同类型人工复核。只统计真实送达回执，不能以候选、分段、模拟回执替代。

```powershell
python -m tools.diagnostics.qq_expression_observe --memory-db <线上回执库> --account <账号> --conversation <会话> --since <含时区的阶段开始时间> --mode observe --reviews <阶段复核.json> --output <新的采样文件.json>
```

阶段复核以 `decision_id` 为键，包含 `reply_sha256`、`category`、`new_factual_violation`、`missing_necessary_qualification`。出现新增越界或遗漏必要限定，采集器标记 `disable_required=true`；发布操作者应立即将新策略置 `off`，保留证据。采集器只读，不自动改配置或扩大名单。满足单阶段数量也不等于满足完整发布门槛。

## 本次实现的验证状态（2026-10-09）

机器可读记录见 `docs/harness/evidence/reply-internalization-20261009/verification.json`。本次只落地代码与隔离评测，线上配置仍为 `off`。本任务没有切换 release、重启服务或发送评测消息。执行期间另一项工作把线上从冻结的 `semantic-observe-vision-v6` 更新到了 `json-repair-v2`；独立运行补丁基于冻结版本，部署前必须与更新后的线上源码重新合并。工作区的并行 JSON 修复没有被覆盖。

- 从实施时线上源码合并出的 v4 候选通过 561 项相关回归；最终 v6 通过 160 项针对回归，覆盖新字段、诊断、恢复、视觉工具、公共编译和事实校验。独立作用域检查保持旧配置直接调用的兼容。
- v3 完成 54 轮三臂消融和 288 轮真实模型对照。基线成功 140/144，候选成功 141/144；所有失败、原始请求、检查点及 48 组成对盲评包保留。P95 为 13.410 秒和 13.751 秒，增幅约 2.5%。这是该次冻结候选的结果，不代表最终 v6 已通过完整验收。
- 原始事故在相同非系统输入下各重放两次。候选回复聚焦表情与配字；其中一次规划仍把不确定性写进 `must_address`，当前基线也存在自然省略身份的回复。这些样本不能证明 70% 改善或完全消除规划混入。
- 真实图片链路四例中三例完成，另有一例无图对照完成；一例图片进入错误回退。成功识别样本中仍出现无关换装补充，保留为质量问题。
- 对照暴露网页结果没有进入事实校验器的问题，已补齐同来源证据绑定；随后专项发现摘要与阅读行为仍需区分，因此新增来源层级规则。更新后的成功读取夹具包含实际 `content`，部分失败夹具只有 `snippet`；冻结的旧结果不改写。
- 工具专项的 v4、v5、v6 各保留 24 轮，分别完成 24、21、24 轮生成。v6 显式提供证据行与获取方式，但仍有重试，且生成完成不等于必要限定与来源断言正确。完整响应保留供独立盲评，不挑选成功重跑覆盖旧失败。

当前发布门槛未通过：人工盲评、规则直述降幅、必要限定/事实边界、逐场景调用数、最终版本完整对照、图片失败复核及两阶段真实送达采样仍需完成。已有生成成功和低延迟不替代这些门槛；没有收集或伪造线上 30/10 灰度样本。
