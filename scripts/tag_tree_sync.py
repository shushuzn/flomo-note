#!/usr/bin/env python3
"""tag_tree_sync.py — 标签树本地快照的现采重写与核对。

云端标签树的本地快照（格式见 SKILL「标签树本地留存」）此前没有工具，
每轮靠临时脚本手搓；而闸门的核对口径是另一份独立实现。两侧口径一旦漂移，
就会出现「重写明明做了、闸门仍判不自洽」的死锁——写卡流程卡在写云前。
本脚本把渲染与计数一律委托 `memo_util`，与 `sop_gate.py` 共用同一份实现。

用法：
  python scripts/tag_tree_sync.py                    # 现采云端 → 整体重写两处快照 → 复核
  python scripts/tag_tree_sync.py --check            # 只读比对（云端 vs 本地），不改文件
  python scripts/tag_tree_sync.py --response <path>  # 用已保存的 tag_tree 响应重写（离线）

退出码：0 = 快照与云端自洽（或已重写到自洽）；1 = 不一致且未重写（--check / 云端数据异常）。

`--response` 接受 flomo 工具的原始输出，兼容 structuredContent 与 content[].text 两种包裹，
便于离线重写与测试，不发任何网络请求。
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from memo_util import count_snapshot, render_tag_tree, snapshot_total  # noqa: E402

# 快照的全部留存路径：闸门对每条路径都做核对，须同时保持自洽。
SNAPSHOT_PATHS = [PROJECT_ROOT / "tag_tree.txt", SCRIPT_DIR / "tag_tree.txt"]


def _tags_total(result):
    """从 tag_tree 响应提取 (total, tags)。

    结构化数据优先取 structuredContent；极少数情况只给 content[].text 内层 JSON，
    此时兜底解析。取不到返回 (None, None)。
    """
    if not isinstance(result, dict):
        return None, None
    total = tags = None
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        total, tags = sc.get("total"), sc.get("tags")
    if total is None or tags is None:
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            try:
                inner = json.loads(block.get("text") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(inner, dict):
                continue
            if total is None:
                total = inner.get("total")
            if tags is None:
                tags = inner.get("tags")
    return total, tags


def _local_state(paths=None):
    """读各处快照现状，返回 [(路径, 首行 total, 实际列出数)]；缺文件时后两项为 None。"""
    state = []
    for p in (paths or SNAPSHOT_PATHS):
        if not p.exists():
            state.append((p, None, None))
            continue
        lines = p.read_text(encoding="utf-8-sig").splitlines()
        leaves, bare = count_snapshot(lines)
        state.append((p, snapshot_total(lines), leaves + bare))
    return state


def _check(total, tags, local):
    """核对云端与本地快照，返回 (是否自洽, 问题清单)。

    两件都要成立：① 云端数据自身能渲染出自洽的条数；② 每条本地路径的
    首行 total 与云端一致，且实际列出数与首行 total 一致。
    """
    problems = []
    made = render_tag_tree(tags, total).splitlines()
    expect = sum(count_snapshot(made))
    if expect != total:
        problems.append(f"云端数据自身不自洽：渲染后列出 {expect} 条 ≠ total={total}")
    for p, lt, listed in local:
        if lt is None and listed is None:
            problems.append(f"{p} 不存在（须先整体重写）")
            continue
        if lt != total:
            problems.append(f"{p} 首行 total={lt} ≠ 云端 total={total}")
        if listed != lt:
            problems.append(f"{p} 实际列出 {listed} 条 ≠ 首行 total={lt}")
    return (not problems), problems


def main():
    ap = argparse.ArgumentParser(description="标签树本地快照的现采重写与核对")
    ap.add_argument("--check", action="store_true", help="只读比对，不写文件")
    ap.add_argument("--response", help="改用已保存的 tag_tree 响应 JSON（离线，不发网络请求）")
    args = ap.parse_args()

    if args.response:
        result = json.loads(Path(args.response).read_text(encoding="utf-8"))
    else:
        from flomo_client import FlomoClient, load_token

        token, _src = load_token()
        client = FlomoClient(token)
        client.init()
        result = client.tool("tag_tree", {"limit": 2000})

    total, tags = _tags_total(result)
    if total is None or tags is None:
        print("[tag_tree] 响应未取到 total / tags（工具返回异常）", file=sys.stderr)
        return 1

    ok, problems = _check(total, tags, _local_state())
    if ok:
        print(f"[tag_tree] 本地快照与云端自洽（total={total}）")
        return 0

    print("[tag_tree] 发现不一致：")
    for x in problems:
        print("  · " + x)

    if args.check:
        print("[tag_tree] --check，未改写本地文件")
        return 1

    text = render_tag_tree(tags, total)
    for p in SNAPSHOT_PATHS:
        p.write_text(text, encoding="utf-8")
    print(f"[tag_tree] 已用云端数据整体重写 {len(SNAPSHOT_PATHS)} 处快照")

    ok2, problems2 = _check(total, tags, _local_state())
    if not ok2:
        for x in problems2:
            print("  ! " + x, file=sys.stderr)
        return 1
    print(f"[tag_tree] 复核通过：列出数 == total == {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
