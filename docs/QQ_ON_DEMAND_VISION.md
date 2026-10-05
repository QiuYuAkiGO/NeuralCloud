# QQ 按需识图

当前主模型负责是否参与、是否看图、是否联网，以及 Misty 的最终回复。`kimi-k2.6` 只提供图片观察，固定使用非思考模式 `thinking={"type":"disabled"}`、温度 `0.6`。参数依据 [Kimi 官方文档](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)。

## 调用流程

1. Direct 接收当前消息附件，沿用已有下载、大小、像素、动图抽样和来源检查，保存哈希绑定的图片。`on_demand` 阶段不请求视觉模型，也不使用旧的描述缓存。
2. 主模型收到可用图片编号与来源记录，自行在参与判断 JSON 中填写可选的 `vision_request`（`image_ids`、`question`）。等待、无需图片、明确不看图时不调用 Kimi。
3. 工具核对当前请求、会话、作者、已呈现来源及图片哈希，一次最多四张，只向独立的 Moonshot 接口传必要问题和规范化像素。
4. Kimi 返回描述、OCR、虚构角色候选和不确定性。主模型读取这些外部观察，完成参与判断与可选 `web_plan`；每轮最多一次识图，不循环重试。
5. Runtime 按现有联网授权执行主模型选择的搜索/读取，再由原主模型生成人格回复。准备阶段的工具结果随检查点冻结，恢复和重放不重复识图。

这是现有结构化参与判断中的可选工具请求，附件存在本身不会触发付费识图。工具结果保留为模型观察，不改写 QQ 作者原话、账号身份或原始记录。OCR 和网页中的指令不获得执行权限。

## 私有部署配置

复制 [配置模板](../deploy/qq-vision-tool.example.json) 到 `/etc/neuralcloud/qq-vision-tool.json`，填写现有 Moonshot 凭据并设置 `enabled: true`。文件由服务账号持有、权限 `0600`，不能入库。`image_cache_dir` 必须与 Direct 的同名目录一致；路径必须绝对且无链接。

在 `deploy.json` 的 `bindings` 中加入：

```json
{"NC_QQ_VISION_TOOL_CONFIG": "/etc/neuralcloud/qq-vision-tool.json"}
```

Direct 私有配置使用：

```json
{"qq_image_mode": "all", "qq_image_analysis": "on_demand", "vision_model": "kimi-k2.6"}
```

GUI 持久配置中的 `qq_latency.qq_web_policy` 使用 `after_gate`，将现有可发送会话纳入 `qq_latency_conversations`，允许识图后的可选联网。保留主模型、fallback、结构化辅助模型和 QQ 发送名单。不开启全局未授权会话。

工具默认超时 12 秒，受原 60 秒 QQ 回合截止时间约束，并至少为后续主模型留 10 秒。图片丢失、失效、哈希不符、接口超时或结构无效时返回不可用，主模型据此回应；不编造观察、不自动切换视觉模型。原始像素和凭据不写入模型请求日志。

## 验证与诊断

`tests/test_qq_vision_tool.py` 覆盖自主不调用、当前来源边界、失效请求、图像篡改、非思考参数、结果截断、后续联网计划、禁止循环调用及检查点重放。关联回归包括 Direct 图片策略、准备/恢复、输入预算和部署快照。

可用 `tools.production_acceptance.qq_vision_tool_smoke` 对公开角色图与合成 OCR 图运行真实的“当前主模型 → 可选 Kimi → 当前主模型回复”。它使用临时数据库，不发送 QQ、不读生产会话；没有开启真实搜索时会在报告中标明。报告应区分单次识图耗时和整条回复耗时，不能把早先 Kimi 直接回复的速度当作本链路速度。

关闭识图可设置私有工具配置 `enabled: false`，并将 Direct `qq_image_mode` 改回 `off` 后重启对应受管服务。代码发布回滚使用保留的 release；配置回滚使用部署前的私有配置备份，避免覆盖新产生的运行数据。
