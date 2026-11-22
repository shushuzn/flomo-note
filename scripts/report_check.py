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
     列出的概念名必须与对应 id 集合一致——混在一段里说「建了 4 张卡」会被拦；
  6. 一张卡只占一行（`- 概念：一句话。id，标签，实测 N 字`）——此前是一句话
     一行、数据行一行，读者看到的就是「一个卡片分两点」；拆成两行按旧写法拒。

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

# 旧的明细行：`概念名：id，标签，实测 N 字` 单独占一行。
# 它只用于**识别并拒绝**旧的两行写法（一张卡拆成「一句话」+「数据行」两行）。
LEGACY_DETAIL = re.compile(
    r"^(?P<name>[^\n]{1,80})：\s*(?P<id>[A-Za-z0-9]{8,})\s*[，,]\s*"
    r"(?P<tags>#?[^\s，,]+(?:[ 　]#?[^\s，,]+)*)\s*[，,]\s*实测\s*(?P<n>\d+)\s*字\s*$"
)

# 明细行的右侧尾巴：`id，标签，实测 N 字`。左侧（概念名 + 那句话）在代码里
# 按「最后一个句号之后即 id」倒着切——概念名本身可能含全角冒号
# （如 `Dots：常驻智能体`），一句话里也可能出现顿号与括号。
DETAIL_TAIL = re.compile(
    r"(?P<id>[A-Za-z0-9]{8,})\s*[，,]\s*"
    r"(?P<tags>#?[^\s，,]+(?:[ 　]#?[^\s，,]+)*)\s*[，,]\s*实测\s*(?P<n>\d+)\s*字\s*$"
)

# 分段标题：整行只有「新建」或「更新」（可带冒号）。必须整行匹配——
# 「建了 3 张卡，更新 1 张」这类开头句里的「更新」不算分段，否则每个报告都会
# 被切成两段。
SECTION = re.compile(r"^(新建|更新)\s*[：:]?$")

# 分段里的条目：`- 概念名：一句读者能懂的话。id，标签，实测 N 字`
BULLET = re.compile(r"^[-*]\s+(?P<body>.+)$")


def _split_bullet(body: str) -> dict | None:
    """把「一卡一行」的条目切成 概念名 / 那句话 / id / 标签 / 字数。

    一张卡只占一行：`- 概念：一句话。id，标签，实测 N 字`。此前是两行
    （一句话一行、数据行一行），读者看到的就是「一张卡分两点」，同一张卡被
    写成两条并列事实。切法：先从右侧剥掉 id 尾巴，再把剩下的「概念：一句话。」
    按最后一个冒号断开；名字里自带冒号也不会被切错。
    """
    m = DETAIL_TAIL.search(body)
    if not m:
        return None
    head = body[:m.start()].strip()
    if head.endswith("。"):
        head = head[:-1].rstrip()
    name, found, say = head.rpartition("：")
    if not found or not name.strip() or not say.strip():
        return None
    return {"name": name.strip(), "say": say.strip(), "id": m.group("id"),
            "tags": m.group("tags"), "n": m.group("n")}

# 报告只回答「建了什么」，不回答「我怎么做的」（H6g）。
# 左侧是执行侧中间产物：对象清单、术语核验、查重命中、闸门读数都属内部步骤，
# 写进报告就是让用户替执行者审过程。右侧是请示语：把执行者该定的判回给用户。
# 词表刻意只收确定形态、不做语义猜测——误拦比漏检更危险，判不准的交人读。
LEAK_WORDS = ("对象清单", "术语核验", "查重命中", "查重结果", "闸门凭证",
              "需要我", "要不要我", "是否要我", "请你确认", "等你确认",
              "请你选择", "由你决定", "需要你确认")


# 每张卡那句话的上限。报告的作用是让人对上 id，不是替代卡片本身：
# 一句话超过 40 字就会开始复述卡片内容，读者要么去读卡、要么被这段挤掉注意力。
SENTENCE_LIMIT = 40


def _prose_limit(n: int) -> int:
    """SKILL 的报告口径：1 张 ≤120 字，每多一张 +30 字。

    旧口径是 300 + 60×n，等于允许逐卡写上百字的复述——报告越写越长，
    真正要对账的 id 与字数被埋掉。收紧到「开头一句 + 逐卡一句」的量。
    """
    return 120 + 30 * max(0, n - 1)


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

    # ---- 0. 执行侧步骤不得外发（H6g） ----
    # 扫全文而不只扫说明段：清单与请示语也可能写成一条 `-` 条目，只查说明段会漏。
    for _w in LEAK_WORDS:
        if _w in text:
            errors.append(
                f"报告里出现执行侧中间产物或请示语「{_w}」——只报结果，"
                f"过程与请示都不写（H6g）")

    # ---- 明细行与「新建 / 更新」分段解析 ----
    detail_rows = []
    prose_lines = []
    # 段是否出现单独记在 seen_section 里，不在 sections 的值上区分「没出现」与
    # 「出现但空」：`dict[str, list[str] | None]` 的索引访问每次都是新求值，mypy
    # 无法靠前一个 if 收窄，append 会按 None 报错（CI 的 mypy 阶段就是这么红的）。
    sections: dict[str, list[str]] = {"新建": [], "更新": []}
    # 明细行的段位与行号按 id 另存：塞进 detail_rows 会让每条字段的类型退化成
    # 「str | int | None」，下文按 str 用它们的每一处都会被 mypy 判错。
    row_sec: dict[str, str | None] = {}
    row_ln: dict[str, int] = {}
    seen_section: set[str] = set()
    current = None
    for ln_no, line in enumerate(text.split("\n")):
        s = line.strip()
        m = SECTION.match(s)
        if m:
            current = m.group(1)
            if current in seen_section:
                # 只报重复，不清空已收集的条目：清空会把前面的条目一起丢掉，
                # 随后又误报一条「段缺这些卡」，同一处写错变成两条互相矛盾的提示。
                errors.append(f"「{current}」段出现了两次——两段各自只该有一处标题")
            seen_section.add(current)
            continue
        bm = BULLET.match(s)
        if bm:
            if current is None:
                errors.append(f"这一条没落在「新建」或「更新」段里：{s[:24]}")
                continue
            g = _split_bullet(bm.group("body").strip())
            if g is None:
                if LEGACY_DETAIL.match(s):
                    errors.append(
                        f"「{current}」段用回了旧的两行写法（一张卡拆成小句行 + "
                        f"数据行）：一张卡只写一行，格式为「- 概念：一句话。id，"
                        f"标签，实测 N 字」——{s[:20]}")
                else:
                    errors.append(
                        f"「{current}」段这条没有带本卡的 id 与字数："
                        f"一张卡一行，句子后面空一行就是另一张卡了——{s[:24]}")
                continue
            detail_rows.append(g)
            row_sec[g["id"]] = current
            row_ln[g["id"]] = ln_no
            sections[current].append(g["name"])
            # 一句话的篇幅上限：超了就是在拿报告复述卡片（第四节只许
            # 「一句读者能懂的话」）。此前没有任何判据，报告逐轮变长。
            tlen = _cjk_len(g["say"])
            if tlen > SENTENCE_LIMIT:
                errors.append(
                    f"「{g['name']}」那句话 {tlen} 字，超过上限 "
                    f"{SENTENCE_LIMIT} 字——一句话只讲这张卡记了什么，细节归卡片")
            continue
        if LEGACY_DETAIL.match(s):
            errors.append(
                f"明细行单独占了一行：一张卡只写一行「- 概念：一句话。id，标签，"
                f"实测 N 字」，不要拆成两行——{s[:20]}")
            continue
        if s:
            prose_lines.append(s)

    prose = "".join(prose_lines)
    plen = _cjk_len(prose)
    # 每张卡的明细行落在哪一段（没有段标题时为 None，另行由「缺段」判据拦下）
    detail_section = row_sec
    detail_ln = row_ln

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
    # 同名只提示不拦：两张同名卡可分别更新，也可各自改名，检测层不替人做决定。
    _names: dict[str, list[str]] = {}
    for r in detail_rows:
        _names.setdefault(r["name"].strip(), []).append(r["id"])
    for _nm, _rids in _names.items():
        if len(_rids) > 1:
            _upd = [x.strip() for x in (row.get("updated") or "").split(",") if x.strip()]
            _is_new = [r for r in _rids if r not in _upd]
            if _is_new:
                errors.append(
                    f"完全同标题严格禁止：{'、'.join(_rids)} 共用「{_nm}」，"
                    "新建卡不得与任何卡逐字同名")
            else:
                notes.append(f"两张卡同名「{_nm}」（{'、'.join(_rids)}）均为更新，"
                             "各自更新前须已取全文核对")

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
        got_tags = {t.lstrip("#") for t in re.findall(r"#?[^\s]+", row_m["tags"])}
        if got_tags != {t.lstrip("#") for t in want_tags}:
            errors.append(f"{rid} 标签不符：报告 {sorted(got_tags)} / 云端 {sorted(want_tags)}")
            ok = False
        elif [t.lstrip("#") for t in row_m["tags"].split()] != \
                [t.lstrip("#") for t in first.split()]:
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
            # 每条明细行自带它那句话，故「卡片落在哪一段」就等于「它那一行在哪一段」：
            # 把更新卡整行抄进「新建」段，按段读就会把改写说成新建。
            misfiled = [cloud_names.get(i, i) for i in want_ids
                        if detail_section.get(i) != kind]
            if misfiled:
                errors.append(
                    f"这些{kind}卡写在了别的段里：{'、'.join(misfiled)}"
                    f"——按段读就成了另一类卡")

    # ---- 3c. 明细行之后不再有补充说明 ----
    # 报告尾部的「另外说明」是最容易无限膨胀的一块：它不受逐卡那句话的长度约束，
    # 又被算进说明段总长，久而久之整篇变成流水账。只允许一行「异常：」。
    if detail_rows:
        tail = [ln.strip() for ln in text.split("\n")[max(row_ln.values()) + 1:]
                if ln.strip()]
        prose_tail = [ln for ln in tail if not SECTION.match(ln)]
        extra = [ln for ln in prose_tail if not ln.startswith("异常")]
        abnormal = [ln for ln in prose_tail if ln.startswith("异常")]
        if extra:
            errors.append(
                f"明细行之后还有补充说明「{extra[0][:20]}」——数据行之后只允许"
                f"一行「异常：…」，其余内容都归卡片")
        if len(abnormal) > 1:
            errors.append("「异常」写了两行以上——异常只写闸门开关与逐条认领，一行说完")
        for ln in abnormal:
            if _cjk_len(ln) > SENTENCE_LIMIT:
                errors.append(
                    f"「异常」这一行 {_cjk_len(ln)} 字，超过上限 {SENTENCE_LIMIT} 字")

    # ---- 4. 说明段长度 ----
    limit = _prose_limit(max(1, len(ids)))
    if plen > limit:
        errors.append(f"说明段 {plen} 字，超过上限 {limit} 字（1 张 120 字，每多一张 +30）")
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
