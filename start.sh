#!/usr/bin/env bash
# 启动 NeuralCloud GUI 服务。若已在运行则先停止再启动（重启）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT/.gui.pid"
LOG_DIR="$ROOT/logs"
OUT_LOG="$LOG_DIR/gui.out.log"
ERR_LOG="$LOG_DIR/gui.err.log"
PORT=8765
URL="http://127.0.0.1:${PORT}"

cd "$ROOT"

pid_alive() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk -v port=":${PORT}" 'index($4, port) && $4 ~ port "$" { found=1 } END { exit found ? 0 : 1 }'
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${PORT}" -sTCP:LISTEN -t >/dev/null 2>&1
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser "${PORT}/tcp" >/dev/null 2>&1
    return
  fi
  return 1
}

service_running() {
  local raw
  if [[ -f "$PID_FILE" ]]; then
    raw="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
    if pid_alive "$raw"; then
      return 0
    fi
  fi
  if port_listening; then
    return 0
  fi
  if pgrep -f '(python3?|python).*-m gui(\.server)?' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

find_python() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

wait_port() {
  local i
  for i in $(seq 1 60); do
    if port_listening; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

if service_running; then
  echo "检测到服务已启动，正在重启..."
  if ! bash "$ROOT/stop.sh" --quiet; then
    echo "重启失败：无法停止已有进程。"
    exit 1
  fi
fi

python_exe="$(find_python || true)"
if [[ -z "${python_exe}" ]]; then
  echo "未找到 Python。请先创建虚拟环境：python3 -m venv .venv"
  exit 1
fi

mkdir -p "$LOG_DIR"
rm -f "$OUT_LOG" "$ERR_LOG"

echo "正在启动服务: ${python_exe} -m gui.server"
nohup "$python_exe" -m gui.server >>"$OUT_LOG" 2>>"$ERR_LOG" &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

if ! wait_port; then
  if ! pid_alive "$pid"; then
    echo "启动失败，进程已退出。请查看日志："
    echo "  $ERR_LOG"
    rm -f "$PID_FILE"
    exit 1
  fi
  echo "进程已启动（PID=${pid}），但端口 ${PORT} 尚未就绪，请稍后打开 ${URL}"
  echo "日志: $OUT_LOG"
  exit 0
fi

echo "服务已启动  PID=${pid}"
echo "打开 ${URL}"
echo "日志: $OUT_LOG"
exit 0
