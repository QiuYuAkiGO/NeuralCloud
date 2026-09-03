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
$env:NC_EMBODIMENT_PROVIDER = "mistyai"
$env:NC_MISTYAI_BASE_URL = "http://127.0.0.1:8764"
$env:NC_MISTYAI_TOKEN = "<body-service-token>"
$env:NC_MISTYAI_TIMEOUT_SECONDS = "10"
$env:NC_MISTYAI_POLL_INTERVAL_SECONDS = "0.2"
python -m harness.runtime.service
```

自然语言 Goal 不会直接发送给 MistyAI。只有包含 `EmbodiedActionSpec(intent, success_criteria)` 的 GoalProposal 才能进入真实 Provider。POST timeout、连接中断或 5xx 后 adapter 只按 caller-assigned action id 查询；无法证明结果时返回 UNKNOWN，不会重发动作。

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

运行时在 status/health/snapshot/journal 中公开 negotiated set、瞬时 health 与降级原因；`body_healthy` 仅作为派生兼容字段。
