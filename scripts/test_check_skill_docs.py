#!/usr/bin/env python3
"""check_skill_docs.py 回归测试。

用法：python scripts/test_check_skill_docs.py      退出码 0=全过，1=有失败。

覆盖：日期命中（多种形态，含短横线/斜杠/中文年月）、ENVIRONMENT.md 与协议/版本常量行豁免、
历史事件叙事 WARN、流程时序词（当轮 / 本轮）与「实测」不被误伤、
检测器自身与其回归测试只免于叙事扫描（日期扫描照常生效）。

注意：样本日期一律由片段拼接构造，避免本文件自身被内容自检判定为违规。
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_skill_docs", HERE / "check_skill_docs.py")
CD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CD)

# 拼接构造日期样本（不在源码里写出完整日期字面量）
Y = "20" + "26"
DASH = Y + "-09-26"
SLASH = Y + "/9/26"
CJK = Y + "年9月"
SKILL = ".kilo/skills/flomo-note/SKILL.md"

CASES = [
    # (用例名, 文件名->内容, 期望错数, 期望警数)
    ("干净技能文档", {SKILL: "# 规则\n\n只写规则与流程。\n"}, 0, 0),
    ("短横线日期判错", {SKILL: f"# 规则\n\n改动完成于 {DASH} 并推送。\n"}, 1, 0),
    ("斜杠日期判错", {SKILL: f"# 规则\n\n改动完成于 {SLASH} 并推送。\n"}, 1, 0),
    ("中文年月判错", {SKILL: f"# 规则\n\n{CJK} 起生效。\n"}, 1, 0),
    ("脚本注释里的日期也判错",
     {"scripts/x.py": f"# 修复：{DASH} 那次改动\n"}, 1, 0),
    ("ENVIRONMENT.md 日期豁免",
     {".kilo/skills/flomo-note/ENVIRONMENT.md": f"# 环境\n\n- {DASH} 实测：代理经隧道可用\n"}, 0, 0),
    ("协议版本常量行豁免",
     {"scripts/y.py": '        "protocolVersion": "' + DASH + '",\n'}, 0, 0),
    ("事件叙事词只提示", {SKILL: "# 规则\n\n当时判断为网络问题，遂改走直连。\n"}, 0, 1),
    ("上次推送式叙事只提示", {SKILL: "# 规则\n\n上次推送失败后重试即通。\n"}, 0, 1),
    ("流程时序词不误伤",
     {SKILL: "# 规则\n\n当轮处置，本轮收尾清理；字数取实测值。每轮按序执行。\n"}, 0, 0),
    ("日期与叙事同行各记一条",
     {SKILL: f"# 规则\n\n{DASH} 当时判断为代理问题。\n"}, 1, 1),
    ("非目标后缀不扫描",
     {"scripts/notes.txt": f"{DASH} 事件记录\n"}, 0, 0),
    ("检测器自身免于叙事扫描",
     {"scripts/check_skill_docs.py": "# 当时判断；曾因如此。\n"}, 0, 0),
    ("检测器自身日期仍判错",
     {"scripts/check_skill_docs.py": f"# {DASH} 曾因如此。\n"}, 1, 0),
]


def run_case(name, files, want_err, want_warn):
    ok = True
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for relp, body in files.items():
            p = root / relp
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        errs, warns = [], []
        for p in CD.iter_targets(root):
            e, w, _ = CD.scan_file(p, root)
            errs += e
            warns += w
    got = (len(errs), len(warns))
    ok = got == (want_err, want_warn)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 得到 {got[0]} 错/{got[1]} 警，"
          f"期望 {want_err} 错/{want_warn} 警")
    if not ok:
        for m in errs:
            print("      ERR :", m)
        for m in warns:
            print("      WARN:", m)
    return ok


if __name__ == "__main__":
    results = [run_case(*c) for c in CASES]  # 不用 all() 短路，需跑完全部用例
    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    sys.exit(0 if all(results) else 1)
