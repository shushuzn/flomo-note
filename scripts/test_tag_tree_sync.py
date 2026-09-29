#!/usr/bin/env python3
"""tag_tree_sync.py 回归用例（离线，不联网）。

覆盖：响应解析的两种包裹、快照核对的三类不一致、离线重写的端到端闭环。
用例全部在临时沙箱内进行，不触碰真实快照。退出码 0 = 全部通过。
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tag_tree_sync", HERE / "tag_tree_sync.py")
TS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TS)

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


TAGS = ["科技/机器人", "科技/安全", "投资/", "文化/影视"]


def _body(total=4, tags=None):
    return {"structuredContent": {"total": total, "tags": TAGS if tags is None else tags}}


def _run_main(argv):
    """跑 main() 并吞掉其输出，返回退出码。"""
    old = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["tag_tree_sync.py"] + argv
        with contextlib.redirect_stdout(buf):
            return TS.main()
    finally:
        sys.argv = old


def run_tags_total():
    total, tags = TS._tags_total(_body())
    check("structuredContent 形态可解析", total == 4 and tags == TAGS)

    inner = json.dumps({"total": 4, "tags": TAGS}, ensure_ascii=False)
    t2, g2 = TS._tags_total({"content": [{"type": "text", "text": inner}]})
    check("content[].text 兜底可解析", t2 == 4 and g2 == TAGS)

    check("两种包裹都取不到时返回 (None, None)",
          TS._tags_total({"content": [{"text": "不是JSON"}]}) == (None, None))
    check("非字典输入返回 (None, None)", TS._tags_total(None) == (None, None))


def run_local_state_and_check():
    base = Path(tempfile.mkdtemp())
    good = base / "good.txt"
    good.write_text(TS.render_tag_tree(TAGS, 4), encoding="utf-8")

    state = TS._local_state([good])
    check("_local_state 读出首行 total 与列出数",
          state[0][1] == 4 and state[0][2] == 4)

    ok, problems = TS._check(4, TAGS, state)
    check("自洽时无问题", ok and problems == [])

    bad_total = base / "bad_total.txt"
    bad_total.write_text(TS.render_tag_tree(TAGS, 4).replace("# total=4", "# total=3", 1),
                         encoding="utf-8")
    ok1, p1 = TS._check(4, TAGS, TS._local_state([bad_total]))
    check("首行 total 与云端不符被报出",
          not ok1 and any("total=3" in x for x in p1))

    bad_listed = base / "bad_listed.txt"
    bad_listed.write_text("# total=4\n# 科技\n  科技/机器人\n", encoding="utf-8")
    ok2, p2 = TS._check(4, TAGS, TS._local_state([bad_listed]))
    check("列出数与首行 total 不自洽被报出",
          not ok2 and any("列出" in x for x in p2))

    ok3, p3 = TS._check(4, TAGS, TS._local_state([base / "nope.txt"]))
    check("快照缺失被报出", not ok3 and any("不存在" in x for x in p3))

    ok4, p4 = TS._check(9, TAGS, TS._local_state([good]))
    check("云端数据自身不自洽被报出",
          not ok4 and any("云端数据自身不自洽" in x for x in p4))


def run_end_to_end():
    base = Path(tempfile.mkdtemp())
    resp = base / "resp.json"
    resp.write_text(json.dumps(_body(), ensure_ascii=False), encoding="utf-8")

    p1, p2 = base / "a.txt", base / "b.txt"
    p1.write_text("# total=1\n# 旧\n  旧/残留\n", encoding="utf-8")
    TS.SNAPSHOT_PATHS = [p1, p2]

    check("--check 对陈旧快照返回 1", _run_main(["--response", str(resp), "--check"]) == 1)
    check("--check 不改写已有文件", p1.read_text(encoding="utf-8").startswith("# total=1"))
    check("--check 不创建缺失文件", not p2.exists())

    check("默认重写返回 0", _run_main(["--response", str(resp)]) == 0)
    a, b = p1.read_text(encoding="utf-8"), p2.read_text(encoding="utf-8")
    check("两处快照写入内容一致", a == b)
    check("重写后自洽（列出数 == total == 4）",
          sum(TS.count_snapshot(a.splitlines())) == 4 and a.startswith("# total=4"))
    check("已自洽时幂等（再跑仍返回 0）", _run_main(["--response", str(resp)]) == 0)


if __name__ == "__main__":
    run_tags_total()
    run_local_state_and_check()
    run_end_to_end()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
