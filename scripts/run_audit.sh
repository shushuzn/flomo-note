#!/usr/bin/env bash
# run_audit.sh — 技能文档改动后自校（维护约定）。
#
# 把 audit_skill.sh 所需的 Python、Git 工具链、SOUNDING_TMP、以及克隆 sounding 用的
# 干净隧道一并包好，一条命令跑完，无需每次手搓环境。**跨平台**：Linux/macOS 用
# python3，Windows(Git Bash) 才启用 gh.exe 与 Git 目录的 PATH 修正。
#
# 顺序：先跑离线内容纪律自检（check_skill_docs.py，H28，不依赖网络），再起隧道做结构审计；
# 任一环节失败即非 0 退出（CI 门禁），失败不中断后续环节，便于一次看到全部问题。
#
# 用法：
#   bash scripts/run_audit.sh
#   PYTHON_BIN=/usr/bin/python3 bash scripts/run_audit.sh   # 显式指定解释器
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- 探测 Python：显式 env > python3 > python ----
detect_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then echo "$PYTHON_BIN"; return; fi
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo ""
}
PY="$(detect_python)"
if [[ -z "$PY" ]]; then
  echo "[audit] 未找到 Python 解释器：请安装 python3 或设 PYTHON_BIN" >&2
  exit 1
fi

# ---- 平台判定 ----
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

PORT=$((18120 + (RANDOM % 80)))
rc=0

# 离线内容纪律自检（H28）先行：不依赖网络、隧道与 PATH 修正
echo "== 技能文档内容纪律自检（H28，离线） =="
"$PY" scripts/check_skill_docs.py || rc=1

if [[ "$IS_WINDOWS" == "1" ]]; then
  export PATH="/c/Program Files/Git/bin:/c/Program Files/Git/usr/bin:$PATH"
  export SOUNDING_TMP="${SOUNDING_TMP:-C:/Users/${USERNAME:-$USER}/AppData/Local/Temp/sounding_flomo}"
else
  export SOUNDING_TMP="${SOUNDING_TMP:-${TMPDIR:-/tmp}/sounding_flomo}"
fi
mkdir -p "$SOUNDING_TMP" 2>/dev/null || true

nohup "$PY" scripts/git_tunnel.py "$PORT" > "/tmp/git_tunnel_${PORT}.log" 2>&1 &
TUN=$!
sleep 2
if ! (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  echo "[audit] 隧道启动失败:"; cat "/tmp/git_tunnel_${PORT}.log" 2>/dev/null
  kill "$TUN" 2>/dev/null || true; exit 1
fi
exec 3>&- 2>/dev/null || true

cleanup() { kill "$TUN" 2>/dev/null || true; }
trap cleanup EXIT

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null || true

if [[ "$IS_WINDOWS" == "1" ]]; then
  # Windows：gh.exe 作凭据助手，绕开 reg.exe 黑名单
  GH_EXE="$(command -v gh || echo 'C:\Program Files\GitHub CLI\gh.exe')"
  export GIT_CONFIG_COUNT=4
  export GIT_CONFIG_KEY_0=credential.helper
  export GIT_CONFIG_VALUE_0=
  export GIT_CONFIG_KEY_1=credential.https://github.com.helper
  export GIT_CONFIG_VALUE_1="!'${GH_EXE}' auth git-credential"
  export GIT_CONFIG_KEY_2=http.proxy
  export GIT_CONFIG_VALUE_2="http://127.0.0.1:${PORT}"
  export GIT_CONFIG_KEY_3=https.proxy
  export GIT_CONFIG_VALUE_3="http://127.0.0.1:${PORT}"
else
  # Linux/macOS：关掉可能写死失效代理的凭据助手，只留隧道
  export GIT_CONFIG_COUNT=3
  export GIT_CONFIG_KEY_0=credential.helper
  export GIT_CONFIG_VALUE_0=
  export GIT_CONFIG_KEY_1=http.proxy
  export GIT_CONFIG_VALUE_1="http://127.0.0.1:${PORT}"
  export GIT_CONFIG_KEY_2=https.proxy
  export GIT_CONFIG_VALUE_2="http://127.0.0.1:${PORT}"
fi

bash scripts/audit_skill.sh || rc=1
echo "[audit] done"
exit $rc
