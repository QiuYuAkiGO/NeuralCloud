# 联网搜索身体

联网通过独立 `web.search.v1`、`web.read.v1` 身体能力接入现有版本协商与 Runtime 执行链。首个适配器是 Tavily：搜索使用 `POST /search`，正文使用 `POST /extract`。默认不开启网络调用。

## 使用

1. 更新并重启共享 Runtime 和 GUI，在原 GUI 地址打开 `/web`，或点击“联网搜索”入口。
2. 保留默认端点 `https://api.tavily.com`，填写 API Key、请求限额。
3. 点击“测试连接”验证搜索和正文提取。测试真实请求供应商并计入本地请求额度，不激活配置。
4. 显式选择联网总开关、对话授权和自主授权，保存后按页面提示重启共享服务。运行中的实例继续使用原配置与密钥。
5. 重启后确认生效版本与保存版本一致。对话可提出需要联网的问题，也可提供公开网页链接。

自主授权还受 manifest 原有 `autonomous_capabilities.enabled`、安静期、机会间隔、目标调节和白名单约束。联网配置页不会替其他通道打开全局自主开关。搜索后仍允许等待、不发言；QQ 仍由原 watcher 校验来源并执行最终发送。

连接测试成功仅证明本次 API 调用可用。启动和状态页面不执行付费健康探测，未经请求的能力显示未验证；后续真实请求更新各能力健康。

## 配置与权限

Runtime 在实际 `state_dir/web` 保存配置、独立密钥文件及请求账本。`NC_WEB_CONFIG` 可指定绝对路径的配置索引；仅传入 Runtime 进程。GUI 始终通过认证的 Runtime 控制面保存，密钥不放在浏览器持久存储，也不回显给浏览器。

配置版本和密钥文件均不可变；当前实例锁定启动时版本。修改时的 `base_revision` 用于防止覆盖另一操作员的保存。空白密钥表示保留，显式清除密钥需同时关闭联网授权。

连接设置包含 `provider`、`base_url`、`body_instance_id`、`timeout_seconds`、`max_results`、`daily_requests`、`autonomous_daily_requests`。供应商首期固定为 `tavily`；自定义 HTTPS origin 必须实现相同搜索与提取协议。

授权独立存入配置版本中的 `authorization`，启动时合入冻结 manifest：

```json
{
  "web_access": {
    "enabled": true,
    "conversation_enabled": true,
    "capability_requirements": [
      {"capability": "web.search.v1", "required": false},
      {"capability": "web.read.v1", "required": false}
    ]
  }
}
```

自主授权只调整 `owners.web` 和联网两项白名单。模型无法更改配置、授权、端点、请求头、密钥、身体身份或截止时间。未配置和旧 manifest 保持关闭；legacy 独立开发 GUI/QQ 与 Grounded Agency 冻结研究链路不接入联网。

## 执行与证据

普通对话采用 `PrepareWebStep → WebController → PrepareResponse`；QQ 最后进入原 `PrepareQQResponse`。WebStep 使用同一个主模型及渠道范围内的人格、记忆与观察，只返回能力选择，不提前准备回复或写入 QQ 发送决定。

自主机会通过原 `CapabilityController` 选择 `observe`，结果回填 `channel_result`，持续目标可在下一步读取搜索结果。正文参数是有作用域的 `source_refs`，只有同一轮用户提供的链接或相应自主目标已获得的搜索结果可以解析为 URL。正文由供应商提取，Runtime 不直接爬网页。

来源包含标题、URL、获取时间、可选发布时间、摘要/正文、截断标记及来源类型（用户链接、搜索、读取）。尚未读取的用户链接可供选择，但不作为已取得的证据。网页数据作为 user 数据段进入模型，固定联网规则单独编译；网页不能成为 system 指令。仅有摘要时不能声称读取了全文，缺少材料时不能声称完成核实。原回复提交和 QQ 回执链路继续负责正式对话记忆，获取网页不会自动写入确证知识或给予内稳态奖励。

## 限额和恢复

- 每回合最多两次搜索、一次正文批量读取，正文最多三页；搜索默认五条、配置上限不超过十条。
- HTTP 请求默认 12 秒；普通对话总限时 60 秒，联网阶段最多前 30 秒，最终回答预留 30 秒。QQ 继承原绝对截止时间，不足以保留最终生成时间时跳过联网。自主机会保持原期限。
- API 响应流最多 2 MiB，来源正文持久记录每页最多 8,000 字符，外部上下文最多 12,000 字符并再服从模型 token 预算；裁剪保留完整来源元数据。
- 默认每日 100 次请求，其中自主最多 20 次，按 manifest 时区计日。HTTP 发送前原子预占，失败与未知请求也计数。连接测试计入总额度。供应商返回的 credits 单独保存在调用记录，不把请求数当作实际账单。
- 429、服务错误和网络异常进入按能力冷却；鉴权失败阻止后续调用。没有自动 HTTP 重试、自动换供应商或跨重启重放 POST。
- 已完成请求按调用 ID 幂等返回；取消、超时或中断后的已发送请求保留未知状态和额度，旧对话回合不会因此获得新执行期限。迟到或授权失效的结果不进入当前对话。

`calls.sqlite3` 是私有请求和来源账本；Runtime journal 保存请求编号、用量与引用摘要。关闭能力并重启可停止新调用，不删除旧调用与证据记录。

## 控制面与验证

认证接口为 `GET/PUT /v1/web/config`、`POST /v1/web/test`、`GET /v1/web/status`；GUI 通过同源 `/api/web/*` 代理。`GET /v1/capabilities` 同时呈现联网身体和自主目录。

自动测试覆盖供应商协议、版本协商、权限复核、持久额度、取消/恢复、来源作用域、共享认知结果回注、配置版本和 GUI。`tests/test_web_runtime.py` 使用真实 Runtime、共享认知与模拟 HTTP 验证“搜索→正文→一次正式回复”，不需要真实 API Key，也不会发送 QQ 消息。

2026-09-12 相关回归 **399 passed、1 skipped**，后续启动与认知复验 **80 passed**（与前者重叠），Ruff 和隔离浏览器验证通过。详见[本次验证记录](milestones/evidence/web-body-implementation-2026-09-12.json)。

2026-09-12 已使用用户配置的 Key 完成真实 Tavily 搜索与正文提取，各一次请求，见[在线 API 验证](milestones/evidence/web-body-live-api-2026-09-12.json)。随后按用户请求重启共享服务：Runtime 已运行，联网配置版本 1 已生效，对话与自主授权均已开启，见[启动验证](milestones/evidence/web-body-startup-2026-09-12.json)。真实主模型对话与自主在线验收仍待完成。

协议参考：[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)。
