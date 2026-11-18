#!/usr/bin/env python3
"""check_version.py 的离线回归用例（不联网；git 用例只在本机建临时仓库）。"""

COVERS = ("check_version",)

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CV = _load("check_version", "check_version.py")
from cli_util import cli_entry as CLI_ENTRY

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


def make_repo(version="1.0.0", entries=("1.0.0",), *, pyproject=None, changelog=None):
    """造一个最小仓库副本：pyproject.toml + CHANGELOG.md。返回临时目录。"""
    root = Path(tempfile.mkdtemp(prefix="ver-"))
    if pyproject is None:
        pyproject = f'[project]\nname = "x"\nversion = "{version}"\n'
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if changelog is None:
        body = "# 变更记录\n\n## [未发布]\n\n" + "".join(
            f"## [{v}]\n\n- 条目\n\n" for v in entries)
        changelog = body
    if changelog is not False:
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


def git(root, *args):
    """在临时仓库里跑 git；身份用 -c 传，不依赖本机全局配置。"""
    return subprocess.run(
        ["git", "-C", str(root),
         "-c", "user.name=case", "-c", "user.email=case@example.invalid", *args],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_consistent():
    root = make_repo("1.0.0", ("1.0.0",))
    errs, notes = CV.check(root)
    check("一致时不报错", errs == [], errs)
    check("一致时给出读数", any("1.0.0" in n for n in notes), notes)


def test_version_mismatch():
    # pyproject 升了号、CHANGELOG 没补条目：两边必须都动，只动一边就是这形态
    root = make_repo("1.1.0", ("1.0.0",))
    errs, _ = CV.check(root)
    check("版本对不上时报错", len(errs) == 1, errs)
    check("报错点名两边的值", "1.1.0" in "".join(errs) and "1.0.0" in "".join(errs), errs)
    check("报错说清改哪边", "两头都得动" in "".join(errs), errs)


def test_missing_files():
    root = make_repo("1.0.0", ("1.0.0",), changelog=False)
    errs, _ = CV.check(root)
    check("缺 CHANGELOG 报错", any("缺 CHANGELOG" in e for e in errs), errs)

    root = make_repo(pyproject='[project]\nname = "x"\n')
    errs, _ = CV.check(root)
    check("缺 version 键报错", any("[project].version" in e for e in errs), errs)

    (root / "pyproject.toml").unlink()
    errs, _ = CV.check(root)
    check("缺 pyproject 报错（不抛异常）", len(errs) == 1, errs)


def test_bad_version_shape():
    root = make_repo("1.0", ("1.0.0",))
    errs, _ = CV.check(root)
    check("版本不是 X.Y.Z 报错", any("不是 X.Y.Z" in e for e in errs), errs)
    check("parse_version 不猜形态", CV.parse_version("1.0") is None and CV.parse_version("v1.2.3") == (1, 2, 3))


def test_duplicate_and_order():
    root = make_repo("1.0.0", ("1.0.0", "0.9.0", "1.0.0"))
    errs, _ = CV.check(root)
    check("重复版本报错", any("出现两次" in e for e in errs), errs)

    root = make_repo("1.0.0", ("1.0.0", "1.1.0"))
    errs, _ = CV.check(root)
    check("顺序反了报错", any("顺序应新在上" in e for e in errs), errs)


def test_changelog_without_entries():
    root = make_repo("1.0.0", (), changelog="# 变更记录\n\n还没写。\n")
    errs, _ = CV.check(root)
    check("CHANGELOG 无条目报错", any("没有任何" in e for e in errs), errs)


def test_real_repo_is_consistent():
    # 本仓库自己必须一致：这条让「忘了升号 / 忘了补条目」在 CI 里就红，
    # 而不是等到要发版、要回答「这个版本指哪次提交」的时候才发现。
    root = SCRIPT_DIR.parent
    errs, notes = CV.check(root)
    check("本仓库 pyproject 与 CHANGELOG 一致", errs == [], errs or notes)


def test_tag_missing():
    root = make_repo("1.0.0", ("1.0.0",))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    errs, _ = CV.check(root, check_tag=True)
    check("无标签时报错", any("没有 v1.0.0 标签" in e for e in errs), errs)
    check("报错给出打标签的命令", any("git tag -a v1.0.0" in e for e in errs), errs)

    # 静态三项此时是一致的——标签是**额外**一项，不能因为缺标签就说文件不一致
    errs_static, _ = CV.check(root)
    check("缺标签不影响静态三项", errs_static == [], errs_static)


def test_tag_ok():
    root = make_repo("1.0.0", ("1.0.0",))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    errs, notes = CV.check(root, check_tag=True)
    check("标签就位时通过", errs == [], errs or notes)


def test_tag_rollback():
    # 标签比版本号高：版本号落后于已发布的事实。标签是公开的，改的该是 pyproject。
    root = make_repo("1.0.0", ("1.0.0",))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    git(root, "tag", "-a", "v2.0.0", "-m", "v2.0.0")
    errs, _ = CV.check(root, check_tag=True)
    check("版本倒挂报错", any("版本倒挂" in e for e in errs), errs)
    check("倒挂时点名更高的标签", any("v2.0.0" in e for e in errs), errs)


def test_tag_unavailable_is_not_pass():
    # 取不到标签时必须报「无法判定」，不能静默当成已对齐。
    #
    # **这个场景不能靠「临时目录不在任何仓库里」来造**：`run_tests.sh` 在外部 TMPDIR
    # 为空时（CI 就是这个情形）会把它指到仓库内的 `_tmp/.tmp`，此时 git 会沿目录向上
    # 找到本仓库——「不是仓库」变成「是仓库，只是标签不全」，同一条断言在 CI 与本机
    # 得到相反结论。测出来的红是环境的，不是代码的。故直接把取标签这一步打成失败，
    # 环境不参与判定。
    root = make_repo("1.0.0", ("1.0.0",))
    orig = CV._git
    CV._git = lambda *a, **k: (128, "fatal: not a git repository")
    try:
        errs, _ = CV.check(root, check_tag=True)
    finally:
        CV._git = orig
    check("取不到标签时报无法判定", any("无法判定" in e for e in errs), errs)


def run_cli(argv):
    """跑一次 CLI，回收退出码与两路输出（用例自己的 stdout 只留 PASS/FAIL 行）。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = CLI_ENTRY(lambda: CV.main(argv), errors=())
    return code, out.getvalue(), err.getvalue()


def test_cli():
    root = make_repo("1.1.0", ("1.0.0",))
    code, _out, err = run_cli(["--root", str(root)])
    check("CLI：不一致退出 1", code == 1, code)
    check("CLI：不一致时说明写到 stderr", "版本对不上" in err, err)

    ok_root = make_repo("1.0.0", ("1.0.0",))
    code, out, _err = run_cli(["--root", str(ok_root)])
    check("CLI：一致退出 0", code == 0, code)
    check("CLI：一致时结论可读", "同指一个版本" in out, out)

    root = make_repo("1.0.0", ("1.0.0",))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    code, out, _err = run_cli(["--root", str(root), "--tag"])
    check("CLI：--tag 一致退出 0", code == 0, code)
    check("CLI：--tag 结论点名标签", "git 标签" in out, out)

    for argv, want in ((["--nope"], "未知"), (["--tag", "--tag"], "重复")):
        code, _out, err = run_cli(argv)
        check(f"用法错（{' '.join(argv)}）：退出码 2", code == 2, code)
        check(f"用法错（{' '.join(argv)}）：提示含「{want}」", want in err, err)


if __name__ == "__main__":
    for fn in (test_consistent, test_version_mismatch, test_missing_files,
               test_bad_version_shape, test_duplicate_and_order,
               test_changelog_without_entries, test_real_repo_is_consistent,
               test_tag_missing, test_tag_ok, test_tag_rollback,
               test_tag_unavailable_is_not_pass, test_cli):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
