#!/usr/bin/env python3
"""flomo_client.py 写后回读验收（H11）的离线回归用例。

全部用例**不联网**：`readback_check` 通过注入假 client 桩验证，
只测「比对与判定」这套机械逻辑本身。

退出码 0 = 全部通过。
"""
import importlib.util
import json
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

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


class FakeClient:
    """桩客户端：只实现回读用到的 memo_batch_get，返回可注入的响应。"""

    def __init__(self, memos=None, raises=None):
        self.calls = []
        self._memos = memos if memos is not None else []
        self._raises = raises

    def tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self._raises is not None:
            raise self._raises
        return {"content": [{"type": "text",
                             "text": json.dumps({"memos": self._memos}, ensure_ascii=False)}],
                "structuredContent": {"memos": self._memos}}


BODY = "#科技/机器人\n某测试概念名\n\n结论句。\n\n要点：\n- 要点一\n"


def run_norm_cases():
    """_norm_body 只忽略空白差异，非空白字符与行序一律不宽容。"""
    check("_norm_body 空行不参与比对（连续空行与首尾空行一并不计）",
          FC._norm_body("A  \n\n\nB\n") == "A\nB",
          repr(FC._norm_body("A  \n\n\nB\n")))
    check("_norm_body 统一 CRLF",
          FC._norm_body("A\r\nB") == FC._norm_body("A\nB"))
    check("_norm_body 无空行与有空行等价（云端会自行补空行）",
          FC._norm_body("A\nB") == FC._norm_body("A\n\nB"),
          repr(FC._norm_body("A\n\nB")))
    check("_norm_body 统一行尾空格",
          FC._norm_body("A  \nB\t") == FC._norm_body("A\nB"))
    check("_norm_body 合并成一行必须暴露（行序/行界变化是内容差异）",
          FC._norm_body("A\nB") != FC._norm_body("AB"),
          repr(FC._norm_body("AB")))
    check("_norm_body 不改动非空白字符（错别字必须暴露）",
          FC._norm_body("续航 430 公里") != FC._norm_body("续航 450 公里"))
    check("_norm_body 行内水平空白不参与比对（云端在全角标点后插空格）",
          FC._norm_body("定义入口：`web` 监听 80")
          == FC._norm_body("定义入口： `web` 监听 80"),
          repr(FC._norm_body("定义入口： `web` 监听 80")))
    check("_norm_body 行内空白差异不得掩盖非空白差异",
          FC._norm_body("容器端口 3000") != FC._norm_body("容器端口 3001"))

    # Markdown 元字符转义：flomo 存储层存的是转义后的文本（与 H6 同一事实）。
    # 少了这条还原，任何含下标的卡（数学公式最典型）回读验收 100% 误报失败。
    check("_norm_body 下划线转义不影响判定（数学卡含下标是常态）",
          FC._norm_body("τ_r^a 收敛到 T")
          == FC._norm_body("τ\\_r^a 收敛到 T"),
          repr(FC._norm_body("τ\\_r^a 收敛到 T")))
    check("_norm_body 竖线与方括号转义不影响判定",
          FC._norm_body("|〈a − b〉| ≤ C") == FC._norm_body("\\|〈a − b〉\\| ≤ C"),
          repr(FC._norm_body("\\|〈a − b〉\\| ≤ C")))
    check("_norm_body 反斜杠自身被转义不影响判定",
          FC._norm_body("∫(M\\E) T") == FC._norm_body("∫(M\\\\E) T"),
          repr(FC._norm_body("∫(M\\\\E) T")))
    check("_norm_body 转义宽容不得掩盖公式内容被改（下标数字变了必须暴露）",
          FC._norm_body("(1 + |k|)^(-6)") != FC._norm_body("(1 + |k|)^(-8)"),
          repr(FC._norm_body("(1 + |k|)^(-6)")))
    check("_norm_body 转义宽容不得掩盖非元字符被改",
          FC._norm_body("λ₀(P_k) ≥ c") != FC._norm_body("λ₀(P_k) ≥ d"))


def run_readback_cases():
    """readback_check 的五类判定。"""
    # 1) 一致（含空白差异）→ ok，且带实测字数
    c = FakeClient([{"id": "M1", "content": BODY.replace("\n", "\r\n") + "\n\n"}])
    ok, detail = FC.readback_check(c, "M1", BODY)
    check("回读一致（仅空白差异）判过", ok and "回读一致" in detail, detail)
    check("回读按 id 拉全文（调 memo_batch_get）",
          c.calls == [("memo_batch_get", {"ids": ["M1"]})], str(c.calls))

    # 1b) 云端在标签段后与列表引导行后自行补空行 → 仍判过（否则每张新卡都误报失败）
    cloud_style = BODY.replace("#科技/机器人\n", "#科技/机器人\n\n").replace("要点：\n", "要点：\n\n")
    check("云端补出的空行（标签段后 / 要点后）不影响判定",
          FC.readback_check(FakeClient([{"id": "M1", "content": cloud_style}]), "M1", BODY)[0],
          cloud_style.replace("\n", "\\n"))

    # 1c) 云端在全角标点与行内代码之间补空格 → 仍判过（同属渲染层空白，否则用了行内代码的卡全误报）
    local_code = BODY.replace("要点一", "：`code` 一")
    cloud_code = local_code.replace("：`", "： `")
    check("云端补出的行内空格（全角标点 + 行内代码）不影响判定",
          FC.readback_check(FakeClient([{"id": "M1", "content": cloud_code}]), "M1", local_code)[0],
          cloud_code.replace("\n", "\\n"))
    check("行内空格宽容不得掩盖行内代码内容被改",
          not FC.readback_check(
              FakeClient([{"id": "M1", "content": cloud_code.replace("`code`", "`coda`")}]),
              "M1", local_code)[0])

    # 1d) 云端把 Markdown 元字符存成转义形态→ 仍判过（含下标的数学卡否则全误报失败）
    local_math = BODY.replace("要点一", "τ_r^a 与 ‖T‖_ω")
    cloud_math = local_math.replace("τ_r^a", "τ\\_r^a").replace("‖T‖_ω", "‖T‖\\_ω")
    check("云端转义的下划线不影响判定",
          FC.readback_check(FakeClient([{"id": "M1", "content": cloud_math}]), "M1", local_math)[0],
          cloud_math.replace("\n", "\\n"))
    check("转义宽容不得掩盖下标内容被改",
          not FC.readback_check(
              FakeClient([{"id": "M1", "content": cloud_math.replace("‖T‖\\_ω", "‖T‖\\_0")}]),
              "M1", local_math)[0])

    # 2) 内容不一致 → 判失败，且给出字数与首个差异位置（可定位）
    c = FakeClient([{"id": "M1", "content": BODY.replace("要点一", "要点二")}])
    ok, detail = FC.readback_check(c, "M1", BODY)
    check("回读内容不一致判失败", not ok and "不一致" in detail, detail)
    check("失败信息含字数与首个差异位置",
          "个字符" in detail and "写入" in detail, detail)
    check("失败信息禁止据此重发写请求", "禁止据此重发写请求" in detail, detail)

    # 3) 回读为空（id 对不上库 / 写没落库）→ 判失败
    ok, detail = FC.readback_check(FakeClient([]), "M1", BODY)
    check("回读为空判失败", not ok and "未取到 memo" in detail, detail)

    # 4) 回读返回的是另一张卡 → 判失败
    ok, detail = FC.readback_check(FakeClient([{"id": "M9", "content": BODY}]), "M1", BODY)
    check("回读 id 不符判失败", not ok and "另一张卡" in detail, detail)

    # 5) 正文被云端截断 → 判失败（截断就没法逐字比对，不得当成功）
    ok, detail = FC.readback_check(
        FakeClient([{"id": "M1", "content": BODY, "content_truncated": True}]), "M1", BODY)
    check("回读被截断判失败", not ok and "截断" in detail, detail)

    # 6) 云端调用抛错应向上传播（由 CLI 入口转退出码），不得被吞成「验收通过」
    try:
        FC.readback_check(FakeClient(raises=FC.FlomoError("boom")), "M1", BODY)
        raised = False
    except FC.FlomoError:
        raised = True
    check("回读调用失败不得被吞（抛 FlomoError）", raised)


def run_write_path_cases():
    """写命令内部必须真的调用回读（结构锁定，防被无意摘掉）。"""
    src = (SCRIPT_DIR / "flomo_client.py").read_text(encoding="utf-8")
    m = src[src.index("def main():"):]
    check("写成功分支内调用 readback_check",
          "readback_check(client, created_id" in m, "main() 内未调用")
    check("回读未过时非 0 退出",
          "回读验收未过" in m and "return 1" in m)
    check("写入正文取自本次请求参数（验收对象正确）",
          'arguments.get("content")' in m)


def run_brief_cases():
    """--brief 压缩列表回包，但绝不能把 batch_get 的全文截掉（H1 的后门）。"""
    memos = [
        {"id": "M1", "word_count": 420, "tags": ["科技/虚拟化"],
         "content": "#科技/虚拟化\n沙箱平台\n\n" + "很长的正文。" * 200},
    ]
    payload = {"structuredContent": {"memos": memos}}
    text = FC.brief_text("memo_search", payload)
    check("brief 列出 id / 字数 / 标签 / 概念名",
          "M1" in text and "420 字" in text and "科技/虚拟化" in text and "沙箱平台" in text)
    check("brief 不携带正文长文本（省体积的正是这部分）", "很长的正文。" not in text)

    full = FC.brief_text("memo_batch_get", payload)
    check("batch_get 走全文分支（正文完整保留）", "很长的正文。" * 3 in full)
    check("batch_get 分支标注未截断", "未截断" in full)

    tree = FC.brief_text("tag_tree", {"structuredContent": {"total": 665, "tags": ["A/1", "A/2"]}})
    check("tag_tree 给 total 与 returned", "total=665" in tree and "returned=2" in tree)

    w = FC.brief_text("memo_create", {"structuredContent": {"id": "M2", "word_count": 700,
                                                            "tags": ["文化/影视"]}})
    check("写回包只给 id/字数/标签与载体标记", "id=M2" in w and "link=" in w)

    check("不可压缩形态返回 None（退回完整 JSON）",
          FC.brief_text("whatever", {"structuredContent": {"foo": 1}}) is None)
    # 只解析 content[].text 的回包形态
    alt = {"content": [{"type": "text", "text": json.dumps(
        {"memos": memos}, ensure_ascii=False)}]}
    check("兼容 content[].text 回包形态", "M1" in (FC.brief_text("memo_search", alt) or ""))

    # 结构锁定：main 里 --brief 只影响打印，不得跳过后面的 id 校验与回读验收
    src = (SCRIPT_DIR / "flomo_client.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main():"):]
    check("brief 不短路写后回读验收", "readback_check(client, created_id" in main_src)
    check("brief 通过剥离参数实现，不改工具名解析", 'a != "--brief"' in main_src)


def run_error_contract_cases():
    """失败一律走 `FlomoError`：库层不抛 `SystemExit`，CLI 一律以退出码收场。

    判据取**行为**而不是措辞：`except Exception` 必须接得住，进程不能被异常本身带走。
    旧实现 `raise SystemExit("未找到 token")` 在这里会当场失败——`SystemExit` 继承
    `BaseException`，`except Exception` 接不住，于是 `verify_card --json` 在那条路径上
    既没有 JSON、也没有自己的失败行，只剩解释器打的一句提示。
    """
    import contextlib
    import io
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        orig_root = FC.PROJECT_ROOT
        orig_env = os.environ.pop("FLOMO_TOKEN", None)
        FC.PROJECT_ROOT = Path(d)  # 没有 .mcp.json，等同于没配 token
        try:
            got = ""
            try:
                FC.load_token()
                got = "未抛错"
            except Exception as e:  # noqa: BLE001 — 库调用方的常规写法
                got = type(e).__name__
            except BaseException as e:  # noqa: BLE001 — 逃到这里的正是本次要防的
                got = f"漏出 BaseException：{type(e).__name__}"
            check("无凭据时抛 FlomoError 且可被 except Exception 接住",
                  got == "FlomoError", got)

            # 三个 CLI 的 main：无凭据时必须返回退出码，不许把进程带走
            for name, argv in (("dedup", ["--keywords", "测试"]),
                               ("tagcheck", ["--tags", "AI/RAG"]),
                               ("verify_card", ["--id", "abc"])):
                mod = _load(name, f"{name}.py")
                out, err = io.StringIO(), io.StringIO()
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = mod.main(list(argv))
                    ok, detail = code != 0, f"code={code!r}"
                except BaseException as e:  # noqa: BLE001
                    ok, detail = False, f"抛出 {type(e).__name__}"
                check(f"{name} 无凭据时返回非 0（不抛异常）", ok, detail)
        finally:
            FC.PROJECT_ROOT = orig_root
            if orig_env is not None:
                os.environ["FLOMO_TOKEN"] = orig_env

    # cli_entry：正常返回值不改写，FlomoError 转成指定退出码，输出只有一行
    check("cli_entry 原样返回 main 的退出码", FC.cli_entry(lambda: 0) == 0)

    def boom():
        raise FC.FlomoError("未找到 flomo token")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = FC.cli_entry(boom, code=7, prefix="[失败] 查重未能进行：")
    line = err.getvalue().strip()
    check("cli_entry 把 FlomoError 转成指定退出码", code == 7, str(code))
    check("失败输出为一行，中文冒号后不补空格",
          line == "[失败] 查重未能进行：未找到 flomo token", repr(line))

    # 结构锁定：库层里不许再出现 SystemExit（契约写在模块与类的文档里）
    src = (SCRIPT_DIR / "flomo_client.py").read_text(encoding="utf-8")
    check("库层源码不出现 raise SystemExit", "raise SystemExit" not in src,
          "库层又用 SystemExit 表示失败了")

    # 参数解析：`--limit abc` 此前是裸 int()，用户打错一个字得到一段 traceback
    check("int_arg 正常值原样返回", FC.int_arg("2000", "--limit") == 2000)
    check("int_arg 去首尾空白", FC.int_arg("  12  ", "--limit") == 12)
    bad = []
    for raw, want in [("abc", "需要整数"), ("", "需要整数"), ("2.5", "需要整数"),
                      ("0", "不能小于 1"), ("-3", "不能小于 1"),
                      ("2001", "不能大于 2000")]:
        try:
            FC.int_arg(raw, "--limit", low=1, high=2000)
            bad.append(f"{raw!r} 未拦下")
        except FC.UsageError as e:
            if want not in str(e):
                bad.append(f"{raw!r} -> {e}")
    check("int_arg 非数字/越界一律抛 UsageError 并点明参数", not bad, "; ".join(bad))
    check("float_arg 正常值", FC.float_arg("30.5", "--timeout") == 30.5)
    try:
        FC.float_arg("abc", "--timeout")
        got = "未拦下"
    except FC.UsageError as e:
        got = "ok" if "--timeout" in str(e) else str(e)
    check("float_arg 非数字抛 UsageError 并点明参数", got == "ok", got)
    # UsageError 是 FlomoError 的子类：既有的 `except FlomoError` 不会漏接
    check("UsageError 属于 FlomoError", issubclass(FC.UsageError, FC.FlomoError))

    # cli_entry 对用法错：走独立退出码，且**不套** `prefix`（「查重未能进行：」套在
    # 参数错上会读成云端失败，用户去查网络）
    def bad_limit():
        raise FC.UsageError("--limit 需要整数，收到 'abc'")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = FC.cli_entry(bad_limit, prefix="[失败] 查重未能进行：", usage_code=1)
    line = err.getvalue().strip()
    check("cli_entry 用法错按 usage_code 退出", code == 1, str(code))
    check("cli_entry 用法错不套调用上下文前缀",
          line == "[失败] --limit 需要整数，收到 'abc'", repr(line))


if __name__ == "__main__":
    run_norm_cases()
    run_readback_cases()
    run_write_path_cases()
    run_brief_cases()
    run_error_contract_cases()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
