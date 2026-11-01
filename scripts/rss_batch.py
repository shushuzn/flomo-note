#!/usr/bin/env python3
"""rss_batch.py — 把「一次 10 条、处理前先拉取」固化成机制。

背景：RSS 条目此前的处理形态是「人肉挑一条、抓一条、判一条」，挑哪条全凭当下
翻到哪。条目一多就容易反复抓同一条、或漏掉整组。本脚本把机械部分串起来：

  prepare  先拉取（`rss_fetch`）→ 取 N 条未读 → 逐条抓正文落 `_tmp/`
           → 跑一遍查重预检 → 写一份 manifest
  done     按 manifest 把处理过的条目标记已读

**分工不变**：本脚本只备料，不判断价值。哪条建卡、哪条退出留痕，仍由人按 SOP
决定；判不了的不标记，留在未读里下次再进批。

**为什么先拉取**：未读保留期只有 7 天，超期不拉的条目没见到就没了。批次开始
前拉一次，保证这一批看到的是当前最新的未读集合，而不是几小时前的快照。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402
import rss_store  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "_tmp" / "rss_batch.json"

SPEC = [
    Opt("--size", "size", "int", low=1, high=50, default=10,
        hint="（一批几条，默认 10）"),
    Opt("--feed", "feed", default="", hint="（源地址子串，只取某个源的未读）"),
    Opt("--manifest", "manifest", default=str(DEFAULT_MANIFEST)),
    Opt("--db", "db", default=rss_store.DEFAULT_DB),
    Opt("--no-fetch", "no_fetch", "flag"),
    Opt("--if-older-than", "if_older_than", "float", low=0.0, high=8760.0, default=None),
]


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _fetch_once(url: str) -> tuple[bool, str, str]:
    """抓一条正文，返回 (成功, 本地文件路径, 标题)。

    失败不抛：一个源挂掉不该让整批备料停下来，manifest 里记为失败即可。
    """
    code, out, err = _run([sys.executable, str(SCRIPT_DIR / "fetch_article.py"), url],
                          timeout=180)
    title = ""
    local = ""
    for line in out.splitlines():
        if line.startswith("[标题] "):
            title = line[len("[标题] "):].strip()
        elif line.startswith("[文件] "):
            local = line[len("[文件] "):].strip()
    if code != 0 or not local:
        detail = (err.strip() or out.strip() or "").splitlines()
        return False, "", (detail[0] if detail else f"退出码 {code}")
    return True, local, title


def prepare(opts: dict) -> int:
    """拉取 → 取 N 条未读 → 抓正文 → 预查重 → 写 manifest。"""
    if not opts["no_fetch"]:
        cmd = [sys.executable, str(SCRIPT_DIR / "rss_fetch.py")]
        if opts["if_older_than"] is not None:
            cmd += ["--if-older-than", str(opts["if_older_than"])]
        code, out, err = _run(cmd, timeout=600)
        for line in out.splitlines():
            if line.startswith(("[上次拉取]", "[入库]", "[清理]", "[跳过]", "[告警]")):
                print(line)
        if code not in (0, 2):
            # 2 = 有源失败但其余拉到了，批照样能备
            print(f"[失败] 拉取未完成（退出码 {code}）："
                  f"{(err.strip() or out.strip()).splitlines()[0] if (err.strip() or out.strip()) else ''}",
                  file=sys.stderr)
            return 1

    conn = rss_store.connect(opts["db"])
    try:
        rows = rss_store.list_items(conn, unread_only=True, feed=opts["feed"],
                                    limit=opts["size"])
        if not rows:
            print("[批次] 没有未读条目可处理")
            return 0
        items = []
        for index, row in enumerate(rows, 1):
            ok, local, title = _fetch_once(row["link"])
            entry = {
                "n": index,
                "id": row["id"],
                "title": title or row["title"],
                "feed_title": row["title"],
                "link": row["link"],
                "feed": row["feed"],
                "fetched": ok,
                "local": local,
                "error": "" if ok else title,
            }
            items.append(entry)
            flag = "✓" if ok else "✗"
            print(f"[备料] {index}/{len(rows)} {flag} {entry['title'][:44]}"
                  + ("" if ok else f" —— {entry['error'][:40]}"))
        manifest = {
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "size": len(items),
            "items": items,
        }
        path = Path(opts["manifest"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        ok_count = sum(1 for item in items if item["fetched"])
        print(f"[批次] 已备 {ok_count}/{len(items)} 条，manifest：{path}")
        if ok_count < len(items):
            print(f"[告警] {len(items) - ok_count} 条抓取失败，manifest 里 fetched=false，"
                  f"判不了就不要标记已读", file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


def done(opts: dict, keys: list[str]) -> int:
    """按 manifest 标记已读；`all` 表示整批，否则按编号或链接挑。"""
    path = Path(opts["manifest"])
    if not path.exists():
        print(f"[失败] manifest 不存在：{path}——先跑 prepare", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[失败] manifest 不是合法 JSON：{exc}", file=sys.stderr)
        return 1
    items = manifest.get("items") or []
    if not items:
        print("[失败] manifest 里没有条目", file=sys.stderr)
        return 1

    if keys == ["all"]:
        picked = items
    else:
        picked = []
        for key in keys:
            hit = [i for i in items if str(i.get("n")) == key or i.get("link") == key]
            if not hit:
                print(f"[失败] manifest 里找不到：{key}", file=sys.stderr)
                return 1
            picked.append(hit[0])

    conn = rss_store.connect(opts["db"])
    try:
        links = [item["link"] for item in picked]
        hit, miss = rss_store.mark_read(conn, links)
    finally:
        conn.close()
    print(f"[标记] 已读 {len(hit)} 条" + (f"；未找到 {len(miss)} 条" if miss else ""))
    stat = rss_store.connect(opts["db"])
    try:
        counts = rss_store.counts(stat)
    finally:
        stat.close()
    print(f"[库存] 共 {counts['total']} 条，未读 {counts['unread']}")
    return 1 if miss else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, positional = parse_options(argv, SPEC, positional_ok=True)
    if not positional:
        raise UsageError("缺子命令：prepare / done")
    cmd, keys = positional[0], positional[1:]
    if cmd == "prepare":
        if keys:
            raise UsageError(f"prepare 不接受位置参数：{keys[0]}")
        return prepare(opts)
    if cmd == "done":
        if not keys:
            raise UsageError("done 要给 all 或条目编号/链接")
        return done(opts, keys)
    raise UsageError(f"未知子命令：{cmd}（可用 prepare / done）")


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=1))
