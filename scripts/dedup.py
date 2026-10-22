#!/usr/bin/env python3
"""dedup.py — 查重与近邻复查的逐次现查执行器（流程第 5 步 / 第 8 步）。

为什么要有它：查重必须对每个关键词**单独**调一次 `memo_search`，命中卡再按 H1 用
`memo_batch_get` 取全文。此前这套动作靠人工拼命令加临时脚本解析回包，id 靠手抄——
手抄错一位就指向另一张卡（H5 明令禁止）。

本脚本按 H3 的逐次现查口径执行：
- **每个关键词单独调用一次**，每次返回单独成块打印，不合并成一张表；
- 再做 id 去重与自动分批取全文（服务端单次上限 10 张），不手抄任何 id；
- 末尾给出程序提取的 id 清单，供后续 `memo_batch_get` / 复盘直接引用。

它不替代判断：命中卡是否同一事件，仍由调用方按 H8 读全文判定。

用法：
  python scripts/dedup.py --keywords "智能体,agent,工具契约" [--tag "AI/Agent"] [--limit 10]
  python scripts/dedup.py --id <本卡 memo id> [--limit 10]     # 复盘路：memo_recommended
  python scripts/dedup.py --ids ID1,ID2                         # 点名读这几张的全文（不受 --max-full 限制）
  python scripts/dedup.py ... --no-full                         # 只看命中清单，不取全文
  python scripts/dedup.py ... --max-full 20                     # 全文卡片数上限（默认 8，超出显式列出未读 id）

`--id` 与 `--keywords`/`--tag` 可以同时给，两路都跑；此前 `--id` 会静默吞掉关键词那一路。

退出码：
  0  成功（无命中也算成功，会明确打印「无命中」）
  1  任一云端调用失败——**不作「无命中」处理**，查重没跑完不许进入判定
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from flomo_client import (  # noqa: E402
    FlomoClient,
    FlomoError,
    Opt,
    UsageError,
    _result_memos,
    cli_entry,
    load_token,
    parse_options,
)

BATCH_LIMIT = 10  # 服务端 memo_batch_get 单次上限，超过即整批报错

_OPTS = [
    Opt("--keywords", "keywords_raw", default="",
        hint="；多个关键词请用逗号在一次里给全"),
    Opt("--tag", "tag", default=""),
    Opt("--id", "anchor", default=""),
    Opt("--ids", "ids_raw", default="", hint="；多个 id 请用逗号在一次里给全"),
    Opt("--limit", "limit", "int", low=1, default=10),
    Opt("--max-full", "max_full", "int", low=0, default=8),
    # 取反开关：`--no-full` 给的是「不取全文」，落到 full 上要用 `not`
    Opt("--no-full", "no_full", "flag"),
]


def split_keywords(raw: str) -> list[str]:
    """拆关键词：半角/全角逗号、顿号、分号、换行都认；去空去重保序。"""
    out: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").replace("；", ",").replace(
        ";", ","
    ).split(","):
        k = part.strip()
        if k and k not in out:
            out.append(k)
    return out


def brief_line(memo: dict) -> str:
    content = memo.get("content") or ""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    concept = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    tags = memo.get("tags") or []
    return (
        f"{memo.get('id')} | {memo.get('word_count')} 字 | "
        f"{','.join(tags) if isinstance(tags, list) else tags} | {concept[:48]}"
    )


def collect(memos: list[dict], seen: list[str]) -> None:
    for m in memos:
        mid = m.get("id")
        if mid and mid not in seen:
            seen.append(mid)


def search_one(client: FlomoClient, label: str, args: dict) -> list[dict]:
    """执行一次检索并**单独打印本次返回**（H3：每次调用的结果不许被合并掩盖）。"""
    res = client.tool("memo_search", args)
    memos = _result_memos(res)
    print(f"── {label} → {len(memos)} 条")
    if not memos:
        print("   （无命中）")
    for m in memos:
        print("   " + brief_line(m))
    return memos


def fetch_full(client: FlomoClient, ids: list[str], max_full: int) -> None:
    """按 ≤10 张一批取全文并逐张打印（H1：判正文必须取全文，不能用截断快照）。"""
    if not ids:
        return
    target = ids[:max_full]
    print(f"\n── 全文（{len(target)}/{len(ids)} 张，单批 ≤{BATCH_LIMIT}）")
    if len(ids) > max_full:
        skipped = ids[max_full:]
        print(
            f"   [告警] 超出 --max-full={max_full}，未取全文的 id（{len(skipped)} 张）："
            f"{', '.join(skipped)}"
        )
        print("   [提示] 要点名读其中几张：--ids ID1,ID2（显式点名的不受此上限约束）")
    for i in range(0, len(target), BATCH_LIMIT):
        chunk = target[i:i + BATCH_LIMIT]
        res = client.tool("memo_batch_get", {"ids": chunk})
        memos = _result_memos(res)
        got = {m.get("id") for m in memos}
        for mid in chunk:
            if mid not in got:
                print(f"   [告警] 本批未取到 id={mid}（不重发，须人工核对）")
        for m in memos:
            tags = m.get("tags") or []
            print(
                f"\n===== {m.get('id')} | {m.get('word_count')} 字 | "
                f"{','.join(tags) if isinstance(tags, list) else tags}"
            )
            print(m.get("content") or "")


def main(argv: list[str] | None = None, client=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    ns, _pos = parse_options(args, _OPTS)
    keywords_raw = ns["keywords_raw"]
    tag = ns["tag"]
    anchor = ns["anchor"]
    ids_raw = ns["ids_raw"]
    limit = ns["limit"]
    full = not ns["no_full"]
    max_full = ns["max_full"]

    if not keywords_raw and not tag and not anchor and not ids_raw:
        raise UsageError("至少给 --keywords / --tag / --id / --ids 之一")

    if not keywords_raw and not tag and not anchor and not ids_raw:
        print("[失败] 至少给 --keywords / --tag / --id / --ids 之一", file=sys.stderr)
        return 1

    if client is None:
        # 建连失败（含凭据缺失）在 main 里就收成退出码：`cli_entry` 那层是兜底，
        # 不该让「没配 token」这种可预期失败走到兜底上——消息会少掉这里的上下文。
        try:
            token, _src = load_token()
            client = FlomoClient(token)
            client.init()
        except FlomoError as exc:
            print(f"[失败] 查重未能进行：{exc}", file=sys.stderr)
            return 1

    seen: list[str] = []
    fetched: list[str] = []
    explicit = split_keywords(ids_raw)
    try:
        if explicit:
            # 点名要读的卡一律读全：不受 --max-full 与 --no-full 影响
            fetch_full(client, explicit, len(explicit))
            fetched.extend(explicit)
        if anchor:
            # 复盘路：按本卡 id 定向推荐（memo_recommended 的参数名就是 id）
            res = client.tool("memo_recommended", {"id": anchor, "limit": limit})
            memos = _result_memos(res)
            print(f"── memo_recommended id={anchor} → {len(memos)} 条")
            if not memos:
                print("   （无命中）")
            for m in memos:
                print("   " + brief_line(m))
            collect(memos, seen)
        if keywords_raw or tag:
            keywords = split_keywords(keywords_raw)
            total = len(keywords) + (1 if tag else 0)
            idx = 0
            for kw in keywords:
                idx += 1
                memos = search_one(
                    client, f"关键词 {idx}/{total}：{kw}", {"keywords": kw, "limit": limit}
                )
                collect(memos, seen)
            if tag:
                idx += 1
                memos = search_one(
                    client, f"标签 {idx}/{total}：{tag}", {"tag": tag, "limit": limit}
                )
                collect(memos, seen)
    except FlomoError as exc:
        print(f"[失败] 查重调用未完成：{exc}", file=sys.stderr)
        print("[注意] 查重没跑完不等于无命中，禁止据此进入判定或写云", file=sys.stderr)
        return 1

    rest = [i for i in seen if i not in fetched]
    if full:
        try:
            fetch_full(client, rest, max_full)
        except FlomoError as exc:
            # 清单已到手、正文没取到：这时候最危险的是「拿清单当读过」，故显式拦下
            print(f"[失败] 全文未取到：{exc}", file=sys.stderr)
            print("[注意] 只拿到 id 清单，未读正文不得做同事件判断", file=sys.stderr)
            return 1
    elif rest:
        print(f"\n[跳过全文] --no-full：{len(rest)} 张卡只列清单，未取正文")

    all_ids = fetched + rest
    print(f"\n[汇总] 唯一卡 {len(all_ids)} 张：{', '.join(all_ids) if all_ids else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, prefix="[失败] 查重未能进行：", usage_code=1))
