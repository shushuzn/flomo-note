#!/usr/bin/env python3
"""baseline_check.py 回归用例（离线，临时 git 仓库内进行）。

为什么单独钉：这条判据决定「本轮是按哪一版细则跑的」，误判的两个方向都危险——
把镜像旧副本判成 ok，等于放行旧细则；把落后副本判成 ok，等于带病写云。
故这里不起网络、只造本地仓库，把四种结论（ok / behind / overlay / no-git）
与「判不出就写 unknown」分别钉住。

用法：python tests/test_baseline_check.py   退出码 0=全过，1=有失败。
"""

COVERS = ("baseline_check",)

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location("baseline_check", SCRIPT_DIR / "baseline_check.py")
BC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BC)

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _make_repo(tmp: Path) -> Path:
    """造一个带一次提交、且 origin/master 指向同一提交的仓库。"""
    root = tmp / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "update-ref", "refs/remotes/origin/master", head)
    return root


def run_cases():
    tmp = Path(tempfile.mkdtemp(prefix="baseline-check-"))
    try:
        root = _make_repo(tmp)

        st = BC.inspect(root, persist_root=str(root))
        check("持久副本且与上游同引用 → ok", st["verdict"] == "ok", st["verdict"])
        check("persistent 用 resolve 后比较", st["persistent"] is True, st["persistent"])
        check("dirty 为假", st["dirty"] is False, st["dirty"])

        # 路径不同 → 判成镜像内副本
        st2 = BC.inspect(root, persist_root=str(tmp / "elsewhere"))
        check("路径不是持久副本 → overlay", st2["verdict"] == "overlay", st2["verdict"])
        check("overlay 文案点出容器重启会退回", "重启" in st2["reason"], st2["reason"])

        # 本地多一次提交、上游引用不动 → behind
        (root / "a.txt").write_text("2\n", encoding="utf-8")
        _git(root, "commit", "-q", "-am", "second")
        st3 = BC.inspect(root, persist_root=str(root))
        check("本地领先上游引用 → behind", st3["verdict"] == "behind", st3["verdict"])

        # 没有 origin 引用 → 判不出落后，但不判 ok
        _git(root, "update-ref", "-d", "refs/remotes/origin/master")
        st4 = BC.inspect(root, persist_root=str(root))
        check("缺上游引用时 behind 写 unknown", st4["behind"] == "unknown", st4["behind"])

        # 没有 git 元数据 → no-git，且不冒充 ok
        plain = tmp / "plain"
        plain.mkdir()
        st5 = BC.inspect(plain, persist_root=str(plain))
        check("无 git 元数据 → no-git", st5["verdict"] == "no-git", st5["verdict"])

        # 退出码：ok=0，其它非 0
        rc_ok = BC.main(["--root", str(root), "--persist-root", str(tmp / "elsewhere"), "--json"])
        check("overlay 时退出码非 0", rc_ok != 0, rc_ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    run_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
