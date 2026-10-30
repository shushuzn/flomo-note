#!/usr/bin/env python3
"""retire.py 回归用例（离线，纯函数）。

退场骨架是「用户按标签批量删卡」的唯一入口，它的形态判据错一次，就会把正文
写成骨架（内容被永久压掉）或把骨架写成正文（被当成真卡留在库里）。故这里钉住：

  - 骨架固定三行：标签 + 概念名 + 「原标签/原字数」，且只有首行允许出现 `#`；
  - `parse_card` 从原文取概念名与标签：首行标签段可带多个标签，概念名取第一个
    非空且非标签的行（空行不占位）；
  - 概念名里的 `#` 被剥掉——否则骨架会被渲染成第二个标签；
  - 单级标签 `#待清理` 与 flomo_client 的放行判据同源（`is_retire_skeleton`），
    这里断言两者对同一段文本给出一致的结论，防两处各改各的。

用法：python tests/test_retire.py   退出码 0=全过，1=有失败。
"""

COVERS = ("retire",)

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, HERE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("retire_mod", "retire.py")
FC = _load("fc_mod", "flomo_client.py")

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def main():
    skeleton = R.build_skeleton("周永富案", ["时政/反腐", "政策/生态"], 500)
    check("骨架首行是待清理标签", skeleton.splitlines()[0] == "#待清理")
    check("骨架保留概念名", skeleton.splitlines()[1] == "周永富案")
    check("骨架记原标签与原字数", "原标签：时政/反腐、政策/生态" in skeleton and "原字数：500" in skeleton)
    check("骨架里除首行外不含 #", not any(l.startswith("#") for l in skeleton.splitlines()[1:]))

    dirty = R.build_skeleton("#伪标签 概念", [], 0)
    check("概念名里的 # 被剥掉", "#伪标签" not in dirty, dirty)
    check("无标签时写占位", "原标签：（无）" in dirty, dirty)

    name, tags = R.parse_card("#时政/反腐 #政策/生态\n周永富案\n\n正文第一段\n")
    check("parse_card 取概念名", name == "周永富案", name)
    check("parse_card 取标签（去掉 #）", tags == ["时政/反腐", "政策/生态"], tags)

    name2, _ = R.parse_card("#标签/二级\n\n#待清理\n真名\n")
    check("parse_card 跳过空行与标签行", name2 == "真名", name2)

    check("空正文 parse_card 不炸", R.parse_card("") == ("", []))
    check("retire 与 flomo_client 判据同源", FC.is_retire_skeleton(skeleton) is True)
    check("正文撑大的不认作骨架", FC.is_retire_skeleton(skeleton + "\n- 正文夹带") is False)
    check("标签不对的不认作骨架", FC.is_retire_skeleton("#时政/反腐\n周永富案") is False)

    passed = sum(1 for r in RESULTS if r)
    print(f"[结果] {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
