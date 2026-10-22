#!/usr/bin/env python3
"""tagcheck.py 的离线回归用例（不联网）。"""

COVERS = ("tagcheck", "sop_gate",)

import contextlib
import importlib.util
import json
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


TC = _load("tagcheck", "tagcheck.py")
from cli_util import cli_entry as CLI_ENTRY  # noqa: E402  （SCRIPT_DIR 已在 sys.path）
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


def run_cli(argv, client):
    """走真实入口：`main` 抛的 `UsageError` 由 `cli_entry` 渲染成一行提示 + 退出码。

    参数写错时**不冒 traceback** 是这条路的判据，所以必须经入口跑，不能直接调 `main`。
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = CLI_ENTRY(lambda: TC.main(argv, client=client), errors=(TC.FlomoError,))
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


def test_missing_tag_leaf_only_tree():
    # 真实 tag_tree 只返回叶子「顶层/二级」，顶层不会单独成条。
    # 用例夹具若带裸顶层（如上 TAGS 里的 "AI"），这条误判就永远测不出来。
    leaf_only = ["AI/Agent", "AI/AgentHarness", "科技/测试"]
    code, out, _err = run(["--tags", "AI/不存在"], FakeClient(leaf_only))
    check("只有叶子标签时：缺失二级不误报顶层缺失", "该顶层存在" in out, out)
    check("只有叶子标签时：不提示连同顶层一起新建", "连同顶层一起新建" not in out, out)
    check("只有叶子标签时：退出码仍为 2", code == 2, code)


def test_three_levels():
    code, out, _err = run(["--tags", "AI/Agent/子级"], FakeClient(TAGS))
    check("三级标签：退出码 2", code == 2, code)
    check("三级标签：指出 H17", "严格两级" in out, out)


class ContentOnlyClient(FakeClient):
    """回包只在 `content[].text` 里（协议层允许，云端可能这样回）。

    此前 `load_tree` 只认 `structuredContent`，同一份数据它报「回包缺 total 字段」，
    而 `sop_gate` 的逐字段兜底能读出来——同一个回包两个结论，还错怪了标签树。
    """

    def tool(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        inner = json.dumps({"tags": self.tags, "total": self.total}, ensure_ascii=False)
        return {"content": [{"type": "text", "text": inner}]}


def test_content_only_shape():
    client = ContentOnlyClient(TAGS)
    code, out, err = run(["--tags", "AI/Agent,科技/测试"], client)
    check("content[].text 形态：退出码 0（不再报缺 total）", code == 0, err)
    check("content[].text 形态：判定与 structuredContent 形态一致", out.count("✓") == 2, out)

    # 跨层对齐：同一个回包，tagcheck 与 sop_gate 必须给出同一个 total
    gate = _load("sop_gate", "sop_gate.py")
    # 注意两侧返回顺序不同：tagcheck 是 (tags, total)，sop_gate 是 (total, tags)
    t_tags, t_total = TC.load_tree(ContentOnlyClient(TAGS), 2000)
    g_total, g_tags = gate._tag_tree_total_and_count(ContentOnlyClient(TAGS))
    check("同一回包：tagcheck 与 sop_gate 的 total 一致",
          t_total == g_total == len(TAGS), (t_total, g_total))
    check("同一回包：两侧标签集合一致", list(t_tags) == list(g_tags) or sorted(t_tags) == sorted(g_tags))

    # 解析得出、但字段缺失（真截断）仍必须阻塞：宽容只给「回包形态」，不给「数据不全」
    code, _out, err = run(["--tags", "AI/Agent"], FakeClient(TAGS, total=5))
    check("字段缺失仍判截断（宽容不放行「数据不全」）", code == 1 and "截断" in err, err)

    # 完全读不到：必须抛错，不能当成「标签树是空的」继续走
    class DeadClient:
        def tool(self, name, args=None):
            return {"isError": True, "content": [{"type": "text", "text": "token expired"}]}

    try:
        TC.load_tree(DeadClient(), 2000)
        raised = ""
    except Exception as e:  # noqa: BLE001 — 只断言抛了且说清原因
        raised = str(e)
    check("工具级错误：抛错并带出错误正文", "token expired" in raised, raised)


def test_truncated_tree():
    code, _out, err = run(["--tags", "AI/Agent"], FakeClient(TAGS, total=5))
    check("标签树截断：退出码 1", code == 1, code)
    check("标签树截断：结论不可信", "截断" in err, err)


def test_hash_prefix_and_split():
    code, out, _err = run(["--tags", "#AI/Agent，科技/测试"], FakeClient(TAGS))
    check("去 # 前缀与全角逗号分隔", code == 0 and out.count("✓") == 2, out)


def test_grep_lists_matches():
    code, out, _err = run(["--grep", "Agent|测试"], FakeClient(TAGS))
    check("--grep 命中：退出码 0", code == 0, code)
    check("--grep 列出匹配标签", "AI/AgentHarness" in out and "科技/测试" in out, out)
    check("--grep 明确标为匹配清单", "[匹配]" in out, out)


def test_grep_no_hit():
    code, out, _err = run(["--grep", "根本不存在的词"], FakeClient(TAGS))
    check("--grep 无命中：退出码 0（搜不到不是失败）", code == 0, code)
    check("--grep 无命中：提示按 H18 判，不据搜不到就建簇", "H18" in out, out)


def test_grep_with_tags():
    code, out, _err = run(["--grep", "AI", "--tags", "AI/不存在"], FakeClient(TAGS))
    check("--grep 与 --tags 并存：两段都输出", "[匹配]" in out and "不存在" in out, out)
    check("--grep 与 --tags 并存：退出码仍按标签判定", code == 2, code)


def test_written_differently():
    # 判据与第 ⑧ 步闸门同一份（memo_util.tag_key）。此前这里没有这一项，
    # `AI / Agent` 被判成「不存在，可直接新建此二级」——定标签的工具亲手把人
    # 引向新建近义簇，只在写前闸门才被拦下。三档都属「确定是错」，一律拦。
    for label, tag in [("斜杠两侧空格", "AI / Agent"), ("全角斜杠", "AI／Agent"),
                       ("大小写", "ai/agent")]:
        code, out, _err = run(["--tags", tag], FakeClient(TAGS))
        check(f"写法重复（{label}）：退出码 2", code == 2, code)
        check(f"写法重复（{label}）：点出树里的写法",
              "与现有「AI/Agent」" in out and "只差空白" in out, out)
        check(f"写法重复（{label}）：不再建议新建", "可直接新建" not in out, out)


def test_fullwidth_slash_not_depth_error():
    # 全角斜杠会让层级判定失败。先判写法重复再判层级，否则报「层级不合规」
    # 会把人引向「改格式」，而真正该做的是改回树里那个写法。
    _code, out, _err = run(["--tags", "AI／Agent"], FakeClient(TAGS))
    check("全角斜杠：报写法重复而非层级不合规", "层级不合规" not in out, out)


def test_case_only_top_exists():
    # 顶层名大小写不同、二级是新的：顶层存在性必须按键比，逐字比会报
    # 「该顶层也不存在，须连同顶层一起新建」——把已有的顶层重开一遍。
    _code, out, _err = run(["--tags", "ai/新二级"], FakeClient(TAGS))
    check("大小写不同的顶层：仍判为该顶层存在", "该顶层存在" in out, out)
    check("大小写不同的顶层：不提示连同顶层一起新建",
          "连同顶层一起新建" not in out, out)


def test_bad_limit():
    for raw in ("abc", "0", "-3", "2.5"):
        code, _out, err = run_cli(["--tags", "AI/Agent", "--limit", raw], FakeClient(TAGS))
        check(f"--limit {raw}：退出码 2", code == 2, code)
        check(f"--limit {raw}：一行提示、不冒 traceback",
              err.startswith("[失败] --limit") and "Traceback" not in err, err)


def test_duplicate_flag():
    # 重复给同一选项时后者会静默覆盖前者（只核了最后一个），而漏核正是本脚本要防的。
    # 参数错误一律由入口渲染（`main` 不再自己打印），故这条路要走 run_cli。
    code, _out, err = run_cli(["--tags", "AI/Agent", "--tags", "科技/测试"], FakeClient(TAGS))
    check("重复 --tags：退出码 2", code == 2, code)
    check("重复 --tags：点明只能给一次", "只能给一次" in err, err)


def test_empty_grep():
    # 空正则会命中全部标签，打出的「命中 N 个」不构成任何证据。
    code, _out, err = run_cli(["--grep", ""], FakeClient(TAGS))
    check("空 --grep：退出码 2", code == 2, code)
    check("空 --grep：说明空正则命中全部", "空正则" in err, err)


def test_missing_value_and_unknown():
    # `--tags` 落在末尾时，旧实现报「未知参数：--tags」——用户以为选项名写错、去翻文档，
    # 而事实只是漏了值。两种错误的修法不同，不能共用一句提示。
    for argv, want in [(["--tags"], "缺少参数值"),
                       (["--tags", "--grep", "x"], "缺少参数值"),
                       (["--tags", "AI/Agent", "--bogus"], "未知参数"),
                       (["AI/Agent"], "多余的参数")]:
        code, _out, err = run_cli(argv, FakeClient(TAGS))
        check(f"用法错（{' '.join(argv)}）：退出码 2", code == 2, code)
        check(f"用法错（{' '.join(argv)}）：提示含「{want}」", want in err, err)


if __name__ == "__main__":
    for fn in (test_existing_ok, test_missing_tag, test_missing_tag_leaf_only_tree,
               test_three_levels, test_truncated_tree, test_hash_prefix_and_split,
               test_content_only_shape, test_grep_lists_matches, test_grep_no_hit, test_grep_with_tags,
               test_written_differently, test_fullwidth_slash_not_depth_error,
               test_case_only_top_exists, test_bad_limit, test_duplicate_flag,
               test_empty_grep, test_missing_value_and_unknown):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
