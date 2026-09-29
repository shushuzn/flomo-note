#!/usr/bin/env python3
"""cleanup.py 回归用例（H27 收尾清理）。

只跑离线逻辑：把扫描基准目录重定向到临时目录，验证
  - collect() 能同时收集**文件与目录**（曾只收文件，目录形态残留永远清不掉，
    导致 `--verify` 在残留尚存时误报「残留 = 0」——虚假通过）；
  - KEEP 命中项（含活动工作区）不被收集、不被删除；
  - 归档完整性断言按"顶层条目"比对，不再拿展开后的成员数误判；
  - 归档条目名与 tarfile 成员名同口径（反斜杠平台不得产出反斜杠条目名，
    否则完整性断言在删除循环之前恒失败，残留永远清不掉）；
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


def run_scan_pattern_cases():
    """SCAN 的 glob 规则必须覆盖真实出现过的残留形态（漏项会让 --verify 假通过）。

    注意：前面的用例会就地改写 C.SCAN，这里必须重新加载一份干净模块取真实规则。
    """
    import fnmatch

    clean = _load("cleanup_scan_src", "cleanup.py")
    pats = [pat for _base, _pats in clean.SCAN for pat in _pats]

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
    # 真正无需保留的项不该被任何规则命中（sounding_*/_tmp_* 属"命中但被 KEEP 挡住"，另论）
    check("SCAN 未误命中 flomo-push",
          not any(fnmatch.fnmatch("flomo-push", pat) for pat in pats))
    check("SCAN 未误命中 arxiv_test.xml",
          not any(fnmatch.fnmatch("arxiv_test.xml", pat) for pat in pats))


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


def run_arcname_cases():
    """归档条目名必须与 tarfile 写入成员名时的归一化口径一致。

    tarfile 存成员名时会把 os.sep 换成 '/' 并剥掉前导 '/'。若脚本生成的条目名
    不做同一归一化，Windows 上的反斜杠路径会与归档内的正斜杠名永不相等，
    而完整性断言位于删除循环之前——归档成功、删除却一步不走，残留永远清不掉。
    故直接断言条目名形态与归一化幂等性，实现一旦退回旧口径即失败。
    """
    import os
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
    candidates = C.collect()
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


if __name__ == "__main__":
    run_collect_cases()
    run_scan_pattern_cases()
    run_verify_cases()
    run_force_remove_cases()
    run_arcname_cases()
    run_archive_cases()
    print("---")
    ok = all(RESULTS)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)
