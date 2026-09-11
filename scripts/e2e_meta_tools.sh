#!/usr/bin/env bash
# End-to-end verification of the meta-tools through the REAL agent loop
# (run_agent.py -> LLM -> tool calls -> observations). No direct tool
# invocation: every case is a natural-language prompt, and the script
# checks the resulting workspace state afterwards.
#
# Usage:
#   bash scripts/e2e_meta_tools.sh          # run all cases
#   bash scripts/e2e_meta_tools.sh 3        # run only case 3
#
# Requirements: .env with LLM_API_KEY / LLM_BASE_URL / MODEL_ID.
# See labs/agent/006-meta-tools.md for what each case verifies.
set -uo pipefail
cd "$(dirname "$0")/.."

SANDBOX=/tmp/agent-runtime-e2e
TRACES=/tmp/agent-runtime-e2e-traces
HARNESS=meta-v1
CASE_NO="${1:-0}"
PASS=0; FAIL=0

agent() {  # agent "<prompt>" <trace-name>
  local prompt="$1" trace="$2"
  mkdir -p "$TRACES"
  AGENT_RUNTIME_WORKSPACE="$SANDBOX" \
    uv run python run_agent.py --harness "$HARNESS" \
    --trace "$TRACES/$trace.jsonl" "$prompt" 2>&1
}

check() {  # check "<description>" <shell-condition>
  local desc="$1"
  if eval "$2" >/dev/null 2>&1; then
    echo "  PASS: $desc"; PASS=$((PASS+1))
  else
    echo "  FAIL: $desc"; FAIL=$((FAIL+1))
  fi
}

snapshot() {  # content fingerprint of the sandbox, for read-only cases
  find "$SANDBOX" -type f -exec md5sum {} + | sort | md5sum
}

fresh_sandbox() {
  rm -rf "$SANDBOX"; mkdir -p "$SANDBOX/src/models" "$SANDBOX/tests"
  cat > "$SANDBOX/src/app.py" <<'PY'
def serve():
    return "ok"

def main():
    return serve()
PY
  cat > "$SANDBOX/src/models/user.py" <<'PY'
class User:
    def save(self):
        return "saved"

def make_user():
    return User()
PY
  cat > "$SANDBOX/tests/test_app.py" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.app import main

if __name__ == "__main__":
    assert main() == "ok", f"expected ok, got {main()!r}"
    print("all tests passed")
PY
  echo "serve is also mentioned here" > "$SANDBOX/notes.txt"
}

echo "Sandbox: $SANDBOX  (traces: $TRACES)"
echo "Harness: $HARNESS  (all 12 tools enabled)"
echo

if [[ "$CASE_NO" == "1" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 1: grep_search — 模型用正则搜索并汇报行号 ====="
fresh_sandbox
before=$(snapshot)
agent "用 grep_search 工具搜索代码库中所有 'serve' 的出现位置（含 notes.txt），逐条报告 文件:行号:内容。不要用 shell 的 grep。" trace-1-grep
check "沙盒未被修改" '[[ "$(snapshot)" == "$before" ]]'
echo
fi

if [[ "$CASE_NO" == "2" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 2: find_files — 模型一次调用找全 .py 文件 ====="
fresh_sandbox
before=$(snapshot)
agent "用 find_files 工具找出工作区内所有 .py 文件的路径，并报告总数和完整清单。不要用 find/ls 命令。" trace-2-find
check "沙盒未被修改" '[[ "$(snapshot)" == "$before" ]]'
echo
fi

if [[ "$CASE_NO" == "3" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 3: find_symbol + find_references — 定义与引用 ====="
fresh_sandbox
before=$(snapshot)
agent "用 find_symbol 定位 User 类的定义位置，然后用 find_references 找出 make_user 对它的引用，最后用 read_file(offset=...) 精确读出 User 类定义的前 3 行并原文报告。" trace-3-symbols
check "沙盒未被修改" '[[ "$(snapshot)" == "$before" ]]'
echo
fi

if [[ "$CASE_NO" == "4" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 4: todo_write — 计划与状态推进 ====="
fresh_sandbox
agent "这是一个三步任务：1) 阅读 src/app.py 2) 修复 notes.txt 中的错别字（把 'mentioned' 改成 'noted'）3) 汇报完成。先用 todo_write 建立任务清单（第一项 in_progress），完成每步后用 todo_write 更新状态，全部完成后清单应全为 completed。" trace-4-todo
check ".todo.json 已持久化" '[[ -f $SANDBOX/.todo.json ]]'
check "全部任务 completed" 'python3 -c "import json;t=json.load(open(\"$SANDBOX/.todo.json\"));assert t and all(i[\"status\"]==\"completed\" for i in t)"'
check "notes.txt 已被修改" 'grep -q noted "$SANDBOX/notes.txt"'
echo
fi

if [[ "$CASE_NO" == "5" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 5: apply_patch — 一次调用多文件编辑 ====="
fresh_sandbox
agent "用 apply_patch 工具一次性完成两处修改：1) 在 src/app.py 里把函数 serve 改名为 serve_all（函数体不变）2) 新建文件 src/version.py，内容为一行 VERSION = '1.0'。要求一次 apply_patch 调用完成，不要用 write_file 或 edit_file。完成后报告两个文件的内容。" trace-5-patch
check "src/app.py 已改名" 'grep -q "def serve_all" "$SANDBOX/src/app.py"'
check "src/version.py 已创建" 'grep -q "VERSION" "$SANDBOX/src/version.py"'
echo
fi

if [[ "$CASE_NO" == "6" || "$CASE_NO" == "0" ]]; then
echo "===== CASE 6: 工具链闭环 — 定位 -> 读 -> 改 -> 验证 ====="
fresh_sandbox
agent "src/app.py 中的 serve 函数返回值有 bug，应该返回 'fixed' 而不是 'ok'。请先用 grep_search 定位 serve 的定义行，再用 read_file 按行号读出该函数原文，然后用 apply_patch（或 edit_file）修复它，最后用 run_command 运行 python3 tests/test_app.py 并根据测试结果（预期会失败，因为断言还是 'ok'）决定是否把测试也一并修复，直到测试全部通过。" trace-6-loop
check "serve 返回值已修复" 'grep -q "return \"fixed\"" "$SANDBOX/src/app.py"'
check "测试已同步修复且通过" 'python3 "$SANDBOX/tests/test_app.py" | grep -q "all tests passed"'
echo
fi

echo "=================================="
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" == "0" ]]
