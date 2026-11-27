#!/usr/bin/env python3
"""score_check.py — 卡片「评分」标签的算分器与校准器（H35 的配套工具）。

为什么要有它：`#评分/N` 若只靠手感填，同一篇材料两次写会差出十几分，标签就失去
可比性。这里把评分拆成「等级底分 + 四项修正」，等级只有六档且每档有明确判据；
四个校准锚点（已证定理 100、黎曼猜想 80、准黎曼猜想 60、已被反例否证 0）写进
校验表，`--selftest` 逐条断言，口径漂了就先红。

用法：
  python scripts/score_check.py --grade mainstream                    # 80
  python scripts/score_check.py --grade mainstream --precision -5 --testability 5
  python scripts/score_check.py --grade weakened --explain            # 逐项拆开
  python scripts/score_check.py --tag "#评分/78"                      # 只核标签写法
  python scripts/score_check.py --selftest                            # 跑校准表

等级（底分）：proved 100 / mainstream 80 / weakened 60 / partial 40 / heuristic 20 / refuted 0
修正（各 −5…+5，总分夹在 0–100）：precision 表述精确性、testability 可检验性、
independence 证据独立性、consistency 与既有结果一致性。

退出码：
  0  算出的分与标签写法都合规
  1  校准表有断言不成立（口径漂了）
  2  参数写错或标签写法不合规
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cli_util import Opt, UsageError, cli_entry, force_utf8, parse_options  # noqa: E402

GRADES = {
    "proved": 100,
    "mainstream": 80,
    "weakened": 60,
    "partial": 40,
    "heuristic": 20,
    "refuted": 0,
}

GRADE_LABELS = {
    "proved": "已有完整证明并经同行复核",
    "mainstream": "主流未证猜想，表述精确且有大量独立检验",
    "weakened": "弱化或条件化版本，整体证据不足",
    "partial": "仅有限情形或局部结果得到验证",
    "heuristic": "只有类比、启发式或量级论证",
    "refuted": "已被反例否证，或与已证结果直接矛盾",
}

ADJUST = ("precision", "testability", "independence", "consistency")

TAG_RE = re.compile(r"^评分/(0|[1-9]\d?|100)$")


def compute(grade: str, adjustments: dict[str, int]) -> int:
    base = GRADES[grade]
    total = base + sum(adjustments.get(k, 0) for k in ADJUST)
    return max(0, min(100, total))


def parse_tag(tag: str) -> tuple[bool, str]:
    name = tag.strip().lstrip("#").strip()
    if not name.startswith("评分"):
        return False, "不是评分标签"
    if not TAG_RE.match(name):
        return False, "写法须为 `#评分/N`，N 取 0–100 的整数（不留前导零）"
    return True, ""


def selftest() -> int:
    cases = [
        ("已证定理", "proved", {}, 100),
        ("黎曼猜想", "mainstream", {}, 80),
        ("准黎曼猜想", "weakened", {}, 60),
        ("有限情形已验证", "partial", {}, 40),
        ("仅有类比", "heuristic", {}, 20),
        ("已被反例否证", "refuted", {}, 0),
        ("黎曼猜想·表述含混", "mainstream", {"precision": -5}, 75),
        ("黎曼猜想·四项全加", "mainstream",
         {"precision": 5, "testability": 5, "independence": 5, "consistency": 5}, 100),
        ("已证定理·一致性存疑", "proved", {"consistency": -5}, 95),
        ("已证定理·四项全减", "proved",
         {"precision": -5, "testability": -5, "independence": -5, "consistency": -5}, 80),
        ("已否证·仍想加分", "refuted", {"precision": 5}, 5),
    ]
    ok = 0
    for name, grade, adj, want in cases:
        got = compute(grade, adj)
        flag = "✓" if got == want else "✗"
        if got == want:
            ok += 1
        print(f"{flag} {name}：{got}（期望 {want}）")
    tags = [("#评分/80", True), ("#评分/0", True), ("#评分/100", True),
            ("#评分/078", False), ("#评分/101", False), ("#评分/-1", False),
            ("#评分", False), ("#评分/80分", False)]
    for tag, want in tags:
        good, _ = parse_tag(tag)
        if good == want:
            ok += 1
            print(f"✓ 标签 {tag}：{'合法' if want else '按预期拒绝'}")
        else:
            print(f"✗ 标签 {tag}：判定与预期不符")
    total_cases = len(cases) + len(tags)
    print(f"\n[校准] {ok}/{total_cases} 条成立")
    if ok != total_cases:
        print("[结论] 层级表与锚点表不一致，先修口径再写卡")
        return 1
    print("[结论] 六档等级与四个锚点全部对上（黎曼猜想 80、准黎曼猜想 60）")
    return 0


_OPTS = [
    Opt("--grade", "grade", default=None,
        hint="等级：proved/mainstream/weakened/partial/heuristic/refuted"),
    Opt("--tag", "tag", default=None, hint="只核 `#评分/N` 的写法"),
    Opt("--precision", "precision", "int", low=-5, high=5, default=0),
    Opt("--testability", "testability", "int", low=-5, high=5, default=0),
    Opt("--independence", "independence", "int", low=-5, high=5, default=0),
    Opt("--consistency", "consistency", "int", low=-5, high=5, default=0),
    Opt("--explain", "explain", "flag", hint="逐项打印底分与修正"),
    Opt("--selftest", "selftest", "flag", hint="跑六档等级与四个锚点的校准表"),
]


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if args else 2
    try:
        ns, _pos = parse_options(args, _OPTS)
    except UsageError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 2
    if ns["selftest"]:
        return selftest()

    grade = ns["grade"]
    tag = ns["tag"]
    adjustments = {k: ns[k] for k in ADJUST if ns[k]}
    if grade is None and tag is None:
        print("[失败] 至少给 --grade 或 --tag（校准表用 --selftest）", file=sys.stderr)
        return 2

    if tag is not None:
        good, why = parse_tag(tag)
        if not good:
            print(f"[评分] 标签不合规：{tag} —— {why}")
            return 2
        print(f"[评分] 标签写法合规：{tag}")

    if grade is not None:
        if grade not in GRADES:
            print(f"[失败] 未知等级：{grade}（可选 {'、'.join(GRADES)}）", file=sys.stderr)
            return 2
        score = compute(grade, adjustments)
        if ns["explain"]:
            print(f"  等级 {grade}（{GRADE_LABELS[grade]}）底分 {GRADES[grade]}")
            for k in ADJUST:
                print(f"  {k}：{adjustments.get(k, 0):+d}")
            print("  合计夹在 0–100")
        print(f"[评分] {score}")
        print(f"[标签] #评分/{score}")
        if tag is not None and tag.strip().lstrip("#") != f"评分/{score}":
            print("[提示] 标签里的数值与算分不一致：以算分为准，改标签或改修正项")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(UsageError,)))
