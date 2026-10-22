#!/usr/bin/env python3
"""scan_concept_duplication.py 回归用例（离线，不联网、不读云端）。

只验证 normalize 这一个纯函数——它是「同一对象被切成平行卡」的全部判据：
抹掉切片限定后键相同，就判为同一对象的平行卡。断言分三类：

  - 各种切片形态都被抹干净（括号年份、括号内年份带修饰、代次、阶段号）；
  - **无切片的对象名一个字符都不许动**——多抹一个字，两张无关的卡就会撞到
    同一个键，报出一组不存在的重复，而报告看上去完全正常；
  - 区间年份整段吃掉：早期只把 `2023-2024` 匹到 `2023-20`，残留的 `24` 被当成
    对象名的一部分，于是同一对象的两种写法归一成不同键（漏报）、不同对象的
    残串又可能撞键（误报），两种错都长得像真发现。

用法：python tests/test_scan_concept_duplication.py   退出码 0=全过，1=有失败。
"""

COVERS = ("scan_concept_duplication",)

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location("scan_dup_mod", HERE / "scan_concept_duplication.py")
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def run_cases():
    # 1) 被括号包裹的切片：括号本身也要一起剥掉，不能剩一对空括号当对象名
    check("括号包裹的年份被抹掉", D.normalize("RAG（2024）") == "RAG", D.normalize("RAG（2024）"))
    check("括号内年份带修饰一并抹掉", D.normalize("GPT-4（2024 修订）") == "GPT-4",
          D.normalize("GPT-4（2024 修订）"))
    check("括号包裹的代次切片被抹掉", D.normalize("Transformer（第 2 代）") == "Transformer",
          D.normalize("Transformer（第 2 代）"))

    # 2) 不带括号的切片
    check("阶段号被抹掉", D.normalize("RAG 第一阶段") == "RAG", D.normalize("RAG 第一阶段"))
    check("裸年份被抹掉", D.normalize("2024 的 RAG 落地") == "的 RAG 落地",
          D.normalize("2024 的 RAG 落地"))

    # 3) 回归：年份区间不得留下半截数字
    got = D.normalize("2023-2024 的 RAG 落地")
    check("区间年份整段吃掉，不残留半截年份", not any(c.isdigit() for c in got), got)
    check("区间两端写法归一后同键",
          D.normalize("2023-2024 的 RAG 落地") == D.normalize("2024 的 RAG 落地"),
          (D.normalize("2023-2024 的 RAG 落地"), D.normalize("2024 的 RAG 落地")))

    # 4) 无切片的名字必须原样返回：误删一个字就会把两张无关的卡并成一簇
    for name in ("RAG 检索", "向量数据库", "混合专家模型", "GPT-4 模型"):
        check(f"无切片的名字不被改动：{name}", D.normalize(name) == name, D.normalize(name))

    # 5) 同一对象的不同切片写法归到同一个键——这正是本脚本存在的目的
    a, b = D.normalize("RAG（2024）"), D.normalize("RAG 第一阶段")
    check("不同切片的同一对象归一到同键", a == b == "RAG", (a, b))

    # 6) 空输入不炸（调用方可能拿到缺 content 的卡）
    check("空字符串返回空串", D.normalize("") == "", D.normalize(""))


if __name__ == "__main__":
    run_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
