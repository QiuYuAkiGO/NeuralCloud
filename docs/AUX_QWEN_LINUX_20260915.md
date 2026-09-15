# Linux Qwen auxiliary service

Deployment target: the existing `qiuyu@frp-fan.com:54849` Linux instance.

## Model and endpoint

- Model: `qwen3:4b-instruct-2507-q4_K_M`.
- Linux endpoint: `http://127.0.0.1:11435/v1` (OpenAI-compatible).
- Dedicated service: `neuralcloud-aux-ollama.service`, enabled at boot.
- Dedicated account and data: `neuralcloud-aux`, `/var/lib/neuralcloud-aux-ollama`.
- Existing `/usr/local/bin/ollama` version 0.31.1 is reused. The original
  `ollama.service` on port 11434 and NeuralCloud services are not reconfigured.

The official model is Qwen3-4B-Instruct-2507, with Q4_K_M quantization and
nonthinking output. Its native context is larger; this service deliberately
uses 8192 tokens. Sources:
[Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
[Ollama model tag](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M).

## Resource settings

The server has a Ryzen 7 7840HS, Radeon 780M integrated GPU, approximately
60 GiB system RAM and 299 GiB free disk before deployment. The auxiliary model
has its own model directory and does not replace any existing model.

The unit sets `OLLAMA_CONTEXT_LENGTH=8192`, `OLLAMA_KEEP_ALIVE=-1`,
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `MemoryMax=10G`, and
`Nice=5`. Vulkan is enabled with `OLLAMA_VULKAN=1`. Ollama 0.31.1 additionally
requires `OLLAMA_IGPU_ENABLE=1` for this integrated GPU. Only the auxiliary
account is added to the `render` and `video` device groups. No driver or runtime
upgrade is performed. Shared GPU memory is part of system RAM.

The systemd unit binds to loopback and uses `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome=true` and a writable dedicated state path.

On this deployment the Linux host's registry connections stalled after 81.2%
of the main blob. The three missing byte ranges (468,240,090 bytes total) and
small metadata blobs were fetched from the same official Ollama registry on
the local machine, then transferred over the authenticated SSH connection.
The original partial file was preserved; the auxiliary service was stopped
while merging the ranges. The expected full model blob SHA256 is
`85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9`;
the official tag manifest SHA256 is
`0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`.

## Verified result

The full blob hash and manifest hash matched. A synthetic Chinese JSON probe
completed after a 2.883-second warmup: appraisal took 1.008 seconds (70 input
and 16 output tokens), and guidance took 1.604 seconds. Both responses were
valid JSON. These are single short requests; full EmotionService quality,
deadline behavior and P95 latency are separate acceptance checks.

Ollama reported 8192 context, all 37 layers on Vulkan, and 3,873,366,343 bytes
of shared GPU model/context allocation. The dedicated data directory occupied
2,500,026,568 bytes. After the probe the host had 41,954 MiB available RAM and
318,247,436,288 bytes available disk. GPU memory is shared system memory;
systemd memory accounting should not be treated as the complete GPU limit.

The original Ollama, NeuralCloud Cognitive, and Runtime process IDs and start
times were unchanged and their units remained active. Full nonsecret evidence:
[deployment verification](milestones/evidence/aux-qwen-linux-2026-09-15.json).

## Reproduction

First inspect the target with `python3 tools/deploy/aux_qwen_probe.py probe`.
On this target, run `sudo bash tools/deploy/install-aux-ollama.sh` to install
the dedicated unit and download the official model. The installer refuses
unowned unit/state paths and an occupied auxiliary port. It validates successful
completion of the model pull, and an interrupted download can be resumed by
running it again.

Validate model output with `python3 tools/deploy/aux_qwen_probe.py verify`.
The probe uses synthetic Chinese input only, warms the model at 8192 context,
then times separate JSON appraisal and guidance requests. Simple probe timings
are not a substitute for testing the full EmotionService prompts and budgets.

For local testing, run `python tools/deploy/aux_ssh_tunnel.py` in a terminal
with Paramiko installed. It prompts for the existing SSH password and checks
the pinned host fingerprint. It never accepts a password in command arguments.
Alternatively, `--password-file <existing-file>`, `--key-file <existing-key>`
or `--agent` reuses credentials already configured by the operator; the helper
does not create or persist credentials.
The temporary listener is always `127.0.0.1:11435`, forwarding to the same
loopback port on Linux. Keep the terminal open during testing; Ctrl-C closes
the tunnel. The Linux model service remains available after the tunnel closes.

This deployment does not enable production `emotion_mode=on` or alter the
primary model configuration. End-to-end behavior is verified separately before
the emotion rollout is enabled.

## Isolated emotion configuration

Use this configuration in the test process, or in a separately reviewed
deployment configuration after validation. Do not append it to the running
instance's saved GUI configuration during model installation.

```dotenv
NC_AUX_LLM_BASE_URL=http://127.0.0.1:11435/v1
NC_AUX_LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M
NC_AUX_LLM_API_KEY=not-needed
NC_AUX_LLM_PRELOAD=true
NC_AUX_LLM_KEEP_ALIVE=-1
NC_AUX_LLM_NUM_CTX=8192
NC_EMOTION_MODE=shadow
```

The endpoint is the same inside Linux and in a local test process while the
SSH tunnel is running. The loopback Ollama service does not require an API
key. Shared Runtime mode comes from the frozen manifest; use
`presets/runtime/shared-misty-emotion-shadow-v1.json` for a reviewed shadow
rollout. `NC_EMOTION_MODE` applies to independent development entry points.
See [Emotion director](EMOTION_DIRECTOR.md) for rollout and blind evaluation
requirements.
