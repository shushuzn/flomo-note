#!/usr/bin/env python3
"""tagcheck.py 的离线回归用例（不联网）。"""
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


TC = _load("tagcheck", "tagcheck.py")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


class FakeClient:
    def __init__(self, tags, total=None):
        self.tags = tags
        self.total = len(tags) if total is None else total
        self.calls = []

    def init(self):
        pass

    def tool(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        return {"structuredContent": {"tags": self.tags, "total": self.total}}


def run(argv, client):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = TC.main(argv, client=client)
    return code, out.getvalue(), err.getvalue()


TAGS = ["AI", "AI/Agent", "AI/AgentHarness", "科技/测试"]


def test_existing_ok():
    code, out, _err = run(["--tags", "AI/Agent,科技/测试"], FakeClient(TAGS))
    check("已存在且两级：退出码 0", code == 0, code)
    check("已存在：给同顶层兄弟", "AI/AgentHarness" in out, out)
    check("标签树全量口径打印", "4 个" in out, out)


def test_missing_tag():
    code, out, _err = run(["--tags", "AI/不存在"], FakeClient(TAGS))
    check("缺失标签：退出码 2", code == 2, code)
    check("缺失标签：指出可新建二级", "该顶层存在" in out, out)


def test_three_levels():
    code, out, _err = run(["--tags", "AI/Agent/子级"], FakeClient(TAGS))
    check("三级标签：退出码 2", code == 2, code)
    check("三级标签：指出 H17", "严格两级" in out, out)


def test_truncated_tree():
    code, _out, err = run(["--tags", "AI/Agent"], FakeClient(TAGS, total=5))
    check("标签树截断：退出码 1", code == 1, code)
    check("标签树截断：结论不可信", "截断" in err, err)


def test_hash_prefix_and_split():
    code, out, _err = run(["--tags", "#AI/Agent，科技/测试"], FakeClient(TAGS))
    check("去 # 前缀与全角逗号分隔", code == 0 and out.count("✓") == 2, out)


if __name__ == "__main__":
    for fn in (test_existing_ok, test_missing_tag, test_three_levels,
               test_truncated_tree, test_hash_prefix_and_split):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
