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
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flomo_client as FC  # noqa: E402
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

    # 13) 与本地草稿的逐字比对：口径必须与写路径 readback_check 同源
    #     （FC._norm_body：丢空行 / 还原转义 / 去行内水平空白）。假 client 要按
    #     真实平台行为构造回读，共四层差异：渲染层在标签段后与「要点：」后补空行、
    #     全角标点与行内代码之间插空格、存储层给 Markdown 元字符加反斜杠、草稿文件
    #     末尾换行被修掉。
    #     旧口径只还原了标签段后那一处，于是「要点：后无空行」的草稿（卡 37 形态）
    #     报出的「正文 + 渲染补行」分解是错的，数字还靠「补行 +1」与「末换行 −1」
    #     相互抵消蒙对——看起来对上了，其实一层都没核。
    with tempfile.TemporaryDirectory(prefix="vc_test_") as td:
        draft = ("#科技/安全\n"
                 "持有型会话凭证\n"
                 "\n"
                 "要点：\n"
                 "- 稠密系统 [Z][I]=[V] 与下标 a_i 一并入正文。\n")
        stored = (draft
                  .replace("[", "\\[").replace("]", "\\]").replace("_", "\\_")
                  .replace("#科技/安全\n", "#科技/安全\n\n", 1)   # 渲染层：标签段后补行
                  .replace("要点：\n", "要点：\n\n", 1)           # 渲染层：引导行后补行
                  .rstrip("\n"))                                  # 存储层：修掉末尾换行
        draft_path = Path(td) / "draft_tight.txt"
        draft_path.write_text(draft, encoding="utf-8")
        checks, info = VC.run("TESTID", FakeClient(memo(stored)), draft_path=str(draft_path))
        ok, detail = by_name(checks)["与本地草稿逐字比对（同写路径口径）"]
        check("要点后无空行的草稿：四层平台差异下仍逐字比对通过", ok, detail)
        check("差额分解四类都有出处（转义 7 处 / 空行 2 / 行内空白两侧同去）",
              info["escapes"] == 7 and info["blanks"] == 2 and info["spaces"] == 5,
              f"esc={info['escapes']} blanks={info['blanks']} spaces={info['spaces']}")
        # blanks=2 = 草稿自带的空行 1（概念名与「要点：」之间）+ 渲染层在「要点：」
        # 后补的 1；标签段后那一处已在结构还原时删掉。spaces=5 是草稿自带的中英混排
        # 空格（`- 稠密系统 [Z][I]=[V] 与下标 a_i 一并入正文。`），两侧同样去除，
        # 不是平台插入的——标注为「口径去除」而非「平台差异」。
        check("净字符与草稿同口径相等",
              info["net"] == len(FC._norm_body(draft)),
              f"{info['net']} vs {len(FC._norm_body(draft))}")
        # 反向一：真差一个字，比对必须失败（判据要能当场证伪）
        checks2, _ = VC.run("TESTID", FakeClient(memo(stored.replace("正文", "正文内容"))),
                            draft_path=str(draft_path))
        check("云端与草稿真差一字时比对判失败",
              not by_name(checks2)["与本地草稿逐字比对（同写路径口径）"][0])
        # 反向二：草稿「要点：」后本来就有空行（卡 38 形态），同样要通过——
        #     口径不能反过来把草稿自己的空行当差异
        draft2 = draft.replace("要点：\n", "要点：\n\n", 1)
        draft2_path = Path(td) / "draft_loose.txt"
        draft2_path.write_text(draft2, encoding="utf-8")
        stored2 = (draft2
                   .replace("[", "\\[").replace("]", "\\]").replace("_", "\\_")
                   .replace("#科技/安全\n", "#科技/安全\n\n", 1)
                   .rstrip("\n"))
        checks3, _ = VC.run("TESTID", FakeClient(memo(stored2)), draft_path=str(draft2_path))
        check("要点后有空行的草稿同样通过（不把草稿自有的空行判成差异）",
              by_name(checks3)["与本地草稿逐字比对（同写路径口径）"][0])
        # 反向三：draft 不一致时 main 退出码为 1
        buf3 = io.StringIO()
        try:
            VC._default_client = lambda: FakeClient(memo(stored.replace("正文", "正文内容")))
            with redirect_stdout(buf3):
                rc3 = VC.main(["--id", "TESTID", "--draft", str(draft_path)])
        finally:
            VC._default_client = orig_default
        check("草稿比对不过时退出码为 1", rc3 == 1, str(rc3))


def main() -> int:
    run_cases()
    print("---")
    ok = all(_RESULTS)
    print(f"{'全部通过' if ok else '存在失败'}（{len(_RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
