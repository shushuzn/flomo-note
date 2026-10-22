#!/usr/bin/env python3
"""covers.py 回归用例（离线，临时目录内进行）。

`covers.py` 是「脚本有没有用例」的唯一解析实现，闸门（`check_skill_docs`）与控制台
（`console_data` 的 `has_test`）都读它。它的判据错一处，两处会同时错，而且错法都是
静默的——所以这里把解析的每条边界都钉住：

  - 「没声明」与「声明为空元组」必须分开（前者是漏写，后者是有意）；
  - 声明只收字面量（变量、拼接、非字符串一律判问题，核不了的不装作核过）；
  - 声明之外文件里没提到该模块 = 只声明不测，判为假覆盖；
  - 解析失败（语法错、读不到）报问题而不是抛——调用方是闸门与控制台页面，
    一个坏文件不该让整张页面打不开。

用法：python tests/test_covers.py   退出码 0=全过，1=有失败。
"""

COVERS = ("covers",)

import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location("covers_mod", HERE / "covers.py")
CV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CV)

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def write(dirpath: Path, name: str, body: str) -> Path:
    p = dirpath / name
    p.write_text(body, encoding="utf-8")
    return p


def run_declared_cases(td: Path):
    # 1) 正常声明：名字、无问题、行号区间
    p = write(td, "t_ok.py", '"""用例。"""\n\nCOVERS = ("alpha", "beta")\n')
    names, probs, span = CV.declared_covers(p)
    check("正常声明解析出名字", names == ("alpha", "beta"), names)
    check("正常声明没有解析问题", probs == [], probs)
    check("行号区间指向声明所在行", span == (3, 3), span)

    # 2) 多行元组：区间要覆盖整段，否则「挖掉声明」只挖一半，核对又会变恒真
    p = write(td, "t_multi.py", 'COVERS = (\n    "alpha",\n    "beta",\n)\n')
    names, probs, span = CV.declared_covers(p)
    check("多行声明同样解析", names == ("alpha", "beta"), names)
    check("多行声明的行号区间覆盖整段", span == (1, 4), span)

    # 3) 空元组 = 有意的「不针对具体脚本」，与「没声明」严格区分
    p = write(td, "t_empty.py", "# 跨模块约束\nCOVERS = ()\n")
    names, probs, span = CV.declared_covers(p)
    check("空元组是合法声明", names == () and probs == [], (names, probs))

    p = write(td, "t_none.py", '"""没声明。"""\n\nimport os\n')
    names, probs, span = CV.declared_covers(p)
    hit = names == () and len(probs) == 1 and "COVERS" in probs[0]
    check("没声明被判为问题（与空元组区分）", hit, probs)

    # 4) 非字面量：动态拼出来的声明核不了，必须判问题而不是当成空声明放行
    p = write(td, "t_var.py", 'NAMES = ("alpha",)\nCOVERS = NAMES\n')
    _, probs, _ = CV.declared_covers(p)
    check("声明是变量时判问题", len(probs) == 1 and "字面量" in probs[0], probs)

    p = write(td, "t_nonstr.py", "COVERS = ('alpha', 3)\n")
    _, probs, _ = CV.declared_covers(p)
    check("声明里有非字符串时判问题", len(probs) == 1, probs)

    p = write(td, "t_syntax.py", "COVERS = ('alpha'\n")
    names, probs, span = CV.declared_covers(p)
    check("语法错的文件报问题而不抛", names == () and len(probs) == 1, probs)


def run_map_cases(td: Path):
    tdir = td / "tests"
    tdir.mkdir()

    write(tdir, "test_a.py", 'COVERS = ("alpha", "beta")\nimport alpha\nimport beta\n')
    write(tdir, "test_b.py", 'COVERS = ("beta",)\nfrom beta import thing\n')
    covered, probs = CV.coverage_map(tdir)
    check("聚合多个用例的声明", sorted(covered) == ["alpha", "beta"], sorted(covered))
    check("同一脚本被多处认领时记全", len(covered["beta"]) == 2, covered["beta"])
    check("没有问题时问题列表为空", probs == [], probs)

    # 声明之外没提到 → 只声明不测
    write(tdir, "test_c.py", 'COVERS = ("gamma",)\n')
    covered, probs = CV.coverage_map(tdir)
    hit = any("gamma" in m and "没出现过" in m for m in probs)
    check("声明之外没提到该模块 = 假覆盖", hit, probs)
    check("假覆盖不计入已覆盖", "gamma" not in covered, sorted(covered))

    # 用文件名形态提到（动态加载）也算提到——判据不该绑死加载写法
    write(tdir, "test_c.py", 'COVERS = ("gamma",)\nSPEC = "gamma.py"\n')
    covered, _ = CV.coverage_map(tdir)
    check("以 <脚本>.py 字面量提到也算", "gamma" in covered, sorted(covered))

    # 只认 test_*.py，别的不该被当作用例
    write(tdir, "helper.py", 'COVERS = ("delta",)\nimport delta\n')
    covered, _ = CV.coverage_map(tdir)
    check("非 test_*.py 不参与聚合", "delta" not in covered, sorted(covered))

    # 目录不存在：返回空而不是抛（控制台在缺 tests/ 时也要能渲染）
    covered, probs = CV.coverage_map(td / "nope")
    check("用例目录不存在时返回空", covered == {} and probs == [], (covered, probs))


def main():
    with tempfile.TemporaryDirectory() as d:
        run_declared_cases(Path(d))
        run_map_cases(Path(d))
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
