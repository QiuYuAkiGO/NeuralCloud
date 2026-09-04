# Embodiment Provider 运行手册

## 1. 默认与安全边界

缺少 `ExperimentManifest.embodiment` 时默认为 `mode: off`；`NC_EMBODIMENT_PROVIDER=none` 保持原有 chat-only 行为。Provider 的 URL、token、timeout 和 poll interval 只存在于 Runtime composition root，不写入研究 manifest。

Provider 额外声明的能力不会自动启用。manifest 中 required 与 optional requirements 的并集就是本次运行 allowlist，且 activation set 在启动时冻结。required 能力不可用且 `required_for_runtime=true` 时 Runtime 进入 `PAUSED`，Cognitive、Homeostasis 和 journal 不进入故障态。

## 2. Manifest 示例

```json
{
  "embodiment": {
    "mode": "grounded",
    "required_for_runtime": false,
    "body_instance_id": "misty-body-01",
    "capability_requirements": [
      {"capability": "action.execute.v1", "required": true},
      {"capability": "action.verify.v1", "required": true},
      {"capability": "action.safe-point.v1", "required": false},
      {"capability": "action.reconcile.v1", "required": false},
      {"capability": "observation.snapshot.v1", "required": true},
      {"capability": "evidence.reference.v1", "required": true},
      {"capability": "physiology.body-state.v1", "required": false}
    ],
    "body_state_interval_seconds": 5,
    "body_state_max_age_seconds": 10,
    "replan_hysteresis": 0.05,
    "minimum_replan_interval_seconds": 300,
    "anthropomorphic_labels": false,
    "blinded_names": false,
    "enable_drive_preemption": false
  }
}
```

`observe` 只记录 BodyState；`grounded` 以高 precision 仅同化 `energy_reserve`；`active` 额外在 target-band 穿越、hysteresis 与最小间隔满足时触发 goal reconsideration。terminal 不受 debounce 限制，停止新动作但保留感知、verify/reconcile、Persona、Memory 与 Runtime。

## 3. MistyAI

```powershell
$tokenFile = "C:\ProgramData\MistyAI\body-service.token"
./start.ps1 `
  -ManifestPath presets/runtime/grounded-mistyai-v1.json `
  -EmbodimentProvider mistyai `
  -MistyAIBaseUrl http://127.0.0.1:8787 `
  -MistyAITokenFile $tokenFile
```

token 文件必须只含一个至少 16 字符的真实令牌，并与 MistyAI 的 `MISTYAI_SERVICE_TOKEN` 完全一致；应使用操作系统 ACL 限制读取者。Runtime 优先读取 `NC_MISTYAI_TOKEN_FILE`，仅为兼容旧部署保留 `NC_MISTYAI_TOKEN`。空值、开发值和占位值会在停止已有进程前被拒绝；启动脚本只在创建 Runtime 子进程时短暂注入凭据，Cognitive、Homeostasis 与 GUI 不继承它，令牌也不会写入进程参数、PID registry 或 manifest。

本机回环地址可使用 HTTP；任何非回环 MistyAI 地址必须使用 HTTPS，避免 Bearer token 与 evidence 在传输中以明文暴露。

当 manifest 设置 `required_for_runtime=true` 时，MistyAI 启动只有在 Runtime 为 `RUNNING`、Provider 为 `mistyai/available`、body identity 一致、required capability 无缺失/不兼容且全部激活时才成功；`PAUSED` 不算 ready，失败时本轮新进程会被清理。可选 embodiment 则保留 Runtime 自身的降级语义。

自然语言 Goal 不会直接发送给 MistyAI。生产 Cognitive 只把显式 `cognitive-action-request.v1` 编译成 `GoalProposal`；intent 参数、目标绑定和 typed success criteria 必须通过 allowlist/schema，风险、可逆性和审批要求由代码策略表赋值，调用方不可覆盖。当前 Homeostasis 只产生建议 policy，不会自行附加动作请求，因此不会静默升级成自治执行。受认证的 Runtime `/v1/events` 可提交如下 payload：

```json
{
  "event_type": "user_message",
  "priority": 20,
  "source": "trusted-action-client",
  "payload": {
    "text": "执行已选择的安全策略",
    "policies": [
      {"policy_id": "observe_workspace", "label": "观察已授权工作区", "rank": 1}
    ],
    "action_request": {
      "schema_version": "cognitive-action-request.v1",
      "policy_id": "observe_workspace",
      "intent": {"intent_id": "control.observe.v1", "arguments": {}},
      "success_criteria": [{"kind": "intentional_noop", "condition": "observed"}]
    }
  }
}
```

普通 `/v1/messages` 文本不会被猜测成动作。POST timeout、连接中断或 5xx 后 adapter 只按 caller-assigned action id 查询；无法证明结果时返回 `UNKNOWN`，不会重发动作。observation、action、verify、reconcile 与 body-state 的 `evidence://sha256/...` 会先经独立超时、16 MiB 上限、媒体类型与 SHA-256 校验，再进入 NeuralCloud；证据不可验证的成功结果会降级为 `UNKNOWN`。

## 4. Simulator

```powershell
$env:NC_EMBODIMENT_PROVIDER = "simulator"
$env:NC_SIMULATOR_SEED = "7"
$env:NC_SIMULATOR_PHYSIOLOGY = "true"
python -m harness.runtime.service
```

可用 `NC_SIMULATOR_OFFLINE=true` 或 `NC_SIMULATOR_OBSERVATION_ONLY=true` 验证降级。测试代码还可注入 clock、脚本化 world state 和每个 intent 的 success/failure/unknown。

## 5. 黑盒验收

固定顺序为：health → capabilities → observation → typed action → independent verify → evidence → body-state → terminal/degradation。

```powershell
# NeuralCloud
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check harness tests

# MistyAI（必须使用 Python 3.12）
cd D:\GameDev\MistyAI
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests migrations
.\.venv\Scripts\mypy.exe src\mistyai
```

CI 还会在独立进程启动真实 MistyAI FastAPI 应用，仅替换机器相关的 Desktop Agent，然后通过 TCP 验收认证、capability negotiation、observation/body evidence、typed action 与 independent verify。手工连接已运行的 MistyAI 时可执行：

```powershell
$env:NC_CROSS_PROCESS_MISTYAI_URL = "http://127.0.0.1:8787"
$env:NC_CROSS_PROCESS_MISTYAI_TOKEN = "<同一个 body service token>"
.\.venv\Scripts\python.exe -m pytest -q tests/test_cross_process_mistyai.py
```

运行时在 status/health/snapshot/journal 中公开 negotiated set、瞬时 health 与降级原因；`body_healthy` 仅作为派生兼容字段。
