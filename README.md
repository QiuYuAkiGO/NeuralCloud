# NeuralCloud：源码阅读入口

这套文档依据当前工作区源码重新编写（2026-11-16），描述代码能够做什么以及从哪里读起。本轮清理未运行测试、未连接或修改线上服务；这里的命令和接口说明不构成部署验收结论。

NeuralCloud 默认构建的是 `neuralcloud-memory`：一个按角色隔离、以 SQLite 保存原始事件和修订的本地记忆服务。Misty 的 JavaScript 插件通过 HTTP 调用它。本地旧 Python 角色运行时、GUI、基础设施及其专用启动和验证配套已于 2026-11-17 删除。MistyAI 的 JavaScript 宿主与这里的旧运行时不是同一套代码；新记忆服务及必要接入继续保留。

## 阅读顺序

1. [架构与源码导航](docs/architecture.md)：目录职责、写入和查询链路、旧模块边界。
2. [接口与接入](docs/interfaces.md)：HTTP 契约、幂等与修订、插件和手机渠道。
3. [运行与排障](docs/operations.md)：依赖、隔离启动、检索配置和故障定位。
4. [维护与修改指南](docs/maintenance.md)：归档边界、源码修改入口以及人和 AI 的工作方式。

## 首先打开的源码

- [pyproject.toml](pyproject.toml)：Python 版本、依赖、默认打包和测试范围。
- [命令行入口](src/neuralcloud/__main__.py)：`init`、`serve`、`backup` 与基础契约导出。
- [HTTP 服务](src/neuralcloud/api.py)：实际公开接口及错误映射。
- [存储实现](src/neuralcloud/storage.py)：数据库创建、事务、幂等、查询和修订。
- [Misty 记忆插件](plugins/dsh-neuralcloud/src/index.mjs)：客户端、持久发送队列和工具绑定。

旧说明、设计稿和文字报告已从当前工作区的项目文档集合中移除；需要追溯旧提交时使用 Git 历史。保留的 JSON、脚本、样本和许可证是数据或程序资产，不代表当前使用说明。
