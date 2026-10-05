# 本机 Qwen3.8 与 llama.cpp

部署目标：Windows RTX 5090 32GB / Ryzen 9 9950X / 64GB RAM，为 NeuralCloud 提供本机和 Linux 局域网调用的 OpenAI 兼容接口。模型、运行库和密钥放在系统盘 NVMe，避免占满项目 D: 盘。

## 模型与运行库

- 模型：`ggml-org/Qwen3.8-27B-GGUF`，`Qwen3.8-27B-Q4_K_M.gguf`，18,973,870,528 字节。
- 固定 revision：`71bc7b627595dc8a91039addd9c791ae548d6747`。
- SHA-256：`c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747`。
- llama.cpp：`b11146` / `7fe450e19`，Windows x64 CUDA 13.4。
- 根目录：`%LOCALAPPDATA%\NeuralCloud\llama.cpp`；权重在 `models`，程序在 `bin`，日志在 `logs`。
- 视觉投影：`mmproj-Qwen3.8-27B-Q8_0.gguf`，629,247,008 字节，SHA-256 为 `2e968a6af97ce35d8971890b257b9b7edabf20ad91450501fa53162a19ee33eb`。支持文本与图片输入；QQ 图片理解默认复用主端点，因此一并提供投影文件。

上游：[Qwen 模型说明](https://huggingface.co/Qwen/Qwen3.8-27B)、[llama.cpp 团队的 GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF)、[固定版本发布](https://github.com/ggml-org/llama.cpp/releases/tag/b11146)。

## 启停与接口

在项目根目录的 PowerShell 中：

```powershell
.\tools\deploy\install-llama-local.ps1  # 断点下载、哈希校验、启动及真实模型探针
.\tools\deploy\llama-local.ps1 start
.\tools\deploy\llama-local.ps1 status
.\tools\deploy\llama-local.ps1 stop
```

后台启动，不打开额外窗口。通过 `Win32_Process.Create` 脱离调用终端的进程作业，避免终端清理时结束模型服务；日志写入安装目录的 `logs/server.log`。启动脚本会检查下载验证记录和端口占用，停止时核对进程路径与启动时间。脚本不会注册开机自动启动。

`tools/deploy/templates/qwen38-neuralcloud.jinja` 从固定 revision 的原生 GGUF 模板派生，只允许追加的 system 消息以原角色、原顺序渲染，保留原有视觉与工具格式。原生模板会拒绝 NeuralCloud 的后置协议、情绪和修改指令；启动脚本将兼容模板复制到安装目录后加载。

- 本机：`http://127.0.0.1:8080/v1`
- Linux：`http://192.168.1.105:8080/v1`
- 模型名：`qwen3.8-27b`
- API Key：读取 `%LOCALAPPDATA%\NeuralCloud\llama.cpp\api-key.txt`，不要写入版本库。
- 健康检查：`GET /health`。
- 模型列表：`GET /v1/models`；推理：`POST /v1/chat/completions`。

Windows 防火墙规则 `NeuralCloud-Llama-8080-LAN` 仅在 Private 网络允许 Linux `192.168.1.103` 访问本机 `192.168.1.105:8080`。IP 变化时须更新客户端地址和规则。

## NeuralCloud 连接设置

2026-10-05 已将 Linux 认知服务的主模型切换为本机 `qwen3.8-27b`，并配置 DeepSeek Flash 为备用。运行中的 `/v1/ui/snapshot` 已确认主模型地址、可达状态和备用通道。生效文件为 `/etc/neuralcloud/gui-settings.json`；部署配置 `/etc/neuralcloud/deploy.json` 引用私有 `/etc/neuralcloud/main-fallback.json`（`0600`），同时将 QQ 输入预算改为 60,000。当前人设、辅助模型、记忆模型和情绪配置均保留。

Windows 的 `data/gui_settings.json` 已改为 live、`http://127.0.0.1:8080/v1`、`qwen3.8-27b`；`.env` 和 `launch.json` 同步主模型、备用配置路径及输入预算。备用凭据保存在 `%LOCALAPPDATA%/NeuralCloud/llama.cpp/main-fallback.json`，不放入仓库。Linux 原有连接备份 `/home/qiuyu/.config/neuralcloud/local-llama.env` 仍保留。

备用机制适用于主通道 `complete_chat*`，包含正文及走主通道的 goal 决策；辅助 `complete_json` 和独立记忆通道保持原路由。连接错误、HTTP 超时、401/403/404/408/429、5xx 或空白正文触发一次备用请求；两次尝试使用相同输入和总时限，Qwen 的请求时限最多 20 秒，并为备用预留最多 10 秒（短时限按一半分配）。错误来自预算服务、已耗尽的全局时限、400 参数错误或应用校验失败时不会追加付费调用。Qwen 的合法结束标记仍由现有正文协议处理。下一次请求继续优先尝试 Qwen。

备用默认最多生成 2,048 tokens；调用者已指定的输出上限会被保留。每次物理请求沿用现有生产预算检查和 token 计量，`model_requests.jsonl` 记录实际模型，备用标记 `attempt=2`，无需把密钥或正文写入故障日志。

切换验证：本地 72 项模型与计量回归、32 项启动器检查通过；Linux 使用真实当前人设的隔离 `CharacterModel.generate` 经 LAN 调用 Qwen，632 输入 / 13 输出 tokens，约 1.402 秒；仅在隔离客户端中注入断连后，真实 DeepSeek 备用返回正文，23 输入 / 3 输出 tokens，约 0.702 秒。本机真实 Qwen 正文也通过。上述隔离验证未发送 QQ 消息、未写线上记忆。首次切换阶段在 Windows 执行套件；后续完整 Linux 发布使用独立检查虚拟环境执行主套件和关联回归，生产虚拟环境未安装 pytest 等测试依赖。

完整重构发布 `20261005-refactor-130601` 已部署到 Linux。维护前 Runtime 的历史恢复问题已修复，当前六个服务均为 active，Runtime 为 RUNNING，GUI 完整状态 HTTP 200，QQ worker 已连接并核验当前进程、心跳和发布目录；认知服务确认 Qwen 可达、DeepSeek fallback 启用。独立 QQ 客户端因旧版本被服务端拒绝登录而升级，用户扫码后恢复连接。本轮未人工发送 QQ 测试消息；连接验收与真实端到端回复质量分别记录。部署结果与停止态备份见 [完整 Linux 发布记录](refactor/linux-refactor-deployment-20261005.md)。首次主备接线的私有回滚备份仍位于 Linux `/var/backups/neuralcloud/qwen-main-20261005` 与 Windows `%LOCALAPPDATA%/NeuralCloud/llama.cpp/backups/qwen-main-20261005`。

```dotenv
NC_LLM_BASE_URL=http://192.168.1.105:8080/v1
NC_LLM_MODEL=qwen3.8-27b
NC_LLM_API_KEY=<从独立密钥文件读取>
NC_LLM_TIMEOUT=60
NC_QQ_INPUT_TOKEN_BUDGET=60000
NO_PROXY=localhost,127.0.0.1,::1,.local,192.168.1.105
```

两类任务共享同一份 GPU 权重。启动配置为总上下文 131,072 tokens、2 个并发槽位，即每槽 65,536；客户端输入预算 60,000 留出模板及输出空间。原项目 96,000 的 QQ 输入预算不能直接用于这个配置。上下文容量与延迟达标是两个独立指标，超长输入必须另测。

goal 决策实际走主模型的 `complete_chat_result`，并非辅助 `complete_json` 端点。服务器通过 `--reasoning off` 默认关闭思考，避免无正文的推理消耗掉项目 25 秒期限；普通聊天也直接输出正文。保留 512 tokens 的思考预算配置，但启用思考后的复杂任务尚未验收。客户端须读取最终 `content`，思考字段不可替代正文。NeuralCloud 现有客户端尚不支持 SSE 消费；llama.cpp 接口支持流式输出，因此接口首字延迟不等于当前 NeuralCloud UI 首字延迟。

## 验证

```powershell
.\.venv\Scripts\python.exe -m tools.deploy.verify_llama_local `
  --key-file "$env:LOCALAPPDATA\NeuralCloud\llama.cpp\api-key.txt" `
  --out "$env:LOCALAPPDATA\NeuralCloud\llama.cpp\verification.json"
```

该脚本使用内置小雾人格、合成背景和现有 goal 准备/校验代码，记录聊天首字时间、完整时间、token 用量，以及无能力时等待、提出活动、未完成步骤不能完成、合成独立保存回执支持完成和并发调用。另外验证实际 `CharacterModel.generate` 非流式正文生成、合成图片识别、后置 system 的渲染顺序，以及缺失/错误 Key 的 401。它不会发送 QQ、写留言板或修改线上记忆。

2026-10-05 收尾探针全部通过，goal 四个场景加并发场景均满足 25 秒期限：

| 探针 | 实测结果 |
| --- | --- |
| goal 决策，含 JSON 和证据校验 | 1.563–5.923 秒；并发 start 5.307 秒 |
| 暖缓存聊天 SSE | 首字 0.145–0.344 秒，完整 1.204–1.276 秒 |
| 与 goal 并发聊天 | 首字 0.851 秒，完整 2.188 秒 |
| 实际 CharacterModel 非流式正文 | 暖缓存 0.676 秒；此前无相同提示缓存时约 1.05–1.25 秒 |
| 独立保存回执完成场景重复验证 | 补齐提示词后 5/5 通过，1.668–5.129 秒 |
| Linux 调用 | 健康、模型列表、中文生成均 HTTP 200 |

早期失败包括原生模板拒绝后置 system，以及 complete 输出漏填顶层 `evidence_refs`。后者通过补齐 `harness/cognitive/goals.py` 已有校验契约的提示说明解决，未改写模型输出或放宽校验器；46 项 goal 工程回归通过。失败与修复后报告一起保存在 [本轮证据目录](harness/evidence/local-qwen38-20261005/)。

下载完成、模型可加载、短输入性能达标和长期 goal 能力需要分别确认。实际运行结果以安装目录 `verification.json` 和归档报告为准；本轮 goal 输入约 5–6K tokens，聊天约 2K tokens，不能由此保证 60K 超长输入也在 25 秒内完成。小样本不能证明长期自主运行的可靠性。
