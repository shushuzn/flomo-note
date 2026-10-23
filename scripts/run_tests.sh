#!/usr/bin/env bash
# run_tests.sh — 一键跑完全部离线回归用例（维护约定）。
#
# 改动脚本后须跑其配套用例，逐个手跑既重复又易漏。本脚本把 tests/test_*.py
# 全部串起来，并一并跑技能文档内容纪律自检（scripts/check_skill_docs.py）；任一失败即非 0 退出。
# 用例全部离线（不联网），可随时安全重跑。
#
# 工具在 scripts/，用例在 tests/：判据是「它是不是交付物」——用例不进任何流程，
# 只用来验证流程脚本，混在一层里会让 scripts/ 看不出哪些是入口。
# 命名硬约束：tests/ 下的 .py 必须叫 test_*.py，否则下面这行 glob 收不到它，
# 用例会静默不跑（check_skill_docs.py 已把这条做成 ERR）。
#
# 用法：
#   bash scripts/run_tests.sh
#   PYTHON_BIN=/usr/bin/python3 bash scripts/run_tests.sh   # 显式指定解释器
set -uo pipefail

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
  echo "[tests] 未找到 Python 解释器：请安装 python3 或设 PYTHON_BIN" >&2
  exit 1
fi

# 临时目录：用例大量用 tempfile 建临时目录，而受限环境（agent 沙箱、容器、把系统
# temp 设成只读的机器）会让它全部 PermissionError [Errno 13]——实测 24 项里 13 项红，
# 看起来像代码坏了，其实一行没改。所以先探一次：外部给的临时目录能写就尊重它，
# 不能写就自动落到仓库内 _tmp（已 ignore，不进 git），并明说改了，不静默。
tmp_probe() {
  "$PY" -c 'import pathlib, tempfile; d = pathlib.Path(tempfile.mkdtemp()); (d / "probe").write_text("ok")' >/dev/null 2>&1
}
if [[ -n "${TMPDIR:-}" ]] && ! tmp_probe; then
  echo "[tests] 外部 TMPDIR 不可写（${TMPDIR}），改用仓库内 _tmp/.tmp —— 这是环境问题，不是代码问题"
  unset TMPDIR TEMP TMP
fi
if [[ -z "${TMPDIR:-}" ]]; then
  mkdir -p "$REPO_ROOT/_tmp/.tmp"
  export TMPDIR="$REPO_ROOT/_tmp/.tmp"
  export TEMP="${TEMP:-$TMPDIR}"
  export TMP="${TMP:-$TMPDIR}"
fi
if ! tmp_probe; then
  echo "[tests] 临时目录仍不可写：TMPDIR=${TMPDIR:-（未设）} —— 用例里的 tempfile 会全部" >&2
  echo "        PermissionError [Errno 13]。请把 TMPDIR/TEMP/TMP 指到可写目录后重跑。" >&2
  exit 1
fi

rc=0
total=0
failed=0

# 用例目录必须真的有 test_*.py。空目录（或全被改名 / 搬走）时，下面的 glob 会原样
# 传成字面量 `tests/test_*.py` 去跑，报的是一句「can't open file」——看起来像环境
# 问题，实际是**一个用例都没跑**。这里显式拦下来，报成它本来的样子。
if ! compgen -G "tests/test_*.py" > /dev/null; then
  echo "[tests] tests/ 下没有任何 test_*.py：用例全丢了，或文件名不符合 test_ 前缀" >&2
  exit 1
fi

run_one() {
  local label="$1"; shift
  total=$((total + 1))
  printf "[tests] %-38s " "$label"
  local out
  if out="$("$@" 2>&1)"; then
    echo "PASS"
  else
    echo "FAIL"
    printf '%s\n' "$out" | sed 's/^/    /'
    rc=1
    failed=$((failed + 1))
  fi
}

run_one "check_skill_docs.py" "$PY" scripts/check_skill_docs.py
for t in tests/test_*.py; do
  run_one "$(basename "$t")" "$PY" "$t"
done

echo "---"
if [[ "$rc" == "0" ]]; then
  echo "[tests] 全部通过（$total 项）"
else
  echo "[tests] 存在失败（$failed/$total 项）"
fi
exit $rc
