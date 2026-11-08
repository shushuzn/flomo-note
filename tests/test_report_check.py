#!/usr/bin/env python3
"""report_check.py 回归用例（离线，不联网）。

两次事故各钉一组判据：
  1. 卡片全做对、关账也做了，末条回复却只发「建了 2 张卡。」——逐卡明细整段
     缺失，报告是流水线里唯一没有校验器的一段，机器侧看不见；
  2. 新建 3 张、更新 1 张混在一句「建了 4 张卡」里——明细齐全，但读者分不出
     哪几张是这轮新落的、哪几张是改既有卡。云端也分不出（新建卡的 created_at
     可能就是今天），所以分类只能由账本 `updated` 给出。

本用例钉住：明细条数、id 归属、字数/标签/概念名对账、说明段字数上限、
「新建」「更新」两段分写与归类正确。

账本与云端都用假数据注入：`round_ledger.ledger_path` 指到临时文件，
`verify_card.fetch` 换成返回固定 word_count 的假函数——用例不碰真实云、不联网。
退出码 0 = 全部通过。
"""

COVERS = ("report_check",)

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    line = f"{'PASS' if ok else 'FAIL'}  {label}"
    if not ok and detail:
        line += f"\n      {detail}"
    print(line)


CARDS = {
    "MjY0MTIzNTAw": {"word_count": 444, "tags": ["a", "b"],
                     "content": "#产业/游戏<br>\n蓝图（Blueprints）<br>\n\n正文\n"},
    "MjY0MTIzNTMw": {"word_count": 479, "tags": ["a"],
                     "content": "#科技/编程语言 #产业/游戏<br>\nVerse<br>\n\n正文\n"},
    "MjY0MTIzNTQw": {"word_count": 512, "tags": ["a"],
                     "content": "#产业/游戏<br>\n场景图<br>\n\n正文\n"},
    "MjY0MTIzNTUw": {"word_count": 600, "tags": ["a"],
                     "content": "#AI/智能体<br>\nDots：常驻智能体<br>\n\n正文\n"},
}
URL = "https://example.com/a"


def _prepare(ids, outcome="published", updated=()):
    tmp = tempfile.mkdtemp(prefix="rc_")
    ledger = Path(tmp) / "rounds.json"
    ledger.write_text(json.dumps({
        "rounds": [{
            "url": URL, "article": "", "opened_at": "t", "closed_at": "t",
            "outcome": outcome, "detail": ",".join(ids),
            "updated": ",".join(updated),
        }]
    }, ensure_ascii=False), encoding="utf-8")
    os.environ["FLOMO_ROUND_LEDGER"] = str(ledger)


_REAL_FETCH = None


def _install_fake_fetch():
    """换掉 fetch 取假卡片；必须在 main 里成对还原——本仓库的用例是被当脚本
    在同一进程里执行的，漏还原会让后面 test_verify_card 拿到假取件而 KeyError。"""
    global _REAL_FETCH
    import verify_card
    if _REAL_FETCH is None:
        _REAL_FETCH = verify_card.fetch
    verify_card.fetch = lambda memo_id, client=None: dict(CARDS[memo_id], id=memo_id)


def _restore_fetch():
    import verify_card
    if _REAL_FETCH is not None:
        verify_card.fetch = _REAL_FETCH


def _run(text):
    import report_check
    return report_check.main(["--text", text, "--url", URL])


def main() -> int:
    global _LEDGER_ENV
    _LEDGER_ENV = os.environ.get("FLOMO_ROUND_LEDGER")
    _install_fake_fetch()
    try:
        return _run_all()
    finally:
        _restore_fetch()
        if _LEDGER_ENV is None:
            os.environ.pop("FLOMO_ROUND_LEDGER", None)
        else:
            os.environ["FLOMO_ROUND_LEDGER"] = _LEDGER_ENV


def _run_all() -> int:

    # 1. 事故原形：只有一句「建了 2 张卡。」→ 必须拦
    _prepare(list(CARDS)[:2])
    rc = _run("建了 2 张卡。")
    check("只发半句的报告被拦（事故原形）", rc == 1, f"退出码 {rc}")

    # 2. 全新建 + 独立「新建」段 → 放行
    good = (
        "建了 2 张卡。\n\n"
        "新建：\n"
        "- 蓝图（Blueprints）：引擎里搭逻辑的节点图，这轮记它与 Verse 的分工。\n"
        "- Verse：新语言，这轮记它的类型系统与场景图接法。\n\n"
        "蓝图（Blueprints）：MjY0MTIzNTAw，#产业/游戏，实测 444 字\n"
        "Verse：MjY0MTIzNTMw，#科技/编程语言 #产业/游戏，实测 479 字"
    )
    rc = _run(good)
    check("明细齐全且对账一致时放行", rc == 0, f"退出码 {rc}")

    # 2b. 全新建却没写「新建」段（都并进开头句）→ 必须拦
    flat = good.replace("新建：\n", "")
    rc = _run(flat)
    check("全新建但缺「新建」段被拦", rc == 1, f"退出码 {rc}")

    # 2c. 概念名本身含全角冒号：明细行要按最后一个冒号切出 id → 放行
    _prepare(["MjY0MTIzNTUw"])
    colon_name = (
        "建了 1 张卡。\n\n"
        "新建：\n"
        "- Dots：常驻智能体：这轮记它的常驻形态。\n\n"
        "Dots：常驻智能体：MjY0MTIzNTUw，#AI/智能体，实测 600 字"
    )
    rc = _run(colon_name)
    check("概念名含冒号时明细仍可解析", rc == 0, f"退出码 {rc}")

    # 3. 字数抄错 → 必须拦
    bad_n = good.replace("实测 444 字", "实测 450 字")
    rc = _run(bad_n)
    check("字数与云端不符被拦", rc == 1, f"退出码 {rc}")

    # 4. 标签少写 → 必须拦
    bad_tag = good.replace("#科技/编程语言 #产业/游戏，实测 479", "#科技/编程语言，实测 479")
    rc = _run(bad_tag)
    check("标签少写被拦", rc == 1, f"退出码 {rc}")

    # 5. 概念名写错 → 必须拦
    bad_name = good.replace("Verse：MjY0MTIzNTMw", "Varse：MjY0MTIzNTMw")
    rc = _run(bad_name)
    check("概念名不符被拦", rc == 1, f"退出码 {rc}")

    # 6. 明细里混入不属于本轮的 id → 必须拦
    extra = good + "\n别的卡：MjYwMDAwMDAw，#产业/游戏，实测 300 字"
    rc = _run(extra)
    check("混入非本轮 id 被拦", rc == 1, f"退出码 {rc}")

    # 7. 说明段超限（2 张卡上限 360 字）→ 必须拦
    long_prose = "字" * 361
    rc = _run(long_prose + "\n\n新建：\n- 蓝图（Blueprints）：一句话。\n- Verse：一句话。\n\n"
              + good.split("\n\n")[-1])
    check("说明段超字数上限被拦", rc == 1, f"退出码 {rc}")

    # 8. 退出轮（无卡）：明细为空、只写说明段 → 放行
    _prepare([], outcome="exit")
    rc = _run("这篇没有建卡：对象都只有一两句提及，已按退出留痕关账。")
    check("退出轮只写说明段即放行", rc == 0, f"退出码 {rc}")

    # 9. 混合轮：2 新建 + 1 更新，分两段写 → 放行
    mixed_ids = ["MjY0MTIzNTAw", "MjY0MTIzNTMw", "MjY0MTIzNTQw"]
    _prepare(mixed_ids, updated=["MjY0MTIzNTQw"])
    mixed = (
        "建了 3 张卡，更新 1 张。\n\n"
        "新建：\n"
        "- 蓝图（Blueprints）：引擎里搭逻辑的节点图。\n"
        "- Verse：新语言，记它的类型系统。\n\n"
        "更新：\n"
        "- 场景图：补了新的渲染管线，删掉旧的两行。\n\n"
        "蓝图（Blueprints）：MjY0MTIzNTAw，#产业/游戏，实测 444 字\n"
        "Verse：MjY0MTIzNTMw，#科技/编程语言 #产业/游戏，实测 479 字\n"
        "场景图：MjY0MTIzNTQw，#产业/游戏，实测 512 字"
    )
    rc = _run(mixed)
    check("新建与更新分段写时放行", rc == 0, f"退出码 {rc}")

    # 9b. 混合轮都并进开头句、不分段 → 必须拦
    rc = _run(mixed.replace("新建：\n", "").replace("更新：\n", ""))
    check("混合轮不分段被拦", rc == 1, f"退出码 {rc}")

    # 9c. 更新卡写进「新建」段 → 必须拦（等于把改动说成新建）
    misplaced = mixed.replace("- 场景图：补了新的渲染管线，删掉旧的两行。\n", "")
    misplaced = misplaced.replace(
        "- Verse：新语言，记它的类型系统。\n",
        "- Verse：新语言，记它的类型系统。\n- 场景图：补了新的渲染管线，删掉旧的两行。\n")
    rc = _run(misplaced)
    check("更新卡写进新建段被拦", rc == 1, f"退出码 {rc}")

    # 9d. 只有更新的一轮写了空「新建」段 → 必须拦
    _prepare(["MjY0MTIzNTQw"], updated=["MjY0MTIzNTQw"])
    empty_new = (
        "更新 1 张卡。\n\n"
        "新建：\n\n"
        "更新：\n"
        "- 场景图：补了新的渲染管线，删掉旧的两行。\n\n"
        "场景图：MjY0MTIzNTQw，#产业/游戏，实测 512 字"
    )
    rc = _run(empty_new)
    check("空的新建段被拦", rc == 1, f"退出码 {rc}")

    # 9e. 只有更新、只写「更新」段 → 放行
    rc = _run(empty_new.replace("新建：\n\n", ""))
    check("只更新时只写更新段即放行", rc == 0, f"退出码 {rc}")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} 通过")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
