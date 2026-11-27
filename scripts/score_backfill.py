#!/usr/bin/env python3
"""score_backfill.py — 给存量卡片补评分位（H35 的存量通道）。

为什么单独一支：`publish.py` 只对新草稿要求评分位，存量卡当初是在没有这条硬限时写的。
「所有内容卡片都能评分」若只对新卡成立，库里就长期分成两套口径——统计时一半有分一半没有，
查重比对又得先判「这张是按旧规写的」。所以存量要有一条**只动首行**的回填路：正文一个字不改，
只在标签行末尾加 `#评分/N`。

分数不由脚本猜。等级必须由人（或读完正文后的判断）逐张给出，脚本只做三件机械的事：
① 读出原文，核出可回填的形态；② 按 `score_check` 的口径算分并拼标签；③ 生成草稿与批次清单，
交给 `publish.py --batch` 走完整条链（闸门 → 质检 → 覆盖 → 复核，覆盖前自动落改前快照）。

保守边界：
- 正文里带截断占位符（`[此处省略…]`）的卡一律跳过——回写会把它固化成真内容（H1）；
- `#待清理` 骨架跳过，那是等着删的卡；
- 首行不是标签行的卡跳过，不在本工具职责内（先按 H6 修形态）；
- 卡上已有评分位的，默认跳过（`--overwrite` 才重写），避免同一张卡来回改分。

用法：
  python scripts/score_backfill.py --ids "MjU2...,MjY3..." --grades _tmp/grades.json --plan
  python scripts/score_backfill.py --ids "..." --grades _tmp/grades.json --apply
  python scripts/score_backfill.py --file _tmp/ids.txt --grades ... --apply --limit 20

`--grades` 是 JSON：`{"<id>": "mainstream"}`，或带修正的
`{"<id>": {"grade": "weakened", "adj": {"precision": -5}}}`。缺哪张的等级就不回填哪张，
绝不套默认档——分数一旦是猜的，整套口径就失效。
"""
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import flomo_client  # noqa: E402
import memo_util  # noqa: E402
import score_check  # noqa: E402
from cli_util import Opt, UsageError, cli_entry, force_utf8, parse_options  # noqa: E402

BATCH_LIMIT = 10
SCORE_RE = re.compile(r"#评分/[^\s#]*")
SKIP_TAGS = ("#待清理",)


class Skip(Exception):
    """这张卡不该由本工具动，理由随异常带出。"""


def split_card(content: str) -> tuple[str, str, str]:
    """拆成 (标签行, 概念名, 其余正文)。首行必须是标签行，否则抛 Skip。"""
    # 云端把换行序列化成 `<br>`（不是 `\n`），先归一，否则标签行会带着 `<br>` 尾巴
    # 被当成标签名的一部分，写回时再被质检判成非法字符。
    lines = content.replace("<br>", "\n").split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None:
        raise Skip("整卡为空")
    tagline = lines[idx].strip()
    if not tagline.startswith("#"):
        raise Skip("首行不是标签行（先按 H6 修形态）")
    rest = lines[idx + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    if not rest:
        raise Skip("没有概念名")
    concept = rest[0].strip()
    body = "\n".join(rest[1:])
    return tagline, concept, body


def desired_tagline(tagline: str, score: int, *, overwrite: bool) -> str:
    if SCORE_RE.search(tagline):
        if not overwrite:
            raise Skip("已有评分位（要改分加 --overwrite）")
        return SCORE_RE.sub(f"#评分/{score}", tagline, count=1)
    return f"{tagline} #评分/{score}"


def load_grades(path: str) -> dict[str, dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise UsageError("--grades 必须是 {id: 等级} 的对象")
    out = {}
    for mid, spec in raw.items():
        if isinstance(spec, str):
            out[mid] = {"grade": spec, "adj": {}}
        elif isinstance(spec, dict) and isinstance(spec.get("grade"), str):
            out[mid] = {"grade": spec["grade"], "adj": spec.get("adj") or {}}
        else:
            raise UsageError(f"{mid} 的等级写法不认识：{spec!r}")
    return out


def plan_one(mid: str, content: str, spec: dict, *, overwrite: bool) -> dict:
    grade = spec["grade"]
    adj = {k: int(v) for k, v in (spec.get("adj") or {}).items()}
    if grade not in score_check.GRADES:
        raise Skip(f"未知等级 {grade}")
    score = score_check.compute(grade, adj)
    if any(t in content.split("\n")[0] for t in SKIP_TAGS):
        raise Skip("待清理骨架")
    if memo_util.find_truncation(content):
        raise Skip("正文含截断占位符，回写会固化缺失段落")
    tagline, concept, body = split_card(content)
    return {"id": mid, "concept": concept, "tagline": tagline, "body": body,
            "new_tagline": desired_tagline(tagline, score, overwrite=overwrite),
            "score": score, "grade": grade, "adj": adj}


def fetch(client, ids: list[str]) -> dict[str, str]:
    got = {}
    for i in range(0, len(ids), BATCH_LIMIT):
        chunk = ids[i:i + BATCH_LIMIT]
        res = client.tool("memo_batch_get", {"ids": chunk})
        for memo in flomo_client._result_memos(res):
            body = memo.get("content")
            if isinstance(body, str) and memo.get("id"):
                got[str(memo["id"])] = body
    return got


_OPTS = [
    Opt("--ids", "ids_raw", default="", hint="逗号分隔；与 --file 二选一"),
    Opt("--file", "file", default="", hint="每行一个 id"),
    Opt("--grades", "grades", default="", hint="JSON：{id: 等级} 或 {id: {grade, adj}}"),
    Opt("--dir", "out_dir", default="_tmp/cards_score", hint="草稿目录（仓库相对）"),
    Opt("--limit", "limit", "int", low=1, default=0, hint="最多处理前 N 张（0=全部）"),
    Opt("--plan", "plan", "flag", hint="只出计划，不写草稿"),
    Opt("--apply", "apply", "flag", hint="写草稿并调用 publish --batch"),
    Opt("--overwrite", "overwrite", "flag", hint="已有评分位也重写"),
]

USAGE = """score_backfill.py — 存量卡片补评分位（只动首行，正文不改）

  python scripts/score_backfill.py --ids id1,id2 --grades grades.json --plan
  python scripts/score_backfill.py --ids ... --grades ... --apply
"""


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if args else 2
    try:
        ns, _ = parse_options(args, _OPTS)
    except UsageError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 2
    if not ns["grades"]:
        print("[失败] 必须给 --grades（分数不由脚本猜）", file=sys.stderr)
        return 2
    if not ns["plan"] and not ns["apply"]:
        print("[失败] --plan / --apply 至少选一个", file=sys.stderr)
        return 2

    ids = [s.strip() for s in ns["ids_raw"].split(",") if s.strip()]
    if ns["file"]:
        ids += [ln.strip() for ln in Path(ns["file"]).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    if not ids:
        print("[失败] 没有要处理的 id", file=sys.stderr)
        return 2
    ids = list(dict.fromkeys(ids))
    if ns["limit"]:
        ids = ids[:ns["limit"]]

    try:
        grades = load_grades(ns["grades"])
    except (OSError, json.JSONDecodeError, UsageError) as exc:
        print(f"[失败] 读不到等级表：{exc}", file=sys.stderr)
        return 2

    missing = [mid for mid in ids if mid not in grades]
    if missing:
        print(f"[跳过] {len(missing)} 张没有给等级：{', '.join(missing)}")

    token, _src = flomo_client.load_token()
    client = flomo_client.FlomoClient(token)
    client.init()
    bodies = fetch(client, [m for m in ids if m in grades])
    for mid in [m for m in ids if m in grades and m not in bodies]:
        print(f"[跳过] {mid}：读不到全文")

    plans, skips = [], []
    for mid, content in bodies.items():
        try:
            plans.append(plan_one(mid, content, grades[mid], overwrite=ns["overwrite"]))
        except Skip as exc:
            skips.append((mid, str(exc)))

    print(f"\n[计划] 可回填 {len(plans)} 张，跳过 {len(skips)} 张")
    for p in plans:
        print(f"  {p['id']} | {p['grade']} → #评分/{p['score']} | {p['concept']}")
    for mid, why in skips:
        print(f"  [跳过] {mid}：{why}")

    if not ns["apply"] or not plans:
        if not plans:
            print("\n[结论] 没有可回填的卡")
            return 1 if skips else 0
        print("\n[RESULT] plan_only=1（加 --apply 才写草稿并走 publish 覆盖）")
        return 0

    out = Path(ns["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    batch = out / "batch.txt"
    rows = []
    for p in plans:
        draft = out / f"{p['id']}.txt"
        draft.write_text(f"{p['new_tagline']}\n{p['concept']}\n\n{p['body']}\n",
                         encoding="utf-8")
        rows.append(f"{draft.as_posix()} | - | {p['id']} | - | skipweb:补评分位不动正文")
    batch.write_text("# 回填评分位（score_backfill 生成）\n" + "\n".join(rows) + "\n",
                     encoding="utf-8")
    print(f"\n[草稿] {len(rows)} 份 → {out.as_posix()}；清单 {batch.as_posix()}")
    print(f"[下一步] python scripts/publish.py --batch {batch.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(flomo_client.FlomoError,)))
