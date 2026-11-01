#!/usr/bin/env python3
"""rss_fetch.py — 从订阅源清单拉取 RSS/Atom，写入 `rss_store`，并跑保留期清理。

背景：此前挑素材靠人贴链接，源多的时候一天漏掉十几篇。本脚本把「拉取」这步固
化：读 OPML 清单 → 逐源抓 → 解析 RSS 2.0 / Atom → 入库 → 清理过期。

**与选做流程的分工**：本脚本只负责「把条目拿到手、摆进库里」，不判断哪条值得做
卡。挑条目仍由人（或后续流程）看 `rss_store list --unread` 决定；处理完一条就
`rss_store mark-read <链接>`，保留期从此按「已读一天」走。判据之所以留在人这
边，是因为「按流程处理过了」只有流程自己知道。

**OPML 而非自造配置**：`.gitignore` 里早已为 `feeds.opml` 留了位（阅读器导出的订
阅清单），直接复用它——一份清单既能喂阅读器也能喂本脚本，不必维护第二份。

**失败不阻塞**：单个源抓取或解析失败只记账继续跑，最后汇总报错。一个源挂了不
该让其余十几个源也拉不到。
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402
import net_util  # noqa: E402  出网通道与错误分类的唯一实现
import rss_store  # noqa: E402  存储、读/未读状态与保留期清理的唯一实现

DEFAULT_OPML = "feeds.opml"


def _strip_html(raw: str) -> str:
    """去标签压空白，只用来生成摘要——条目详情仍以原文为准，不走这里。"""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def _local(tag: str) -> str:
    """去掉命名空间前缀：Atom 与 RSS 的元素名按本地名匹配。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_feed(raw: bytes) -> list[dict]:
    """解析 RSS 2.0 或 Atom，返回统一形态的条目列表。

    两种格式的元素名不同（`item` / `entry`、`link` 的属性 vs 子元素），但字段语义
    一致，故在这里归一，`rss_store` 不必知道来源是哪种格式。
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"不是合法的 XML：{exc}") from exc

    out: list[dict] = []
    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        item: dict = {"title": "", "link": "", "author": "", "published": "", "summary": ""}
        for child in node:
            cname = _local(child.tag)
            text = (child.text or "").strip()
            if cname == "title":
                item["title"] = text
            elif cname == "link":
                # RSS 的 link 是元素文本；Atom 的 link 地址在 href 属性里
                item["link"] = text or child.attrib.get("href", "")
            elif cname in ("pubDate", "published", "updated", "date"):
                item["published"] = text
            elif cname in ("creator", "author"):
                if cname == "author":
                    # Atom 的 author 下还有一层 name
                    inner = [c for c in child if _local(c.tag) == "name"]
                    text = (inner[0].text or "").strip() if inner else text
                item["author"] = text
            elif cname in ("description", "summary", "encoded"):
                item["summary"] = _strip_html(text)[:500]
        if item["link"]:
            out.append(item)
    return out


def read_opml(path: str | Path) -> list[str]:
    """读 OPML 里的 xmlUrl。缺文件返回空表——由调用方决定是报错还是跳过。"""
    p = Path(path)
    if not p.exists():
        return []
    root = ET.fromstring(p.read_bytes())
    urls: list[str] = []
    for node in root.iter():
        if _local(node.tag) == "outline":
            url = node.attrib.get("xmlUrl")
            if url:
                urls.append(url.strip())
    return urls


SPEC = [
    Opt("--opml", "opml", default=DEFAULT_OPML),
    Opt("--db", "db", default=rss_store.DEFAULT_DB),
    Opt("--limit", "limit", "int", low=1, high=500, default=100,
        hint="（每个源最多取几条，防止个别源一次刷出几百条）"),
    Opt("--if-older-than", "if_older_than", "float", low=0.0, high=8760.0, default=None,
        hint="（小时；上次拉取还没这么久就整体跳过，供高频调用方使用）"),
    Opt("--no-gc", "no_gc", "flag"),
    Opt("--timeout", "timeout", "float", low=1.0, high=120.0, default=30.0),
]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, positional = parse_options(argv, SPEC, positional_ok=True)
    if positional:
        raise UsageError(f"不接受位置参数：{positional[0]}（用法见 --help）")

    feeds = read_opml(opts["opml"])
    if not feeds:
        print(f"[失败] 没读到订阅源：{opts['opml']} 不存在或其中没有 xmlUrl",
              file=sys.stderr)
        return 1

    conn = rss_store.connect(opts["db"])

    # 距上次拉取太久就先说一声：本环境没有 cron，拉取全靠人（或调用方）触发，
    # 而「未读七天」的保留期意味着超过七天不拉，条目没见到就过期了。
    age = rss_store.fetch_age_hours(conn)
    if age is not None:
        stale = "（已超过 7 天，未读条目可能正在过期）" if age > 24 * rss_store.UNREAD_KEEP_DAYS else ""
        print(f"[上次拉取] {age:.1f} 小时前{stale}")
    else:
        print("[上次拉取] 从未拉取过")

    if opts["if_older_than"] is not None and age is not None and age < opts["if_older_than"]:
        print(f"[跳过] 上次拉取仅 {age:.1f} 小时前，不足 {opts['if_older_than']} 小时")
        return 0
    total_new = total_dup = 0
    failures: list[str] = []
    try:
        for feed in feeds:
            try:
                reply = net_util.request(feed, timeout=opts["timeout"])
                items = parse_feed(reply.body)[: opts["limit"]]
            except Exception as exc:  # 单源失败不阻塞其余源
                failures.append(f"{feed}：{exc}")
                continue
            added, dup = rss_store.add_items(conn, feed, items)
            total_new += added
            total_dup += dup
            print(f"[拉取] {feed} → 新增 {added}、重复 {dup}")

        stat = rss_store.counts(conn)
        stamp = rss_store.record_fetch(conn)
        print(f"[入库] 合计新增 {total_new}、重复 {total_dup}；"
              f"库内 {stat['total']} 条（未读 {stat['unread']}）；拉取于 {stamp[:19]}")

        if opts["no_gc"]:
            print("[清理] 已跳过（--no-gc）")
        else:
            result = rss_store.gc(conn)
            print(f"[清理] 删除 {result['removed']} 条"
                  f"（未读早于 {result['unread_cut'][:10]}、"
                  f"已读早于 {result['read_cut'][:10]}）")

        if failures:
            print(f"[告警] {len(failures)} 个源失败：", file=sys.stderr)
            for line in failures:
                print(f"  - {line}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=1))
