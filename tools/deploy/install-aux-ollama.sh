#!/usr/bin/env bash
# Reuse an installed Ollama binary in an isolated NeuralCloud auxiliary service.
# Official model: https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M
# Run hardware inventory first: python3 aux_qwen_probe.py probe
set -euo pipefail

test "$(id -u)" = 0 || { echo 'run_as_root'; exit 1; }
test -d /run/systemd/system || { echo 'systemd_required'; exit 1; }
binary="${NC_AUX_OLLAMA_BINARY:-/usr/local/bin/ollama}"
test -x "$binary" || { echo 'existing_ollama_binary_required'; exit 1; }
test "$(readlink -f "$binary")" = "$binary" || { echo 'use_resolved_binary_path'; exit 1; }
unit=/etc/systemd/system/neuralcloud-aux-ollama.service
state=/var/lib/neuralcloud-aux-ollama
owner=neuralcloud-aux
marker='NeuralCloud auxiliary Ollama owner v1'

if test -e "$unit" && ! grep -Fq "$marker" "$unit"; then
  echo 'unowned_service_conflict'; exit 1
fi
if test -L "$state" || test -L "$unit"; then
  echo 'symlink_target_rejected'; exit 1
fi
if ! test -e "$unit" && ss -ltn | grep -qE ':11435[[:space:]]'; then
  echo 'auxiliary_port_already_in_use'; exit 1
fi
if test -d "$state" && ! test -f "$state/.neuralcloud-aux-owner"; then
  echo 'unowned_state_directory'; exit 1
fi
if ! id "$owner" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$owner"
fi
test "$(getent passwd "$owner" | cut -d: -f6)" = "$state" || { echo 'user_home_conflict'; exit 1; }
install -d -m 0750 -o "$owner" -g "$owner" "$state" "$state/models"
printf '%s\n' "$marker" > "$state/.neuralcloud-aux-owner"
for device_group in render video; do
  if getent group "$device_group" >/dev/null; then
    usermod -a -G "$device_group" "$owner"
  fi
done
cat > "$unit" <<EOF
# $marker
[Unit]
Description=NeuralCloud isolated Qwen auxiliary model
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$owner
Group=$owner
ExecStart=$binary serve
WorkingDirectory=$state
Environment=HOME=$state
Environment=OLLAMA_HOST=127.0.0.1:11435
Environment=OLLAMA_MODELS=$state/models
Environment=OLLAMA_CONTEXT_LENGTH=8192
Environment=OLLAMA_KEEP_ALIVE=-1
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_VULKAN=1
Environment=OLLAMA_IGPU_ENABLE=1
Restart=on-failure
RestartSec=3
Nice=5
MemoryMax=10G
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$state

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now neuralcloud-aux-ollama.service

python3 - <<'PY'
import json, time, urllib.request
base = 'http://127.0.0.1:11435'
for attempt in range(20):
    try:
        with urllib.request.urlopen(base + '/api/version', timeout=2) as response:
            print('auxiliary_version', json.load(response), flush=True)
        break
    except OSError:
        if attempt == 19:
            raise
        time.sleep(1)
payload = json.dumps({'model': 'qwen3:4b-instruct-2507-q4_K_M', 'stream': True}).encode()
request = urllib.request.Request(base + '/api/pull', data=payload, headers={'Content-Type': 'application/json'})
last = None
completed = False
with urllib.request.urlopen(request, timeout=1800) as response:
    for line in response:
        value = json.loads(line)
        if 'error' in value:
            raise RuntimeError(value['error'])
        status = value.get('status', '')
        completed = completed or status == 'success'
        if value.get('total'):
            status += f" {int(value.get('completed', 0) / value['total'] * 10) * 10}%"
        if status != last:
            print(status, flush=True)
            last = status
if not completed:
    raise RuntimeError('model pull ended before success; rerun to resume')
print('model_ready', flush=True)
PY
systemctl is-active neuralcloud-aux-ollama.service
