#!/usr/bin/env bash
#
# run.sh - 一键启动 / 停止 / 重启 SunAgent Web UI
#
# 用法:
#   ./run.sh start     # 启动 webui（后台运行，日志写入 webui.log）
#   ./run.sh stop      # 停止 webui
#   ./run.sh restart   # 重启 webui
#   ./run.sh status    # 查看运行状态
#   ./run.sh logs      # 查看实时日志
#
# 环境变量（可选）:
#   AGENT_RUNTIME_WEBUI_HOST  监听地址 (默认 127.0.0.1)
#   AGENT_RUNTIME_WEBUI_PORT  监听端口 (默认 8000)
set -uo pipefail

# ---- 基础路径（脚本所在目录，即项目根目录）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
cd "$ROOT_DIR"

# ---- 配置 ----
HOST="${AGENT_RUNTIME_WEBUI_HOST:-127.0.0.1}"
PORT="${AGENT_RUNTIME_WEBUI_PORT:-8000}"
PID_FILE="$ROOT_DIR/.webui.pid"
LOG_FILE="$ROOT_DIR/webui.log"
HEALTH_URL="http://${HOST}:${PORT}/api/health"

# 是否安装了 uv
has_uv() {
  command -v uv >/dev/null 2>&1
}

# 检查进程是否还在运行
is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# 等待 webui 就绪（最多 30 秒）
wait_for_ready() {
  local waited=0
  while [[ $waited -lt 30 ]]; do
    if command -v curl >/dev/null 2>&1; then
      if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        return 0
      fi
    else
      # 没有 curl 时：只要进程还在且端口被占用，就认为就绪
      if is_running; then
        return 0
      fi
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

cmd_start() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "webui 已经在运行 (PID: $pid)，地址: http://${HOST}:${PORT}"
    return 0
  fi

  if ! has_uv; then
    echo "错误: 未找到 uv，请先安装 uv (https://github.com/astral-sh/uv)" >&2
    return 1
  fi

  echo "启动 webui ... (http://${HOST}:${PORT})"
  # 后台启动，日志重定向到 log 文件，nohup 保证终端关闭后仍然运行
  nohup uv run webui --host "$HOST" --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  if wait_for_ready; then
    echo "webui 已启动 (PID: $(cat "$PID_FILE"))，地址: http://${HOST}:${PORT}"
    echo "日志: $LOG_FILE"
    return 0
  else
    echo "警告: webui 启动超时，请查看日志: $LOG_FILE" >&2
    return 1
  fi
}

cmd_stop() {
  if ! is_running; then
    echo "webui 未在运行"
    # 清理可能存在的残留 pid 文件
    rm -f "$PID_FILE"
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE")"
  echo "正在停止 webui (PID: $pid) ..."
  # 先尝试优雅停止 (SIGTERM)
  kill -TERM "$pid" 2>/dev/null || true

  local waited=0
  while [[ $waited -lt 15 ]]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done

  # 如果还没退出，强制杀掉
  if kill -0 "$pid" 2>/dev/null; then
    echo "进程未响应 SIGTERM，强制停止 (SIGKILL) ..."
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
  fi

  rm -f "$PID_FILE"

  if kill -0 "$pid" 2>/dev/null; then
    echo "错误: 无法停止 webui (PID: $pid)" >&2
    return 1
  fi

  echo "webui 已停止"
  return 0
}

cmd_restart() {
  cmd_stop
  echo "------------------------------"
  cmd_start
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "webui 运行中 (PID: $pid)，地址: http://${HOST}:${PORT}"
    echo "日志: $LOG_FILE"
    return 0
  else
    echo "webui 未运行"
    return 1
  fi
}

cmd_logs() {
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "日志文件不存在: $LOG_FILE" >&2
    return 1
  fi
  tail -n 100 -f "$LOG_FILE"
}

# ---- 入口 ----
CMD="${1:-}"
case "$CMD" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs}"
    echo ""
    echo "  start    启动 webui（后台运行）"
    echo "  stop     停止 webui"
    echo "  restart  重启 webui"
    echo "  status   查看运行状态"
    echo "  logs     查看实时日志"
    echo ""
    echo "环境变量:"
    echo "  AGENT_RUNTIME_WEBUI_HOST  监听地址 (默认 127.0.0.1)"
    echo "  AGENT_RUNTIME_WEBUI_PORT  监听端口 (默认 8000)"
    exit 1
    ;;
esac
