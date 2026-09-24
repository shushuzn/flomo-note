#!/usr/bin/env python3
"""cleanup.py — 每轮收尾强制清理残留临时文件（H27）。

背景：写卡 / 更新 SKILL 会产生大量中间产物——工作区 _tmp_*、D:\\tmp 抓取件
（arxiv_* / ithome_* / flomo_*）、.workbuddy/lb_out 历轮请求与回执。这些必须每轮
归档删除，否则堆积会被判定严重违规。此前每次都手搓一个 _tmp_cleanup_*.py，
既重复又易漏。本脚本把"显式列名→打包 trash→断言成员数→删除→复核残留0"固化。

保留项（绝不删）：
  - 项目根 _tmp_extract.py（项目抽取工具）
  - tag_tree.txt（本地标签快照，gitignore）
  - .workbuddy/trash/*（历史备份包）
  - D:\\tmp/arxiv_test.xml、arxiv_vibe.xml（他项目旧件，不在本技能范围）

删除范围：
  - 项目根：_tmp_*（除 _tmp_extract.py）
  - D:\\tmp：arxiv_*.html、arxiv_*_abs.html、ithome_*.html、flomo_*
            （显式排除 arxiv_test.xml、arxiv_vibe.xml）
  - .workbuddy/lb_out/*_create.json、*_memo.txt、*.err

用法：
  python scripts/cleanup.py            # 执行清理（默认）
  python scripts/cleanup.py --dry-run  # 只列名不删
  python scripts/cleanup.py --verify   # 只复核残留是否为 0（CI / 自校，非 0 则退出码 1）
"""
import argparse
import os
import stat
import sys
import tarfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path("D:/tmp")
TRASH_DIR = PROJECT_ROOT / ".workbuddy" / "trash"
LB_OUT = PROJECT_ROOT / ".workbuddy" / "lb_out"

# (基准目录, [glob 模式...])
SCAN = [
    (PROJECT_ROOT, ["_tmp_*"]),
    (TMP_DIR, ["arxiv_*.html", "arxiv_*_abs.html", "ithome_*.html", "flomo_*"]),
    (LB_OUT, ["*_create.json", "*_memo.txt", "*.err"]),
]

# 显式保留（命中 glob 也不删）：键为所在目录，值为文件名集合
KEEP = {
    PROJECT_ROOT: {"_tmp_extract.py", "tag_tree.txt"},
    TMP_DIR: {"arxiv_test.xml", "arxiv_vibe.xml"},
}


def _force_remove(p: Path):
    try:
        p.chmod(stat.S_IWRITE)
    except OSError:
        pass
    try:
        os.remove(p)
    except FileNotFoundError:
        pass


def collect():
    candidates = []
    for base, patterns in SCAN:
        if not base.exists():
            continue
        for pat in patterns:
            for p in base.glob(pat):
                if not p.is_file():
                    continue
                keep = KEEP.get(base)
                if keep is not None and p.name in keep:
                    continue
                candidates.append(p)
    # 去重并排序，稳定输出
    uniq = sorted({str(p) for p in candidates})
    return [Path(p) for p in uniq]


def verify():
    left = collect()
    return left


def main():
    ap = argparse.ArgumentParser(description="每轮收尾强制清理残留临时文件 (H27)")
    ap.add_argument("--dry-run", action="store_true", help="只列名不删")
    ap.add_argument("--verify", action="store_true", help="只复核残留是否为 0")
    args = ap.parse_args()

    if args.verify:
        left = verify()
        if left:
            print(f"[cleanup] 残留 {len(left)} 个（应清未清）：")
            for p in left:
                print(f"  {p}")
            sys.exit(1)
        print("[cleanup] 残留 = 0，OK")
        return

    candidates = collect()
    print(f"[cleanup] 待清理 {len(candidates)} 个：")
    for p in candidates:
        print(f"  {p}")

    if args.dry_run:
        print("[cleanup] --dry-run，未删除")
        return

    if not candidates:
        print("[cleanup] 无残留，无需清理")
        return

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = TRASH_DIR / f"temp-cleanup-{ts}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for p in candidates:
            tf.add(p, arcname=str(p).replace(":", ""))
    members = len(tarfile.open(archive).getmembers())
    assert members == len(candidates), f"归档成员数 {members} != 应删 {len(candidates)}"

    for p in candidates:
        _force_remove(p)

    left = verify()
    assert len(left) == 0, f"复核失败，残留 {len(left)} 个"
    print(f"[cleanup] 已归档 {archive}（{members} 个），删除并复核残留 = 0，OK")


if __name__ == "__main__":
    main()
