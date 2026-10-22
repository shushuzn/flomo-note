#!/usr/bin/env python3
"""dedup.py 的离线回归用例（不联网）。

用假客户端驱动：钉住「每关键词单独调一次」「id 去重」「batch 不超过 10 张」
「失败不当无命中」这些机械口径。
退出码 0 = 全部通过。
"""
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FC = _load("flomo_client", "flomo_client.py")
DD = _load("dedup", "dedup.py")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


def card(mid, concept, words=100):
    return {
        "id": mid,
        "word_count": words,
        "tags": ["AI/测试"],
        "content": f"#AI/测试\n{concept}\n\n正文内容",
    }


class FakeClient:
    """记录调用参数，按 name 返回预置结果；可注入失败。"""

    def __init__(self, search_map=None, batch=None, recommended=None, fail_on=None):
        self.calls = []
        self.search_map = search_map or {}
        self.batch = batch or {}
        self.recommended = recommended or []
        self.fail_on = fail_on

    def init(self):
        pass

    def tool(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        if self.fail_on and name == self.fail_on:
            raise FC.FlomoError("boom")
        if name == "memo_search":
            key = args.get("keywords") or ("tag:" + args.get("tag", ""))
            return {"structuredContent": {"memos": self.search_map.get(key, [])}}
        if name == "memo_batch_get":
            ids = args.get("ids") or []
            return {"structuredContent": {"memos": [self.batch[i] for i in ids if i in self.batch]}}
        if name == "memo_recommended":
            return {"structuredContent": {"memos": self.recommended}}
        raise AssertionError(f"未预置的工具调用：{name}")


def run(argv, client):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = DD.main(argv, client=client)
    return code, out.getvalue(), err.getvalue()


def test_per_keyword_calls():
    client = FakeClient(
        search_map={"甲": [card("A", "概念甲")], "乙": [card("B", "概念乙")]},
        batch={"A": card("A", "概念甲"), "B": card("B", "概念乙")},
    )
    code, out, _err = run(["--keywords", "甲,乙", "--tag", "AI/测试", "--limit", "3"], client)
    searches = [c for c in client.calls if c[0] == "memo_search"]
    check("每关键词 + 标签各单独调用一次", len(searches) == 3, len(searches))
    check("关键词参数逐次传入",
          [c[1].get("keywords") for c in searches[:2]] == ["甲", "乙"],
          searches)
    check("标签路单独调用", searches[2][1].get("tag") == "AI/测试", searches[2])
    check("逐次打印每次返回", "关键词 1/3：甲" in out and "标签 3/3：AI/测试" in out, out[:200])
    check("退出码 0", code == 0, code)


def test_id_dedup_and_batching():
    ids = [f"ID{i:02d}" for i in range(12)]
    client = FakeClient(
        search_map={"甲": [card(i, f"概念{i}") for i in ids]},
        batch={i: card(i, f"概念{i}") for i in ids},
    )
    code, out, _err = run(["--keywords", "甲", "--max-full", "20"], client)
    batches = [c for c in client.calls if c[0] == "memo_batch_get"]
    check("12 张卡分两批取全文（单批 ≤10）",
          [len(b[1]["ids"]) for b in batches] == [10, 2],
          [len(b[1]["ids"]) for b in batches])
    check("去重后汇总 12 张", "[汇总] 唯一卡 12 张" in out, out[-200:])
    check("退出码 0（批处理路）", code == 0, code)


def test_default_full_cap_is_explicit():
    """默认 --max-full=8：超出部分不静默丢，必须列出 id 并给出 --ids 出口。"""
    ids = [f"ID{i:02d}" for i in range(12)]
    client = FakeClient(
        search_map={"甲": [card(i, f"概念{i}") for i in ids]},
        batch={i: card(i, f"概念{i}") for i in ids},
    )
    code, out, _err = run(["--keywords", "甲"], client)
    batches = [c for c in client.calls if c[0] == "memo_batch_get"]
    check("默认上限 8 张", sum(len(b[1]["ids"]) for b in batches) == 8,
          [len(b[1]["ids"]) for b in batches])
    check("超限有显式告警", "[告警] 超出 --max-full=8" in out, out[-400:])
    check("未读的 id 逐个列出", "ID08" in out and "ID11" in out, out[-400:])
    check("给出 --ids 出口", "--ids" in out, out[-400:])
    check("汇总仍报全量 12 张（不把未读当不存在）",
          "[汇总] 唯一卡 12 张" in out, out[-200:])
    check("退出码 0", code == 0, code)


def test_explicit_ids():
    """--ids 点名读：不受 --max-full 与 --no-full 影响。"""
    client = FakeClient(batch={"A": card("A", "概念甲"), "B": card("B", "概念乙")})
    code, out, _err = run(["--ids", "A,B", "--no-full", "--max-full", "1"], client)
    batches = [c for c in client.calls if c[0] == "memo_batch_get"]
    check("--ids 一次取回点名的全部卡",
          len(batches) == 1 and batches[0][1]["ids"] == ["A", "B"], batches)
    check("--ids 输出全文（不受 --no-full 影响）", "正文内容" in out, out[:200])
    check("--ids 计入汇总", "[汇总] 唯一卡 2 张" in out, out[-200:])
    check("退出码 0", code == 0, code)


def test_id_and_keywords_together():
    """--id 与 --keywords 同给时两路都跑：此前 --id 会静默吞掉关键词路。"""
    client = FakeClient(
        recommended=[card("R1", "邻卡一")],
        search_map={"甲": [card("A", "概念甲")]},
        batch={"R1": card("R1", "邻卡一"), "A": card("A", "概念甲")},
    )
    code, out, _err = run(["--id", "SELF", "--keywords", "甲"], client)
    names = [c[0] for c in client.calls]
    check("两路都调用", "memo_recommended" in names and "memo_search" in names, names)
    check("两路结果都取全文",
          ("邻卡一" in out) and ("概念甲" in out), out[:400])
    check("退出码 0", code == 0, code)


def test_dedup_across_keywords():
    same = card("SAME", "同一张卡")
    client = FakeClient(
        search_map={"甲": [same], "乙": [same]},
        batch={"SAME": same},
    )
    code, out, _err = run(["--keywords", "甲,乙"], client)
    batches = [c for c in client.calls if c[0] == "memo_batch_get"]
    check("跨关键词同 id 只取一次全文",
          sum(len(b[1]["ids"]) for b in batches) == 1, batches)
    check("汇总为 1 张", "[汇总] 唯一卡 1 张" in out, out[-120:])


def test_no_full():
    client = FakeClient(search_map={"甲": [card("A", "概念甲")]})
    code, out, _err = run(["--keywords", "甲", "--no-full"], client)
    check("--no-full 不调 batch_get",
          all(c[0] != "memo_batch_get" for c in client.calls), client.calls)
    check("--no-full 显式声明未取正文", "未取正文" in out, out[-120:])


def test_failure_not_empty():
    client = FakeClient(fail_on="memo_search")
    code, out, err = run(["--keywords", "甲"], client)
    check("云端失败退出码非 0", code == 1, code)
    check("失败明确区别于无命中",
          "不等于无命中" in err and "无命中" not in out, (out, err))


def test_recommended_mode():
    client = FakeClient(
        recommended=[card("R1", "邻卡一")],
        batch={"R1": card("R1", "邻卡一")},
    )
    code, out, _err = run(["--id", "SELF", "--limit", "5"], client)
    rec = [c for c in client.calls if c[0] == "memo_recommended"]
    check("复盘路按本卡 id 定向推荐",
          len(rec) == 1 and rec[0][1] == {"id": "SELF", "limit": 5}, rec)
    check("复盘路输出命中", "邻卡一" in out, out[:200])
    check("复盘路退出码 0", code == 0, code)


def test_bad_numeric_args():
    """`--limit abc` / `--max-full -1` 该给一行提示，而不是 traceback。

    此前两处直接写 `int(args[i + 1])`，用户打错一个字就得到一段调用栈；更糟的是
    `--max-full -1` 会被 `ids[:-1]` 静默变成「少读一张」。现在解析在 flomo_client，
    越界与非数字都抛 `UsageError`，由入口按本脚本的用法码退出。
    """
    for argv in (["--keywords", "x", "--limit", "abc"],
                 ["--keywords", "x", "--limit", "0"],
                 ["--keywords", "x", "--max-full", "-1"]):
        client = FakeClient()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = DD.cli_entry(lambda: DD.main(argv, client=client),
                                errors=(FC.FlomoError,), usage_code=1)
        label = f"{argv[2]} {argv[3]}"
        check(f"{label}：退出码 1", code == 1, code)
        check(f"{label}：一行提示、不冒 traceback",
              err.getvalue().startswith("[失败] ") and "Traceback" not in err.getvalue(),
              err.getvalue())
        check(f"{label}：未发起任何云端调用", client.calls == [], client.calls)


def test_bad_option_syntax():
    """缺值 / 重复 / 未知三类各有说法，且都不发起云端调用。

    旧循环把 `--ids`（落在末尾，漏了值）报成「未知参数：--ids」——用户会去翻文档确认
    选项名，实际只是漏了值；而 `--ids A --ids B` 只读 B 且不吭声，「以为核过两张」正是
    查重最该防的事。
    """
    cases = [
        (["--ids"], "缺少参数值", "缺值"),
        (["--keywords", "x", "--ids", "A", "--ids", "B"], "只能给一次", "重复 --ids"),
        (["--keywords", "x", "--bogus"], "未知参数：--bogus", "未知选项"),
        (["--keywords", "x", "AI/Agent"], "多余的参数", "值写在选项前面"),
    ]
    for argv, want, label in cases:
        client = FakeClient()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = DD.cli_entry(lambda: DD.main(argv, client=client),
                                errors=(FC.FlomoError,), usage_code=1)
        msg = err.getvalue()
        check(f"{label}：退出码 1", code == 1, code)
        check(f"{label}：提示含「{want}」", want in msg, msg)
        check(f"{label}：未发起任何云端调用", client.calls == [], client.calls)


if __name__ == "__main__":
    for fn in (test_per_keyword_calls, test_id_dedup_and_batching,
               test_default_full_cap_is_explicit, test_explicit_ids,
               test_id_and_keywords_together, test_dedup_across_keywords,
               test_no_full, test_failure_not_empty, test_recommended_mode,
               test_bad_numeric_args, test_bad_option_syntax):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
