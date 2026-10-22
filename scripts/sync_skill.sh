#!/usr/bin/env bash
# sync_skill.sh — 把仓库里的 SKILL.md 同步到实际被加载的技能目录。
#
# 为什么需要它：规则唯一来源是仓库的 SKILL.md，而技能加载读的是
# ~/.workbuddy/skills/flomo-note/SKILL.md，两者之间没有任何自动关联。
# 手工复制是唯一的同步动作，漏做时**不会让任何东西报错**——只表现为
# 「照着旧规则写卡」。实测仓库版与技能目录版差了 19 天的改动。
#
# 用法：
#   bash scripts/sync_skill.sh          # 复制并逐字节核对
#   bash scripts/sync_skill.sh --check  # 只比对（审计用），不一致非 0 退出
#
# 目标目录可用 FLOMO_SKILL_DIR 覆盖。
# 目标不存在时：--check 明确打印「未比对」（本机没装技能），同步模式报错退出、
# 不擅自创建目录——建一个没有 _user_meta.json 的目录，技能加载照样不认。
#
# 退出码：0 一致 / 已同步；1 不一致（--check）；2 参数或目标目录有问题；3 复制后核对失败
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/SKILL.md"
DST_DIR="${FLOMO_SKILL_DIR:-$HOME/.workbuddy/skills/flomo-note}"
DST="$DST_DIR/SKILL.md"

MODE="sync"
case "${1:-}" in
  "") ;;
  --check) MODE="check" ;;
  -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "[sync] 未知参数：$1（只接受 --check）" >&2; exit 2 ;;
esac

if [[ ! -f "$SRC" ]]; then
  echo "[sync] 找不到仓库 SKILL.md：$SRC" >&2
  exit 2
fi

if [[ ! -f "$DST" ]]; then
  if [[ "$MODE" == "check" ]]; then
    echo "[sync][跳过] 目标技能目录未安装（$DST_DIR），本机无从比对——不算通过"
    exit 0
  fi
  echo "[sync] 目标技能目录不存在或缺少 SKILL.md：$DST_DIR" >&2
  echo "[sync] 请先在工作台里安装 flomo-note 技能，或设 FLOMO_SKILL_DIR 指向它" >&2
  exit 2
fi

if cmp -s "$SRC" "$DST"; then
  echo "[sync] 一致：$DST（$(wc -c < "$SRC" | tr -d ' ') 字节）"
  exit 0
fi

if [[ "$MODE" == "check" ]]; then
  echo "[sync][不一致] 技能目录里的 SKILL.md 与仓库版不同——技能按旧规则执行" >&2
  echo "        仓库：$(wc -c < "$SRC" | tr -d ' ') 字节，$(date -r "$SRC" '+%Y-%m-%d %H:%M')" >&2
  echo "        技能：$(wc -c < "$DST" | tr -d ' ') 字节，$(date -r "$DST" '+%Y-%m-%d %H:%M')" >&2
  echo "        修法：bash scripts/sync_skill.sh" >&2
  exit 1
fi

# 复制前留一份旧副本，同步错了还能当场退回
cp "$DST" "$DST.bak"
cp "$SRC" "$DST"
if ! cmp -s "$SRC" "$DST"; then
  echo "[sync] 复制后核对不一致，已把旧版留在 $DST.bak" >&2
  exit 3
fi
echo "[sync] 已同步：$DST（$(wc -c < "$DST" | tr -d ' ') 字节；旧版留在 $DST.bak）"
