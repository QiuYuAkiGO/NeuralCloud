# DeepSeek 前缀缓存优化与链路重构评估

更新日期：2026-09-16。本文区分已实现的请求布局调整、官方机制和待验证的链路方案。没有调用付费模型验证收益，也没有部署；不能把离线公共前缀长度当作实际缓存命中率。

## 官方机制与适用边界

- 缓存默认开启。当前 SWA 机制按已落盘的完整前缀单元命中：落盘发生在请求输入结束、输出结束、公共前缀检测，以及长内容中的固定 token 间隔。官方例子中，`A+B` 后的 `A+C` 可能不命中；两次请求形成公共单元 `A` 后，`A+D` 才能命中。构建为秒级，闲置数小时到数天清理，属于尽力而为。[上下文硬盘缓存](https://api-docs.deepseek.com/zh-cn/guides/kv_cache/)
- 2024 年公告中的 64 token 存储单位是历史说明。当前指南未给出固定间隔数值，不能据此添加 64 token 填充、承诺任意公共前缀的第二次调用就命中。[旧缓存公告](https://api-docs.deepseek.com/news/news0802/)
- `user_id` 会隔离 KVCache；同一业务用户的值应稳定，不能使用每请求变化的 ID。保留真实的隐私隔离域。官方没有公开通过固定 IP、连接或节点绑定提高命中的契约，也没有承诺不同 API Key 的缓存共享。[限速与隔离](https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit/)
- Responses 的 `prompt_cache_key`、`prompt_cache_retention` 不受支持；Anthropic 兼容接口忽略 `cache_control`，不能用这些字段指定缓存单元或保留时长。[Responses](https://api-docs.deepseek.com/guides/responses_api/)、[Anthropic](https://api-docs.deepseek.com/guides/anthropic_api/)
- 带 `tools` 的 thinking 请求必须完整回传全部历史 `reasoning_content`，包括没有调用工具的轮次；不带 `tools` 时该字段会被忽略。不能为减少输入或提高缓存而擅自删改必需的思考与工具历史。[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)
- 当前官方模型名、实际模型版本和路由会变化。2026-09-10 公告推荐 `deepseek-flash`，并说明自 9 月 14 日 12:00 起 `deepseek-v4-pro` 临时路由 V4.1 Flash。实验需记录请求模型与响应模型，不能只依赖旧别名推断行为。[V4.1 Flash 公告](https://api-docs.deepseek.com/zh-cn/news/news260910/)

## 本阶段实现

### QQ 历史块

实际发送布局改为固定 system、会话范围 user、零个或多个 `closed_observations` user 历史块、动态 user 尾段。历史块每 32 条记录按时间排列，不按成员重新分组；header 不包含增长中的 ID 清单、块数、最新资料或别名。63→64、95→96 等跨块增长只追加新块，既有块字节不变。

完整的已核验资料快照仍在动态尾段中，历史观察通过原有 `profile_record_id` 与之关联。新成员、昵称与资料更新、别名、情绪和请求不会重写既有观察块。输入仍从最新准入记录构造，不保存可能陈旧的正文缓存；原始记录发生修正或被撤销时必须反映到新输入，不能为了命中继续使用旧记录。

预算与发送共同使用 `encode_model_payload` 的同一组有序块。32/64/128 的窗口规则与完整记录裁剪不变；窗口换代、预算裁剪分别记录 `context_window_prefix_rebuilds`、`context_budget_prefix_rebuilds`。群聊提示词版本升为 v19，私聊为 v7，避免旧决定与新输入格式混用。正文生成仍完整续接本轮 gate 请求及其答案。

### 认知阶段

Board、Diary、Goal、Capability、Web 使用统一人格序列化与共同前缀。Goal/Capability/Web 的固定 schema 和能力定义先于动态机会、观察、有效期和状态；定义按稳定能力字段关联，`offer_id`、有效期与执行授权仍来自当轮可用机会，定义本身不授予任何权限。

模型专用记忆投影保留一次带归属的原文、source IDs 与用途，内部证据绑定和哈希仍在原始对象中。只对已选中的可识别记忆记录按时间排列，不改变检索集合、撤销语义或受众权限。情绪仍为可信 system 指令，但在固定 schema 与资料之后追加，每轮刷新。

### 普通 chat 与 validator

`render_emotion_guidance` 将原有固定表达规则放在动态的“当前情绪演绎提示”之前。固定规则的内容没有删减，仍说明情绪可突破通常表达习惯、身份与知识边界持续有效、不得泄露内部指标或编造情绪原因。每次仍从当轮输入取得情绪提示，保留 critical 状态阻断与缺少提示时的既有降级行为。

这项改动只调整一个可信情绪段内部的顺序。普通 chat 的 segment 顺序、预算、记忆准入、角色、世界时间、近期对话、来源记录都保持原来的处理方式。世界时间仍会使其后的前缀变化，近期对话也仍为预算约束的窗口；本阶段没有把这些动态事实移动到别的信任层级，也没有声称已经实现整个会话 append-only。

Validator 的独立请求保持同一 critic system 和证据投影，只将 user JSON 的字段顺序调整为：人格、已准入记忆证据、本轮状态和情绪、候选回复。这样同一证据与状态下，修改候选回复不会在最前面打断 critic 自己的公共前缀。字段语义、证据完整性与不确定性、归属、私有来源审计、规则检查、模型路由和失败处理均不变。它仍是独立校验，未复用生成阶段的 system 或前缀。

**尚未上线：**gate/persona 合并、validator 复用生成完整前缀，以及 `CharacterModel.generate` 中 continuation 协议的布局变更。固定 continuation 协议仍在现有位置追加。不同角色提示合并、改变生成阶段输入、把证据提升为指令都不属于本次缓存调整。

### 请求计量与报表

新增 `NC_MODEL_REQUEST_LOG_ENABLED`（默认 true），独立于含正文调试记录的 `NC_CALL_LOG_ENABLED`。物理请求元数据保存在 `NC_TRACE_DIR/model_requests.jsonl`，记录每次尝试的模型、阶段、输入输出、缓存字段覆盖、耗时和稳定前缀摘要；不写提示词、回复或密钥。已返回但随后超时或被格式检查拒绝的请求仍保留供应商用量。只有实际匹配本轮 gate 稳定消息的调用才沿用它的前缀范围，辅助模型请求不继承错误的范围。

`TokenUsage` 增加向后兼容的缓存已知/未知输入量及输入、输出用量来源。缓存字段缺失不等于零命中；显式 `hit=0` 且有输入总量才可算出未命中量。部分用量或估算数据不冒充完整 API 计量。

首选报表入口（不调用模型）：

```powershell
.venv\Scripts\python.exe tools/qq_cache_report.py --requests traces/model_requests.jsonl
```

报表按 UTC 小时、阶段、请求模型、响应模型、角色和用量来源分组，记录物理调用次数、输入输出、已覆盖缓存命中率、未知输入量、API 用量覆盖率及请求耗时 p50/p95。命中率分母只包含有可靠缓存分区的输入；全局缓存覆盖率在输入总量缺失时为 null，并单列已记录输入的覆盖率。命中率无数据时为 null，不是 0%。保留 `--trace` 和只读 `--monitor`，但旧 turn 汇总无法补出缺失的模型、调用次数与 API 来源，不能与物理请求总量相加。移除固定旧价格推算，`estimated_usd=null`，按当时真实价格另行核算。

## 独立对照与链路方案

1. **布局对照。**以同一固定输入集分别运行基线与布局调整，单独评估普通 chat、QQ gate、QQ persona、validator 和 cognitive 路径。比较变更前后的实际出站 messages，要求内容和权限边界一致，动态情绪、时刻、截止时间等仍随当轮变化。只报告可复用前缀的结构变化，不能将字符数换算为官方命中 token 数。
2. **缓存观察。**未来开展授权的线上对照时，固定 endpoint、账户隔离域、实际模型、工具集和抽样方案，分别统计冷启动、同会话追加、共享前缀不同尾部、修订、压缩后首轮及稳定后续轮次。共享前缀场景至少保留第一、第二、第三次请求各自结果，并记录时间间隔，避免把缓存尚未落盘误判为布局无效。真实任务顺序和自然间隔是主样本；预热或等待实验单列，其调用成本也计入。
3. **gate/persona 合并候选。**单独立项并与布局变化分开实验。候选方案输出必须保留 gate 的 speak/abstain 决策和 persona 回复之间的显式区分；abstain 不能产生外发文字，失败不能默认放行。首先做影子重放，使用当前双调用结果及独立人工标注检查发言资格、角色一致性、引用证据、发送权限和遗漏，不接入发送链路。只有质量验收通过后，才评估减少一次模型调用的整体收益。
4. **validator 全前缀复用候选。**单独设计 critic 接口，明确生成指令只作为待审数据、证据仍限于本轮准入、候选输出必须被独立评判。先与当前独立 critic 做影子对照，加入提示注入、错误自我认可、跨群归属和缺证据断言案例。不能直接把生成 system 变成 critic system；无法证明独立性与安全边界时，继续使用本阶段独立请求。

前两项评估缓存布局，后两项评估调用链架构。对照时一次只改变一个因素，不能将质量变化、调用次数变化和缓存命中变化归为同一收益。

## 验收与观测口径

- **确定性回归：**固定表达规则先于可变情绪；更新情绪与世界时间后出站内容必须更新且不残留旧值；critical 阻断不变；segment 顺序和预算不变；validator 修改候选后保留同一人格、已准入证据与状态前缀；证据原文、归属和私有审计继续保持完整。
- **质量门槛：**隐私串域、未授权发送、记忆归属错配、将用户证据作为指令、无证据身体断言等安全用例必须全部通过。影子抽样采用同一标注集、盲评、按路由报告；合并方案不得增加 gate 误放行或降低证据忠实度。即使缓存比例更高，也不能用它抵消质量回归。
- **缓存口径：**在具有可靠缓存分区的样本内汇总 `sum(prompt_cache_hit_tokens) / sum(prompt_tokens)`，同时展示 hit/miss token 总量、缓存字段覆盖率和 API usage 覆盖率；不能平均每条请求的百分比，也不能将缺少 API usage 的估算值当作已观察到的缓存未命中。字段来源以官方 usage 为准。[Chat Completions usage](https://api-docs.deepseek.com/api/create-chat-completion/)
- **成本与延迟：**同时比较每个完成任务的输入 miss/hit token、输出 token、调用次数、修订与重试，以及完整任务延迟的 p50/p95；有流式首 token 测量时单列 TTFT，没有时不以总耗时替代。按实验时适用的真实价格分别计费，预热、失败和被 gate 拒绝的任务仍计入总成本。
- **上线判断：**确定性与安全门槛先通过；再依据同一任务集的成本、p50/p95 延迟和质量对照决定是否发布。若命中率上升但任务总成本或延迟退化，保持基线并分析输入膨胀、输出变长、排队与额外调用。本文不提供尚未测得的提升比例。

新增测试入口：`tests/test_qq_cache_prefix.py`、`tests/test_cognitive_prompt_cache.py`、`tests/test_cache_accounting.py`、`tests/test_chat_cache_prefix_layout.py`。配合 QQ 预算/私聊/身份、compiler、validator projection/audit/context routing、emotion expression routing 的既有回归。运行测试使用模型替身，不触发付费模型请求。

本次最终组合验证：42 个相关测试文件，共 **747 passed**；修改的 Python 文件 Ruff 检查与 `git diff --check` 通过。另有两条既有 Starlette/httpx 弃用警告。此结果验证功能与计量契约，不代表供应商端缓存收益或模型质量已完成线上对照。
