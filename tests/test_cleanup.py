#!/usr/bin/env python3
"""cleanup.py 回归用例（H27 收尾清理）。

只跑离线逻辑：把扫描基准目录重定向到临时目录，验证
  - collect() 能同时收集**文件与目录**（曾只收文件，目录形态残留永远清不掉，
    导致 `--verify` 在残留尚存时误报「残留 = 0」——虚假通过）；
  - KEEP 命中项（含活动工作区）不被收集、不被删除；
  - 归档完整性断言按"顶层条目"比对，不再拿展开后的成员数误判；
  - 归档条目名与 tarfile 成员名同口径（反斜杠平台不得产出反斜杠条目名，
    否则完整性断言在删除循环之前恒失败，残留永远清不掉）；
  - 近轮窗口：修改时间落在窗口内的项不算残留（H24/H27），窗口外的才清；
  - _force_remove 对文件与目录树都生效。

不触碰真实 /tmp 与项目根，全部在 tempfile 沙箱内进行。
退出码：0 = 全过。
"""

COVERS = ("cleanup",)

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_HERE))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _HERE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("cleanup_mod", "cleanup.py")

RESULTS = []


def check(label, cond):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {label}")


def _fresh_sandbox():
    """建一个隔离的 base 目录并在其中摆好样本，返回 (base, keep_name)。"""
    base = Path(tempfile.mkdtemp())
    # 文件类残留
    (base / "arxiv_1.html").write_text("x", encoding="utf-8")
    (base / "x1_body.txt").write_text("x", encoding="utf-8")
    (base / "x1_create.json").write_text("{}", encoding="utf-8")
    # 目录类残留（此前完全漏扫）
    d = base / "kilo_removed_archive"
    d.mkdir()
    (d / "inner.txt").write_text("x", encoding="utf-8")
    # 保留项：活动工作区目录
    keep = base / "flomo-push"
    keep.mkdir()
    (keep / "keep.txt").write_text("x", encoding="utf-8")
    # 保留项：显式文件名
    (base / "arxiv_test.xml").write_text("keep", encoding="utf-8")
    return base, keep


def run_scan_pattern_cases():
    """SCAN 的 glob 规则必须覆盖真实出现过的残留形态（漏项会让 --verify 假通过）。

    注意：前面的用例会就地改写 C.SCAN，这里必须重新加载一份干净模块取真实规则。
    """
    import fnmatch

    clean = _load("cleanup_scan_src", "cleanup.py")
    # `_tmp/` 工作区用 `*` 全收：若混进下面的扁平面孔，所有 _matches() 会恒真
    # （恒真的检查等于没有检查），故单独取出、只做专门断言。
    _TMP_DIR = clean.PROJECT_ROOT / "_tmp"
    pats = [pat for base, _pats in clean.SCAN if base != _TMP_DIR for pat in _pats]
    tmp_dir_pats = [pat for base, _pats in clean.SCAN if base == _TMP_DIR for pat in _pats]

    def _matches(name):
        return any(fnmatch.fnmatch(name, pat) for pat in pats)

    # 写卡工作区目录
    check("SCAN 覆盖 wrN 工作区目录", _matches("wr1") and _matches("wr3"))
    # 裸请求体小文件
    check("SCAN 覆盖 q*_tmp.json", _matches("q_tmp2.json") and _matches("qG_tmp.json"))
    check("SCAN 覆盖 yA_tmp.json", _matches("yA_tmp.json"))
    # 既有形态不回归
    check("SCAN 仍覆盖 *_body.txt", _matches("x1_body.txt"))
    check("SCAN 仍覆盖 kilo_removed_archive", _matches("kilo_removed_archive"))
    # 写卡草稿：内容已入云，落入窗口外即应清理（H26/H27）
    check("SCAN 覆盖 memo_body*.txt", _matches("memo_body.txt") and _matches("memo_body_A.txt"))
    # `tmp_*` 与 `_tmp_*` 同义：两种前缀都真实出现过（曾只列 `_tmp_*`，
    # 365 个 `tmp_*` 永不清理，而 --verify 复用同一 SCAN 故报「残留=0」）。
    root_pats = [pat for base, pats in clean.SCAN
                 if base == clean.PROJECT_ROOT for pat in pats]
    check("项目根规则覆盖 tmp_*", "tmp_*" in root_pats)
    check("项目根规则覆盖 _tmp_*", "_tmp_*" in root_pats)
    # 同源断言：.gitignore 认定不入库的前缀，cleanup 必须同样认得，
    # 否则「版本库干净」与「工作区干净」会长期背离而无人发现。
    ignored = [ln.strip() for ln in (clean.PROJECT_ROOT / ".gitignore")
               .read_text(encoding="utf-8").splitlines()]
    tmp_prefixes = [ln for ln in ignored if ln in ("tmp_*", "_tmp_*")]
    check(".gitignore 与 SCAN 对临时前缀口径一致",
          bool(tmp_prefixes) and all(p in root_pats for p in tmp_prefixes))
    # 真正无需保留的项不该被任何规则命中（sounding_*/_tmp_* 属"命中但被 KEEP 挡住"，另论）
    check("SCAN 未误命中 flomo-push",
          not any(fnmatch.fnmatch("flomo-push", pat) for pat in pats))
    check("SCAN 未误命中 arxiv_test.xml",
          not any(fnmatch.fnmatch("arxiv_test.xml", pat) for pat in pats))
    # `_tmp/` 目录形态此前一条规则都不命中，堆了 580 个历轮产物仍报「残留 0」
    check("SCAN 覆盖 _tmp 工作区目录", bool(tmp_dir_pats))


def run_collect_cases():
    base, _keep = _fresh_sandbox()
    C.SCAN = [(base, ["arxiv_*.html", "x1_body.txt", "x1_create.json",
                      "kilo_removed_archive", "flomo-*", "arxiv_test.xml"])]
    C.KEEP = {base: {"arxiv_test.xml", "flomo-push"}}
    got = {p.name for p in C.collect(0)}  # 关闭近轮窗口，专测收集逻辑

    check("文件类残留被收集", {"arxiv_1.html", "x1_body.txt", "x1_create.json"} <= got)
    check("目录类残留被收集（不再漏扫）", "kilo_removed_archive" in got)
    check("保留目录不被收集", "flomo-push" not in got)
    check("保留文件不被收集", "arxiv_test.xml" not in got)
    check("collect 结果不含保留项", not ({"flomo-push", "arxiv_test.xml"} & got))


def run_verify_cases():
    base, _ = _fresh_sandbox()
    C.SCAN = [(base, ["arxiv_*.html", "kilo_removed_archive"])]
    C.KEEP = {base: {"flomo-push"}}
    left = C.verify(0)
    check("verify 在有残留时如实报出（非 0）", len(left) == 2)

    C.SCAN = [(base, ["__no_such_pattern__"])]
    check("verify 无残留时为 0", len(C.verify(0)) == 0)


def run_force_remove_cases():
    base = Path(tempfile.mkdtemp())
    f = base / "f.txt"
    f.write_text("x", encoding="utf-8")
    C._force_remove(f)
    check("_force_remove 删除文件", not f.exists())

    d = base / "sub"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    (d / "nested").mkdir()
    (d / "nested" / "b.txt").write_text("x", encoding="utf-8")
    C._force_remove(d)
    check("_force_remove 递归删除目录树", not d.exists())


def run_recent_window_cases():
    """近轮窗口：窗口内的项不算残留（H24/H27），窗口外的才清。"""
    base = Path(tempfile.mkdtemp())
    old = base / "z_old_tmp.txt"
    new = base / "z_new_tmp.txt"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))  # 推到窗口之外

    C.SCAN = [(base, ["z_*_tmp.txt"])]
    C.KEEP = {}
    skipped = []
    got = {p.name for p in C.collect(24 * 60, skipped)}

    check("窗口外残留被收集", got == {"z_old_tmp.txt"})
    check("窗口内残留被跳过", "z_new_tmp.txt" not in got)
    check("被跳过的项可读出（可见性，不静默放行）",
          {p.name for p in skipped} == {"z_new_tmp.txt"})
    check("窗口关闭（0）时全部参与清理",
          {p.name for p in C.collect(0)} == {"z_old_tmp.txt", "z_new_tmp.txt"})
    check("verify 与 collect 同口径，不把窗口内项算作残留",
          [p.name for p in C.verify(24 * 60)] == ["z_old_tmp.txt"])


def run_arcname_cases():
    """归档条目名必须与 tarfile 写入成员名时的归一化口径一致。

    tarfile 存成员名时会把 os.sep 换成 '/' 并剥掉前导 '/'。若脚本生成的条目名
    不做同一归一化，Windows 上的反斜杠路径会与归档内的正斜杠名永不相等，
    而完整性断言位于删除循环之前——归档成功、删除却一步不走，残留永远清不掉。
    故直接断言条目名形态与归一化幂等性，实现一旦退回旧口径即失败。
    """
    base = Path(tempfile.mkdtemp())
    p = base / "a.txt"
    p.write_text("x", encoding="utf-8")
    name = C._arcname(p)

    check("_arcname 不含反斜杠（跨平台归一化）", "\\" not in name)
    check("_arcname 不含盘符冒号", ":" not in name)
    check("_arcname 无前导斜杠", not name.startswith("/"))
    check("_arcname 对 tarfile 口径幂等",
          name == name.replace(os.sep, "/").lstrip("/"))

    d = base / "adir"
    d.mkdir()
    check("_arcname 目录形态同样不含反斜杠", "\\" not in C._arcname(d))


def run_archive_cases():
    """归档断言：顶层条目必须齐全，目录展开不得误判为缺失。"""
    import tarfile
    base = Path(tempfile.mkdtemp())
    (base / "a.txt").write_text("x", encoding="utf-8")
    d = base / "adir"
    d.mkdir()
    (d / "inner.txt").write_text("x", encoding="utf-8")

    C.SCAN = [(base, ["a.txt", "adir"])]
    C.KEEP = {}
    candidates = C.collect(0)  # 关闭近轮窗口，专测归档逻辑
    check("归档前候选含文件与目录", {p.name for p in candidates} == {"a.txt", "adir"})

    archive = base / "t.tar.gz"
    want = {C._arcname(p) for p in candidates}
    with tarfile.open(archive, "w:gz") as tf:
        for p in candidates:
            tf.add(p, arcname=C._arcname(p))
    with tarfile.open(archive) as tf:
        have = {m.name for m in tf.getmembers()}
    check("归档顶层条目齐全", not (want - have))
    check("目录展开后成员数 > 顶层数（旧断言会误报）", len(have) > len(want))
    check("归档成员名不含反斜杠", not any("\\" in n for n in have))


def run_prune_cases():
    """保留策略：归档按数量收敛、凭证按时长收敛，dry-run 不动文件。"""
    base = Path(tempfile.mkdtemp())
    trash = base / "trash"
    gate = base / "gate"
    trash.mkdir()
    gate.mkdir()

    now = time.time()
    for i in range(12):
        p = trash / f"temp-cleanup-2026010{i}-000000.tar.gz"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (now - (12 - i) * 60, now - (12 - i) * 60))
    old_cred = gate / "old.json"
    new_cred = gate / "new.json"
    old_cred.write_text("{}", encoding="utf-8")
    new_cred.write_text("{}", encoding="utf-8")
    os.utime(old_cred, (now - 48 * 3600, now - 48 * 3600))

    C.TRASH_DIR, C.GATE_DIR = trash, gate
    try:
        dry_a, dry_c = C.prune(dry_run=True)
        check("dry-run 只报不动（归档仍在）",
              len(dry_a) == 2 and len(list(trash.glob("*.tar.gz"))) == 12)
        check("dry-run 只报不动（凭证仍在）", len(dry_c) == 1 and old_cred.exists())

        gone_a, gone_c = C.prune(dry_run=False)
        remain = sorted(p.name for p in trash.glob("*.tar.gz"))
        check("归档只留最近 10 个", len(remain) == 10 and len(gone_a) == 2)
        check("删的是最旧的两个", {p.name for p in gone_a}
              == {"temp-cleanup-20260100-000000.tar.gz",
                  "temp-cleanup-20260101-000000.tar.gz"})
        check("超期凭证被删、未超期保留",
              len(gone_c) == 1 and not old_cred.exists() and new_cred.exists())
    finally:
        C.TRASH_DIR = C.PROJECT_ROOT / ".workbuddy" / "trash"
        C.GATE_DIR = C.PROJECT_ROOT / ".sop_gate"


def run_window_boundary_cases():
    """一轮里跨过窗口边界的项：collect 与 verify 必须共用同一个基准时刻。

    实测缺陷：`_tmp/it_283.txt` 距 24 小时边界只差 1 秒，collect() 把它当近轮
    跳过、没列进候选；打包 24 个文件耗掉几秒后 verify() 重新取 `time.time()`，
    该项已出窗口 → 删除明明成功，却被断言判成「残留 1 个」。检查与被检查对象
    用了两个时钟，等于让运行时长决定结论。
    """
    base = Path(tempfile.mkdtemp())
    f = base / "b_edge.txt"
    f.write_text("x", encoding="utf-8")
    t0 = time.time()
    edge = t0 - 24 * 3600 + 1  # 距边界 1 秒，仍在窗口内
    os.utime(f, (edge, edge))

    C.SCAN = [(base, ["b_edge.txt"])]
    C.KEEP = {}
    check("距边界 1 秒的项按近轮跳过", C.collect(24 * 60, now=t0) == [])
    check("同一基准时刻下 verify 不误报残留", C.verify(24 * 60, now=t0) == [])
    check("基准时刻后移 10 秒才成为候选（故必须共用 now）",
          [p.name for p in C.collect(24 * 60, now=t0 + 10)] == ["b_edge.txt"])


def run_cross_process_stamp_cases():
    """收尾是「清理 → `--verify`」两个进程，各取 `time.time()` 会让边界文件翻转。

    进程内共用 now 只解决了「一轮里打包耗时」那半；实测跨进程仍会翻：两个文件
    mtime 距边界 11 秒，清理报 0，十几秒后 `--verify` 报「残留 2 个」。故清理落
    基准时间戳、复核读它——否则「残留不是 0」会变成可忽略的噪声，检查就废了。
    """
    base = Path(tempfile.mkdtemp())
    f = base / "c_edge.txt"
    f.write_text("x", encoding="utf-8")
    t0 = time.time()
    edge = t0 - 24 * 3600 + 1          # 距边界 1 秒，清理那一刻仍在窗口内
    os.utime(f, (edge, edge))

    C.SCAN = [(base, ["c_edge.txt"])]
    C.KEEP = {}
    orig_stamp = C.STAMP_FILE
    C.STAMP_FILE = base / "stamp"
    try:
        check("跨进程：清理口径下跳过（无候选）", C.collect(24 * 60, now=t0) == [])
        check("跨进程：若各取当前时刻，10 秒后即成为候选",
              [p.name for p in C.collect(24 * 60, now=t0 + 10)] == ["c_edge.txt"])

        C._write_stamp(t0)
        ts, why = C._read_stamp()
        check(f"跨进程：复核读回清理基准（{ts}）", ts == float(int(t0)))
        check("跨进程：沿用基准后不再判残留",
              ts is not None and C.collect(24 * 60, now=ts) == [])
        check(f"跨进程：输出写明沿用几秒前（{why}）", "沿用上次清理时刻" in why)

        C._write_stamp(t0 - 7200)
        ts2, why2 = C._read_stamp()
        check(f"跨进程：过期记录不采用（防拿旧基准放宽窗口）[{ts2} {why2}]",
              ts2 is None and "已过期" in why2)

        C.STAMP_FILE.write_text("不是数字", encoding="utf-8")
        ts3, why3 = C._read_stamp()
        check(f"跨进程：记录损坏退回当前时刻（{why3}）",
              ts3 is None and "无上次清理记录" in why3)
    finally:
        C.STAMP_FILE = orig_stamp


def run_verify_command_cases():
    """整条命令：`--verify` 走 stamp 口径，边界文件不再被报成残留。"""
    import contextlib
    import io

    base = Path(tempfile.mkdtemp())
    f = base / "d_edge.txt"
    f.write_text("x", encoding="utf-8")
    t0 = time.time()
    edge = t0 - 24 * 3600 + 1
    os.utime(f, (edge, edge))

    C.SCAN = [(base, ["d_edge.txt"])]
    C.KEEP = {}
    orig_stamp, orig_trash, orig_gate = C.STAMP_FILE, C.TRASH_DIR, C.GATE_DIR
    C.STAMP_FILE = base / "stamp"
    C.TRASH_DIR = base / "trash"
    C.GATE_DIR = base / "gate"
    C._write_stamp(t0)
    try:
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["cleanup.py", "--verify"]
        code = 0
        try:
            with contextlib.redirect_stdout(buf):
                C.main()
        except SystemExit as exc:
            code = exc.code or 0
        finally:
            sys.argv = old_argv
        out = buf.getvalue()
        check(f"--verify 命令：残留 0 且退出码 0（{code}）",
              code == 0 and "残留 = 0" in out)
        check("--verify 命令：打印所用基准", "沿用上次清理时刻" in out)

        # 残留非 0 时必须**返回** 1，而不是抛 SystemExit：库函数结束进程会让
        # 「import cleanup 后调 main()」的调用方被掀掉，拿不到可判断的返回值
        # （__main__ 处的 sys.exit(main()) 才负责把它转成进程退出码）。
        # mtime 取窗口外（早 25 小时）：窗口内的项会被当近轮素材保留，
        # 那样 left 为空，这条用例会假通过。
        residue = base / "d_residue.txt"
        residue.write_text("x", encoding="utf-8")
        old = t0 - 25 * 3600
        os.utime(residue, (old, old))
        C.SCAN = [(base, ["d_edge.txt", "d_residue.txt"])]
        buf3 = io.StringIO()
        sys.argv = ["cleanup.py", "--verify"]
        try:
            with contextlib.redirect_stdout(buf3):
                code3 = C.main()
            raised = False
        except SystemExit:
            code3, raised = None, True
        finally:
            sys.argv = old_argv
        out3 = buf3.getvalue()
        check(f"--verify 有残留：main() 返回 1 且不抛 SystemExit（得 {code3!r}，抛错 {raised}）",
              code3 == 1 and not raised)
        check("--verify 有残留：报出应清未清的清单", "d_residue.txt" in out3)

        # dry-run 没真清理，不能落基准（否则复核会拿一次没删过东西的时刻判窗口）
        C.STAMP_FILE.unlink()
        buf2 = io.StringIO()
        sys.argv = ["cleanup.py", "--dry-run"]
        try:
            with contextlib.redirect_stdout(buf2):
                C.main()
        finally:
            sys.argv = old_argv
        check(f"--dry-run 不落基准（{C.STAMP_FILE}）", not C.STAMP_FILE.exists())
    finally:
        C.STAMP_FILE, C.TRASH_DIR, C.GATE_DIR = orig_stamp, orig_trash, orig_gate


if __name__ == "__main__":
    run_collect_cases()
    run_scan_pattern_cases()
    run_verify_cases()
    run_force_remove_cases()
    run_recent_window_cases()
    run_window_boundary_cases()
    run_cross_process_stamp_cases()
    run_verify_command_cases()
    run_arcname_cases()
    run_archive_cases()
    run_prune_cases()
    print("---")
    ok = all(RESULTS)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)
