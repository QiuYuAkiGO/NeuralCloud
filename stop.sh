#!/usr/bin/env bash
# 停止 NeuralCloud GUI 服务（python -m gui.server，默认端口 8765）
set +e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT/.gui.pid"
PORT=8765
QUIET=0

if [[ "${1:-}" == "--quiet" || "${1:-}" == "-q" ]]; then
  QUIET=1
fi

info() {
  if [[ "$QUIET" -eq 0 ]]; then
    printf '%s\n' "$*"
  fi
}

pid_alive() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

port_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | awk -v port=":${PORT}" '
      index($4, port) && $4 ~ port "$" {
        while (match($0, /pid=[0-9]+/)) {
          print substr($0, RSTART + 4, RLENGTH - 4)
          $0 = substr($0, RSTART + RLENGTH)
        }
      }
    '
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null
  elif command -v fuser >/dev/null 2>&1; then
    fuser "${PORT}/tcp" 2>/dev/null | tr -s ' ' '\n'
  fi
}

cmdline_pids() {
  pgrep -f '(python3?|python).*-m gui(\.server)?' 2>/dev/null
}

collect_pids() {
  local pid raw
  {
    if [[ -f "$PID_FILE" ]]; then
      raw="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
      if pid_alive "$raw"; then
        printf '%s\n' "$raw"
      fi
    fi
    port_pids
    cmdline_pids
  } | awk 'NF && $1 ~ /^[0-9]+$/ && $1+0 > 1 { print $1+0 }' | sort -u
}

mapfile -t PIDS < <(collect_pids)
if [[ ${#PIDS[@]} -eq 0 ]]; then
  info "服务未在运行。"
  rm -f "$PID_FILE"
  exit 0
fi

for pid in "${PIDS[@]}"; do
  info "正在停止进程 PID=${pid} ..."
  kill "$pid" 2>/dev/null || true
done

deadline=$((SECONDS + 10))
while (( SECONDS < deadline )); do
  mapfile -t LEFT < <(collect_pids)
  if [[ ${#LEFT[@]} -eq 0 ]]; then
    break
  fi
  sleep 0.2
done

mapfile -t LEFT < <(collect_pids)
if [[ ${#LEFT[@]} -gt 0 ]]; then
  for pid in "${LEFT[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 0.2
fi

rm -f "$PID_FILE"

mapfile -t LEFT < <(collect_pids)
if [[ ${#LEFT[@]} -gt 0 ]]; then
  info "未能完全停止服务，残留 PID: ${LEFT[*]}"
  exit 1
fi

info "服务已停止。"
exit 0
