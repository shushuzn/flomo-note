#!/usr/bin/env python3
"""check_skill_docs.py 回归测试。

用法：python scripts/test_check_skill_docs.py      退出码 0=全过，1=有失败。

覆盖：日期命中（多种形态，含短横线/斜杠/中文年月）、ENVIRONMENT.md 与协议/版本常量行豁免、
历史事件叙事 WARN、流程时序词（当轮 / 本轮）与「实测」不被误伤、
检测器自身与其回归测试只免于叙事扫描（日期扫描照常生效）。
另含**真实目录防回归**用例：直接对本项目真实技能根断言 SKILL.md 等文档被纳入扫描
（历史事故：扫描路径曾硬编码目录名，导致技能文档全部漏检而自检假通过）。

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

# 夹具：技能根下的文档（扫描逻辑按"技能根"识别人工维护的文档）
SKILL = "SKILL.md"
ENV_DOC = "ENVIRONMENT.md"

CASES = [
    # (用例名, 文件名->内容, 期望错数, 期望警数)
    ("干净技能文档", {SKILL: "# 规则\n\n只写规则与流程。\n"}, 0, 0),
    ("短横线日期判错", {SKILL: f"# 规则\n\n改动完成于 {DASH} 并推送。\n"}, 1, 0),
    ("斜杠日期判错", {SKILL: f"# 规则\n\n改动完成于 {SLASH} 并推送。\n"}, 1, 0),
    ("中文年月判错", {SKILL: f"# 规则\n\n{CJK} 起生效。\n"}, 1, 0),
    ("脚本注释里的日期也判错",
     {"scripts/x.py": f"# 修复：{DASH} 那次改动\n"}, 1, 0),
    ("ENVIRONMENT.md 日期同样判错（H28 无例外）",
     {ENV_DOC: f"# 环境\n\n- 代理经隧道可达（{DASH} 实测）\n"}, 1, 0),
    ("ENVIRONMENT.md 叙事词同样受检",
     {ENV_DOC: "# 环境\n\n当时判断为代理问题。\n"}, 0, 1),
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
    ("技能根下的 AGENTS.md 也纳入扫描",
     {"AGENTS.md": f"# 总则\n\n{DASH} 定稿。\n"}, 1, 0),
    ("技能根下的 README.md 也纳入扫描",
     {"README.md": f"# 说明\n\n{DASH} 发布。\n"}, 1, 0),
    ("技能根下夹空行样本不误伤",
     {SKILL: "# 规则\n\n\n只写规则。\n"}, 0, 0),
    # 瞬时产物（抓取件 / 写卡草稿）不是技能文档：其中别人写的日期不是 H28 违规。
    # 真实事故：项目根下的 _tmp_*.md 抓取件被当技能文档扫出 ERR，污染夹具精确计数，
    # 使 test_check_skill_docs 两条用例在真实工作区里失败——而那些日期根本不是我们写的。
    ("根下抓取件含日期不判错",
     {"_tmp_src.md": f"# External\n\nreleased {DASH}.\n"}, 0, 0),
    ("根下写卡草稿含日期不判错",
     {"memo_body_x.txt": f"要点：\n\n{DASH} 实测。\n"}, 0, 0),
]


def run_case(name, files, want_err, want_warn):
    """夹具：临时目录即"技能根"，内含 scripts/ 子目录（与真实布局一致）。"""
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


def run_real_tree_case():
    """防回归：直接对**真实技能根**断言维护类文档被纳入扫描。

    历史事故：扫描路径曾硬编码某个仓库目录名，而实际技能目录名不同，
    导致 SKILL.md / AGENTS.md / README.md 全部漏检、H28 自检"0 错"是假通过。
    本用例不依赖夹具，直击真实目录，任何路径推导回归都会在此暴露。
    """
    real_root = HERE.parent
    names = {p.name for p in CD.iter_targets(real_root)}
    want = {"SKILL.md", "AGENTS.md", "README.md"}
    missing = want - names
    ok = not missing
    print(f"{'PASS' if ok else 'FAIL'}  真实技能根扫描覆盖维护文档"
          f"（缺 {sorted(missing)}）" if missing
          else "PASS  真实技能根扫描覆盖维护文档")
    # 瞬时产物不得进扫描目标：工作区里常驻抓取件，它们含别人写的日期，
    # 进来就会报假警并让本文件的精确计数断言随工作区状态漂移。
    leaked = sorted(n for n in names if CD.is_transient(n))
    ok &= not leaked
    print(f"{'PASS' if not leaked else 'FAIL'}  真实技能根扫描排除瞬时产物"
          f"（混入 {leaked}）" if leaked
          else "PASS  真实技能根扫描排除瞬时产物")
    return ok


def run_external_root_case():
    """防回归：--root 指向外部目录时，该目录顶层的 .md 也必须纳入扫描。

    历史缺陷：顶层只按 SCRIPT_SUFFIXES 收集，外部 root 的 .md 全部漏检，
    使针对任意目录做内容纪律自检时给出假通过。
    """
    ok = True
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "EXTERNAL.md").write_text(f"# 外部文档\n\n{DASH} 事件。\n", encoding="utf-8")
        (root / "plain.py").write_text(f"# {DASH} 注释\n", encoding="utf-8")
        names = {p.name for p in CD.iter_targets(root)}
        for want in ("EXTERNAL.md", "plain.py"):
            hit = want in names
            ok &= hit
            print(f"{'PASS' if hit else 'FAIL'}  外部 root 顶层纳入扫描：{want}")
        errs = []
        for p in CD.iter_targets(root):
            e, _, _ = CD.scan_file(p, root)
            errs += e
        has = any("EXTERNAL.md" in m for m in errs)
        ok &= has
        print(f"{'PASS' if has else 'FAIL'}  外部 root 的 .md 日期被检出")
    return ok


def run_env_doc_case():
    """防回归：ENVIRONMENT.md 与其它技能文档一视同仁，**不豁免日期**。

    历史缺陷一：它曾整体放入 EXEMPT_NAMES，连 n_files 都不计入——H28 最该
    受约束的归档文件反而成了完全盲区（可无限堆日期与事件经过而自检报 0 错）。
    历史缺陷二：曾按「归档处」名义为它单独开日期豁免——这等于把违规正当化。
    H28 是严格禁止，无例外。
    """
    ok = True
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / ENV_DOC).write_text(f"# 环境\n\n- 隧道可用（{DASH} 实测）\n", encoding="utf-8")
        names = {p.name for p in CD.iter_targets(root)}
        hit = ENV_DOC in names
        ok &= hit
        print(f"{'PASS' if hit else 'FAIL'}  ENVIRONMENT.md 纳入扫描范围")
        errs, warns = [], []
        for p in CD.iter_targets(root):
            e, w, _ = CD.scan_file(p, root)
            errs += e
            warns += w
        has = any(ENV_DOC in m for m in errs)
        ok &= has
        print(f"{'PASS' if has else 'FAIL'}  ENVIRONMENT.md 的日期被判错（无豁免）")
    return ok


if __name__ == "__main__":
    results = [run_case(*c) for c in CASES]  # 不用 all() 短路，需跑完全部用例
    results.append(run_real_tree_case())
    results.append(run_external_root_case())
    results.append(run_env_doc_case())
    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    sys.exit(0 if all(results) else 1)
