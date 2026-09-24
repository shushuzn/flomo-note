#!/usr/bin/env bash
# run_audit.sh — 技能文档改动后自校（维护约定）。
#
# 把 audit_skill.sh 所需的 Git 工具链 PATH、SOUNDING_TMP、以及克隆 sounding 用的
# 干净隧道一并包好，一条命令跑完，无需每次手搓环境。
#
# 用法：
#   bash scripts/run_audit.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON_BIN:-C:/Users/35234/.workbuddy/binaries/python/versions/3.13.12/python.exe}"
PORT=$((18120 + (RANDOM % 80)))

export PATH="/c/Program Files/Git/bin:/c/Program Files/Git/usr/bin:$PATH"
export SOUNDING_TMP="C:/Users/35234/AppData/Local/Temp/sounding_flomo"

nohup "$PY" scripts/git_tunnel.py "$PORT" > "/tmp/git_tunnel_${PORT}.log" 2>&1 &
TUN=$!
sleep 2
if ! (exec 3<>"/dev/tcp/127.0.0.1/${PORT}"); then
  echo "[audit] 隧道启动失败:"; cat "/tmp/git_tunnel_${PORT}.log" 2>/dev/null
  kill "$TUN" 2>/dev/null; exit 1
fi
exec 3>&- 2>/dev/null || true

cleanup() { kill "$TUN" 2>/dev/null; }
trap cleanup EXIT

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null || true
export GIT_CONFIG_COUNT=4
export GIT_CONFIG_KEY_0=credential.helper
export GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.https://github.com.helper
export GIT_CONFIG_VALUE_1="!'C:\Program Files\GitHub CLI\gh.exe' auth git-credential"
export GIT_CONFIG_KEY_2=http.proxy
export GIT_CONFIG_VALUE_2="http://127.0.0.1:${PORT}"
export GIT_CONFIG_KEY_3=https.proxy
export GIT_CONFIG_VALUE_3="http://127.0.0.1:${PORT}"

bash scripts/audit_skill.sh
echo "[audit] done"
