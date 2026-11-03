# Misty 口语表达对照试验

本目录实现离线渠道回放；它不是生产回复策略或部署脚本。

## 基线与隔离

- 基线：`resident-20261028-context-budget-v1`，生产安装的 `Session`、`AgentLoop`、`PiAi` 适配器；同一模型参数、同一工具 schema。
- 真实案例通过生产 `Session.create(...).deriveMessages()` 重构历史表面。当前捕获的系统提示与请求配置覆盖历史提示，因此明确标为重构，而不是历史 HTTP 请求的逐字复现。
- 工具只注册同 schema 的模拟处理器，不挂载 QQ 插件、生产记忆或状态客户端。`misty_send` 仅记录候选文本并返回带 `simulation` 标识的模拟结果。没有冻结读取证据时终止为 `unevaluable`。
- 图片按已记录 attachmentId 从持久内容存储复制并核验 SHA-256，附件服务只挂载隔离副本。不要让回放直接挂载生产附件目录。
- 真实案例、原始请求、附件和盲评材料属于私有试验数据，不提交到 git。模型凭据仅在运行时从既有配置指定文件读取，不写入产物。

## 流程

1. 在具备只读生产日志权限的环境执行 `capture.py CONFIG PRIVATE_OUTPUT`。
2. 使用固定生产 Node 运行 `project.mjs HARNESS_DIRECTORY PRIVATE_OUTPUT`，验证 Session 投影。
3. `prepare.py` 使用本次本地 `data/misty-oral-20261028/projected-cases.json` 生成 36 例、五组提示及 540 轮固定顺序。B 是表达规则、C 加六组示例、D 删除明确重复的追问限制、E 加末尾提醒。A 原样保留捕获的基线系统提示。
4. 将 cases/design 置于隔离运行目录，运行 `freeze_images.py` 冻结附件；运行 `replay.mjs HARNESS_DIRECTORY PRIVATE_OUTPUT pilot`，再运行 `check_gate.mjs PRIVATE_OUTPUT`。
5. 门禁通过后运行 `replay.mjs HARNESS_DIRECTORY PRIVATE_OUTPUT formal`。并发为 3。启动每轮前持久化 running 记录，已有记录一律不自动重跑；中断留下的 running 必须先核实，不可当作完成。
6. `review.py PRIVATE_OUTPUT` 生成匿名左右三次对照、12 对入口、全部 144 对、评分导出 HTML、独立解盲键及描述性统计。

## 本轮修复记录

首次试验拦截注册未生效，被六例契约检查检出，`pilot-0` 等记录全部无效。改为 `llm/stream` 钩子调用固定版本适配器，保留生产 AgentLoop 工具循环。通过捕获实际边界消息、工具 schema 与配置逐项相等确认替换生效。

正式第一批 225 轮图片案例遇到 `UNSUPPORTED_CONTENT`：缺少 durable attachment service。统一冻结附件并验证六个含图案例后，`repair_images.py` 将此完整错误集合移入 `attempts/missing-attachment-service`，再重跑全部受影响轮次。不能选择性保留同一条件下更好的一次；原失败记录必须保留并披露。

`replay.mjs` 使用当前固定版本内部 `adapterStream` 接口以记录真实边界请求。升级 Harness 必须重新验证该接口和六例门禁，不能假设跨版本兼容。

## 评价边界

- 五项人工评分：自然度、具体性、简洁度、人设一致性、回应适当性。每个案例的三次输出整体比较，不能把三次采样当三个独立案例。
- 60% 非平局偏好门槛只能由人工评分计算；没有评分时 `winner=null`，不将自动长度统计视为胜出。
- 不可评、失败、无发送动作与显式等待分别看待。单轮 `misty_send` 后再 `misty_wait` 不计作沉默。
- HTML 首屏 12 对使用固定顺序选取六个真实案例和六个合成案例，各不重复；可切换全部配对。页面不向外发送评分，通过下载 JSON 导出。
- 先保留现行生产策略；本试验不会部署候选规则。

## 校验

```powershell
python tools/production_acceptance/misty_oral/test_design.py
node --check tools/production_acceptance/misty_oral/replay.mjs
node --check tools/production_acceptance/misty_oral/project.mjs
node --check tools/production_acceptance/misty_oral/check_gate.mjs
```

本轮私有产物：`C:/Projects/NeuralCloud/data/misty-oral-20261028/`。远端隔离产物：`/var/tmp/misty-oral-20261028/`。这些目录包含真实聊天，不作为公开报告分发。

人工评分导出后运行 `python tools/production_acceptance/misty_oral/score.py PRIVATE_OUTPUT SCORES_JSON`。全部案例都有已评或不可评标记、每组至少 24 个有效案例才允许推荐。发现严重回归先人工复核，不自动放行。非平局偏好率相差不超过 5 个百分点视为接近，优先 B、C、D、E 中改动更少者。此阈值是本试验的操作约定，不代表统计显著性。
