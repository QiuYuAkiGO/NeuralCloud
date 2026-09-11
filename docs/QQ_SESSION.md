# QQ 多人记忆与自主发言决策服务

`harness.qq.session` 将已核验的 QQ 观察、持久多人记忆和 `speak / wait` 决策接在一起。它是独立的本机会话服务；新观察提交成功后自动判断是否开口，只有 `speak` 才由当前 Misty 人设模型准备正文。`wait` 的正文为空。

服务本身接收观察和执行决策，也不启动普通 NeuralCloud Runtime。经操作者授权后，可通过独立 [QQ 自动采集与发送进程](QQ_WATCHER.md) 连续截屏、点击头像核验作者、写入同一持久记忆并发送新决定的正文。`autonomous_decision_enabled=true` 单独只表示发言决策；是否正在自动采集和发送，还要检查对应状态字段与新鲜的 watcher 心跳。

## 启动与状态

私有状态位于 `%LOCALAPPDATA%/NeuralCloud/QQDesktop`。`session.json` 指定已存在的 `memory_db`、`token_file`、`allowed_group_ids`、本人 `self_member_id`、`decision_enabled` 以及现有模型配置、人设路径。令牌只通过文件引用读取；不要把令牌值、真实观察正文或资料卡放入仓库。

```powershell
& .\tools\qq\Start-QQSession.ps1
```

默认端口 `8789`，仅监听 `127.0.0.1`。启动器检查已经运行的服务是否与私有配置一致，匹配时直接返回；不会停止其他占用者。日志写入同一受限目录。没有新增系统开机自启动项。

在仓库根目录，也可前台执行：

```powershell
$private = Join-Path $env:LOCALAPPDATA 'NeuralCloud\QQDesktop'
.\.venv\Scripts\python.exe -m harness.qq.session --config (Join-Path $private 'session.json')
```

所有 API 均要求 `Authorization: Bearer <session.token 的内容>`。`GET /v1/status` 返回持久存储、已知成员数量、决策是否启用，以及自动截屏、自动发送和普通 Runtime 是否启动。健康状态包含服务启动时间和数据库路径，便于确认重启后仍读同一份记忆。

## 消息进入记忆和决策

1. `POST /v1/profiles` 接收 `QQVerifiedProfileInput`，保存经过核验的群和成员资料来源；不会调用模型。
2. `POST /v1/observations` 接收 `QQVisualObservationInput`。先事务落盘，再在 `decision_enabled=true` 时调用多人发言决策。重复同一截图条目不会再次触发模型。没有显式查询时，决策入口从已核验触发成员的近期观察推导最多 512 字符的历史查询；完整原文仍保留，返回 `query_source`、`effective_query` 和截断标记。只有表情或标点时不强行生成检索词。
3. `GET /v1/groups/{group_id}/memory?query=...` 读取该群各成员近期与较早相关记录，返回选取依据和省略数量。仅允许配置的群号；查询不是向模型发送指令。
4. `POST /v1/groups/{group_id}/decisions` 可显式请求一次决策，正文允许 `trigger_member_id`、`query`、`request`。这里的 `request` 是操作方的检查请求，不能伪装成已经收到的群消息。

新观察已保存、模型却失败时，响应的 `memory` 仍保留真实写入结果，`decision_status=failed` 或决策结果为明确的等待；不会因模型失败伪称记忆未保存，也不会自动重发。错误输出不包含模型原始错误正文或密钥。

## 长期记忆与交付边界

人物使用 `(group_id, member_id)` 隔离，显示名只是可变资料。每名成员分别获得近期和历史配额；中文检索扫描已存历史中的原文与带来源的作者声明，可以命中近期窗口之外的记录。原始来源和昵称变化保留，账号关系声明不自动合并人物。存储及检索机制见 [QQ 视觉人物记忆](QQ_VISUAL_MEMORY.md)。

同输入决策会从 SQLite 返回原 `decision_id`，并标记 `new_decision=false / replayed=true`。调用方不能把缓存的 `speak` 当作新发送任务。已考虑的观察与实际回应过的观察分别记录；只有独立核验的发送回执才能表示已经发言。回执与决策机制见 [QQ 回复与自主决策](QQ_REPLY_DRAFT.md)。

本服务不提供发送接口。普通 Runtime 的内存投递回显不等于 QQ 消息回执。配置 `watcher_status_file` 后，状态接口汇总同群、同本人账号且不超过 30 秒的独立 watcher 心跳；缺失、过期或范围不匹配时自动采集和发送均显示关闭。API 手动请求所得的历史草稿不会被 watcher 自动发送。
