#!/usr/bin/env python3
"""retire.py — 卡片退场执行器（H23：个案卡、被并入的源卡、误建卡不再逐张手删）。

为什么要有它：清空（`content=' '`）只抹正文，空卡仍留在云端，用户只能一张张手动删除。
退场改为「打标签 + 留最小骨架」：卡面换成 `#待清理` 上的最小信息，用户按该标签
在 App 里一次性批量删除。技能侧没有 `memo_delete`，标签就是删除入口。

骨架只保留三项，全部用于删除前辨认与恢复：
  1. 首行标签 `#待清理`（唯一删除入口，禁止混入其他标签）
  2. 第二行原概念名
  3. 末行「原标签 + 原字数」
正文一律备份到 `_tmp/retire_backup/<id>.txt`，云端的骨架不含正文（H26：正文不落盘例外见 H24 素材保留）。

用法：
  python scripts/retire.py --ids ID1,ID2 [--dry-run]
  python scripts/retire.py --file <tsv>        # 取每行第一列 id
先逐张 `memo_batch_get` 取全文核对（H1），再覆盖写入，写入后回读核对骨架与字数。
写前必须能取到全文：取不到就跳过该张并计入失败，绝不「先打标签再说」。
退出码：0=全部成功（含跳过已空卡）；1=有失败；2=用法错误。
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from flomo_client import (  # noqa: E402
    RETIRE_TAG, FlomoClient, FlomoError, load_token, readback_check, tool_data,
)
from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402

_OPTS = [
    Opt("--ids", "ids", default="", hint="；多个 id 请用逗号在一次里给全"),
    Opt("--file", "file", default="", hint="；TSV 清单，取每行第一列为 id"),
    Opt("--manifest", "manifest", default="",
        hint="；id/概念名/字数/标签 四列，给已清空的卡补退场标记"),
    Opt("--dry-run", "dry_run", "flag"),
]
BACKUP_DIR = _HERE.parent / "_tmp" / "retire_backup"


def build_skeleton(name, tags, word_count):
    """骨架固定三行；除首行外不许出现 `#`，避免它被当成真卡或挂错标签。"""
    clean_name = (name or "").strip().replace("#", "").replace("\n", " ")
    tag_text = "、".join(t for t in tags if t) or "（无）"
    return f"{RETIRE_TAG}\n{clean_name}\n\n原标签：{tag_text}；原字数：{word_count}"


def parse_card(content):
    """从原文取「概念名 + 标签」，供骨架与备份文件名使用（H1 全文已在手）。"""
    lines = [ln.strip() for ln in (content or "").splitlines()]
    tags = [t.lstrip("#") for t in (lines[0].split() if lines else []) if t.startswith("#")]
    name = ""
    for ln in lines[1:]:
        if ln and not ln.startswith("#"):
            name = ln
            break
    return name, tags


def retire_one(client, memo_id, dry_run=False, fallback=None):
    """返回 (状态, 说明)。状态取 ok / skip / fail。

    `fallback` = (概念名, 原字数, 标签列表)：卡已被清空时用它建骨架。清空卡云端
    已无正文可取，只剩清单里的这份记录——不传就无从辨认，只能跳过。

    写入一律按「回包取 id + 回读逐字比对」验收：空卡在云端会被判 `memo not found`，
    只看「没抛异常」就把一次失败报成成功，正是本脚本要避免的假成功。
    """
    memos = tool_data(client.tool("memo_batch_get", {"ids": [memo_id]})).get("memos") or []
    content = (memos[0].get("content") or "") if memos else ""
    if content.strip():
        name, tags = parse_card(content)
        word_count = memos[0].get("word_count") or len(content)
        if not name:
            return "fail", "取不到概念名，按 H1 拒绝盲写"
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (BACKUP_DIR / f"{memo_id}.txt").write_text(content, encoding="utf-8")
    elif fallback:
        name, word_count, tags = fallback
    else:
        return "skip", "云端取不到这张卡，也没有清单记录可补骨架"

    skeleton = build_skeleton(name, tags, word_count)
    if dry_run:
        return "ok", f"[dry-run] {name}（原 {word_count} 字）→ 骨架 {len(skeleton)} 字"
    try:
        tool_data(client.tool("memo_update", {"id": memo_id, "content": skeleton}))
    except FlomoError as exc:
        return "fail", f"{name}：写入被云端拒绝（{exc}）——卡可能已被回收"
    ok, detail = readback_check(client, memo_id, skeleton)
    if not ok:
        return "fail", f"{name}：{detail}"
    return "ok", f"{name}（原 {word_count} 字）→ 已挂 {RETIRE_TAG}"


def main(argv=None):
    args, _pos = parse_options(argv if argv is not None else sys.argv[1:], _OPTS)
    ids = []
    if args["ids"]:
        ids += [x.strip() for x in args["ids"].replace("，", ",").split(",") if x.strip()]
    fallback = {}
    if args["manifest"]:
        for line in Path(args["manifest"]).read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.split("\t")]
            if len(cells) < 4 or not cells[0].startswith("M"):
                continue
            tags = [t.strip() for t in cells[3].replace("，", ",").replace("、", ",").split(",") if t.strip()]
            try:
                wc = int(cells[2] or 0)
            except ValueError:
                wc = 0
            fallback[cells[0]] = (cells[1], wc, tags)
            ids.append(cells[0])
    if args["file"]:
        for line in Path(args["file"]).read_text(encoding="utf-8").splitlines():
            cell = line.split("\t")[0].strip()
            if cell.startswith("M"):
                ids.append(cell)
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    if not ordered:
        raise UsageError("至少给 --ids 或 --file 之一")

    token, _src = load_token()
    client = FlomoClient(token)
    client.init()

    tally = {"ok": 0, "skip": 0, "fail": 0}
    for n, memo_id in enumerate(ordered, 1):
        try:
            state, why = retire_one(client, memo_id, dry_run=args["dry_run"],
                                    fallback=fallback.get(memo_id))
        except FlomoError as exc:
            state, why = "fail", str(exc)
        tally[state] += 1
        print(f"{n:>3}/{len(ordered)} {memo_id} {state.upper()} {why}")

    print(f"[结果] 退场 {tally['ok']} 张，跳过 {tally['skip']} 张，失败 {tally['fail']} 张")
    print(f"[备份] {BACKUP_DIR}")
    return 0 if tally["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError,), prefix="[失败] 退场未能完成：", usage_code=2))
