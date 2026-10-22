#!/usr/bin/env python3
"""scan_truncated.py — 扫存量卡是否被「检索回包截断版正文」覆盖过。

为什么要有它：
`memo_search` / `memo_recommended` 会把长正文的**中段**挖掉，替换成
`...[此处省略N字]...`。若把这种回包当正文写回云端，卡里就会永久缺一段，而
此前所有判据都发现不了：结尾是完好的、字数够、写后逐字对账又拿同一段坏文本
比同一段坏文本，必然「一致」。上一轮批量改名即由此覆盖了 8 张卡。

写前的两道闸（`validate_memo` 的 ERR、`flomo_client` 的写前拒绝）只拦**新**写入；
本脚本补的是**存量**：把库里已经被覆盖过的卡找出来。

两条口径，写死在本脚本里，改之前先读：
1. **候选只能从检索回包取 id，绝不读它的 content**——它的 content 本身就可能是截断的，
   拿它判「有没有占位符」会得到 41 张假阳性（实测：关键词搜「此处省略」命中 41 张，
   用 `memo_batch_get` 复核真实正文，命中 0 张）。
2. **判定只看 `memo_batch_get` 的真实正文**。搜索接口的匹配是模糊的（「不可省略」也会命中
   「此处省略」），不能当索引使用。

用法：
  python scripts/scan_truncated.py                     # 按标签树逐标签枚举（默认，全库）
  python scripts/scan_truncated.py --tags "时政/反腐,产业/消费电子"
  python scripts/scan_truncated.py --ids ID1,ID2        # 点名核这几张
  python scripts/scan_truncated.py --json out.json      # 另存机器可读结果

退出码：
  0  扫完且 0 命中
  1  扫到命中（需修复）——命中卡逐张列出 id / 字数 / 标签 / 概念名 / 标记
  2  有云端调用失败，扫描**不完整**：此时不得把「没报出来的」当成干净
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402
from flomo_client import (  # noqa: E402
    FlomoClient, FlomoError, _result_memos, load_token, tool_data,
)
from memo_util import find_truncation, memo_signature  # noqa: E402

BATCH_LIMIT = 10  # 服务端 memo_batch_get 单次上限，超过即整批报错
MAX_LIMIT = 50    # memo_search 单次返回上限（服务端硬上限，无分页参数）

_OPTS = [
    Opt("--ids", "ids_raw", default="", hint="；多个 id 用逗号在一次里给全"),
    Opt("--tags", "tags_raw", default="", hint="；多个标签路径用逗号给全，如 时政/反腐,产业/消费电子"),
    Opt("--limit", "limit", "int", low=1, high=MAX_LIMIT, default=MAX_LIMIT,
        hint=f"；每个标签取多少条，服务端上限 {MAX_LIMIT}"),
    Opt("--json", "json_out", default=""),
]


def split_list(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace("，", ",").replace("、", ",").replace("；", ",").replace(
        ";", ",").replace("\n", ",").split(","):
        s = part.strip()
        if s and s not in out:
            out.append(s)
    return out


def all_tags(client) -> list[str]:
    """取标签树里的全部标签路径。

    注意：`tag_tree` 只给标签名，**不给每个标签下的卡片数**（本项目实测），所以拿不到
    「该扫多少张」的期望值——扫描规模只能报「扫到了多少」，不能报「覆盖率」。
    """
    res = client.tool("tag_tree", {"limit": 2000})
    data = tool_data(res) or {}
    out: list[str] = []
    for t in data.get("tags") or []:
        # 回包里 tags 可能是字符串（标签路径）也可能是带 path/name 的对象——
        # 两种形态都见过，按对象取会在字符串上报 AttributeError。
        path = t if isinstance(t, str) else (t.get("path") or t.get("name") or "")
        if path and path not in out:
            out.append(path)
    return out


def ids_by_tag(client, tag: str, limit: int) -> list[str]:
    """单个标签下的 memo id。

    只取 id：返回的 content 可能是截断的，读它判正文就是本脚本要防的那个错。
    """
    res = client.tool("memo_search", {"tag": tag, "limit": limit})
    return [m.get("id") for m in _result_memos(res) if m.get("id")]


def _fetch_chunk(client, chunk):
    """取一批正文，返回 (id -> memo, 错误说明或空串)。"""
    try:
        res = client.tool("memo_batch_get", {"ids": chunk})
    except FlomoError as exc:
        return {}, f"{chunk[0]}…（{len(chunk)} 张）：{exc}"
    got = _result_memos(res)
    if not got:
        # 单张取不到**不是调用失败**：那是「卡已不在」的形态，由调用方归入 missing。
        # 混成失败会让「删除/漏读」两条完全不同的结论变成同一条报错，而且
        # 「未完成的候选清单」永远不会出现——恰恰是最该被点名的东西。
        if len(chunk) == 1:
            return {}, ""
        return {}, f"{chunk[0]}…（{len(chunk)} 张）：回包无 memos"
    return {m["id"]: m for m in got if m.get("id")}, ""


def fetch_all(client, ids: list[str]) -> tuple[dict, list[str], list[str]]:
    """按 id 取真实正文。返回 (id -> memo, 失败批次说明, 请求了但没返回的 id)。

    第三项不可省：云端可能**静默少回**——回包成功，但里面没有某几条。两种成因都实测过：
    ① 一次要 10 张正文很长的卡时，回包挤不下，只回来 8 张；
    ② id 对应的卡已被删除。
    两种在回包上长得一模一样，所以缺的先**逐张补取**：补到了就是①（数据没丢，是我们
    要得太多），补不到才归入「读不回」。不补取就直接报「未完成」，等于把一条可行路径
    当成死路；反过来，只统计「读回几张」而不点名缺哪几张，读者也无从判断那是删掉的卡
    还是漏读的真卡。
    """
    out: dict = {}
    failed: list[str] = []
    for i in range(0, len(ids), BATCH_LIMIT):
        got, err = _fetch_chunk(client, ids[i:i + BATCH_LIMIT])
        if err:
            failed.append(err)
        out.update(got)

    missing = [mid for mid in ids if mid not in out]
    still: list[str] = []
    for mid in missing:
        got, err = _fetch_chunk(client, [mid])
        if err:
            failed.append(err)
        if mid in got:
            out[mid] = got[mid]
        else:
            still.append(mid)
    return out, failed, still


def run(client, ids: list[str], tags: list[str], limit: int):
    """返回 (命中列表, 候选数, 实际取回正文的张数, 失败说明)。

    `取回张数` 必须单独返回：候选 200 张、只取回 8 张时，若只报「0 命中」，
    读者会把它当成「200 张都干净」，而真相是 192 张根本没读到。
    """
    candidates: list[str] = list(ids)
    failed: list[str] = []

    if tags:
        for tag in tags:
            try:
                found = ids_by_tag(client, tag, limit)
            except FlomoError as exc:
                failed.append(f"标签 {tag}：{exc}")
                continue
            print(f"  [标签] {tag} → {len(found)} 张")
            for mid in found:
                if mid not in candidates:
                    candidates.append(mid)
    elif not ids:
        try:
            tags = all_tags(client)
        except FlomoError as exc:
            tags = []
            failed.append(f"标签树：{exc}")
        if not tags:
            failed.append("标签树为空或取不到，无法枚举（不做全量扫描）")
        print(f"  按标签树枚举：{len(tags)} 个标签，每标签取 {limit} 条")
        for n, tag in enumerate(tags, 1):
            try:
                found = ids_by_tag(client, tag, limit)
            except FlomoError as exc:
                failed.append(f"标签 {tag}：{exc}")
                continue
            for mid in found:
                if mid not in candidates:
                    candidates.append(mid)
            if n % 50 == 0:
                print(f"    …{n}/{len(tags)} 个标签，累计 {len(candidates)} 张候选", flush=True)

    print(f"  候选 {len(candidates)} 张，按 id 取真实正文复核")
    memos, batch_failed, missing = fetch_all(client, candidates)
    failed += batch_failed

    hits = []
    for mid, m in memos.items():
        content = m.get("content") or ""
        marks = find_truncation(content)
        if not marks:
            continue
        sig = memo_signature(content) or {}
        hits.append({
            "id": mid,
            "chars": len(content),
            "word_count": m.get("word_count"),
            "tags": m.get("tags") or [],
            "concept": sig.get("concept") or "",
            "marks": marks,
        })
    return hits, len(candidates), len(memos), failed, missing


def main(argv=None, client=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    # 无参数 = 全库扫描（默认行为），只有显式要帮助才打 usage——
    # 「不给参数就只打帮助」会让「默认全扫」这条写在文档里的用法变成空转。
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    ns, _pos = parse_options(args, _OPTS)
    ids = split_list(ns["ids_raw"])
    tags = split_list(ns["tags_raw"])
    limit = ns["limit"]

    if client is None:
        token, _src = load_token()
        client = FlomoClient(token)
        client.init()

    hits, n_cand, n_read, failed, missing = run(client, ids, tags, limit)

    print()
    if hits:
        print(f"[命中] {len(hits)} 张卡的真实正文含截断占位符：")
        for h in hits:
            tag = h["tags"][0] if h["tags"] else "(无标签)"
            print(f"  {h['id']}  {h['chars']:>5}字  {tag:<18} {h['concept'][:26]:<26} {h['marks'][0]}")
        print("  修复口径：按 id 取原文备份 → 联网找原文补齐中段 → publish --update-id 覆盖；"
              "补不到就清空，不留「缺一段」的卡。")
    elif failed or missing:
        # 「没读到」不等于「没问题」：只要没扫全，就不许出现「干净」这个词。
        print(f"[未完成] 候选 {n_cand} 张，实际读回 {n_read} 张，未见占位符——"
              "但扫描不完整，**不得据此判干净**")
    else:
        print(f"[干净] 候选 {n_cand} 张，真实正文均无截断占位符")

    if failed:
        print(f"\n[未完成] {len(failed)} 处调用失败，本次扫描**不完整**——"
              "没报出来的不能当成干净：")
        for f in failed[:10]:
            print(f"  {f}")
        return 2

    if missing:
        # 「没返回」不等于「没问题」：这些 id 是检索接口给的，却取不回正文。
        # 要么卡已被删除/清空（无害），要么读数出了别的问题（有害）——本脚本不猜，
        # 一律按未完成处理，并点名，让下一个动作有落点。
        print(f"\n[未完成] {len(missing)} 张候选没取回正文，无法判定（退出码 2）：")
        for mid in missing[:20]:
            print(f"  {mid}")
        return 2

    if ns["json_out"]:
        Path(ns["json_out"]).write_text(
            json.dumps({"candidates": n_cand, "read": n_read,
                        "missing": missing, "hits": hits}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"\n已写入 {ns['json_out']}")

    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError, UsageError)))
