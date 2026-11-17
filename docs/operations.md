# 运行与排障

命令由 [__main__.py](../src/neuralcloud/__main__.py) 和 [pyproject.toml](../pyproject.toml) 核对。本轮没有执行以下启动命令；使用前确认目录和端口不属于现有实例。

## 隔离运行记忆服务

Python 要求 >=3.12,<3.13。普通服务依赖 FastAPI、Pydantic 和 Uvicorn；archive、retrieval、dev 是独立可选依赖。先在仓库根目录安装普通依赖：

```powershell
uv sync --locked
```

下面是 Windows 下的新建隔离实例示例。只对从未存在的新数据库执行 init；token.txt 需要预先准备至少 32 个字符的随机令牌，不写入 Git。

```powershell
uv run neuralcloud-memory init --database C:/Temp/neuralcloud-sandbox/memory.sqlite3 --character sandbox
uv run neuralcloud-memory serve --database C:/Temp/neuralcloud-sandbox/memory.sqlite3 --character sandbox --token-file C:/Temp/neuralcloud-sandbox/token.txt --port 18765
```

服务绑定 127.0.0.1；默认端口为 8765，例子选用 18765 避免直接使用默认端口。令牌放在 HTTP Authorization 头中，不放 URL。`init` 使用独占创建，不能用于修复或覆盖已有数据库。

## 检索配置的依赖顺序

这些变量由入口读取，变更后需要在相应进程的启动环境中生效：

- `NC_RETRIEVAL_V2=1`：使用 Retriever；未设置时走基础存储查询。
- `NC_IDENTITY_INDEX`：可选身份索引文件，指定后文件必须存在。
- `NC_SEMANTIC_URL`：可选语义服务地址；设置时必须同时提供 `NC_SEMANTIC_TOKEN_FILE`。
- `NC_HYBRID_ENABLED=1`：开启 hybrid 配置；是否可执行语义查询仍取决于服务与索引状态。
- `NC_SEMANTIC_THRESHOLD`、`NC_KEYWORD_THRESHOLD`：数值阈值，入口默认均为 0。
- `NC_MEMORY_WORKFLOW=1`：仅在 Retriever 已启用时包装证据工作流，同时必须提供 `NC_EVIDENCE_INDEX`。
- `NC_WORKFLOW_DEADLINE_MS`：工作流期限，默认 1800 ms。

这些开关不负责准备模型、构建索引或启动语义服务。环境配置不完整时应修复配置，不通过删除数据库绕过启动检查。

## 先按错误定位

`INITIALIZATION_REQUIRED` 表示数据库文件不存在；先确认路径是否正确。`CHARACTER_MISMATCH` 表示指定角色与数据库不符。`EXPLICIT_MIGRATION_REQUIRED`、`MIGRATION_NOT_COMPLETE`、`EXPLICIT_INDEX_MAINTENANCE_REQUIRED` 要求检查显式迁移和索引状态。

`INSTANCE_ALREADY_OWNED` 表示系统锁获取失败，先检查其他进程是否持有该数据库；删除 owner 文件不能安全释放另一个进程的锁。409 应读取 detail 中的业务拒绝原因，503 应查存储可用性；不要把两者当成一次成功写入。

查询异常按 API → Retriever → SemanticClient / EvidenceIndex → MemoryWorkflow 顺序排查。health 的 capability 字段用于说明配置，实际查询 diagnostics 和语义服务 READY 状态才说明本次路径是否可用。

## 备份与入口边界

`neuralcloud-memory backup --database <db> --character <id> --target <new-path>` 也是通过 MemoryStore 打开数据库，因此受到同一所有权锁约束；不要假定可与占有该库的服务同时运行。具体写入行为见 [MemoryStore.backup](../src/neuralcloud/storage.py)。

本地旧 start/stop、launch 和 dev 启动器已删除，不再提供旧 GUI、Runtime、Cognitive、Homeostasis 启动链。启动新记忆服务使用上面的显式命令。

FRP 和手机入口设施相关工具仍保留；它们的存在不是部署授权。本轮没有改动服务器上的代理路由、systemd 单元或发布目录。
