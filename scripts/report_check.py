#!/usr/bin/env python3
"""report_check.py — 报告环节的机械闸门（此前唯一的无闸门环节）。

事故：某轮建卡、回读、关账、清理全部完成，末条回复却只发了半句
「建了 2 张卡。」——逐卡的「概念名：id，标签，实测 N 字」整段缺失。
卡片本身无误，但报告是三段流水线里唯一**没有校验器**的一段：字数靠记忆复述、
标签靠手抄、明细条数与已建卡数是否相等无人对账，所以「漏一句」机器侧完全看不见。

本脚本把报告变成可判定的产物：
  1. 从账本取本轮 `exit_confirm --published` 落下的 id 清单（不手抄 id）；
  2. 逐 id 回读云端，拿到 word_count / 标签行 / 概念名；
  3. 解析报告里的明细行，逐项对账；
  4. 校验说明段的字数上限（1 张 ≤300 字，每多一张 +60 字，与 SKILL 报告口径一致）；
  5. 校验「新建」「更新」两段分写：账本 `updated` 之外的 id 按新建算，两段各自
     列出的概念名必须与对应 id 集合一致——混在一段里说「建了 4 张卡」会被拦。

用法：
  python scripts/report_check.py --report _tmp/report.txt --url https://mp.weixin.qq.com/s/xxx
  python scripts/report_check.py --text "建了 1 张卡。……" --url <链接>

退出码：0 = 全过；1 = 有对账不过或取不到卡片。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

import cli_util as CLI  # noqa: E402
import flomo_client as FC  # noqa: E402
import round_ledger  # noqa: E402
import verify_card  # noqa: E402

# 明细行：`概念名：id，标签，实测 N 字`
# 标签允许一个或多个 `#顶层/二级`，以空格分隔；全角逗号与半角都收。
DETAIL = re.compile(
    r"^(?P<name>[^：\n]{1,60})：\s*(?P<id>[A-Za-z0-9]{8,})\s*[，,]\s*"
    r"(?P<tags>#[^\s，,]+(?:[ 　]#[^\s，,]+)*)\s*[，,]\s*实测\s*(?P<n>\d+)\s*字\s*$"
)

# 分段标题：整行只有「新建」或「更新」（可带冒号）。必须整行匹配——
# 「建了 3 张卡，更新 1 张」这类开头句里的「更新」不算分段，否则每个报告都会
# 被切成两段。
SECTION = re.compile(r"^(新建|更新)\s*[：:]?$")

# 分段里的条目：`- 概念名：一句读者能懂的话`
BULLET = re.compile(r"^[-*]\s+(?P<body>.+)$")


def _prose_limit(n: int) -> int:
    """SKILL 的报告口径：1 张 ≤300 字，每多一张 +60 字。"""
    return 300 + 60 * max(0, n - 1)


def _cjk_len(text: str) -> int:
    """按「字」计：去掉空白与 Markdown 标记后的可见字符数。"""
    return len(re.sub(r"[\s*_`#>]", "", text))


def _round_for(url: str):
    data = round_ledger._load(round_ledger.ledger_path())
    rows = [r for r in data.get("rounds", []) if r.get("url") == url]
    return rows[-1] if rows else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", help="报告文本文件路径")
    ap.add_argument("--text", help="直接给报告文本（与 --report 二选一）")
    ap.add_argument("--url", required=True, help="本轮素材链接，用于从账本取已建卡 id")
    args = ap.parse_args(argv)

    if not args.report and not args.text:
        print("[报告校验] 缺 --report 或 --text", file=sys.stderr)
        return 1
    text = (Path(args.report).read_text(encoding="utf-8")
            if args.report else args.text)

    row = _round_for(args.url)
    if row is None:
        print(f"[报告校验] 账本里没有这条素材：{args.url}", file=sys.stderr)
        print("  先跑 exit_confirm.py 关账，报告再写——顺序反了就没有 id 可对。",
              file=sys.stderr)
        return 1
    if not row.get("closed_at"):
        print("[报告校验] 该素材还没关账：报告必须在 exit_confirm 之后写。", file=sys.stderr)
        return 1

    outcome = row.get("outcome") or ""
    ids = [x.strip() for x in (row.get("detail") or "").split(",") if x.strip()]
    if outcome != "published":
        ids = []

    errors: list[str] = []
    notes: list[str] = []

    # ---- 明细行与「新建 / 更新」分段解析 ----
    detail_rows = []
    prose_lines = []
    # 段是否出现单独记在 seen_section 里，不在 sections 的值上区分「没出现」与
    # 「出现但空」：`dict[str, list[str] | None]` 的索引访问每次都是新求值，mypy
    # 无法靠前一个 if 收窄，append 会按 None 报错（CI 的 mypy 阶段就是这么红的）。
    sections: dict[str, list[str]] = {"新建": [], "更新": []}
    seen_section: set[str] = set()
    current = None
    for line in text.split("\n"):
        s = line.strip()
        m = DETAIL.match(s)
        if m:
            detail_rows.append(m.groupdict())
            continue
        sm = SECTION.match(s)
        if sm:
            current = sm.group(1)
            if current in seen_section:
                # 只报重复，不清空已收集的条目：清空会把前面的条目一起丢掉，
                # 随后又误报一条「段缺这些卡」，同一处写错变成两条互相矛盾的提示。
                errors.append(f"「{current}」段出现了两次——两段各自只该有一处标题")
            seen_section.add(current)
            continue
        bm = BULLET.match(s)
        if bm and current:
            body = bm.group("body").strip()
            parts = re.split(r"[：:]", body, maxsplit=1)
            if len(parts) < 2 or not parts[0].strip():
                errors.append(
                    f"「{current}」段的这一条不是「概念名：一句」的写法：{body[:24]}")
            else:
                sections[current].append(parts[0].strip())
            continue
        if s:
            prose_lines.append(s)

    prose = "".join(prose_lines)
    plen = _cjk_len(prose)

    # ---- 1. 条数与 id 集合 ----
    if len(detail_rows) != len(ids):
        errors.append(
            f"明细行 {len(detail_rows)} 条，本轮已建卡 {len(ids)} 张——"
            f"报告漏写或写多了（这正是那起事故的形态：卡片全对，报告只发半句）")
    got_ids = [r["id"] for r in detail_rows]
    missing = [i for i in ids if i not in got_ids]
    extra = [i for i in got_ids if i not in ids]
    if missing:
        errors.append(f"明细里缺这些已建卡 id：{', '.join(missing)}")
    if extra:
        errors.append(f"明细里有不属于本轮的 id：{', '.join(extra)}")
    dup = [i for i in set(got_ids) if got_ids.count(i) > 1]
    if dup:
        errors.append(f"同一 id 写了多条明细：{', '.join(dup)}")

    # ---- 2. 逐卡对账（回读云端，不手抄） ----
    print(f"── 本轮已建卡 {len(ids)} 张，报告明细 {len(detail_rows)} 条")
    cloud_names: dict[str, str] = {}
    for rid in ids:
        try:
            memo = verify_card.fetch(rid)
        except Exception as exc:  # noqa: BLE001 — 取不到就是取不到，报出来即可
            errors.append(f"{rid} 回读失败：{exc}")
            continue
        raw = (memo.get("content") or "").replace("\r\n", "\n")
        raw = "\n".join(FC._strip_trailing_br(ln) for ln in raw.split("\n"))
        lines = [ln.strip() for ln in raw.split("\n")]
        first = lines[0] if lines else ""
        second = lines[1] if len(lines) > 1 else ""
        cloud_names[rid] = second
        want_tags = set(re.findall(r"#[^\s]+", first))
        want_n = memo.get("word_count")

        row_m = next((r for r in detail_rows if r["id"] == rid), None)
        if row_m is None:
            continue
        ok = True
        if row_m["name"].strip() != second:
            errors.append(f"{rid} 概念名不符：报告「{row_m['name']}」/ 云端「{second}」")
            ok = False
        got_tags = set(re.findall(r"#[^\s]+", row_m["tags"]))
        if got_tags != want_tags:
            errors.append(f"{rid} 标签不符：报告 {sorted(got_tags)} / 云端 {sorted(want_tags)}")
            ok = False
        elif row_m["tags"].split() != first.split():
            notes.append(f"{rid} 标签一致但书写顺序与云端不同")
        if int(row_m["n"]) != int(want_n or -1):
            errors.append(f"{rid} 字数不符：报告 {row_m['n']} / 云端实测 {want_n}")
            ok = False
        print(f"  {'✓' if ok else '✗'} {second} | {rid} | {want_n} 字 | {first}")

    # ---- 3. 新建 / 更新分写 ----
    # 账本只记了 `published`（全部 id）与 `updated`（其中属于更新既有卡的那几个），
    # 差集即新建。没有 `updated` 字段的旧账本按「全部新建」处理——那一轮的分类
    # 已无从判定，强判会把历史轮次判错。
    marked = [x.strip() for x in (row.get("updated") or "").split(",") if x.strip()]
    updated_ids = [i for i in ids if i in marked]
    created_ids = [i for i in ids if i not in marked]
    if ids:
        for kind, want_ids in (("新建", created_ids), ("更新", updated_ids)):
            block = sections[kind]
            has_section = kind in seen_section
            if want_ids and not has_section:
                errors.append(
                    f"本轮有 {len(want_ids)} 张{kind}卡，报告里没有独立的「{kind}」段——"
                    f"新建与更新必须分开写，不能都并进「建了 N 张卡」")
                continue
            if not want_ids and has_section:
                errors.append(
                    f"报告写了「{kind}」段，但本轮没有{kind}卡——空段会被读成漏了卡")
                continue
            if not want_ids:
                continue
            want_names = [cloud_names.get(i, "") for i in want_ids]
            missing_names = [n for n in want_names if n not in block]
            stray_names = [n for n in block if n not in want_names]
            if missing_names:
                errors.append(f"「{kind}」段缺这些卡：{'、'.join(missing_names)}")
            if stray_names:
                errors.append(
                    f"「{kind}」段里这些不是{kind}卡：{'、'.join(stray_names)}"
                    f"（写错段等于把新建说成更新）")
            # 名字层面查过就不重复报条数——同一次写错会变成三条提示，读的人反而
            # 抓不住该改哪一处；条数只在剩下的情形（同一张卡写两遍）才有信息量。
            elif len(block) != len(want_names):
                errors.append(f"「{kind}」段写了 {len(block)} 条，账上是 {len(want_names)} 张")

    # ---- 4. 说明段长度 ----
    limit = _prose_limit(max(1, len(ids)))
    if plen > limit:
        errors.append(f"说明段 {plen} 字，超过上限 {limit} 字（1 张 300 字，每多一张 +60）")
    if plen == 0:
        errors.append("没有说明段——报告不能只剩明细行")

    for n in notes:
        print(f"  · {n}")

    if errors:
        print("\n[报告校验] 不通过：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print("\n改完再发。逐卡明细必须与账本 id 一一对应，字数从云端回读取。", file=sys.stderr)
        return 1

    print(f"\n[报告校验] 全过：{len(detail_rows)} 条明细对账一致"
          f"（新建 {len(created_ids)} / 更新 {len(updated_ids)}），说明段 {plen}/{limit} 字")
    return 0


if __name__ == "__main__":
    # 与其他命令行脚本同一出口：凭据缺失等 `FlomoError` 收成一行 + 退出码。
    sys.exit(CLI.cli_entry(main, errors=(FC.FlomoError,)))
