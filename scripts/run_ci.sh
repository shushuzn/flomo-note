#!/usr/bin/env bash
# run_ci.sh — 本地一键复现 CI（.github/workflows/ci.yml）的全部阶段。
#
# 为什么要有它：CI 连红七次而本地每次都「全绿」，因为本地习惯只跑 `run_tests.sh`——
# 那条命令只跑用例，不含 ruff / mypy / pytest。静态检查与类型检查不在本地入口里，
# 就等于没有本地入口：红只在推送之后出现，而那时东西已经交付出去了。
#
# 判据仍然只有一份：本脚本不新写任何规则，逐条跑 ci.yml 里的同一条命令
# （用例内容由 `run_tests.sh` 与 `pyproject.toml` 决定）。两处会不会漂，由
# `tests/test_run_ci.py` 比对同一批命令字符串来挡——抄第二份判据就要有人看着它别走样。
#
# 用法：
#   bash scripts/run_ci.sh            # 依次跑四个阶段，任一阶段非 0 即停
#   bash scripts/run_ci.sh --check    # 只查开发期工具是否齐备（CI 装了、本地没装时给安装命令）
#
# 工具缺口**不静默跳过**：缺 ruff 就少跑一道 lint 却仍打印「全绿」，正是这次事故的形态。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

detect_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then echo "$PYTHON_BIN"; return; fi
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo ""
}
PY="$(detect_python)"
if [[ -z "$PY" ]]; then
  echo "[ci] 未找到 Python 解释器：请安装 python3 或设 PYTHON_BIN" >&2
  exit 1
fi

# 探测哪些开发期工具缺位。模块清单可由 `CI_TOOL_MODULES` 覆盖——测试要靠它模拟
# 「缺一个工具」而不改 PATH（PATH 一改，脚本里的 dirname 先挂掉，测到的不是这里）。
TOOL_MODULES="${CI_TOOL_MODULES:-ruff mypy pytest coverage}"
read -r -a TOOLS <<< "$TOOL_MODULES"

MISSING="$("$PY" - "${TOOLS[@]}" <<'EOF'
import importlib.util
import sys

print(" ".join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))
EOF
)"

if [[ -n "$MISSING" ]]; then
  echo "[ci] 缺开发期工具：$MISSING" >&2
  echo "[ci] 安装：$PY -m pip install -e '.[dev]'（清单唯一来源是 pyproject 的 dev extra）" >&2
  exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
  echo "[ci] 工具齐备：$TOOL_MODULES"
  exit 0
fi

echo "[ci] ① ruff check（lint，规则集见 pyproject）"
"$PY" -m ruff check scripts/ tests/

echo "[ci] ② 离线回归（用例 + 技能文档纪律）"
bash scripts/run_tests.sh

echo "[ci] ③ mypy（类型，基线见 pyproject）"
"$PY" -m mypy scripts/

echo "[ci] ④ pytest（第二入口）+ 覆盖率报告"
"$PY" -m coverage run -m pytest -q
"$PY" -m coverage report

echo "[ci] 四个阶段全过——与 GitHub Actions 上是同一批命令。"
