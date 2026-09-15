# Qwen 情绪导演：接线与评测

## 生产接口

Runtime 先核验 QQ 消息来源，再让 Cognitive 构建 `EmotionInput`。第一次辅助调用只评估新增外部消息，程序生成动态权重与 `EmotionFrame`，第二次辅助调用产生 `EmotionGuidance`。同一份提示供 QQ 开口判断与正文演绎使用；这两处不重新评估情绪。

QQ 窗口通过 `harness.qq.emotion_window.build_qq_emotion_messages` 从已核验观察库读取。它按会话隔离，最多20条、最近30分钟，保留本次精确绑定的触发消息。只有该触发消息进入 `new_source_ids`；搜索结果、旧记忆、未发送草稿不充当新刺激。角色自己已核验的发言只提供上下文，部分或全部送达都保持同一分段回执身份，完整汇总回执不重复加入。协议自己的消息回声不另算角色输出，持久送达回执才提供这部分窗口文本，避免跨轮由回声换成回执时重复。文字窗口过期不清空独立衰减的情绪余额。

OneBot 原有入口允许服务器时间最多快5秒。情绪窗口通过同一 DirectQQSettings 的事件库绑定 `received_at`，在核对 source、消息身份和完整观察后，使用消息时间与接收时间中较早者。这个时间对每个源固定，后续合窗不会因时间变化发生源冲突；超过5秒仍拒绝，桌面来源不使用协议事件库。

`emotion_mode=on` 时，QQ 两次主模型请求只使用自然语言情绪提示。模型用户资料块递归移除旧内稳态、心理状态及关系情绪数值，保留事实、消息原文和历史。原 Persona 的身份与事实保持；强情绪可以暂时突破平常的温柔、克制等表达习惯。`off` 保持旧行为；`shadow` 不向主模型注入提示。

## 可重复的离线流程

下面命令仅构造脚本辅助模型和脚本主模型，不读取在线凭据、不调用网络，不写生产记忆，也不发送 QQ：

```powershell
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics.emotion --out eval/reports/emotional_expression_diagnostics/private/qwen-emotion-offline-new
```

输出目录必须是新目录。产物为 `manifest.json`、`evidence.jsonl`、`summary.json`：

- `legacy`：现有内稳态表达路径。
- `rule-fallback`：新链路无独立辅助模型时的程序语义提示。
- `qwen-scripted`：真实 `EmotionService.observe → guidance` 接口的脚本模型替身。

三种条件都进入生产 `InferencePipeline.prepare → ContextCompiler → CharacterModel → ResponseValidator`。九类需要条件各含 nominal/marked，共54条生成记录。新链路记录冻结输入、Frame、Guidance、辅助请求摘要、编译片段与实际主模型 messages；旧链路不伪装成新导演结果。

`summary.json` 另附固定等权与动态立方权重的数学对照；这个54条流程保持原样。全部产物明确 `live=false`、`release_eligible=false`、人工评分0、`quality_result=not_evaluated`。脚本回复的自然度、人格或情绪辨识度没有统计意义。

## 固定与动态权重的多轮生成对照

单独执行：

```powershell
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics.emotion_multiturn --out eval/reports/emotional_expression_diagnostics/private/qwen-emotion-multiturn-offline-new
```

四个冻结场景为强对话、极强内稳态、两者竞争、显式修复与恢复；每个场景5轮，两种权重各40条生成记录中的一半。每臂使用独立持续的生产 `EmotionService`，记录逐轮情绪余额和实际滑动窗口。固定臂只在评测私有子类 `_frame` 返回时设置三源各三分之一，并按生产相同的 max/原键顺序重算 `dominant_source`，随后由生产服务派生 `semantic_priority`，再正常持久化并验证 Guidance 绑定；生产算法没有增加固定模式。精确等权按原键顺序选择 homeostasis，这是明示的并列规则，不要求各类情绪平均表达。强度、事件累积、衰减与恢复规则相同。此对照只解释成功导演调用的结果；降级提示不构成有效权重消融结果。

同轮输入、Persona、内稳态、时间、源事件与历史完全相同。第一臂的实际 appraisal 输出冻结，第二臂回放同一输出，消除首次评估采样差异；两臂各自调用导演，实际请求仅权重及其确定性派生主次信息可以不同。后续历史采用相同的合成“已送达”角色发言 fixture，生成的不同正文保留为结果，不反馈成下一轮新输入。这是受控重放实验，尚不代表闭环真实对话。

每条 `evidence.jsonl` 保留完整冻结输入、Frame、Guidance、窗口及其 hash、两次辅助请求的 prompt/response/hash、是否回放 appraisal、编译片段和实际 main messages/final。`main_history`、`main_history_sha256` 与 `generation_fixture_sha256` 记录主模型实际使用的冻结前文；同一前文经生产 SessionStore 和编译器送入两臂，生成结果不进入历史。主调用另存 `response_metadata`，包括 `finish_reason`。离线流程总计80次辅助接口请求，其中20次为冻结 appraisal 回放、60次委托给脚本模型；40次主模型请求也全部为脚本替身。以后可由已获授权的调用方显式构造同配置真实客户端和 Settings，传给 `evaluate_scenario` 复用此接口；它不会自动选择端点、读取凭据、评分或发布。

本轮新增测试证明相同冻结输入的两臂有相同余额、窗口与非情绪主模型上下文，导演实际收到不同权重，主模型实际收到不同提示并产生不同脚本正文。这些是链路证据，不是动态权重的真实效果收益。人工评分仍为0，发布资格仍为 false。

### 显式真实辅助模型探针

在端点和模型准备好且已授权调用之后，可以显式运行真实辅助模型、脚本主模型的探针：

```powershell
.\.venv\Scripts\python.exe -m eval.emotional_expression_diagnostics.emotion_multiturn --out <新目录> --live-aux --aux-base-url http://127.0.0.1:11435/v1 --aux-model <已部署模型名> --scenario recovery
```

单场景5轮、两臂10次脚本正文，成功路径委托15次真实辅助调用，另5次 appraisal 回放。可以重复指定 `--scenario`；省略时跑完整四场景。实际生产预算为 appraisal 8秒、guidance 4秒，不放宽超时；输出含失败类型与降级状态。`http-evidence.jsonl` 记录生产客户端序列化后的请求与可见响应，排除鉴权头和隐式推理。产物明确 `live_aux=true`、`live_main=false`，无评分和发布资格。辅助参数必须与 `--live-aux` 同时指定；可用 `--aux-api-key-env` 显式指定密钥变量，不自动读取在线凭据。此模式验证真实辅助链路，不验证真实主模型演绎质量。

如已授权验证全真实链路，在同一命令追加 `--live-main-config data/gui_settings.json`。该参数必须与 `--live-aux` 同用，且文件必须已存在并提供主模型 endpoint/model；仅这时通过 `gui.persist` 读取其中密钥并创建真实主模型客户端。默认流程不读取主模型配置。产物改为 `live_aux=true`、`live_main=true`，记录实际模型和两种 HTTP 调用，不记录密钥。主模型仍固定 `max_tokens=240`、temperature 0.1；官方 DeepSeek V4 端点使用现有 `deepseek_low_reasoning_options` 的 low 设置并记入 manifest。即使两类模型都真实调用，固定历史与配对 appraisal 回放仍保留，无独立人工评分就不具备发布资格。

### 2026-09-15 live-v2 的失败发现

历史产物 [qwen-emotion-full-live-v2-20260915](../../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-full-live-v2-20260915/summary.json) 保持原样。该轮使用 Qwen 4B 与 DeepSeek V4 Flash，运行恢复场景的5轮、两臂10条记录。原摘要的 `successful_guidance_records=2` 只是旧生产校验接受了2条，不是2条合格导演提示：逐条检查原始辅助响应，10条均写成角色直接说的话；其余8条触发降级。被误收的例子包括“慢慢说，我听着呢。你刚说的，我懂了。我们一步一步来，不急。”。因此这轮没有可用于比较固定与动态权重收益的有效导演提示，不能把降级主模型正文或误收台词算成成功。

同时发现旧多轮 runner 只把历史提供给情绪评估，每轮新建主模型 SessionStore 为空，正文演绎仅看到当前句子。该轮只能保留为旧契约失败诊断，不能作为完整多轮连续性证据。新版 runner 将相同冻结 SessionTurn 历史提供给两臂主模型，并在实际请求边界验证完整有序前文。此修复改变实验输入，新结果必须写新目录、采用新 manifest/spec hash，不能覆盖或与旧轮直接汇总。

该轮 HTTP 证据另有1条主响应 `finish_reason=length`、9条 `stop`，10次请求不等于10条完整正文。新版指标在 manifest 和 summary 中附 `metric_definitions`。保留的 `successful_*` 字段仅表示生产解析与校验接收；`main_calls` 是请求次数；`main_truncated_calls` 单列 `finish_reason=length`，缺少停止原因的输出也单列为未核验。导演身份、情绪语义和主模型质量都没有自动得分，`director_role_review=not_scored`，人工评分0、发布资格 false。

### 新契约的验收要求

导演辅助输出改为必填 `feeling`、`delivery`、`needs` 三字段，程序组合成唯一自然语言提示；不再接受旧 `text` 输出契约。导演输入使用状态、权重、同会话评估结论及 Persona 核心性格/价值，原始台词窗口仅用于首次评估。两个离线 runner 的 manifest 都直接记录生产 `emotion_prompt_version` 和 JSON Schema/Pydantic 校验策略，使提示版本变化进入 spec hash。

`required_feelings` 明示余额达到0.15的前两项心情及各自趋势。脚本辅助也逐项表达这些心情，即使内稳态主导仍保留明显愤怒或相反心情；恢复词须贴近对应心情。新增反例通过真实服务验证：极强需要下并存平静与愤怒，删去愤怒不能通过；下一轮删去恢复语义也不能通过。此项验证校验器会拒绝已知错误，没有给脚本回复评分。内稳态主导时，最终提示由程序将需要放在首位。

下一次真实运行须逐条检查原始三个字段及组合提示：描述当前感受和表达要求；不替角色回答、不决定开口或行动、不补造原因或身体事实；主次与恢复趋势可辨识。仅 JSON 合法、未命中拒绝正则、主模型产生正文都不够。还须核对两臂的冻结窗口、主模型历史、appraisal、余额和强度相同，唯一干预是权重及确定性派生的主次要求；主请求只有一份提示且没有旧情绪参数，截断和降级单列。真实收益仍须独立盲评及既有发布门禁，本节不预填新版本成功率。

### v4 全真实工程实测与限制

2026-09-15 的 [full-live-v4 摘要](../../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-full-live-v4-20260915/summary.json) 与 [逐条证据](../../eval/reports/emotional_expression_diagnostics/private/qwen-emotion-full-live-v4-20260915/evidence.jsonl) 保留四场景、每场景5轮、两臂40条记录。实际委托60次辅助请求、40次主模型请求；另20次辅助接口为 appraisal 冻结回放。20对的完整输入、窗口、主模型历史和非情绪上下文散列相同。本次不是 QQ 发送、闭环互动或发布验收。

生产校验接受 appraisal 38/40条、guidance 39/40条，两者同时接受37/40条。这些是契约接收计数：真实独立 appraisal 是19/20次；两臂均接受两阶段结果的是18/20对。失败来源分别为：

- `strong_dialogue` 第1轮 dynamic 的 guidance 在4秒预算内发生 `ReadTimeout`，进入程序提示降级。
- `strong_homeostasis` 第5轮 appraisal 返回 `events=[]`，遗漏当前必需的源事件，触发 `emotion_appraisal_source_mismatch`。固定臂回放这一次结果，所以同一失败占两条记录。
- `competing_signals` 第4轮 fixed 主响应为 `finish_reason=length` 且正文为空；HTTP usage 表明240个输出 token 全用于推理，即便请求已使用 low。其余39次主响应为 `stop`。上述18对中有1对因此没有完整双臂正文。

本次开发者只读审阅没有给出人工评分；`quality_result=not_evaluated`、独立评分人数0、`release_eligible=false` 保持不变。39条通过校验的三字段输出仍有下列已观察限制，不能解释为39条语义正确提示：

- 逐条证据第2、22、39行的组合提示包含“维持内稳态”或“内稳态平衡”，并进入主模型输入。所查正文未直接复述该词，但提示端仍存在机制措辞。
- 恢复场景出现“愤怒正在恢复”“愤怒持续恢复”，可能被理解为愤怒重新出现，而非愤怒减弱。强内稳态场景第5轮平静余额从0.5725降至0.4339，却写成“平静持续恢复”；正向心情衰减的措辞仍需澄清。恢复场景第2–5轮 `needs` 也没有明确表达内稳态需要正在缓和，不能用附近愤怒的恢复词替代这一验证。
- 强对话与竞争场景的部分后续中性问句被 appraisal 赋予新的高强度厌恶，同时结论仍描述“被直白批评”。当前窗口确有旧批评，但当前新事件是“我们接下来先整理哪一项？”。这提示旧刺激被重新归因和计入余额的风险；来源 ID 去重测试通过并不能证明模型完成了正确的增量归因。
- 有些提示同时写明显厌恶与“不带情绪波动”，情绪主次与表达要求仍可能相互抵消。是否能自然演绎，需要独立盲评，不能只检查心情关键词覆盖。

本轮只有单一 Persona、单一主辅模型配置、每条件一次采样，并采用固定合成已送达历史。固定等权的并列规则始终优先 homeostasis；它不是各心情平均表达的对照。没有旧路径与新路径的真实配对盲评，也没有实际多会话闭环、长时 soak 或发布质量结论。旧 live-v2/v3 与本轮证据均不覆盖、不混算。

实测后的词汇校验补丁版本为 `emotion-guidance-guard-v2`：增加中文机制词拒收，心情减弱不能仅用“愤怒恢复”表示，主导内稳态需要缓和须明确表达该需要改善。该补丁只做离线拒收验证，没有更改 Qwen 提示或追加真实调用；上面的 v4 历史接收计数保持原样，不能当作新 guard 下的接收率。两个 runner 的新 manifest 均记录 `emotion_validation_version` 并纳入 spec hash，使后续校验变更与历史实测可区分。

## 既有发布评测的兼容

`eval.homeostatic_expression` 支持 `emotion_mode=on` 与 `prompt_version=qwen-emotion-v1` 的 manifest；旧 manifest、旧单片段归一化和旧默认路径保持兼容。新条件使用每次独立的生产 EmotionService，避免 nominal/marked 之间共享情绪余额。新的唯一表达片段是 `emotion_guidance`；同时出现旧内稳态片段和新提示会直接失败。

输入、Frame、Guidance 必须绑定相同 context、scope、revision、时间、Persona 与受信内稳态摘要。配对比较还要求除内稳态外的输入散列相同。原恢复态检查继续要求实际主模型可见提示包含恢复语义；不能用记录中存在但未注入的 trend 替代。

既有九类、每类至少30对、逐类质量门禁、独立新状态30分钟 soak 与 critical 时主辅模型和发送零调用门禁保持。实际 Qwen 模型与主模型配置、请求身份、预算和延迟必须在真实运行前确认；离线流程不会自动启用线上模式或替代发布验证。

## 真实效果研究尚需完成

后续在同一合法事实、Persona、对话窗口与主模型配置下，使用真实模型比较旧路径、新规则提示和新 Qwen 提示。固定与动态权重已有单独冻结的离线生成流程和上述 v4 全真实工程实测；尚未建立动态权重的真实效果收益，也未完成独立盲评。

场景除既有九类外，还应覆盖强单句刺激压过普通内稳态、极强需要压过对话、混合情绪、连续轻度刺激累积、明确道歉或修复、10分钟衰减、窗口移出、高频消息、引用或讽刺、跨会话仅保留无原因情绪余波、重复事件与重试。比较4–6轮连续性，并记录两次 Qwen 调用增加的耗时、失败率与语义降级率。

主指标仍是情境与状态适配，次指标为辨识度、人格与自然度；事实行动、无依据生理表现、机制泄漏和负对照过度表达分别审查。沿用 [既有诊断计划](16-emotional-expression-diagnostic-research-plan.md) 的独立盲评与配对分析规则。9月14日 pilot 缺独立人工评分，不能据其探针结果宣称新链路已改善表达。
