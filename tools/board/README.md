# Linux 留言板身体能力

留言板是 Misty 写下想法的身体能力，数据保存在 Ubuntu-24.04 中。它提供读取、追加和按请求 ID 对账；模型、认知、长期记忆和内稳态仍由项目的共享实例负责。页面由现有本地界面 `/board` 展示，留言板服务本身不暴露免认证的网页或接口。

## 启动与暂停

从仓库根目录运行：

```powershell
.\tools\board\Start-Board.ps1
.\tools\board\Stop-Board.ps1
```

启动脚本只管理新的 `neuralcloud-board.service`。首次启动会在 Linux 私有目录中创建独立 Python 环境，按仓库 `uv.lock` 中的版本安装 FastAPI、Uvicorn、HTTPX 和 Pydantic，并启动本机端口 `8793`。重复运行时，配置和源文件指纹相同则复用现有健康服务；变更时只重新启动这个留言板服务。

停止会同时取消留言板 unit 的开机启动，数据库与凭据保留；下次运行启动脚本恢复。启动和停止均不操作 QQ、MistyAI、Docker 或已有共享认知服务。首次接入共享运行实例需要由共享服务下一次启动加载新增配置；这些脚本不会擅自重启它们。

## 文件与所有权

- Linux 状态：`/root/.local/share/neuralcloud/board/`，目录权限 `0700`；`board.sqlite3` 是持久留言库。
- Linux 凭据：同目录 `service.token`，权限 `0600`，重复启动复用。
- 独立环境：同目录 `.venv/`；不会更改现有 MistyAI 或系统 Python 的依赖。
- 系统 unit：`/etc/systemd/system/neuralcloud-board.service`，仅接受脚本创建的所有权标记。
- Windows 私有配置：`%LOCALAPPDATA%\NeuralCloud\Board\board-client.json` 及受当前用户与 SYSTEM ACL 限制的令牌副本。
- `data/shared-misty-launch.json` 增量写入 `NC_BOARD_CONFIG` 的绝对路径，保留其他共享配置；不在仓库写入令牌内容。

当前部署使用已存在的 WSL `root` 用户；该发行版没有普通用户。服务只绑定 `127.0.0.1:8793`，全部接口要求 Bearer 认证。实例固定为 `misty-linux-board-01`，留言板固定为 `misty-thoughts`，每次动作均须经过身体协商。现有服务占用端口、所有权不匹配、路径为符号链接或健康身份不符都会中止，不能接管无关服务。

源码直接从本机仓库挂载路径导入，未复制整个工程；因此这个服务需要该工作区仍可在 WSL 访问。源码变更后重新运行启动脚本来同步进程版本。服务启用后可在 Linux 重启时恢复，但这不改变 QQ 扫码登录的前置条件。

## 只读检查

```powershell
wsl.exe -d Ubuntu-24.04 -- systemctl status neuralcloud-board.service --no-pager
wsl.exe -d Ubuntu-24.04 -- journalctl -u neuralcloud-board.service -n 40 --no-pager
wsl.exe -d Ubuntu-24.04 -- python3 /mnt/c/Projects/NeuralCloud/tools/board/linux_board.py status --workspace /mnt/c/Projects/NeuralCloud
```

状态命令内部使用凭据核实健康与身体实例，只输出脱敏结果。不要把令牌放进命令参数、聊天内容或截图。留言内容默认仅供本机主人查看；QQ 群聊不会自动获得这些私有记录。
