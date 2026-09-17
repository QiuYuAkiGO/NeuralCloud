# QQ 群聊插话策略切换（2026-09-17）

本次将群聊参与偏好恢复为 v17 的较克制门槛：先判断是否值得开口，多人彼此闲聊、没有新需求或缺乏具体内容时可等待。direct/ambient 冷却仍为 10/60 秒。这是模型判断偏好，不是固定回复概率。

当前生产已前进到 `20260917-json-body-guard-v1`（群提示仍为 `qq-group-turn-taking-v23-substance`），并拦截把 gate JSON 当正文发出。不要直接激活本节两个旧 release，以免丢失其后的留言板/日记修复、言之有物规则与 JSON 拦截。回退请使用 `20260917-substance-v1`。

只替换群 gate 开头的参与规则，保留最新版本的重复回答判断、正文、图片、发送保护和独立 DeepSeek 情绪通道。群 1060979317 的 observe/send 均保持 false。

## 两个保留版本

- 较克制：`20260917-participation-conservative-v1`，群提示标记 `qq-group-turn-taking-v22-conservative`。
- 较积极（本次回调前）：`20260917-emotion-deepseek-v1`，完整保留现有发布目录。

两份源码发布包和 SHA-256 清单额外封存于服务器 `/var/backups/neuralcloud/participation-variants-20260917/`，其中 `variants.json` 记录包路径和哈希。包不包含密钥，使用现有私有配置。

## 以后恢复较积极版本

在服务器执行：

```bash
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json activate --release 20260917-emotion-deepseek-v1
sudo /opt/neuralcloud/current/.venv/bin/python -m harness.deploy --config /etc/neuralcloud/deploy.json verify
```

切回较克制版本时，将 release 参数替换为 `20260917-participation-conservative-v1`。切换会重启服务；不会恢复历史数据库或覆盖当前群权限配置。以后若发布了其他功能，应只移植参与规则，避免直接切旧 release 丢失后续代码改动。

## 验证

- 对照服务器历史 `20260916-context-v1` 提取原始 v17 参与规则。
- 断言新规则等于旧参与前缀加当前剩余规则，保证正文等后续保护不回退。
- 群聊决策及简短回复回归 111 项通过。
- 线上已切换至较克制版本，368 个发布文件校验通过，六个业务服务均为 active/running；群 1060979317 的实时状态仍为 observe=false、send=false。
