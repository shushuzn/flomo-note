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
与现有标签**只差空白、全半角斜杠或大小写**的，一律判「写错」并点出树里的那个写法
（判据与第 ⑧ 步闸门同一份，见 `memo_util.tag_key`）——云端会把它们当两个簇，照原样
写入即分裂。`--grep` 只列匹配标签、不做判定（找不到就说明该新建，而不是「没有这个簇」
就随便建）。只读，不建标签、不改标签。

退出码：
  0  全部存在且严格两级、无写法重复
  1  标签树被截断或调用失败（结论不可信）
  2  有标签缺失 / 层级不合规 / 与现有标签只差写法，或命令行参数写错（须按输出调整）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from flomo_client import FlomoClient, FlomoError, load_token, tool_data  # noqa: E402
from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402
from memo_util import tag_key  # noqa: E402

DEFAULT_LIMIT = 2000
SIBLING_SHOW = 12
GREP_SHOW = 60

_OPTS = [
    Opt("--tags", "raw_tags", default="",
        hint="；多个标签请用逗号在一次 --tags 里给全"),
    # None = 没给；"" = 给了空值（空正则会命中全部标签，另判）
    Opt("--grep", "pattern", default=None),
    Opt("--limit", "limit", "int", low=1, default=DEFAULT_LIMIT),
]


def split_tags(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").split(","):
        t = part.strip().lstrip("#").strip()
        if t and t not in out:
            out.append(t)
    return out


def load_tree(client: FlomoClient, limit: int) -> tuple[list[str], int]:
    """现采标签树，返回 (tags, total)。条数与 total 不符即视为截断（抛错）。

    回包解析走 `flomo_client.tool_data`（唯一实现）。此前只认 `structuredContent`，
    而 `sop_gate` 对同一回包做了逐字段兜底：云端一旦只给 `content[].text`，
    闸门判通过、本脚本报「回包缺 total 字段」——同一份数据两个结论，且错怪了标签树。
    """
    sc = tool_data(client.tool("tag_tree", {"limit": limit}))
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
    """核对单个标签：层级（H17 严格两级）、是否存在、同顶层兄弟二级、写法重复。

    `dup_of` 是**与写前闸门同一个判据**（`memo_util.tag_key`）：只差空白 / 全半角斜杠 /
    大小写的标签，云端会当两个簇，照输入写入即分裂（H18）。此前这里没有这一项，于是
    `AI / Agent`（斜杠两侧带空格）被判成「不存在，可直接新建此二级」——定标签的工具
    亲手把人引向新建近义簇，只在第 ⑧ 步闸门才被拦下，得回头重走一遍第 4 步。
    """
    parts = tag.split("/")
    depth_ok = len(parts) == 2 and all(p.strip() for p in parts)
    top = parts[0].strip() if parts else ""
    exists = tag in set(tags)
    siblings = [
        t for t in tags
        if t != tag and t.startswith(top + "/") and t.count("/") == 1
    ]
    key = tag_key(tag)
    dup_of = next((t for t in tags if t != tag and tag_key(t) == key), "")
    # 标签树只返回叶子形态「顶层/二级」，顶层本身不会单独成条：
    # 只比 t == top 会让任何"新建同顶层下的二级"都被误报成"顶层也不存在"，
    # 与紧跟着列出的同顶层兄弟二级自相矛盾。有任一子标签即视为顶层存在。
    # 按键比而非逐字比：`#ai/agent` 与树里的 `AI/Agent` 只差大小写，逐字比会报
    # 「该顶层也不存在，须连同顶层一起新建」——把已有的顶层重开一遍。
    top_k = tag_key(top)
    top_exists = any(tag_key(t) == top_k or tag_key(t).startswith(top_k + "/") for t in tags)
    return {
        "tag": tag,
        "depth_ok": depth_ok,
        "exists": exists,
        "top": top,
        "top_exists": top_exists,
        "siblings": siblings,
        "dup_of": dup_of,
    }


def main(argv: list[str] | None = None, client=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    # 参数写错（缺值/重复/未知/非整数）一律抛 UsageError，由入口按本脚本的用法码
    # 渲染成一行提示——`main` 里不再各写一份报错文案。
    ns, _pos = parse_options(args, _OPTS)
    raw_tags = ns["raw_tags"]
    pattern = ns["pattern"]
    limit = ns["limit"]

    tags_wanted = split_tags(raw_tags)
    if pattern == "":
        raise UsageError(
            "--grep 为空：空正则会命中全部标签，「命中 N 个」不构成任何证据；"
            "给个词或正则（只核指定标签则用 --tags）"
        )
    if not tags_wanted and pattern is None:
        raise UsageError("未给 --tags 或 --grep")

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
        if info["dup_of"]:
            # 先判写法重复再判层级：全角斜杠会让层级判定失败，报「层级不合规」会把人
            # 引向「改格式」，而真正该做的是改回树里那个写法。
            print(f"✗ {tag}  与现有「{info['dup_of']}」只差空白/全半角/大小写——云端会把它们当两个簇，"
                  "照原样写入即分裂（H18）；改回现有写法")
            bad += 1
            continue
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
    sys.exit(cli_entry(main, errors=(FlomoError,)))
