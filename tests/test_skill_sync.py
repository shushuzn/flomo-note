#!/usr/bin/env python3
"""skill_sync.py 的离线回归用例（只用本地 file:// 仓，不联网）。

守的是「版本自愈」那几条自我约束：落后就快进、脏就一个字不动、领先不吞提交、
取不到远端不阻塞、时间戳内不重复取、`--check` 只报不改。
"""
COVERS = ("skill_sync", "cli_util")

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import skill_sync as SS

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=master"]


def git(cwd, *args, ok=True):
    proc = subprocess.run([*GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    if ok and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc


def head(cwd):
    return git(cwd, "rev-parse", "HEAD").stdout.strip()


def build(tmp: Path):
    """`src` 是工作仓，`bare` 是它的裸副本，`work` 是深度 1 的克隆（模拟镜像里的旧检出）。"""
    src, bare, work = tmp / "src", tmp / "bare", tmp / "work"
    src.mkdir()
    git(src, "init", "-q")
    (src / "f").write_text("1\n", encoding="utf-8")
    git(src, "add", "f")
    git(src, "commit", "-qm", "one")
    (src / "f").write_text("1\n2\n", encoding="utf-8")
    git(src, "commit", "-qam", "two")
    git(tmp, "clone", "-q", "--bare", str(src), str(bare))
    git(tmp, "clone", "-q", "--depth=1", bare.as_uri(), str(work))
    return src, bare, work


def advance(src, bare, text):
    (src / "f").write_text(text, encoding="utf-8")
    git(src, "commit", "-qam", f"rev {text!r}")
    git(src, "push", "-q", str(bare), "master")
    return head(src)


def run(work: Path, **kw):
    """跑一次 ensure_current，返回 (状态, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = SS.ensure_current(work, **kw)
    return status, out.getvalue(), err.getvalue()


def fresh_env(tmp: Path):
    stamp = tmp / "stamp"
    os.environ["FLOMO_SKILL_SYNC_STAMP"] = str(stamp)
    return stamp


def test_not_a_repo(tmp):
    """非仓库目录：返回 no-repo 且不抛。

    目录必须落在任何仓库之外——用例的 tmp 若被指到仓库内（`run_tests.sh` 把 TMPDIR
    设进 `_tmp/` 就正是这种情况），向上找会撞见外层的 `.git`，报 dirty 是对的、
    用例却会错怪实现。故显式用系统 `/tmp`，并在它本身也在仓库里时退化成「不抛即可」。
    """
    import tempfile as _tf
    plain = Path(_tf.mkdtemp(prefix="plain-", dir="/tmp")) if Path("/tmp").is_dir() else tmp / "plain"
    try:
        status, _o, _e = run(plain, ttl=0)
        if SS.repo_root(plain) is None:
            check("非仓库目录：返回 no-repo 且不抛", status == "no-repo", status)
        else:
            check("非仓库目录（外层有仓库）：只返回状态、不抛", isinstance(status, str), status)
    finally:
        import shutil as _sh
        _sh.rmtree(plain, ignore_errors=True)


def test_behind_fast_forwards(tmp):
    src, bare, work = build(tmp)
    old = head(work)
    fresh_env(tmp)
    new = advance(src, bare, "1\n2\n3\n")
    status, _o, err = run(work, ttl=0)
    check("落后一个提交：状态 updated", status == "updated", status)
    check("落后一个提交：HEAD 到了远端", head(work) == new, f"{old[:7]} -> {head(work)[:7]}")
    check("落后一个提交：提示里带前后 hash 与落后数", new[:7] in err and "落后 1 个提交" in err, err)


def test_shallow_depth_one_is_not_enough(tmp):
    """浅克隆只取一跳（`--depth=1`）时栅栏在祖先关系上，会报 unrelated histories。

    这条是实测过的坑：没这条用例，改回 `fetch --depth=1` 会看起来很省流量，实际快进不了。
    """
    src, bare, work = build(tmp)
    fresh_env(tmp)
    advance(src, bare, "1\n2\n3\n")
    subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", "master"],
                   cwd=str(work), capture_output=True, text=True, check=False)
    merged = subprocess.run(["git", "merge", "--ff-only", "FETCH_HEAD"],
                            cwd=str(work), capture_output=True, text=True)
    check("`fetch --depth=1` 后 ff 合并会失败（故实现里不带 depth）",
          merged.returncode != 0 or "Already up to date" in merged.stdout,
          merged.stderr.strip() or merged.stdout.strip())


def test_dirty_is_left_alone(tmp):
    src, bare, work = build(tmp)
    fresh_env(tmp)
    (work / "f").write_text("local edit\n", encoding="utf-8")
    before = head(work)
    advance(src, bare, "1\n2\n3\n")
    status, _o, err = run(work, ttl=0)
    check("本地有未提交改动：状态 dirty", status == "dirty", status)
    check("本地有未提交改动：HEAD 不动", head(work) == before, head(work))
    check("本地有未提交改动：正文没被覆盖", "local edit" in (work / "f").read_text(encoding="utf-8"))
    check("本地有未提交改动：提示说明跳过原因", "未提交改动" in err, err)


def test_ahead_is_not_reset(tmp):
    src, bare, work = build(tmp)
    fresh_env(tmp)
    (work / "g").write_text("mine\n", encoding="utf-8")
    git(work, "add", "g")
    git(work, "commit", "-qm", "local commit")
    mine = head(work)
    advance(src, bare, "1\n2\n3\n")
    status, _o, err = run(work, ttl=0)
    check("本地领先且有分叉：不快进、不重置", status in ("ahead", "stuck"), status)
    check("本地提交还在", head(work) == mine, head(work))
    check("提示里点明本地有未推送提交", "未推送提交" in err or "快进失败" in err, err)


def test_up_to_date_is_silent(tmp):
    _src, _bare, work = build(tmp)
    fresh_env(tmp)
    status, out, err = run(work, ttl=0)
    check("已是最新：状态 current", status == "current", status)
    check("已是最新：不刷屏（stdout/stderr 都为空）", not out.strip() and not err.strip(), out + err)


def test_offline_does_not_block(tmp):
    _src, _bare, work = build(tmp)
    fresh_env(tmp)
    git(work, "remote", "set-url", "origin", (tmp / "no-such-repo").as_uri())
    status, _o, err = run(work, ttl=0)
    check("取不到远端：状态 offline 且不抛", status == "offline", status)
    check("取不到远端：给出可照抄的补救命令", "pull --ff-only origin master" in err, err)
    check("取不到远端：提示里点明写云会拒写", "写云会拒写" in err, err)


def test_write_guard_blocks_unverified(tmp):
    """写云闸门：版本没确认就拒写（退出码 8），确认过的放行。

    只读照跑、写云停住，是这次修的重点——旧版跑只读还能靠人复核，旧版写云只在事后
    人工回滚，两种代价不能同一条规矩。
    """
    _src, _bare, work = build(tmp)
    fresh_env(tmp)
    code_ok = SS.write_guard(work, quiet=True)
    check("仓库是最新：写云放行（0）", code_ok == 0, code_ok)

    git(work, "remote", "set-url", "origin", (tmp / "no-such-repo").as_uri())
    stamp = fresh_env(tmp)
    stamp.unlink(missing_ok=True)  # 时间戳一过期就跳过取远端，这里要连「跳过」一起排除
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = SS.write_guard(work)
    check("取不到远端：写云拒写（8）", code == 8, code)
    check("拒写时给出可照抄的确认命令", "--force --check" in err.getvalue(), err.getvalue())
    check("拒写时点名逃生口", SS.WRITE_BYPASS_ENV in err.getvalue(), err.getvalue())

    os.environ[SS.WRITE_BYPASS_ENV] = "1"
    try:
        code = SS.write_guard(work, quiet=True)
        check("逃生口打开：写云放行", code == 0, code)
    finally:
        os.environ.pop(SS.WRITE_BYPASS_ENV, None)


def test_cli_entry_write_blocks(tmp):
    """`cli_entry(write=True)` 在版本未确认时直接返回 8，且不跑 main。"""
    from cli_util import cli_entry
    real = SS.ensure_current
    ran = []
    try:
        SS.ensure_current = lambda *a, **k: "offline"
        old = sys.argv[0]
        sys.argv[0] = "publish.py"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli_entry(lambda: ran.append(1) or 0, errors=(), write=True)
        check("写云入口在 offline 下返回 8", code == 8, code)
        check("写云入口在 offline 下没跑 main", not ran, ran)
        check("写云入口打了一行阻塞说明", "[阻塞] 版本未确认" in err.getvalue(), err.getvalue())

        SS.ensure_current = lambda *a, **k: "current"
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli_entry(lambda: ran.append(2) or 0, errors=(), write=True)
        check("版本确认后写云入口正常跑 main", code == 0 and ran == [2], (code, ran))

        SS.ensure_current = lambda *a, **k: "offline"
        with contextlib.redirect_stderr(io.StringIO()):
            code = cli_entry(lambda: ran.append(3) or 0, errors=())
        check("只读入口在 offline 下照跑", code == 0 and 3 in ran, (code, ran))
    finally:
        SS.ensure_current = real
        sys.argv[0] = old


def test_stamp_ttl_skips_network(tmp):
    src, bare, work = build(tmp)
    stamp = fresh_env(tmp)
    _status, _o, _e = run(work, ttl=0)
    check("第一次跑后落了时间戳", stamp.exists(), str(stamp))
    new = advance(src, bare, "1\n2\n3\n")
    status, _o, _e = run(work, ttl=900)
    check("时间戳未过期：不再取远端（状态 skip）", status == "skip", status)
    check("时间戳未过期：HEAD 保持不动", head(work) != new, head(work))


def test_dry_run_only_reports(tmp):
    src, bare, work = build(tmp)
    fresh_env(tmp)
    before = head(work)
    advance(src, bare, "1\n2\n3\n")
    status, _o, err = run(work, ttl=0, dry_run=True)
    check("--check：落后时报 behind", status == "behind", status)
    check("--check：不动 HEAD", head(work) == before, head(work))
    check("--check：说明未快进", "未快进" in err, err)


def test_cli_entry_skips_in_tests(_tmp):
    """用例进程里 `cli_entry` 不碰网络：它按 argv0 判是不是 test_*.py。"""
    from cli_util import cli_entry
    old = sys.argv[0]
    sys.argv[0] = str(SCRIPT_DIR.parent / "tests" / "test_skill_sync.py")
    try:
        code = cli_entry(lambda: 0, errors=())
    finally:
        sys.argv[0] = old
    check("test_*.py 进程里 cli_entry 正常返回且不触发同步", code == 0, code)


def main():
    tests = [test_not_a_repo, test_behind_fast_forwards, test_shallow_depth_one_is_not_enough,
             test_dirty_is_left_alone, test_ahead_is_not_reset, test_up_to_date_is_silent,
             test_offline_does_not_block, test_write_guard_blocks_unverified,
             test_cli_entry_write_blocks, test_stamp_ttl_skips_network,
             test_dry_run_only_reports, test_cli_entry_skips_in_tests]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for i, fn in enumerate(tests):
            sub = base / f"case{i}"
            sub.mkdir()
            fn(sub)
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
