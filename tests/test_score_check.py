#!/usr/bin/env python3
"""score_check.py 与 H35 评分位的回归测试。

用法：python tests/test_score_check.py      退出码 0=全过，1=有失败。

锁两件事：一是六档等级与四个校准锚点（黎曼猜想 80、准黎曼猜想 60）的数值口径，
改档位或改锚点必须先在这里红一遍；二是 `#评分/N` 的写法判据——它既是标签，
又是数值位，写错会让同一张卡出现两个簇，比没有评分更难收拾。
"""

COVERS = ("score_check", "validate_memo")

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SC = _load("score_check")
VM = _load("validate_memo")


def run_case(name, fn, want):
    try:
        got = fn()
    except Exception as exc:  # noqa: BLE001 —— 用例炸了本身就是失败，不该中断整轮
        print(f"FAIL {name}：抛出 {type(exc).__name__}: {exc}")
        return False
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}：{got}（期望 {want}）")
    return ok


def cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = SC.main(list(argv))
    return code, buf.getvalue()


def check_errors(content, require=False):
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check(content, require_score=require)
    return len(VM.ERR)


def main():
    results = []

    # ---- 一、等级底分与锚点 ----
    for grade, want in (("proved", 100), ("mainstream", 80), ("weakened", 60),
                        ("partial", 40), ("heuristic", 20), ("refuted", 0)):
        results.append(run_case(f"等级 {grade} 底分", lambda g=grade: SC.compute(g, {}), want))

    results.append(run_case("黎曼猜想锚点 80（不加修正）",
                            lambda: SC.compute("mainstream", {}), 80))
    results.append(run_case("准黎曼猜想锚点 60（不加修正）",
                            lambda: SC.compute("weakened", {}), 60))
    results.append(run_case("四项修正相加",
                            lambda: SC.compute("weakened",
                                               {"precision": 5, "testability": 5,
                                                "independence": 5, "consistency": 5}), 80))
    results.append(run_case("下限夹到 0",
                            lambda: SC.compute("refuted",
                                               {"precision": -5, "consistency": -5}), 0))
    results.append(run_case("上限夹到 100",
                            lambda: SC.compute("proved", {"precision": 5, "testability": 5}), 100))

    # ---- 二、标签写法 ----
    for tag, want in (("#评分/80", True), ("#评分/0", True), ("#评分/100", True),
                      ("#评分/078", False), ("#评分/101", False), ("#评分/80分", False),
                      ("#评分", False), ("评分/60", True)):
        results.append(run_case(f"标签写法 {tag}",
                                lambda t=tag: SC.parse_tag(t)[0], want))

    # ---- 三、命令行 ----
    results.append(run_case("--grade mainstream 输出 80", lambda: cli("--grade", "mainstream")[0], 0))
    results.append(run_case("--grade mainstream 打印 #评分/80",
                            lambda: "#评分/80" in cli("--grade", "mainstream")[1], True))
    results.append(run_case("标签与算分不一致时非 0",
                            lambda: cli("--grade", "mainstream", "--tag", "#评分/75")[0], 2))
    results.append(run_case("越界修正被拒", lambda: cli("--grade", "mainstream", "--precision", "9")[0], 2))
    results.append(run_case("未知等级被拒", lambda: cli("--grade", "maybe")[0], 2))
    results.append(run_case("--selftest 全过", lambda: SC.selftest(), 0))

    # ---- 四、validate_memo 的评分位判据 ----
    base = "#数学/泛函分析 #评分/80\n某概念\n\n要点：\n- 内容\n"
    results.append(run_case("合规评分位不报错", lambda: check_errors(base), 0))
    results.append(run_case("缺评分位默认不报错（存量兼容）",
                            lambda: check_errors("#数学/泛函分析\n某概念\n\n要点：\n- 内容\n"), 0))
    results.append(run_case("缺评分位在 --require-score 下判错",
                            lambda: check_errors("#数学/泛函分析\n某概念\n\n要点：\n- 内容\n", True), 1))
    results.append(run_case("两个评分位判错",
                            lambda: check_errors("#数学/泛函分析 #评分/80 #评分/60\n某概念\n\n要点：\n- 内容\n"), 1))
    # 这几条同时会撞上通用标签判据（点号、百分号、裸顶层），报几条不定；
    # 只要非 0 即视为拦住，具体条数留给各自的规则去管。
    for bad in ("#评分/078", "#评分/101", "#评分/80.5", "#评分/80%", "#评分"):
        results.append(run_case(f"写法 {bad} 判错",
                                lambda b=bad: check_errors(
                                    f"#数学/泛函分析 {b}\n某概念\n\n要点：\n- 内容\n") > 0, True))

    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
