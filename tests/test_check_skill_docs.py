#!/usr/bin/env python3
"""check_skill_docs.py 回归测试。

用法：python tests/test_check_skill_docs.py      退出码 0=全过，1=有失败。

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

HERE = Path(__file__).resolve().parent.parent / "scripts"
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


def run_inventory_case():
    """README 是项目入口：漏列脚本必须判错，且这一项要计入 main 的退出码。

    反面样本来自真实漂移：publish/dedup/tagcheck/fetch_article 与两个 scan_*
    都不在 README，而前三个是每轮必用的流程脚本——按 README 手跑会漏掉写云这一步。
    """
    ok = True
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "scripts").mkdir()
        (root / "scripts" / "publish.py").write_text("# 写云一条链\n", encoding="utf-8")
        # scripts/ 里放一个 test_ 开头的文件：**不再享有豁免**。用例已集中到 tests/，
        # 这条豁免若留着就是不可达分支，还会顺手放行一个混进工具目录的用例。
        (root / "scripts" / "test_leak.py").write_text("# 混进工具目录的用例\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_publish.py").write_text("# 用例\n", encoding="utf-8")
        (root / "README.md").write_text("# 项目\n\n暂无说明。\n", encoding="utf-8")

        errs = CD.check_script_inventory(root)
        hit = (any("publish.py" in m for m in errs)
               and any("test_leak.py" in m for m in errs) and len(errs) == 2)
        ok &= hit
        print(f"{'PASS' if hit else 'FAIL'}  scripts/ 漏列都判错、测试不再豁免（{len(errs)} 条）")

        no_test = not any("tests/test_publish.py" in m for m in errs)
        ok &= no_test
        print(f"{'PASS' if no_test else 'FAIL'}  tests/ 下的用例不要求写进 README")

        (root / "README.md").write_text(
            "# 项目\n\n- `publish.py` 写云一条链\n- `test_leak.py` 混进来的用例\n",
            encoding="utf-8")
        clean = CD.check_script_inventory(root) == []
        ok &= clean
        print(f"{'PASS' if clean else 'FAIL'}  补上后不再报")

        # 命名不符 = 用例永远不被 run_tests.sh 收集，是静默失效，必须判错
        (root / "tests" / "smoke_publish.py").write_text("# 名字写偏\n", encoding="utf-8")
        bad_name = any("smoke_publish.py" in m for m in CD.check_script_inventory(root))
        ok &= bad_name
        print(f"{'PASS' if bad_name else 'FAIL'}  tests/ 下不以 test_ 开头的用例判错")
        (root / "tests" / "smoke_publish.py").unlink()

        # 退出码必须被结构问题拉红，否则这一项等于只打印
        rc = CD.main(["--root", str(root), "--quiet"])
        ok &= rc == 0
        print(f"{'PASS' if rc == 0 else 'FAIL'}  齐全时 main 退出码 0（{rc}）")

        (root / "README.md").write_text("# 项目\n", encoding="utf-8")
        rc_bad = CD.main(["--root", str(root), "--quiet"])
        ok &= rc_bad == 1
        print(f"{'PASS' if rc_bad == 1 else 'FAIL'}  漏列时 main 退出码 1（{rc_bad}）")

        # --root 指向没有 README 的外部目录时，不因缺入口文档判错
        (root / "README.md").unlink()
        ok &= CD.check_script_inventory(root) == []
        print(f"{'PASS' if CD.check_script_inventory(root) == [] else 'FAIL'}  无 README 时不报")
    return ok


def run_cross_ref_case():
    """内部交叉引用：指空了必须判错，指得到必须放行。

    这条检查的全部价值在于「删改之后引用会静默悬空」——把某条硬限、某个附录
    小节、某一步流程从 SKILL.md 删掉或改名，引用它还留在原处，读起来完全通顺，
    只是指不到任何东西。故每个放行用例都配一个**只差目标存在性**的失效用例：
    两边都能过说明判据恒真，这条检查就等于没有。
    """
    ok = True
    BASE = (
        "# 规则\n"
        "\n"
        "## 二、硬限\n"
        "\n"
        "**H1 · 第一条。**\n"
        "**H6b · 第二条（带字母后缀）。**\n"
        "\n"
        "## 三、流程\n"
        "\n"
        "### 1. 抓取\n"
        "### 5. 查重（阻塞）\n"
        "\n"
        "## 五、附录\n"
        "\n"
        "### 标签树现采（带补充说明的标题）\n"
        "### 概念名的机械拦截\n"
    )

    def errs_for(doc_body, extra_files=None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / "scripts" / "real.py").write_text("# 真实脚本\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_real.py").write_text("# 真实用例\n", encoding="utf-8")
            for relp, body in (extra_files or {}).items():
                q = root / relp
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_text(body, encoding="utf-8")
            (root / "SKILL.md").write_text(BASE, encoding="utf-8")
            (root / "DOC.md").write_text(doc_body, encoding="utf-8")
            return CD.check_cross_refs(root)

    def expect(name, doc, want_err, needle=""):
        nonlocal ok
        got = errs_for(doc)
        hit = len(got) == want_err and (not needle or any(needle in m for m in got))
        ok &= hit
        print(f"{'PASS' if hit else 'FAIL'}  {name}: 得到 {len(got)} 错，"
              f"期望 {want_err} 错{'（含 %s）' % needle if needle else ''}")
        if not hit:
            for m in got:
                print("      ERR :", m)

    # —— 硬限编号 ——
    expect("引用已定义的 H 编号放行", "# 说明\n\n见 H1 与 H6b。\n", 0)
    expect("区间引用 H1–H6b 放行", "# 说明\n\n见 H1–H6b 各条。\n", 0)
    expect("引用不存在的 H 编号判错", "# 说明\n\n见 H99。\n", 1, "H99")
    # 只差「定义行被删」：同样的引用，SKILL.md 里没有 H99 定义
    expect("紧邻字母数字的 H 不算编号（HDR1 / H264）", "# 说明\n\nHDR1 与 H264 编码。\n", 0)

    # —— 附录小节 ——
    expect("引用存在的附录小节放行", "# 说明\n\n见「附录 · 概念名的机械拦截」。\n", 0)
    expect("附录小节名带补充说明也能命中",
           "# 说明\n\n口径见「附录 · 标签树现采」。\n", 0)
    expect("引用不存在的附录小节判错",
           "# 说明\n\n见「附录 · 参数速查」。\n", 1, "参数速查")
    expect("「附录结论」是正文词不误判", "# 说明\n\n正文层级含附录结论。\n", 0)
    # 附录里没有「查重」，正文流程里却有 ### 5. 查重：不得拿正文标题冒充附录小节
    expect("不拿正文标题冒充附录小节", "# 说明\n\n见「附录 · 查重」。\n", 1, "查重")

    # —— 流程步骤 ——
    expect("引用存在的步骤放行", "# 说明\n\n按第 5 步执行。\n", 0)
    expect("圈码步骤引用同样核", "# 说明\n\n按第 ⑤ 步执行。\n", 0)
    expect("引用不存在的步骤判错", "# 说明\n\n按第 7 步执行。\n", 1, "第 7 步")
    expect("「第 ⑥ 段」不是流程步骤，不误判", "# 说明\n\n看 publish 第 ⑥ 段的删除行。\n", 0)

    # —— 脚本名 ——
    expect("引用存在的脚本放行", "# 说明\n\n跑 `scripts/real.py`。\n", 0)
    expect("引用已删的脚本判错", "# 说明\n\n跑 `scripts/ghost.py`。\n", 1, "ghost.py")
    # 用例搬进 tests/ 之后，`tests/x.py` 也是引用；只核 scripts/ 会让这类悬空静默通过。
    # 下面两条**只差目录前缀**：同名文件在 scripts/ 存在、在 tests/ 不存在。
    expect("引用存在的用例放行", "# 说明\n\n跑 `tests/test_real.py`。\n", 0)
    expect("引用已删的用例判错", "# 说明\n\n跑 `tests/test_ghost.py`。\n", 1, "test_ghost.py")

    # —— 接入 main：结构问题必须拉红退出码 ——
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text(BASE, encoding="utf-8")
        (root / "README.md").write_text("# 项目\n", encoding="utf-8")
        rc_clean = CD.main(["--root", str(root), "--quiet"])
        ok &= rc_clean == 0
        print(f"{'PASS' if rc_clean == 0 else 'FAIL'}  引用全对时 main 退出码 0（{rc_clean}）")
        (root / "README.md").write_text("# 项目\n\n见 H99。\n", encoding="utf-8")
        rc_bad = CD.main(["--root", str(root), "--quiet"])
        ok &= rc_bad == 1
        print(f"{'PASS' if rc_bad == 1 else 'FAIL'}  引用悬空时 main 退出码 1（{rc_bad}）")

    # —— 真实项目防回归 ——
    real = CD.check_cross_refs(HERE.parent)
    ok &= real == []
    print(f"{'PASS' if not real else 'FAIL'}  真实技能文档交叉引用全部可解析"
          + ("" if not real else f"（{len(real)} 条悬空）"))
    for m in real:
        print("      ERR :", m)
    return ok


def run_cli_flags_case():
    """文档里的命令行，选项必须是该脚本真认识的。

    与「脚本名存在」差一层：名字写错报「文件不存在」，**选项写错只被当成未知参数**，
    脚本打一行用法就退出，读者以为是自己环境问题。参数改名（`--update-id` ↔ `--update_id`）
    又恰恰是改脚本时最容易顺手带上的漂移，文档那头不会跟着红。

    判据只认**片段开头即命令**的形态：自然语言里「…见 scripts/a.py…，b.py --flag 才生效」
    按「最近脚本名」归属会张冠李戴（真实文档实测出过 2 条假报），故不做那种归属。
    """
    ok = True
    ARG = (
        "import argparse\n"
        "ap = argparse.ArgumentParser()\n"
        'ap.add_argument("--draft")\n'
        'ap.add_argument("--verify")\n'
    )
    HAND = (
        "for i, a in enumerate(argv):\n"
        '    if a == "--update-id":\n'
        "        pass\n"
        '    elif a in ("--keep", "--dry-run"):\n'
        "        pass\n"
    )

    def errs_for(doc_body):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "scripts").mkdir()
            (root / "scripts" / "real.py").write_text(ARG, encoding="utf-8")
            (root / "scripts" / "hand.py").write_text(HAND, encoding="utf-8")
            (root / "SKILL.md").write_text("# 规则\n", encoding="utf-8")
            (root / "DOC.md").write_text(doc_body, encoding="utf-8")
            return CD.check_doc_cli_flags(root)

    def expect(name, doc, want_err, needle=""):
        nonlocal ok
        got = errs_for(doc)
        hit = len(got) == want_err and (not needle or any(needle in m for m in got))
        ok &= hit
        print(f"{'PASS' if hit else 'FAIL'}  {name}: 得到 {len(got)} 错，"
              f"期望 {want_err} 错{'（含 %s）' % needle if needle else ''}")
        if not hit:
            for m in got:
                print("      ERR :", m)

    # argparse 风格
    expect("argparse 脚本的已知选项放行",
           "# 说明\n\n跑 `python scripts/real.py --draft x --verify y`。\n", 0)
    expect("argparse 脚本的未知选项判错",
           "# 说明\n\n跑 `python scripts/real.py --draftt x`。\n", 1, "--draftt")
    # 手写解析风格：`a == "--x"` 与 `a in ("--x", "--y")` 都要采到
    expect("手写解析的选项也采得到（== 形态）",
           "# 说明\n\n跑 `scripts/hand.py --update-id abc`。\n", 0)
    expect("手写解析的选项也采得到（in 元组形态）",
           "# 说明\n\n跑 `scripts/hand.py --keep`。\n", 0)
    expect("代码块里的命令同样核",
           "# 说明\n\n```bash\npython scripts/real.py --ghost\n```\n", 1, "--ghost")
    # 不按「最近脚本名」归属：只有片段开头即命令才核对，句中的脚本名不吞后文参数
    expect("句中提及的脚本不吞掉后文参数",
           "# 说明\n\n见 `scripts/real.py` 的说明；另有 `--draft` 一词属别的脚本。\n", 0)
    expect("无反引号的自然语言不参与核对",
           "# 说明\n\n见 scripts/real.py 的说明，hand.py --draft 才生效。\n", 0)
    # 脚本不存在时交给交叉引用检查，这里不重复报
    expect("不存在的脚本不在这里报",
           "# 说明\n\n跑 `scripts/ghost.py --whatever`。\n", 0)

    real = CD.check_doc_cli_flags(HERE.parent)
    ok &= real == []
    print(f"{'PASS' if not real else 'FAIL'}  真实文档的命令行选项全部对得上")
    for m in real:
        print("      ERR :", m)
    return ok


if __name__ == "__main__":
    results = [run_case(*c) for c in CASES]  # 不用 all() 短路，需跑完全部用例
    results.append(run_real_tree_case())
    results.append(run_external_root_case())
    results.append(run_env_doc_case())
    results.append(run_inventory_case())
    results.append(run_cross_ref_case())
    results.append(run_cli_flags_case())
    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    sys.exit(0 if all(results) else 1)
