#!/usr/bin/env bash
#
# run.sh - 一键启动 / 停止 / 重启 SunAgent Web UI
#
# 定位：本地开发快捷入口（非部署方案）。start/stop 用的 PID 文件后台管理
# 只是临时方案，以后常驻交由 systemd / compose 接管，前台运行优先：
# uv run webui --harness <harness>。
#
# 用法:
#   ./run.sh start [--harness <harness>]  # 启动 webui（后台运行，日志写入 webui.log）
#   ./run.sh restart [--harness <harness>] # 重启 webui（可顺带切换 harness）
#   ./run.sh stop                     # 停止 webui
#   ./run.sh status                   # 查看运行状态
#   ./run.sh logs                     # 查看实时日志
#   ./run.sh harness record <name> <model> [notes]  # 记录一次运行（等价于 uv run python -m agent_runtime.harness record jobs/<name> --model <model> --notes <notes>）
#   ./run.sh harbor run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]  # 运行 Harbor 任务
#
# 启动时指定 harness（任选其一，优先级 CLI > 环境变量 > 默认 common-v0）:
#   ./run.sh start --harness tb-v4
#   ./run.sh start --harness code-v6
#   AGENT_RUNTIME_HARNESS=tb-v4 ./run.sh start
#
# 环境变量（可选）:
#   AGENT_RUNTIME_WEBUI_HOST  监听地址 (默认 127.0.0.1)
#   AGENT_RUNTIME_WEBUI_PORT  监听端口 (默认 8000)
#   AGENT_RUNTIME_HARNESS     默认 harness（可被 --harness 覆盖）
set -uo pipefail

# ---- 基础路径（脚本所在目录，即项目根目录）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
cd "$ROOT_DIR"

# ---- 配置 ----
HOST="${AGENT_RUNTIME_WEBUI_HOST:-127.0.0.1}"
PORT="${AGENT_RUNTIME_WEBUI_PORT:-8000}"
PID_FILE="$ROOT_DIR/.webui.pid"
HARNESS_FILE="$ROOT_DIR/.webui.harness"
LOG_FILE="$ROOT_DIR/webui.log"

# 启动时默认使用的 harness：显式 --harness > 环境变量 > 不指定（webui 侧回落 common-v0）
DEFAULT_HARNESS="${AGENT_RUNTIME_HARNESS:-}"

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

cmd_start() {
  local harness="$DEFAULT_HARNESS"

  # 解析 ./run.sh start [--harness <harness>]，同时兼容 --harness=<harness>
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --harness)
        if [[ $# -lt 2 || -z "${2:-}" || "$2" == -* ]]; then
          echo "错误: --harness 需要一个值" >&2
          echo "用法: $0 start [--harness <harness>]" >&2
          return 1
        fi
        harness="$2"
        shift 2
        ;;
      --harness=*)
        harness="${1#--harness=}"
        if [[ -z "$harness" ]]; then
          echo "错误: --harness 需要一个值" >&2
          echo "用法: $0 start [--harness <harness>]" >&2
          return 1
        fi
        shift
        ;;
      -h|--help)
        echo "用法: $0 start [--harness <harness>]"
        echo ""
        echo "  --harness <harness>  harness id（如 tb-v4）或 manifest 路径，省略则用"
        echo "                       AGENT_RUNTIME_HARNESS，仍省略则用默认 common-v0"
        return 0
        ;;
      *)
        echo "错误: 未知参数 '$1'" >&2
        echo "用法: $0 start [--harness <harness>]" >&2
        return 1
        ;;
    esac
  done

  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "webui 已经在运行 (PID: $pid)，地址: http://${HOST}:${PORT}"
    if [[ -n "$harness" ]]; then
      echo "提示: 已在运行的实例不会切换 harness，如需切换请用: $0 restart --harness $harness" >&2
    fi
    return 0
  fi

  if ! has_uv; then
    echo "错误: 未找到 uv，请先安装 uv (https://github.com/astral-sh/uv)" >&2
    return 1
  fi

  # 启动前校验 harness 是否存在，失败直接报错而不是静默回落
  if [[ -n "$harness" ]]; then
    if [[ ! -f "harnesses/${harness}.yaml" && ! -f "$harness" ]]; then
      echo "错误: harness '$harness' 不存在（harnesses/${harness}.yaml 或文件路径 $harness）" >&2
      echo "可用 harness: $(ls harnesses/*.yaml 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.yaml$//' | tr '\n' ' ')" >&2
      return 1
    fi
  fi

  if [[ -n "$harness" ]]; then
    echo "启动 webui ... (http://${HOST}:${PORT}, harness=${harness})"
  else
    echo "启动 webui ... (http://${HOST}:${PORT})"
  fi
  # 后台启动，日志重定向到 log 文件，nohup 保证终端关闭后仍然运行
  # harness 经由环境变量 + CLI 透传：环境变量覆盖 --reload 子进程，CLI 参数 fail-fast 校验
  local webui_args=(--host "$HOST" --port "$PORT")
  if [[ -n "$harness" ]]; then
    webui_args+=(--harness "$harness")
  fi
  if [[ -n "$harness" ]]; then
    AGENT_RUNTIME_MEMORY=1 AGENT_RUNTIME_HARNESS="$harness" nohup uv run webui "${webui_args[@]}" \
      >> "$LOG_FILE" 2>&1 &
  else
    AGENT_RUNTIME_MEMORY=1 nohup uv run webui "${webui_args[@]}" \
      >> "$LOG_FILE" 2>&1 &
  fi
  echo $! > "$PID_FILE"
  # 记录本次启动的 harness，供 status 展示；未指定则清理旧记录（即默认 harness）
  if [[ -n "$harness" ]]; then
    printf '%s' "$harness" > "$HARNESS_FILE"
  else
    rm -f "$HARNESS_FILE"
  fi

  # 短暂等待后确认进程存活即视为启动成功（不做 HTTP 健康检查：
  # /api/health 在开启认证时需要鉴权，curl 探活会误报失败）
  sleep 1
  if is_running; then
    if [[ -n "$harness" ]]; then
      echo "webui 已启动 (PID: $(cat "$PID_FILE"))，地址: http://${HOST}:${PORT}，harness: $harness"
    else
      echo "webui 已启动 (PID: $(cat "$PID_FILE"))，地址: http://${HOST}:${PORT}"
    fi
    echo "日志: $LOG_FILE"
    return 0
  else
    echo "错误: webui 启动失败，请查看日志: $LOG_FILE" >&2
    rm -f "$PID_FILE"
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
  # 支持 ./run.sh restart [--harness <harness>]：透传给 start；
  # 不带参数时沿用上次启动的 harness（若有记录），避免静默回落到默认。
  if [[ $# -eq 0 && -f "$HARNESS_FILE" ]]; then
    set -- --harness "$(cat "$HARNESS_FILE")"
  fi
  cmd_stop
  echo "------------------------------"
  cmd_start "$@"
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "webui 运行中 (PID: $pid)，地址: http://${HOST}:${PORT}"
    if [[ -f "$HARNESS_FILE" ]]; then
      echo "harness: $(cat "$HARNESS_FILE")"
    elif [[ -n "$DEFAULT_HARNESS" ]]; then
      echo "harness: $DEFAULT_HARNESS (来自环境变量)"
    fi
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

# ./run.sh harness record <name> <model> [notes]
# 等价于: uv run python -m agent_runtime.harness record jobs/<name> --model <model> [--notes <notes>]
cmd_harness_record() {
  local name="${1:-}"
  local model="${2:-}"
  local notes="${3:-}"

  if [[ -z "$name" || -z "$model" ]]; then
    echo "错误: 需要指定 name 和 model" >&2
    echo "用法: $0 harness record <name> <model> [notes]" >&2
    return 1
  fi

  if ! has_uv; then
    echo "错误: 未找到 uv，请先安装 uv (https://github.com/astral-sh/uv)" >&2
    return 1
  fi

  local args=("jobs/${name}" "--model" "$model")
  if [[ -n "$notes" ]]; then
    args+=("--notes" "$notes")
  fi

  echo "记录运行: name=$name model=$model${notes:+ notes=$notes}"
  uv run python -m agent_runtime.harness record "${args[@]}"
}

# ./run.sh harbor run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]
# 示例: ./run.sh harbor run tb-2-1 --harness tb-v4 --job-name terminal-bench-2-1-tb-v4-deepseekv4.1-flash-1017
# 默认等价于: uv run python scripts/run_with_image_gc.py --dataset terminal-bench/terminal-bench-2-1 --harness tb-v4 --job-name terminal-bench-2-1-tb-v4-deepseekv4.1-flash-1017 -n 4 --timeout-multiplier 3
cmd_harbor_run() {
  local name="${1:-}"

  if [[ -z "$name" ]]; then
    echo "错误: 需要指定任务名" >&2
    echo "用法: $0 harbor run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]" >&2
    return 1
  fi
  shift || true

  if ! has_uv; then
    echo "错误: 未找到 uv，请先安装 uv (https://github.com/astral-sh/uv)" >&2
    return 1
  fi

  local dataset="terminal-bench/terminal-bench-2-1"
  local harness="tb-v4"
  local job_name="terminal-bench-2-1-tb-v4-deepseekv4.1-flash-1017"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --harness)
        harness="$2"
        shift 2
        ;;
      --job-name)
        job_name="$2"
        shift 2
        ;;
      --dataset)
        dataset="$2"
        shift 2
        ;;
      *)
        echo "错误: 未知参数 '$1'" >&2
        echo "用法: $0 harbor run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]" >&2
        return 1
        ;;
    esac
  done

  echo "运行 Harbor 任务: name=$name dataset=$dataset harness=$harness job_name=$job_name"
  uv run python scripts/run_with_image_gc.py \
    --dataset "$dataset" \
    --harness "$harness" \
    --job-name "$job_name" \
    -n 4 \
    --timeout-multiplier 3
}

# ---- 入口 ----
CMD="${1:-}"
case "$CMD" in
  start)     shift; cmd_start "$@" ;;
  stop)      cmd_stop ;;
  restart)   shift; cmd_restart "$@" ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  harness)
    shift
    subcmd="${1:-}"
    case "$subcmd" in
      record) cmd_harness_record "${@:2}" ;;
      *)      echo "错误: 未知的 harness 子命令 '$subcmd'" >&2
              echo "用法: $0 harness record <name> <model> [notes]" >&2
              exit 1 ;;
    esac
    ;;
  harbor)
    shift
    subcmd="${1:-}"
    case "$subcmd" in
      run)   cmd_harbor_run "${@:2}" ;;
      *)     echo "错误: 未知的 harbor 子命令 '$subcmd'" >&2
             echo "用法: $0 harbor run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]" >&2
             exit 1 ;;
    esac
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs|harness|harbor}"
    echo ""
    echo "  start [--harness <harness>]    启动 webui（后台运行）"
    echo "    例: $0 start --harness tb-v4"
    echo "        $0 start --harness harnesses/tb-v4.yaml"
    echo "  stop         停止 webui"
    echo "  restart [--harness <harness>]  重启 webui（不带参数时沿用上次的 harness）"
    echo "  status       查看运行状态（含 harness）"
    echo "  logs         查看实时日志"
    echo "  harness      运行记录相关命令"
    echo "    record <name> <model> [notes]"
    echo "               等价于 uv run python -m agent_runtime.harness record jobs/<name> --model <model> [--notes <notes>]"
    echo "  harbor       Harbor 任务相关命令"
    echo "    run <name> [--harness <harness>] [--job-name <job-name>] [--dataset <dataset>]"
    echo "               运行 Harbor 任务（默认参数见脚本内 cmd_harbor_run）"
    echo ""
    echo "环境变量:"
    echo "  AGENT_RUNTIME_WEBUI_HOST  监听地址 (默认 127.0.0.1)"
    echo "  AGENT_RUNTIME_WEBUI_PORT  监听端口 (默认 8000)"
    echo "  AGENT_RUNTIME_HARNESS     默认 harness（可被 --harness 覆盖）"
    exit 1
    ;;
esac
