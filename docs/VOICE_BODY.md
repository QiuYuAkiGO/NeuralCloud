# MistyAI 语音身体功能

NeuralCloud 通过独立 HTTP 适配器将 MistyAI D008 注册为语音资产身体功能。认知侧决定台词与表演，Runtime 绑定角色档案、提交异步任务并核对产物；GPU 模型仍由 MistyAI 的独立 worker 管理。NeuralCloud 无需安装或导入 MistyAI、Qwen 或 CosyVoice。

注册的版本化能力为：

- `expression.voice.profiles.v1`：读取绑定的声音档案。
- `expression.voice.render.v1`：用固定档案生成结构化语音脚本。
- `expression.voice.jobs.v1`：查询和取消生成任务。
- `expression.voice.assets.v1`：读取并核对 SHA-256 寻址的 WAV/JSON 资产。

音频生成不等于播放、QQ 发送或他人收到，不写入送达事实或满足内稳态需要的奖励。API 健康只证明资产服务和存储可用，GPU、模型可用性与听感需要实际任务和听评确认。`body_instance_id` 是启动配置中的适配器逻辑身份；现有 Voice API 不提供可远程核验的身体实例身份。

## 绑定现有服务与档案

先按 MistyAI 的 `deploy/voice/README.md` 部署 Voice Asset Service，创建并确认真实 `VoiceProfile` 的 ID 和 revision。与 NeuralCloud 同机时，将 Voice Compose 私有 `.env` 中的 `MISTYAI_VOICE_PORT` 设为 `8794`，因为 NeuralCloud Runtime 使用 `8770`。远程 GPU 可用本地转发 `ssh -N -L 8794:127.0.0.1:8770 your-gpu-host`。不要修改 Runtime 端口来迎合未配置的语音服务。

复制 `presets/voice/mistyai-client.example.json` 到私有绝对路径，填写已有档案 ID、revision、实际 API 地址与 API token 文件路径。API token 必须是独立语音凭据，不能使用 worker、Body 或 NeuralCloud control token。配置不包含 token 明文；只有 Runtime 读取 token 文件。

```powershell
$env:NC_VOICE_CONFIG = 'C:\ProgramData\MistyAI\voice\neuralcloud-client.json'
.\start.ps1
```

也可将 `NC_VOICE_CONFIG` 的绝对路径加入现有 `data/shared-misty-launch.json`，正常启动会恢复绑定。不要覆盖该文件的其他路径。未设置此项时语音关闭；配置中的 `enabled=false` 也不会创建语音客户端。`mode=observe` 允许只读操作，拒绝生成和取消。语音服务离线不会阻断桌面、认知或内稳态服务，语音状态会显示不可用。

小雾 A2 最终试听资产尚未建立为 D008 `VoiceProfile`，不能将示例 ID 或 A2 文件名当作已存在的服务档案。现有服务从设计任务候选创建档案；外部最终 WAV 的导入不在此注册适配器中实现。

## Runtime 接口

所有入口使用现有 NeuralCloud Runtime control Bearer 认证，语音 API token 不下发给调用方：

- `GET /v1/voice`：绑定与协商健康状态。
- `GET /v1/voice/profiles`：当前绑定的档案。
- `POST /v1/voice/render-jobs`：提交生成，返回异步任务。
- `GET /v1/voice/jobs/{job_id}`：查询生成结果。
- `POST /v1/voice/jobs/{job_id}:cancel`：请求取消。
- `GET /v1/voice/assets/{digest}`：读取经摘要校验的产物。
- `GET /v1/capabilities`：通过 `bodies.voice` 查看注册状态，包括自主选择关闭时的状态。

生成请求示例：

```json
{
  "idempotency_key": "neuralcloud:voice:example-001",
  "script": {
    "segments": [
      {"kind": "speech", "segment_id": "line-1", "text": "早上好，今天也一起慢慢来吧。", "style": "gentle"},
      {"kind": "pause", "segment_id": "pause-1", "duration_ms": 350}
    ]
  },
  "seed": 0
}
```

调用者不传 profile，适配器注入配置绑定的 ID/revision。相同幂等键与同一请求复用原任务，冲突被拒绝；不自动更换键重试。查询 `succeeded` 后，使用结果中的资产摘要下载 WAV、时间线和来源清单。超时不能当作未提交，也不能冒充生成完成。取消不保证立即终止 CUDA 内核。

## 共享认知选择

显式调用上述接口不依赖自主选择。需要将语音提供给现有共享能力选择器时，在现有 manifest 的 `autonomous_capabilities` 中启用 `enabled`，将 `owners.voice` 设为 `shared`，并将 `expression.voice.profiles.v1` 与 `expression.voice.render.v1` 加入 `allowed_capabilities`。保留其他通道配置。默认没有语音自主归属，也不会自动触发声音生成。

模型只能从当前有效目录选择读取档案或提交脚本，不能改变档案、身体、幂等键或期限。任务关联保存在 Runtime 私有状态中；重启后的未完成任务按原 job ID 查询，不重新生成。成功仅记录语音资产结果，播放和发送由其他独立执行能力负责。

契约由 `harness.embodiment.schema` 导出到 `contracts/embodiment/expression.voice.v1.schema.json`。验证使用 `tests/test_voice_body.py`、`tests/test_voice_runtime.py`、`tests/test_voice_config.py` 与 `tests/test_voice_launcher.py`；MistyAI 的 `tests/voice/test_neuralcloud_integration.py` 使用真实 API 和临时数据库验证跨仓库协议，合成器替身不作为音色质量证据。
