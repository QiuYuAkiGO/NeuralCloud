# Linux 完整重构发布记录（2026-10-05）

按用户指令，将 NeuralCloud 当前工作区的完整源码、测试、工具、契约和文档冻结后发布到 `yumemiyuki`（`192.168.1.103`）。发布版本为 `20261005-refactor-130601`，运行目录为 `/opt/neuralcloud/current`。服务器沿用现有运行数据和私有配置；凭据不进入源码包。

## 发布内容与完整性

- 完整源码清单：1,410 个文件；运行包清单：419 个文件。包含本次冻结时尚未提交的工作区重构改动。
- Git 基线：`e6951ed3524bf6d1f2ab7925e15829bfd45f6a7d`。发布内容包含该基线之后的工作区改动；本轮不是 GitHub 推送或远端 CI 验收。
- 运行包 SHA-256：`a851b3b5d970c9370d537cfa85768bb3910486848a4460e9e44240f10fcca794`。
- 完整源码包 SHA-256：`c91d2a37204b1d1dad0062386e20e5962a1318145622e8ce5e8df6e1f2766ce1`。
- 服务器实际部署的 MistyAI 身体端 7 个契约 schema 的规范 JSON 哈希与生产锁一致。
- 排除密钥、SSH 凭据、虚拟环境、运行数据、个人目录和 `eval/reports` 运行产物。发布后生成的收据与文档另存于持久证据目录，保留冻结源码的完整性。

## 发布期间修复

依赖锁元数据与项目声明对齐；备份发现和路径映射纳入主回复备用配置。固定分词器资产恢复 Git 原始 LF 字节，并通过 `.gitattributes` 保持其校验和。修正调节 worker 的可控时钟测试、文件替换重试测试以及与当前协议不匹配的两处评估 fixture，保留生产校验规则。

实机启动采样发现 `GoalController.recover_contributions` 对每条历史结果重复解析大型 episode。恢复改为复用一次读取的 episode 投影，并在同一事务中提交贡献和游标。新增真实 journal / recovery-store 回归覆盖大型 episode、故障回滚及幂等重放。没有修改历史事实、强制改写生命周期或重发未确认的外部动作。

旧共享记忆的 UI 读取会同步阻塞认知服务的事件循环。读取改在线程中执行，GUI 的 systemd 就绪检查改用现有 `/api/ops/availability` 轻量接口，仍要求 Runtime 就绪。新增事件循环响应性和部署健康路径测试，保留网页的实际记忆内容与权限范围。

完整回归还发现发布评估工具调用 `logging.disable` 后没有恢复全局日志阈值，导致后续 5 项日志捕获断言失败。两个工具现在在 `finally` 中恢复日志阈值，证据工具同时恢复原 umask；新增错误路径回归。

## 验证结果

在 Linux 独立检查虚拟环境中执行主套件，避免将 pytest 等测试依赖写入生产虚拟环境：

- 完整主套件共 7,632 项：7,129 passed、498 skipped、5 failed，500.63 秒；5 项失败均为上述全局日志污染导致的日志捕获断言，已保留原始结果。
- 修复后的最终版本执行 174 项有顺序的关联回归，全部通过，25.90 秒；顺序包含先调用评估工具、再验证日志捕获，以及 UI、部署、Runtime 和 GUI 相关测试。
- 最终源树共覆盖 7,635 个不同主套件用例。完整运行之后增加的 3 项回归及所涉及模块纳入上述 174 项检查；没有把分段结果描述为一次完整最终绿灯运行。
- 独立发布工具测试 11 passed、2 skipped；Ruff 通过。
- Windows 的 UI / 部署关联回归 63 passed、1 skipped；先前真实 Linux Qwen 与 DeepSeek 故障备用调用证据仍保留在 [模型连接说明](../LOCAL_QWEN38_LLAMA.md)。本轮部署未追加付费模型比较。

脱敏原始摘要和发布收据见 [发布证据目录](../harness/evidence/linux-refactor-deployment-20261005/)。

## 数据保护与回滚

最终停止态快照 `/var/backups/neuralcloud/20261005-refactor-130601-state` 已验证：10,528 个文件、9,931,212,819 字节、21 个 SQLite 数据库，以及 749,099 条完整 journal 链记录。快照清单 SHA-256 为 `f9e35a763c00553444408d68b53a305c142256bf2349c042853846b173547205`。

快照前确认全部受管写入服务退出；旧 GUI 停止异常在备份完成校验后确认并清除。之前的维护快照仍保留。没有自动恢复旧数据库覆盖新状态。

旧版源码保存在经 371 个文件校验的 `/opt/neuralcloud/releases/20261005-refactor-113933-previous`，包含维护前实际使用的本地 Qwen 与 fallback 接线。发布标记的旧版目标指向该可校验副本。源码回滚与状态恢复分别处理，旧版备份可用不代表旧 Runtime 的历史启动问题已通过验收。

## 独立 QQ 客户端恢复

NeuralCloud 的 NapCat 实例启动后需要重新登录；首次扫码被 QQ 服务端以错误码 45 拒绝，原因是旧客户端 `3.2.23-44343` 版本过低。独立实例升级到 `3.2.34-53644`，保留 NapCat `v4.18.28`、账号数据目录和私有 OneBot 配置，其他 QQ 实例未重启。

安装包从腾讯 CDN 下载，187,185,496 字节，SHA-256 为 `436c65e1dd28422e128a21cbddb5fe4e2e6c079d7f27fec2541e014d38cc336e`，与 [Flathub 的 QQ 打包清单](https://github.com/flathub/com.qq.QQ/blob/master/com.qq.QQ.yaml) 中相同腾讯源包的校验和一致。采用链接 `https://qqdl.gtimg.cn/qqfile/QQNT/9.9.36/beta/9ee04bef/linuxqq_3.2.34-53644_amd64.deb`；[QQ 官网](https://im.qq.com/linuxqq/index.shtml) 的当前版本配置同为 3.2.34，但其另一安装包地址在本次下载中返回 403，因此记录实际采用的包路径。

新程序位于 `/opt/neuralcloud-napcat/releases/napcat-v4.18.28-qq-3.2.34-53644`。原程序保留，旧服务配置和完整停止态账号目录备份位于 `/var/backups/neuralcloud/qq-version-upgrade-20261005`。账号压缩包 1,139,069,566 字节，SHA-256 为 `51e8b8f18da9a833b5c478303636a5799a5be0beeac667b624b968d4bf950e2b`，已检查可读归档。未恢复或清空账号状态。

## 发布验收结果

发布已完成，`/opt/neuralcloud/current` 指向 `20261005-refactor-130601`。六个受管服务均为 systemd `active/running`：

| 服务 | 实机验收 |
| --- | --- |
| cognitive | 健康；主模型 `qwen3.8-27b` 可达 |
| homeostasis | 健康 |
| runtime | `RUNNING`；journal、认知、内稳态、LLM 与身体健康标记全部通过，无降级项 |
| gui | 就绪接口确认 Runtime `RUNNING`；完整 `/api/status` HTTP 200 |
| board | 健康 |
| qq_direct | `running=true`、`connected=true`；进程、发布目录和当前心跳核验通过 |

主回复使用 `http://192.168.1.105:8080/v1` 的 `qwen3.8-27b`；DeepSeek `deepseek-v4-flash` fallback 已启用，主通道最多占用 20 秒请求时限。辅助模型可达并预加载，记忆通道保持原路由。`deploy.json`、`gui-settings.json`、`main-fallback.json`、`manifest.json`、`homeostasis-profile.json` 五份私有配置的 SHA-256 与维护前一致，人设、权限和通道设置未改写。

完整 GUI 状态读取耗时 10.295 秒；在这次读取期间，认知健康接口 HTTP 200，耗时 0.057 秒，确认旧记忆读取已不再阻塞认知事件循环。公开网页和状态接口在未认证访问下均返回 401，原有访问保护保留。

本次最终启动按 strict 模式检查 749,099 条历史记录，journal 打开耗时 97.707 秒；已提交的模块恢复游标使模块恢复耗时降为 0.004 秒。此前首次历史模块恢复约 921 秒，未以跳过日志链或修改历史事实缩短启动。最终恢复无错误、监控 backfill 已完成，当前 `pending_actions=0`。Runtime 按正常恢复逻辑进入 RUNNING，本轮未手动强制恢复生命周期。

服务器持久证据目录为 `/var/lib/neuralcloud/release-evidence/20261005-refactor-130601`；发布后生成的收据及更新文档存放在该目录，冻结发布中的两个文件清单均重新校验通过。QQ 登录由用户在新版客户端二维码上完成，OneBot 只读身份核验通过；本轮未人工发送 QQ 测试消息，未将连接验收描述为长期运行或端到端回复质量验收。
