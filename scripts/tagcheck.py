#!/usr/bin/env python3
"""tagcheck.py — 定标签前的标签树核对（流程第 4 步）。

为什么要有它：定标签要求「现采 tag_tree，核验拟用标签是否已存在、是否严格两级、
同一顶层下已有哪些二级」。此前这一步靠人工调 tag_tree 再肉眼扫——671 个标签里
找一个二级名是否已存在，看漏了就会多建一个近义簇（H18 要防的正是这个）。

用法：
  python scripts/tagcheck.py --tags "AI/Agent,科技/测试"
  python scripts/tagcheck.py --tags "#AI/Agent" --limit 2000
  python scripts/tagcheck.py --grep "多语言|本地化"      # 只有标签名不确定时：先列出匹配的现有标签

输出：逐个标签给 存在 / 层级 / 同顶层兄弟二级（供判断该复用还是新建更精准的二级）。
`--grep` 只列匹配标签、不做判定（找不到就说明该新建，而不是「没有这个簇」就随便建）。
只读，不建标签、不改标签。

退出码：
  0  全部存在且严格两级
  1  标签树被截断或调用失败（结论不可信）
  2  有标签缺失或层级不合规（须按输出调整）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from flomo_client import FlomoClient, FlomoError, load_token  # noqa: E402

DEFAULT_LIMIT = 2000
SIBLING_SHOW = 12
GREP_SHOW = 60


def split_tags(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").split(","):
        t = part.strip().lstrip("#").strip()
        if t and t not in out:
            out.append(t)
    return out


def load_tree(client: FlomoClient, limit: int) -> tuple[list[str], int]:
    """现采标签树，返回 (tags, total)。条数与 total 不符即视为截断（抛错）。"""
    res = client.tool("tag_tree", {"limit": limit})
    sc = res.get("structuredContent") or {}
    tags = sc.get("tags") or []
    total = sc.get("total")
    if total is None:
        raise RuntimeError("tag_tree 回包缺 total 字段，无法判断是否截断")
    if len(tags) != total:
        raise RuntimeError(
            f"标签树被截断（返回 {len(tags)} / total {total}），结论不可信；"
            "调大 --limit 重采"
        )
    return [str(t) for t in tags], int(total)


def inspect_tag(tag: str, tags: list[str]) -> dict:
    """核对单个标签：层级（H17 严格两级）、是否存在、同顶层兄弟二级。"""
    parts = tag.split("/")
    depth_ok = len(parts) == 2 and all(p.strip() for p in parts)
    top = parts[0].strip() if parts else ""
    exists = tag in set(tags)
    siblings = [
        t for t in tags
        if t != tag and t.startswith(top + "/") and t.count("/") == 1
    ]
    top_exists = any(t == top for t in tags)
    return {
        "tag": tag,
        "depth_ok": depth_ok,
        "exists": exists,
        "top": top,
        "top_exists": top_exists,
        "siblings": siblings,
    }


def main(argv: list[str] | None = None, client=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    raw_tags = ""
    pattern = ""
    limit = DEFAULT_LIMIT
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tags" and i + 1 < len(args):
            raw_tags = args[i + 1]
            i += 2
        elif a == "--grep" and i + 1 < len(args):
            pattern = args[i + 1]
            i += 2
        elif a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        else:
            print(f"[失败] 未知参数：{a}", file=sys.stderr)
            return 2

    tags_wanted = split_tags(raw_tags)
    if not tags_wanted and not pattern:
        print("[失败] 未给 --tags 或 --grep", file=sys.stderr)
        return 2

    try:
        if client is None:
            token, _src = load_token()
            client = FlomoClient(token)
            client.init()
        tags, total = load_tree(client, limit)
    except (FlomoError, RuntimeError) as exc:
        print(f"[失败] 标签树未取到：{exc}", file=sys.stderr)
        return 1

    print(f"[标签树] {total} 个（limit={limit}，条数与 total 相符）")
    if pattern:
        try:
            rx = re.compile(pattern, re.I)
        except re.error as exc:
            print(f"[失败] --grep 正则有误：{exc}", file=sys.stderr)
            return 2
        hits = [t for t in tags if rx.search(t)]
        print(f"[匹配] 正则「{pattern}」命中 {len(hits)} 个")
        for t in hits[:GREP_SHOW]:
            print("   " + t)
        if len(hits) > GREP_SHOW:
            print(f"   …另 {len(hits) - GREP_SHOW} 个未列出（缩小 --grep 范围再看）")
        if not hits:
            print("   现有标签里没有匹配项；该新建还是换词再找，按 H18 判，不要据「搜不到」就建近义簇")
        if not tags_wanted:
            return 0

    bad = 0
    for tag in tags_wanted:
        info = inspect_tag(tag, tags)
        if not info["depth_ok"]:
            print(f"✗ {tag}  层级不合规：须严格两级（#顶层/二级），禁三级或单层（H17）")
            bad += 1
            continue
        if info["exists"]:
            print(f"✓ {tag}  已存在（同顶层 {len(info['siblings'])} 个二级）")
        else:
            hint = "该顶层存在，可直接新建此二级" if info["top_exists"] else "该顶层也不存在，须连同顶层一起新建"
            print(f"✗ {tag}  不存在；{hint}")
            bad += 1
        if info["siblings"]:
            shown = info["siblings"][:SIBLING_SHOW]
            more = len(info["siblings"]) - len(shown)
            print(f"   同顶层二级：{', '.join(shown)}" + (f" …另 {more} 个" if more > 0 else ""))

    if bad:
        print(f"\n[结论] {bad}/{len(tags_wanted)} 个标签不可直接用，按上面提示调整后重跑")
        return 2
    print("\n[结论] 全部存在且严格两级，可直接写入首行标签段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
