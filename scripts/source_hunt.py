#!/usr/bin/env python3
"""source_hunt.py — 只给主题/术语时的自动取源（流程第 1 步的检索分支）。

为什么要有它：用户发一个词（`绿债`）时，正确动作是**自己去检索并抓回一篇能支撑
写卡的材料**，而不是回一句「请给我链接」。把执行者该做的检索推回用户，等于让
流程在原地空转：词已经说明要写什么，链不链接是执行侧该解决的事。

做法固定成一条链，每一步都有判据、失败也不回头问用户：
  1. `search_web` 现搜（引擎直连那一路，与流程第 2 步同一实现）；
  2. 按结果顺序逐条真抓，抽正文、裁首尾噪音；
  3. 只收「术语词块在正文里真的出现」且长度过阈的那一条——抓到 200 却讲别的，
     正是本脚本要挡的假成功；
  4. 落到 `_tmp/article_<slug>.txt` 并 `open_round` 开账，后面接的就是常规流程：
     Read 读回 → 列对象 → 定标签 → 查重 → …。

抓不到就非 0 退出并点名每条候选被否的原因（长度不足 / 词块不命中 / 抓取失败），
**不返回「没有这个说法」**——那是把网络问题读成事实问题。

用法：
  python scripts/source_hunt.py "绿债"
  python scripts/source_hunt.py "绿债" --query "绿债 市场规模 2026" --min-chars 1200
  python scripts/source_hunt.py "转型金融" --max-try 6 --json _tmp/hunt.json

退出码：
  0  已取到材料并开账（打印采用链接与落盘路径）
  1  所有候选都没过判据，或搜索无一路可信
  2  用法错误
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_article as FA
import round_ledger
import search_web as SW
from cli_util import Opt, UsageError, cli_entry, force_utf8, parse_options

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIN_CHARS = 600
DEFAULT_MAX_TRY = 5

_OPTS = [
    Opt("--query", "query", default=""),
    Opt("--min-chars", "min_chars", "int", low=1, default=DEFAULT_MIN_CHARS),
    Opt("--max-try", "max_try", "int", low=1, default=DEFAULT_MAX_TRY),
    Opt("--timeout", "timeout", "float", low=1, default=30.0),
    Opt("--via", "via", default="auto"),
    Opt("--json", "json_out", default=None),
    Opt("--no-ledger", "no_ledger", "flag"),
]


def _chunks(term: str) -> list[str]:
    """术语里的中文词块；英文/数字按整体算一个块。"""
    runs = re.findall(r"[\u4e00-\u9fa5]{2,}", term)
    tail = re.sub(r"[\u4e00-\u9fa5]+", " ", term).strip()
    if tail:
        runs.append(tail)
    return runs


def _relevance(body: str, chunks: list[str]) -> tuple[int, int]:
    return sum(1 for c in chunks if c in body), len(chunks)


def hunt(term: str, query: str, min_chars: int, max_try: int,
         timeout: float, via: str) -> dict:
    """搜一枪、逐条试抓；返回 {accepted, candidates, search}。"""
    q = query or term
    report = SW.run_search(q, timeout=timeout, via=via)
    chunks = _chunks(term)
    seen: set[str] = set()
    tried: list[dict] = []
    accepted = None
    for item in report["results"]:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if len(tried) >= max_try:
            break
        row = {"url": url, "title": item.get("title", ""), "ok": False, "reason": "",
               "chars": 0, "hits": 0, "file": ""}
        try:
            page, ctype, _channel = FA.fetch(url, timeout, via)
            row["ctype"] = ctype
            body, _extract_channel = FA.extract(page)
            body, _lead = FA.trim_lead_noise(body)
            body, _tail = FA.trim_tail_noise(body, min_chars)
        except FA.FetchError as exc:
            row["reason"] = f"抓取失败：{exc}"
            tried.append(row)
            continue
        except Exception as exc:  # noqa: BLE001 — 单条抽取出错不该终止整轮
            row["reason"] = f"抽取异常：{type(exc).__name__}: {exc}"
            tried.append(row)
            continue
        hits, _total = _relevance(body, chunks)
        row["chars"] = len(body)
        row["hits"] = hits
        if len(body) < min_chars:
            row["reason"] = f"正文仅 {len(body)} 字符（阈值 {min_chars}）"
            tried.append(row)
            continue
        # 词块必须真的出现：抓回来 200 却讲别的，是最贵的那种假成功。
        if hits == 0:
            row["reason"] = f"正文里没有术语词块（{'、'.join(chunks)}）"
            tried.append(row)
            continue
        out = PROJECT_ROOT / "_tmp" / f"article_{FA._slug(url)}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        row.update({"ok": True, "reason": "", "file": str(out)})
        tried.append(row)
        accepted = row
        break
    return {"term": term, "query": q, "search": report, "candidates": tried,
            "accepted": accepted, "chunks": chunks}


def main(argv=None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    ns, pos = parse_options(args, _OPTS, positional_ok=True)
    if not pos:
        raise UsageError('缺主题：python scripts/source_hunt.py "绿债" [选项]')
    if len(pos) > 1:
        raise UsageError(f"只接受一个主题词，多给了 {len(pos) - 1} 个：{' '.join(pos[1:])}")
    term = pos[0].strip()
    if not term:
        raise UsageError("主题词为空")

    result = hunt(term, ns["query"], ns["min_chars"], ns["max_try"],
                  ns["timeout"], ns["via"])

    report = result["search"]
    print(f"[检索] {result['query']}（引擎：{report.get('engine_used') or '无'}）")
    if report.get("search_url"):
        print(f"[检索页] {report['search_url']}")
    for item in report.get("rejected", []):
        print(f"[否决] {item['engine']}：{item['reason']}")

    for index, row in enumerate(result["candidates"], 1):
        mark = "采用" if row["ok"] else "跳过"
        print(f"[{mark}] {index}. {row['title'][:60] or row['url'][:60]}")
        print(f"     {row['url'][:110]}")
        if row["ok"]:
            print(f"     正文 {row['chars']} 字符，词块命中 {row['hits']}")
        else:
            print(f"     {row['reason']}")

    if ns["json_out"]:
        out = Path(ns["json_out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[文件] {out}")

    row = result["accepted"]
    if row is None:
        print("[失败] 没有一条候选能支撑写卡——换查询词（--query）或换网络，"
              "别把「没抓到」当成「没这个主题」", file=sys.stderr)
        return 1
    if not ns["no_ledger"]:
        round_ledger.open_round(row["url"], row["file"])
        print("[关账] 已开账：建卡或退出后跑 exit_confirm.py --url 关闭")
    print(f"[采用链接] {row['url']}")
    print(f"[文件] {row['file']}")
    print(f"[下一步] 读回全文：Read {row['file']}（流程第 1 步纪律，未读回不得提炼）")
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=2))
