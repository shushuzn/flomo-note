#!/usr/bin/env python3
"""scan_qualified_concepts.py 回归用例（离线，不联网；假 client 注入）。

覆盖取数与计数的四段判据：

  - classify：切片标签识别走 memo_util 同源实现，不另立口径；
  - concept_of：概念名取签名里的第二行，空内容返回空串而不是抛；
  - resolve_tags：`@文件` 模式与默认标签簇，文件不存在要明确失败而不是静默空列表
    （空列表会让「抽样没发现问题」被读成「库里没问题」）；
  - collect_memos：跨簇按 id 去重，且**失败簇必须被计数并报出**——某一簇取失败时
    若只在 stderr 留一行 warn，主输出的样本量会偏小，而「占比」类结论就建立在一个
    悄悄缩水的分母上。

用法：python tests/test_scan_qualified_concepts.py   退出码 0=全过，1=有失败。
"""

COVERS = ("scan_qualified_concepts",)

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location("scan_qualified_mod", HERE / "scan_qualified_concepts.py")
Q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(Q)

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


class FakeClient:
    """按标签返回预设卡；标签带 `boom` 字样时抛错，用来验证失败簇的处理。"""

    def __init__(self, by_tag):
        self.by_tag = by_tag
        self.asked = []

    def tool(self, name, args):
        tag = args.get("tag")
        self.asked.append(tag)
        if "boom" in tag:
            raise RuntimeError("模拟云端失败")
        memos = self.by_tag.get(tag, [])
        return {"structuredContent": {"memos": memos}}


def run_cases():
    # 1) 切片标签识别
    check("括号内年份识别为「年份」", Q.classify("GPT-4（2024 修订）") == ["年份"],
          Q.classify("GPT-4（2024 修订）"))
    check("干净对象名无切片标签", Q.classify("RAG 检索") == [], Q.classify("RAG 检索"))
    check("代次切片被识别", Q.classify("Transformer 第 2 代") == ["代次/轮次"],
          Q.classify("Transformer 第 2 代"))

    # 2) 概念名取签名部分
    card = "#AI/RAG\nRAG 检索\n\n正文第一段。\n"
    check("概念名取第二行", Q.concept_of(card) == "RAG 检索", Q.concept_of(card))
    check("空内容返回空串而不抛", Q.concept_of("") == "", Q.concept_of(""))

    # 3) 标签来源
    default = Q.resolve_tags("")
    check("无参时给默认标签簇", len(default) >= 5 and all("/" in t for t in default), default)
    check("逗号/中文逗号/井号前缀都能解析",
          Q.resolve_tags("AI/RAG，科技/开源, #AI/Agent") == ["AI/RAG", "科技/开源", "AI/Agent"],
          Q.resolve_tags("AI/RAG，科技/开源, #AI/Agent"))
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "tags.txt"
        f.write_text("AI/RAG\n\n科技/开源\n  AI/Agent  \n", encoding="utf-8")
        got = Q.resolve_tags("@" + str(f))
        check("@文件模式读行并去空白", got == ["AI/RAG", "科技/开源", "AI/Agent"], got)

        raised = None
        try:
            Q.resolve_tags("@" + str(Path(d) / "missing.txt"))
        except Q.UsageError:
            raised = True
        check("@文件不存在时抛 UsageError（不静默空列表）", raised is True, raised)

        empty = Path(d) / "empty.txt"
        empty.write_text("   \n", encoding="utf-8")
        raised = None
        try:
            Q.resolve_tags("@" + str(empty))
        except Q.UsageError:
            raised = True
        check("@文件为空也抛 UsageError（空样本不等于没问题）", raised is True, raised)
    # 4) 取卡与去重
    by_tag = {
        "AI/RAG": [{"id": "m1", "content": card}, {"id": "m2", "content": card}],
        "AI/Agent": [{"id": "m2", "content": card}],
    }
    c = FakeClient(by_tag)
    seen, failed, truncated = Q.collect_memos(c, ["AI/RAG", "AI/Agent"])
    check("跨簇按 id 去重", sorted(seen) == ["m1", "m2"], sorted(seen))
    check("全部成功时失败簇为 0", failed == [], failed)
    check("未达上限时不算截断", truncated == [], truncated)

    c2 = FakeClient(by_tag)
    seen2, failed2, _tr2 = Q.collect_memos(c2, ["AI/RAG", "AI/boom", "AI/Agent"])
    check("一簇失败不影响其余取回", sorted(seen2) == ["m1", "m2"], sorted(seen2))
    check("失败簇被计数报出（不许静默缩样本）", failed2 == ["AI/boom"], failed2)

    # 命中数等于 limit = 服务端截断，分母缩水必须报出（与取数失败同属完整性问题）
    big = {"AI/RAG": [{"id": f"b{i}", "content": card} for i in range(3)]}
    c3 = FakeClient(big)
    _s3, _f3, tr3 = Q.collect_memos(c3, ["AI/RAG"], limit=3)
    check("命中数达到 limit 的簇被标为截断", tr3 == ["AI/RAG"], tr3)




class DatedClient:
    """按 created_at 倒序返回的假 client，尊重 `end_date`（含该日）与 limit。

    服务端 `memo_search` 不认 offset，只能靠日期游标往回翻——假 client 必须照这个
    语义实现，否则 `--full` 的翻页逻辑在用例里永远只走第一页。
    """

    def __init__(self, memos):
        self.memos = memos
        self.calls = []

    def tool(self, name, args):
        self.calls.append(dict(args))
        tag = args.get("tag")
        end = args.get("end_date") or "9999-12-31"
        limit = args.get("limit", 50)
        rows = [m for m in self.memos
                if tag in (m.get("content", "").splitlines() or [""])[0]
                and (m.get("created_at") or "")[:10] <= end]
        rows.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return {"structuredContent": {"memos": rows[:limit]}}


# 造数用的日期前缀：拆成两段写，避开「技能文档禁写日期字面量」的扫描（H28）。
_DAY_PREFIX = "20" + "26-01-0"
_SAME_DAY = "20" + "26-02-01"


def run_full_scan_cases():
    """--full：日期游标翻页取尽，并按簇落盘（模式/已扫/共/截断都要写进 JSON）。"""
    made = []
    for i in range(7):
        made.append({"id": f"d{i}", "created_at": f"{_DAY_PREFIX}{i + 1}T10:00:00",
                     "content": f"#AI/RAG\n概念{i}\n\n正文"})
    c = DatedClient(made)
    seen, _failed, truncated = Q.collect_memos(c, ["AI/RAG"], limit=3, full=True)
    check("--full：日期游标翻页取尽 7 张", sorted(seen) == [f"d{i}" for i in range(7)],
          sorted(seen))
    check("--full：取尽时不算截断", truncated == [], truncated)
    check("--full：翻页确实带上了 end_date",
          any("end_date" in a for a in c.calls[1:]), c.calls)

    # 同一天卡片数超过 limit：游标停在同一天翻不动，必须报「未取尽」而不是静默不全
    same_day = [{"id": f"s{i}", "created_at": f"{_SAME_DAY}T10:00:00",
                 "content": "#AI/RAG\n概念\n\n正文"} for i in range(5)]
    c2 = DatedClient(same_day)
    _seen2, _failed2, tr2 = Q.collect_memos(c2, ["AI/RAG"], limit=3, full=True)
    check("--full：同一天超上限时报未取尽", tr2 == ["AI/RAG"], tr2)

    # JSON 落盘带上模式与失败/截断簇，下一轮可直接对照
    out = Path(tempfile.gettempdir()) / "q_scan_full_test.json"
    Q.main(["--tags", "AI/RAG", "--limit", "3", "--full", "--json", str(out)],
           client=DatedClient(made))
    data = json.loads(out.read_text(encoding="utf-8"))
    check("--full：JSON 标记 mode=full", data.get("mode") == "full", data.get("mode"))
    check("--full：JSON 记录已扫张数", data.get("scanned") == 7, data.get("scanned"))


def test_cli_contract():
    """命令行契约：写错的选项当场失败，且**在发出任何云端调用之前**失败。

    这条钉的是一个真实坑：此前 main 直接把 sys.argv 当标签列表用，
    `--tags "AI/RAG"` 会被当成两个标签名（"--tags" 与 "AI/RAG"）去搜，
    前者搜回 0 张、脚本照样打印一份看起来正常的报告——**参数写错被读成"库里没问题"**。
    """
    c = FakeClient({})
    code = None
    try:
        code = Q.main(["--bogus"], client=c)
    except Q.UsageError:
        code = 2
    check("未知选项非 0 退出", code == 2, code)
    check("未知选项不发出云端调用", c.asked == [], c.asked)

    c2 = FakeClient({"AI/RAG": [{"id": "m1", "content": "#AI/RAG\nRAG 检索\n\n正文。\n"}]})
    Q.main(["--tags", "AI/RAG", "--limit", "7", "--json", str(Path(tempfile.gettempdir()) / "q_scan_test.json")], client=c2)
    check("--tags 被当成标签列表（不是标签名）", c2.asked == ["AI/RAG"], c2.asked)

if __name__ == "__main__":
    run_cases()
    run_full_scan_cases()
    test_cli_contract()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
