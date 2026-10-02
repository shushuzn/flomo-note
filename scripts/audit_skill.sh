#!/usr/bin/env bash
# audit_skill.sh — flomo-note SKILL.md 本地自校（sounding 静态审计）
#
# 把 SKILL 记忆维护段的"改动后本地自校"落成一键命令：克隆 sounding（缺失时）→
# 审计仓库根目录（SKILL.md 所在处）→ 默认用完即清临时克隆（符合 SKILL 记忆维护纪律）。
# 审计确定性、不联网、不改文件、可复现；目标 score 100/100。
#
# 用法:
#   ./audit_skill.sh            # 审计 SKILL.md，结束清理临时克隆
#   ./audit_skill.sh --keep     # 审计后保留临时克隆（调试用）
#   ./audit_skill.sh --mcp      # 额外导出 FLOMO_MCP_TOOLS descriptor 并用 audit 审 flomo MCP 工具
#
# 环境变量（按需覆盖）:
#   SOUNDING_PY   审计用 Python（默认托管 python 3.13）
#   SOUNDING_TMP  sounding 目录：指向已存在的**可用**副本即复用、跳过克隆（可离线跑），
#                 且本脚本不再删除该目录；不设时自动 mktemp 并在结束时清理。
#                 「可用」按能否 import sounding.cli 实测判定，残缺副本会被清掉重克隆。
#
# 离线用法（github.com 不可达时）:
#   curl -sSL -o /tmp/sounding.tar.gz \
#     https://codeload.github.com/alinotfoundbtw/sounding/tar.gz/refs/heads/main
#   mkdir -p .workbuddy/cache/sounding
#   tar xzf /tmp/sounding.tar.gz -C .workbuddy/cache/sounding --strip-components=1
#   SOUNDING_TMP=.workbuddy/cache/sounding ./audit_skill.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# SKILL.md 位置：技能文档的唯一位置是仓库根目录。
# 曾误判为 .kilo/skills/flomo-note（编辑器项目目录），导致仓库里并存两份技能文档、
# 且被审计的是陈旧副本；此处直接取仓库根，并做目录名校验（sounding 按 frontmatter.name 校验）。
SKILL_DIR="$REPO_ROOT"
FLOMO_CLIENT="$SCRIPT_DIR/flomo_client.py"

# Python 解释器：优先 SOUNDING_PY，其次当前平台 python3/python（跨平台，不写死路径）
if [ -n "${SOUNDING_PY:-}" ]; then
  PYTHON="$SOUNDING_PY"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "未找到 Python 解释器，请设置 SOUNDING_PY" >&2; exit 2
fi

# SOUNDING_OWNED=1 表示本目录由本次运行创建，结束时可安全清理；
# 用户通过环境变量传入的目录一律保留，避免误删其本地副本。
if [ -z "${SOUNDING_TMP:-}" ]; then
  SOUNDING_OWNED=1
  SOUNDING_TMP="$(mktemp -d)"
else
  SOUNDING_OWNED=0
fi

# 路径形态：仅当 cygpath 可用（Windows git-bash）才转换，其它平台用原路径
if command -v cygpath >/dev/null 2>&1; then
  SKILL_DIR_WIN="$(cygpath -w "$SKILL_DIR")"
  FLOMO_CLIENT_WIN="$(cygpath -w "$FLOMO_CLIENT")"
  SOUNDING_TMP_WIN="$(cygpath -w "$SOUNDING_TMP")"
else
  SKILL_DIR_WIN="$SKILL_DIR"
  FLOMO_CLIENT_WIN="$FLOMO_CLIENT"
  SOUNDING_TMP_WIN="$SOUNDING_TMP"
fi

DO_MCP=0
KEEP=0
rc=0
for a in "$@"; do
  case "$a" in
    --mcp)  DO_MCP=1 ;;
    --keep) KEEP=1 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

# 跑一次审计并解析 score：score<100（即存在 finding）则记失败，便于接 CI 门禁
run_audit() {
  local target="$1"
  local out
  out="$( cd "$SOUNDING_TMP" && PYTHONPATH="$SOUNDING_TMP_WIN/src" "$PYTHON" -m sounding.cli audit "$target" 2>&1 )"
  echo "$out"
  local score
  score="$(printf '%s' "$out" | grep -oE 'score[[:space:]]+[0-9]+' | grep -oE '[0-9]+' | head -1)"
  if [ -z "$score" ] || [ "$score" -lt 100 ]; then
    echo "  !! 审计未通过（score=${score:-?}/100），见上方输出"
    return 1
  fi
  return 0
}

# 准备 sounding：已有**可用**副本则复用（可离线），否则克隆。
# 可用性必须实测导入，不能只看目录存在——残留目录（源文件已失、只剩 __pycache__）
# 会被当成可复用副本直接跳过克隆，随后 `-m sounding.cli` 报 "No module named"，
# 看起来像"审计未通过"，实则是副本残缺：既误导结论，也让 audit 通道整体不可用。
sounding_usable() {
  [ -d "$SOUNDING_TMP/src/sounding" ] || return 1
  ( cd "$SOUNDING_TMP" && PYTHONPATH="$SOUNDING_TMP_WIN/src" "$PYTHON" \
      -c 'import sounding.cli' >/dev/null 2>&1 )
}

if sounding_usable; then
  echo "复用已有 sounding 副本 $SOUNDING_TMP（跳过克隆）"
elif [ -d "$SOUNDING_TMP/src/sounding" ]; then
  echo "sounding 副本 $SOUNDING_TMP 残缺（无法导入 sounding.cli），改用干净副本 ..."
  rm -rf "$SOUNDING_TMP"
  echo "克隆 sounding 到 $SOUNDING_TMP ..."
  git clone --depth 1 https://github.com/alinotfoundbtw/sounding.git "$SOUNDING_TMP" \
    || { echo "sounding 克隆失败，请检查网络/路径"; \
         echo "  github.com 不可达时可用 codeload 离线包（见脚本头部注释），"; \
         echo "  再以 SOUNDING_TMP=<副本目录> 重跑本脚本。"; exit 1; }
  sounding_usable || { echo "克隆后仍无法导入 sounding.cli，副本不完整" >&2; exit 1; }
elif [ -d "$SOUNDING_TMP/.git" ]; then
  echo "已有 git 仓库但缺 src/sounding，尝试更新 ..."
  git -C "$SOUNDING_TMP" pull --depth 1 || echo "  !! 更新失败，按现有内容继续"
else
  echo "克隆 sounding 到 $SOUNDING_TMP ..."
  git clone --depth 1 https://github.com/alinotfoundbtw/sounding.git "$SOUNDING_TMP" \
    || { echo "sounding 克隆失败，请检查网络/路径"; \
         echo "  github.com 不可达时可用 codeload 离线包（见脚本头部注释），"; \
         echo "  再以 SOUNDING_TMP=<副本目录> 重跑本脚本。"; exit 1; }
fi

# 审计 SKILL.md
echo "== sounding audit: SKILL.md =="
run_audit "$SKILL_DIR_WIN" || rc=1

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
  run_audit "$DESC_WIN" || rc=1
fi

# 清理临时克隆（默认用完即清，符合 SKILL 记忆维护纪律）
# 只清理本次运行创建的临时目录；SOUNDING_TMP 由用户传入时一律保留。
if [ "$KEEP" -eq 0 ] && [ "$SOUNDING_OWNED" -eq 1 ]; then
  rm -rf "$SOUNDING_TMP"
  echo "已清理临时克隆 $SOUNDING_TMP"
else
  echo "保留 $SOUNDING_TMP（非本次创建或指定了 --keep）"
fi

# 任一次审计 score<100 即返回非 0（CI 门禁）
exit $rc
