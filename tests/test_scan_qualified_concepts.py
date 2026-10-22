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
    default = Q.resolve_tags([])
    check("无参时给默认标签簇", len(default) >= 5 and all("/" in t for t in default), default)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "tags.txt"
        f.write_text("AI/RAG\n\n科技/开源\n  AI/Agent  \n", encoding="utf-8")
        got = Q.resolve_tags(["@" + str(f)])
        check("@文件模式读行并去空白", got == ["AI/RAG", "科技/开源", "AI/Agent"], got)

        raised = None
        try:
            Q.resolve_tags(["@" + str(Path(d) / "missing.txt")])
        except SystemExit as e:
            raised = e.code
        check("@文件不存在时明确失败（不静默空列表）", raised == 2, raised)

    # 4) 取卡与去重
    by_tag = {
        "AI/RAG": [{"id": "m1", "content": card}, {"id": "m2", "content": card}],
        "AI/Agent": [{"id": "m2", "content": card}],
    }
    c = FakeClient(by_tag)
    seen, failed = Q.collect_memos(c, ["AI/RAG", "AI/Agent"])
    check("跨簇按 id 去重", sorted(seen) == ["m1", "m2"], sorted(seen))
    check("全部成功时失败簇为 0", failed == [], failed)

    c2 = FakeClient(by_tag)
    seen2, failed2 = Q.collect_memos(c2, ["AI/RAG", "AI/boom", "AI/Agent"])
    check("一簇失败不影响其余取回", sorted(seen2) == ["m1", "m2"], sorted(seen2))
    check("失败簇被计数报出（不许静默缩样本）", failed2 == ["AI/boom"], failed2)


if __name__ == "__main__":
    run_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
