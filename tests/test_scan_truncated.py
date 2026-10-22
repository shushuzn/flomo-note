#!/usr/bin/env python3
"""scan_truncated.py 的离线回归用例（不联网）。

钉住四件事，每一条都对应一个真实踩过的坑：
1. **候选只取 id，绝不拿检索回包的 content 判正文**——那个 content 本身就是截断的，
   用它判会得到成片假阳性（实测搜「此处省略」41 张，真实正文命中 0 张）；
2. 命中只在 `memo_batch_get` 的真实正文里认定，且必须报出 id / 标记 / 修复口径；
3. **取不到正文 ≠ 干净**：任一云端调用失败即退出码 2，不许把「没读到」报成「没问题」；
4. 分批不超单批上限。

退出码 0 = 全部通过。
"""

COVERS = ("scan_truncated",)

import contextlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

ST = None
FC = None
RESULTS = []


def _load():
    global ST, FC
    import importlib.util
    for name, fn in (("flomo_client", "flomo_client.py"), ("scan_truncated", "scan_truncated.py")):
        spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / fn)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        if name == "scan_truncated":
            ST = mod
        else:
            FC = mod


_load()


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


def card(mid, concept, body="正文内容", tags=("AI/测试",)):
    content = f"#{tags[0]}\n{concept}\n\n{body}\n"
    return {"id": mid, "word_count": len(content), "tags": list(tags), "content": content}


class FakeClient:
    def __init__(self, search=None, batch=None, tags=None, fail_on=None, max_per_call=None):
        self.calls = []
        # 模拟「回包挤不下」：一次最多只回这么多条，其余静默丢掉
        self.max_per_call = max_per_call
        # search: tag -> [memo]；这些 memo 的 content 模拟「检索回包」——可以是截断的
        self.search = search or {}
        # batch: id -> memo；这是「真实正文」，判定只看它
        self.batch = batch or {}
        self.tags = tags if tags is not None else []
        self.fail_on = fail_on

    def init(self):
        pass

    def tool(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        if self.fail_on and name == self.fail_on:
            raise FC.FlomoError("boom")
        if name == "memo_search":
            return {"structuredContent": {"memos": self.search.get(args.get("tag", ""), [])}}
        if name == "memo_batch_get":
            ids = args.get("ids") or []
            memos = [self.batch[i] for i in ids if i in self.batch]
            if self.max_per_call is not None:
                memos = memos[:self.max_per_call]
            return {"structuredContent": {"memos": memos}}
        if name == "tag_tree":
            # 真实回包给的是字符串列表（曾按对象取 path 而 AttributeError），
            # 用例按真实形态构造，避免脚本里两种形态各写一份解法的坑再犯。
            return {"structuredContent": {"tags": list(self.tags)}}
        raise AssertionError(f"未预置的工具调用：{name}")


def run(argv, client):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ST.main(argv, client=client)
    return code, out.getvalue()


MARK = "...[此处省略419字]..."


def test_search_content_never_judged():
    """检索回包的 content 带占位符、真实正文干净 → 必须判干净（不拿回包判正文）。"""
    fake_search = card("A", "某概念", body="甲\n\n" + MARK + "\n\n乙")
    clean = card("A", "某概念", body="甲\n\n乙")
    client = FakeClient(search={"AI/测试": [fake_search]}, batch={"A": clean})
    code, out = run(["--tags", "AI/测试"], client)
    check("回包截断不被当成命中", "干净" in out and code == 0, out[-200:])
    check("确实只按 tag 取 id、再按 id 取正文",
          [c[0] for c in client.calls] == ["memo_search", "memo_batch_get"],
          [c[0] for c in client.calls])


def test_hit_detected_in_real_body():
    body = "甲\n\n" + MARK + "\n\n乙"
    client = FakeClient(search={"AI/测试": [card("A", "某概念")]},
                        batch={"A": card("A", "某概念", body=body)})
    code, out = run(["--tags", "AI/测试"], client)
    check("真实正文含占位符即命中", "命中" in out and code == 1, (code, out[-200:]))
    check("报出 id", "A" in out, out[-300:])
    check("报出标记原文", "此处省略419字" in out, out[-300:])
    check("报出概念名（便于人工定位）", "某概念" in out, out[-300:])
    check("给出修复口径", "publish --update-id" in out, out[-300:])


def test_failure_is_not_clean():
    """取正文失败必须退出码 2 且明说「不完整」，不得报干净。"""
    client = FakeClient(search={"AI/测试": [card("A", "某概念")]}, fail_on="memo_batch_get")
    code, out = run(["--tags", "AI/测试"], client)
    check("取正文失败 → 退出码 2", code == 2, code)
    check("明说扫描不完整", "不完整" in out, out[-300:])
    check("不把失败报成干净", "[干净]" not in out and "未完成" in out, out[-300:])


def test_tag_tree_failure_is_not_clean():
    client = FakeClient(tags=[], fail_on="tag_tree")
    code, out = run([], client)
    check("标签树取不到 → 退出码 2", code == 2, code)
    check("不静默扫 0 张当干净", "[干净]" not in out and "未完成" in out, out[-300:])


def test_silent_miss_is_not_clean():
    """云端「成功但少回一条」也是未完成：必须点名缺失 id 并退出码 2。"""
    client = FakeClient(search={"A/甲": [card("A", "甲概念"), card("B", "乙概念")]},
                        batch={"A": card("A", "甲概念")})  # B 故意不回
    code, out = run(["--tags", "A/甲"], client)
    check("少回一张 → 退出码 2", code == 2, code)
    check("点名缺失的 id", "B" in out, out[-300:])
    check("不据此判干净", "[干净]" not in out, out[-300:])


def test_short_reply_is_retried_one_by_one():
    """回包挤不下少回的，必须逐张补取——补到了就不是「未完成」。"""
    ids = ["A", "B", "C"]
    client = FakeClient(search={"A/甲": [card(i, f"概念{i}") for i in ids]},
                        batch={i: card(i, f"概念{i}") for i in ids}, max_per_call=2)
    code, out = run(["--tags", "A/甲"], client)
    check("少回后逐张补取 → 三张都读到", "候选 3 张" in out and code == 0, (code, out[-200:]))
    check("补取后不再报未完成", "[未完成]" not in out, out[-200:])
    singles = [c for c in client.calls if c[0] == "memo_batch_get" and len(c[1]["ids"]) == 1]
    check("补取按单张发（不是重发整批）", len(singles) == 1,
          [c[1].get("ids") for c in client.calls if c[0] == "memo_batch_get"])


def test_batching_and_dedup():
    ids = [f"ID{i:02d}" for i in range(12)]
    # 两张标签下重复出现同一批卡：去重后只取一次
    client = FakeClient(
        search={"A/甲": [card(i, f"概念{i}") for i in ids],
                "B/乙": [card(i, f"概念{i}") for i in ids]},
        batch={i: card(i, f"概念{i}") for i in ids},
    )
    code, out = run(["--tags", "A/甲,B/乙"], client)
    batches = [c for c in client.calls if c[0] == "memo_batch_get"]
    check("单批 ≤10 张", [len(b[1]["ids"]) for b in batches] == [10, 2],
          [len(b[1]["ids"]) for b in batches])
    check("跨标签去重后 12 张", "候选 12 张" in out, out[-300:])
    check("退出码 0", code == 0, code)


def test_all_tags_path():
    client = FakeClient(search={"A/甲": [card("X", "概念X")]}, batch={"X": card("X", "概念X")},
                        tags=["A/甲", "B/乙"])
    code, out = run([], client)
    names = [c[0] for c in client.calls]
    check("默认走 tag_tree 再逐标签检索",
          names == ["tag_tree", "memo_search", "memo_search", "memo_batch_get"], names)
    check("两个标签都检索", [c[1].get("tag") for c in client.calls if c[0] == "memo_search"]
          == ["A/甲", "B/乙"],
          [c[1] for c in client.calls if c[0] == "memo_search"])


def test_marker_forms():
    check("半角括号 + 省略号上下文", ST.find_truncation("甲\n" + MARK + "\n乙") == ["[此处省略419字]"])
    check("全角括号与空格", ST.find_truncation("［此处省略 12 字］") == ["［此处省略 12 字］"])
    check("无数字形态", ST.find_truncation("[此处省略若干字]") == ["[此处省略若干字]"])
    check("散文「不可省略」不误判", ST.find_truncation("分割假设一般不可省略") == [])
    check("纯文本无标记", ST.find_truncation("正文完整，甲乙丙") == [])


def test_json_output():
    tmp = Path(tempfile.mkdtemp())
    try:
        p = tmp / "hits.json"
        client = FakeClient(search={"A/甲": [card("A", "某概念")]},
                           batch={"A": card("A", "某概念", body="甲\n" + MARK + "\n乙")})
        code, out = run(["--tags", "A/甲", "--json", str(p)], client)
        ok = p.exists()
        data = json.loads(p.read_text(encoding="utf-8")) if ok else {}
        check("--json 落盘", ok and data.get("candidates") == 1, (ok, data.get("candidates")))
        check("落盘内容含命中 id", [h["id"] for h in data.get("hits", [])] == ["A"], data)
        check("命中时退出码仍为 1", code == 1, code)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_search_content_never_judged()
    test_hit_detected_in_real_body()
    test_failure_is_not_clean()
    test_tag_tree_failure_is_not_clean()
    test_silent_miss_is_not_clean()
    test_short_reply_is_retried_one_by_one()
    test_batching_and_dedup()
    test_all_tags_path()
    test_marker_forms()
    test_json_output()
    bad = RESULTS.count(False)
    print(f"\n{len(RESULTS)} 项断言，{len(RESULTS) - bad} 通过，{bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
