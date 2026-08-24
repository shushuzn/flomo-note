#!/usr/bin/env bash
# audit_skill.sh — flomo-note SKILL.md 本地自校（sounding 静态审计）
#
# 把 SKILL 记忆维护段的"改动后本地自校"落成一键命令：克隆 sounding（缺失时）→
# 审计 .kilo/skills/flomo-note → 默认用完即清临时克隆（符合 SKILL 记忆维护纪律）。
# 审计确定性、不联网、不改文件、可复现；目标 score 100/100。
#
# 用法:
#   ./audit_skill.sh            # 审计 SKILL.md，结束清理临时克隆
#   ./audit_skill.sh --keep     # 审计后保留临时克隆（调试用）
#   ./audit_skill.sh --mcp      # 额外导出 FLOMO_MCP_TOOLS descriptor 并用 audit 审 flomo MCP 工具
#
# 环境变量（按需覆盖）:
#   SOUNDING_PY   审计用 Python（默认托管 python 3.13）
#   SOUNDING_TMP  sounding 临时克隆目录
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_DIR="$REPO_ROOT/.kilo/skills/flomo-note"
FLOMO_CLIENT="$SCRIPT_DIR/flomo_client.py"

PYTHON="${SOUNDING_PY:-/c/Users/35234/.workbuddy/binaries/python/versions/3.13.12/python.exe}"
SOUNDING_TMP="${SOUNDING_TMP:-/c/Users/35234/AppData/Local/Temp/sounding_audit}"

# 传给 Windows Python 的路径需转成 Windows 形态（git-bash 的 /d/... 在 Windows Python 下会解析错）
SKILL_DIR_WIN="$(cygpath -w "$SKILL_DIR")"
FLOMO_CLIENT_WIN="$(cygpath -w "$FLOMO_CLIENT")"
SOUNDING_TMP_WIN="$(cygpath -w "$SOUNDING_TMP")"

DO_MCP=0
KEEP=0
for a in "$@"; do
  case "$a" in
    --mcp)  DO_MCP=1 ;;
    --keep) KEEP=1 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

# 克隆 sounding（缺失时）
if [ ! -d "$SOUNDING_TMP/.git" ]; then
  echo "克隆 sounding 到 $SOUNDING_TMP ..."
  rm -rf "$SOUNDING_TMP"
  git clone --depth 1 https://github.com/alinotfoundbtw/sounding.git "$SOUNDING_TMP" \
    || { echo "sounding 克隆失败，请检查网络/路径"; exit 1; }
fi

# 审计 SKILL.md
echo "== sounding audit: SKILL.md =="
( cd "$SOUNDING_TMP" && PYTHONPATH=src "$PYTHON" -m sounding.cli audit "$SKILL_DIR_WIN" )

# 可选：导出并审计 flomo MCP 工具
if [ "$DO_MCP" -eq 1 ]; then
  DESC="$SOUNDING_TMP/flomo_mcp_tools.json"
  DESC_WIN="$SOUNDING_TMP_WIN\\flomo_mcp_tools.json"
  "$PYTHON" - "$FLOMO_CLIENT_WIN" "$DESC_WIN" <<'PY'
import ast, json, sys
src = open(sys.argv[1], encoding="utf-8").read()
tree = ast.parse(src)
tools = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "FLOMO_MCP_TOOLS":
                tools = ast.literal_eval(node.value)
if tools is None:
    sys.exit("FLOMO_MCP_TOOLS 未找到")
descriptor = {"name": "flomo", "version": "1.0.0", "tools": tools}
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(descriptor, f, ensure_ascii=False, indent=2)
print("导出 descriptor ->", sys.argv[2])
PY
  echo "== sounding audit: flomo MCP tools =="
  ( cd "$SOUNDING_TMP" && PYTHONPATH=src "$PYTHON" -m sounding.cli audit "$DESC_WIN" )
fi

# 清理临时克隆（默认用完即清，符合 SKILL 记忆维护纪律）
if [ "$KEEP" -eq 0 ]; then
  rm -rf "$SOUNDING_TMP"
  echo "已清理临时克隆 $SOUNDING_TMP"
fi
