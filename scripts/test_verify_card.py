#!/usr/bin/env python3
"""test_verify_card.py — `verify_card.py` 的离线用例（假 client，不联网）。

覆盖的是**判据本身**：成功卡要全过，每一类缺陷都要能让对应项转红。只有正向用例
时，「全过」也可能是因为检查根本没生效。

用法：python scripts/test_verify_card.py     退出码 0=全过，1=有失败。
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_card as VC  # noqa: E402

_RESULTS = []

GOOD = (
    "#科技/安全\n"
    "持有型会话凭证\n"
    "\n"
    "要点：\n"
    "- 公开标识只做定位，私有 token 才授权。\n"
    "- 服务端只存摘要，比对走常量时间。\n"
)


def check(name, ok, detail=""):
    _RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


class FakeClient:
    """只实现 `tool()`：返回一张卡，字段与云端回包同构。"""

    def __init__(self, memo):
        self.memo = memo
        self.calls = []

    def tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"structuredContent": {"memos": [self.memo]}}


def memo(content, *, wc=None, truncated=False, tags=None):
    return {
        "id": "TESTID",
        "content": content,
        "word_count": len(content) if wc is None else wc,
        "content_truncated": truncated,
        "tags": tags if tags is not None else ["科技/安全"],
    }


def by_name(checks):
    return {n: (ok, d) for n, ok, d in checks}


def run_cases():
    # 1) 正常卡：全部机械项通过
    checks, info = VC.run("TESTID", FakeClient(memo(GOOD)))
    m = by_name(checks)
    check("正常卡全过", all(ok for ok, _ in m.values()), str([k for k, v in m.items() if not v[0]]))
    check("元信息带标签与行数", info["tags"] == ["科技/安全"] and info["lines"] >= 5, str(info["lines"]))
    check("末行取到正文最后一行", info["tail"].startswith("- 服务端只存摘要"), info["tail"][:40])

    # 2) 截断：字数与结构都不可信，必须判失败
    checks, _ = VC.run("TESTID", FakeClient(memo(GOOD, truncated=True)))
    ok, detail = by_name(checks)["回读完整（未被截断）"]
    check("回读被截断时判失败", not ok, detail)
    check("截断时给出处置提示", "memo_batch_get" in detail, detail)

    # 3) 云端回读形态：渲染层在标签段后补一个空行，概念名落到第三行。
    #    这不是「缺概念名」——真机上每张合规卡都是这个形态，判错等于全部误报。
    cloud_style = "#科技/安全\n\n持有型会话凭证\n\n要点：\n- 内容。\n"
    checks, info = VC.run("TESTID", FakeClient(memo(cloud_style)))
    check("云端补空行形态不算缺概念名",
          by_name(checks)["第二行是概念名"][0] and info["second"] == "持有型会话凭证",
          repr(info["second"]))
    check("补空行被标记为渲染层还原", info["render_padded"] is True, str(info["render_padded"]))
    check("补空行不触发「第二行不应为空」的假提示",
          not any("第二行" in w for w in info["warnings"]), str(info["warnings"])[:150])

    # 4) 缺第二行概念名（早期存量卡的典型形态）
    no_concept = "#科技/安全\n\n要点：\n- 只有正文。\n"
    checks, _ = VC.run("TESTID", FakeClient(memo(no_concept)))
    check("缺概念名判失败", not by_name(checks)["第二行是概念名"][0])

    # 5) 第二行直接写结构标签（等价于缺概念名）
    label_second = "#科技/安全\n要点：\n- 直接进正文。\n"
    checks, _ = VC.run("TESTID", FakeClient(memo(label_second)))
    check("第二行是结构标签判失败", not by_name(checks)["第二行是概念名"][0])

    # 6) 公式占位符残留：抽取坏的唯一可观测特征
    broken = GOOD + "- 界为 〈M12〉 与 〖M7〗。\n"
    checks, _ = VC.run("TESTID", FakeClient(memo(broken)))
    ok, detail = by_name(checks)["无内容占位符残留（抽取未丢正文）"]
    check("占位符残留判失败", not ok, detail)

    # 7) 形态违规（加粗）由复用的 validate_memo 拦下——证明两边同源
    bold = GOOD + "- 这里**加粗**了。\n"
    checks, _ = VC.run("TESTID", FakeClient(memo(bold)))
    ok, detail = by_name(checks)["形态检查（复用 validate_memo）"]
    check("加粗等形态违规判失败（与写前校验同源）", not ok, detail)

    # 8) 首行没有标签段
    checks, _ = VC.run("TESTID", FakeClient(memo("没有标签的卡\n概念\n\n要点：\n- x\n")))
    check("无标签段判失败", not by_name(checks)["首行是完整标签段"][0])

    # 9) 回包为空 → run 抛异常，main 转成退出码 1（不能静默当通过）
    class Empty(FakeClient):
        def tool(self, name, arguments=None):
            return {"structuredContent": {"memos": []}}

    raised = False
    try:
        VC.run("TESTID", Empty(memo(GOOD)))
    except RuntimeError:
        raised = True
    check("空回包抛错而非静默通过", raised)

    # 10) 默认构造必须解包 `(token, 来源)`——load_token() 真实返回是元组。
    #    这条是被真机回读打出来的：直接把元组塞进构造器会报
    #    `can only concatenate str (not "tuple") to str`，而「整个替换掉客户端」的
    #    用例根本走不到这段代码。
    class RecordingClient:
        def __init__(self, token):
            assert isinstance(token, str), f"token 应为 str，实际 {type(token).__name__}"
            self.token = token

        def tool(self, name, arguments=None):
            return {"structuredContent": {"memos": [memo(GOOD)]}}

    orig_ctor, orig_lt = VC.FC.FlomoClient, VC.FC.load_token
    try:
        VC.FC.FlomoClient = RecordingClient
        VC.FC.load_token = lambda: ("faketoken", "测试来源")
        try:
            c = VC._default_client()
            check("默认构造解包 (token, 来源) 后传入字符串", c.token == "faketoken", repr(c.token))
        except AssertionError as e:
            check("默认构造解包 (token, 来源) 后传入字符串", False, str(e))
    finally:
        VC.FC.FlomoClient, VC.FC.load_token = orig_ctor, orig_lt

    # 11) --json 输出可被自动化消费，且如实带出「须人读」项
    orig_default = VC._default_client
    buf = io.StringIO()
    try:
        VC._default_client = lambda: FakeClient(memo(GOOD))
        with redirect_stdout(buf):
            rc = VC.main(["--id", "TESTID", "--json"])
    finally:
        VC._default_client = orig_default
    payload = json.loads(buf.getvalue())
    check("--json 可解析且退出码 0", rc == 0 and isinstance(payload, dict), buf.getvalue()[:120])
    check("--json 带出 check 结论", payload.get("ok") is True and len(payload.get("checks") or []) >= 5)
    check("--json 标注人工判断项（不虚报覆盖）",
          len(payload.get("human_judgment") or []) == 2,
          str(payload.get("human_judgment")))

    # 12) 失败时退出码必须非 0（成功判据不能只是「打印了什么」）
    buf2 = io.StringIO()
    try:
        VC._default_client = lambda: FakeClient(memo(broken))
        with redirect_stdout(buf2):
            rc2 = VC.main(["--id", "TESTID"])
    finally:
        VC._default_client = orig_default
    check("机械项失败时退出码为 1", rc2 == 1, str(rc2))

    # 13) 字符对账：H11 ④ 的「实测字数」要能和本地草稿对得上。
    #     云端回读天然比草稿多两类字符——渲染层补的空行、存储层对 Markdown 元字符
    #     做的反斜杠转义。若直接报原始长度，差额会随卡里元字符多少而变（卡 34 差 7），
    #     每张卡都要人当场算账；报「还原转义后」的长度才能一眼对账。
    draft = ("#科技/安全\n"
             "持有型会话凭证\n"
             "\n"
             "要点：\n"
             "- 稠密系统 [Z][I]=[V] 与下标 a_i 一并入正文。\n")
    stored_body = draft.replace("[", "\\[").replace("]", "\\]").replace("_", "\\_")
    stored = stored_body.replace("#科技/安全\n", "#科技/安全\n\n", 1)   # 渲染层补的空行
    checks, info = VC.run("TESTID", FakeClient(memo(stored)))
    check("字符数按「还原转义后」口径，可与草稿逐字对账",
          info["chars"] == len(draft), f"{info['chars']} vs 草稿 {len(draft)}")
    check("原始字符数仍如实保留（不被抹掉）",
          info["chars_raw"] == len(stored), f"{info.get('chars_raw')} vs 回读 {len(stored)}")
    check("差额来源分别计数（渲染补行 / 存储转义）",
          info["render_pad"] == 1 and info["escapes"] == 7,
          f"pad={info.get('render_pad')} escapes={info.get('escapes')}")
    # 反向：数字对得上就必须真对得上——草稿与回读差两个字符，口径立即可见差异
    checks2, info2 = VC.run("TESTID", FakeClient(memo(stored.replace("正文", "正文内容"))))
    check("草稿与回读真差两字时字符数随之变化",
          info2["chars"] == len(draft) + 2, f"{info2['chars']} vs 草稿 {len(draft)}")


def main() -> int:
    run_cases()
    print("---")
    ok = all(_RESULTS)
    print(f"{'全部通过' if ok else '存在失败'}（{len(_RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
