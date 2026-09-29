#!/usr/bin/env python3
"""cleanup.py 回归用例（H27 收尾清理）。

只跑离线逻辑：把扫描基准目录重定向到临时目录，验证
  - collect() 能同时收集**文件与目录**（曾只收文件，目录形态残留永远清不掉，
    导致 `--verify` 在残留尚存时误报「残留 = 0」——虚假通过）；
  - KEEP 命中项（含活动工作区）不被收集、不被删除；
  - 归档完整性断言按"顶层条目"比对，不再拿展开后的成员数误判；
  - _force_remove 对文件与目录树都生效。

不触碰真实 /tmp 与项目根，全部在 tempfile 沙箱内进行。
退出码：0 = 全过。
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
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


def run_collect_cases():
    base, keep = _fresh_sandbox()
    C.SCAN = [(base, ["arxiv_*.html", "x1_body.txt", "x1_create.json",
                      "kilo_removed_archive", "flomo-*", "arxiv_test.xml"])]
    C.KEEP = {base: {"arxiv_test.xml", "flomo-push"}}
    got = {p.name for p in C.collect()}

    check("文件类残留被收集", {"arxiv_1.html", "x1_body.txt", "x1_create.json"} <= got)
    check("目录类残留被收集（不再漏扫）", "kilo_removed_archive" in got)
    check("保留目录不被收集", "flomo-push" not in got)
    check("保留文件不被收集", "arxiv_test.xml" not in got)
    check("collect 结果不含保留项", not ({"flomo-push", "arxiv_test.xml"} & got))


def run_verify_cases():
    base, _ = _fresh_sandbox()
    C.SCAN = [(base, ["arxiv_*.html", "kilo_removed_archive"])]
    C.KEEP = {base: {"flomo-push"}}
    left = C.verify()
    check("verify 在有残留时如实报出（非 0）", len(left) == 2)

    C.SCAN = [(base, ["__no_such_pattern__"])]
    check("verify 无残留时为 0", len(C.verify()) == 0)


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
    candidates = C.collect()
    check("归档前候选含文件与目录", {p.name for p in candidates} == {"a.txt", "adir"})

    archive = base / "t.tar.gz"
    want = {str(p).replace(":", "").lstrip("/") for p in candidates}
    with tarfile.open(archive, "w:gz") as tf:
        for p in candidates:
            tf.add(p, arcname=str(p).replace(":", "").lstrip("/"))
    with tarfile.open(archive) as tf:
        have = {m.name for m in tf.getmembers()}
    check("归档顶层条目齐全", not (want - have))
    check("目录展开后成员数 > 顶层数（旧断言会误报）", len(have) > len(want))


if __name__ == "__main__":
    run_collect_cases()
    run_verify_cases()
    run_force_remove_cases()
    run_archive_cases()
    print("---")
    ok = all(RESULTS)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)
