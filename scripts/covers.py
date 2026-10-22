#!/usr/bin/env python3
"""脚本-用例覆盖关系的唯一解析实现（读用例里的 `COVERS` 声明）。

**为什么会独立成一个模块**：两个地方都要回答「这个脚本有没有用例」——`check_skill_docs`
（闸门，不给用例就红）与 `console_data`（控制台「工具」页的 `has_test` 列）。两处各写
一份解析，就会出现同一事实两个出处、两个相反结论：闸门认 `serve_console.py` 有
`test_console_serve.py` 覆盖，页面按「文件名是否等于 test_<脚本>.py」判却说「没有回归
覆盖」，而页面那个更早被人看到，会先把人带偏。

判据的口径（「声明，不是命名巧合」）只在 `check_skill_docs` 里讲一遍，本模块只负责
把它变成可复用的解析。**只依赖标准库**：调用方一个是闸门、一个是控制台数据层，
谁都不该为读一段源码引入网络或云端依赖。
"""
from __future__ import annotations

import ast
from pathlib import Path

COVERS_VAR = "COVERS"


def declared_covers(path: Path):
    """读用例模块顶层的 `COVERS = (...)`，返回 `(脚本名元组, 问题列表, 行号区间)`。

    `COVERS` 的语义是「本文件**验证了**哪些脚本的行为」，不是「import 了哪些」。
    「没声明」与「声明为空元组」严格区分：前者是漏写（报问题），后者是明确表示
    本文件不针对具体脚本（跨模块约束、前后端对齐这类）。

    行号区间供调用方把声明本身从源码里挖掉后再核「有没有提到这个模块」——否则
    声明里那几个字符串会让那条核对恒真（声明写什么，源码里就有什么）。
    """
    try:
        src = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
    except (OSError, UnicodeDecodeError, SyntaxError) as e:
        return (), [f"{path.name}: 无法解析（{type(e).__name__}），覆盖声明读不到"], None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == COVERS_VAR for t in node.targets):
            continue
        span = (node.lineno, node.end_lineno or node.lineno)
        val = node.value
        if not isinstance(val, (ast.Tuple, ast.List)):
            return (), [f"{path.name}: `{COVERS_VAR}` 须是元组或列表字面量"
                        f"（当前是 {type(val).__name__}）——动态拼出来的声明核不了"], None
        names = []
        for elt in val.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                return (), [f"{path.name}: `{COVERS_VAR}` 里只能写字面量字符串"
                            f"（脚本名，不带 .py 与路径）"], None
            names.append(elt.value)
        return tuple(names), [], span
    return (), [
        f"{path.name}: 缺少模块级 `{COVERS_VAR} = (...)` 声明——"
        f"「这个用例测的是哪个脚本」不能靠文件名猜（同名约定在本仓库已多处不成立），"
        f"须显式写出；确实不针对具体脚本的写 `{COVERS_VAR} = ()`"
    ], None


def coverage_map(tests_dir: Path, known=None):
    """聚合整个用例目录的声明，返回 `(脚本名 -> 用例文件名列表, 问题列表)`。

    `known` 是可选的脚本名全集。给了就顺带判「声明指向不存在的脚本」——**必须在
    计入 covered 之前判**：不存在的脚本谈不上被覆盖，先当成假覆盖踢掉的话，
    「改名后声明悬空」这条永远不会被报出来，报出来的是另一句更含糊的话。
    不给则不判（控制台只关心已存在的脚本，多报一条它也不知道该显示到哪一行）。

    其余问题都在这一层判定：缺声明、声明不是字面量、声明之外文件里没提过该模块
    （「只声明不测」）。
    """
    covered: dict[str, list[str]] = {}
    problems: list[str] = []
    if not tests_dir.is_dir():
        return covered, problems
    for t in sorted(tests_dir.glob("test_*.py")):
        names, probs, span = declared_covers(t)
        problems += probs
        if not names:
            continue
        try:
            src = t.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            problems.append(f"{t.name}: 读取失败，覆盖声明无法与源码对照")
            continue
        # 把声明那几行挖掉再核：声明里就写着这几个名字，带着它核等于恒真。
        body_src = src
        if span:
            lines = src.splitlines()
            start, end = span
            body_src = "\n".join(lines[: start - 1] + lines[end:])
        for n in names:
            if known is not None and n not in known:
                problems.append(
                    f"{t.name}: `{COVERS_VAR}` 里的 `{n}` 在 scripts/ 下不存在"
                    f"——脚本改名或拼错，这条声明指空了"
                )
                continue
            if not _mentions(body_src, n):
                problems.append(
                    f"{t.name}: 声称覆盖 `{n}`，但声明之外整个文件里没出现过这个名字"
                    f"——声明与实现对不上，覆盖是假的"
                )
                continue
            covered.setdefault(n, []).append(t.name)
    return covered, problems


def _mentions(src: str, name: str) -> bool:
    """`name` 是否作为独立标识符出现在源码里。

    宽松是刻意的：判据的用途只是拦「声明了一个毫不相干的模块」，不是验证加载形态。
    用例把被测模块 import 进来、拼出 `<name>.py` 路径、或通过 helper 动态加载，
    三种写法都算——按加载写法去认，改一次加载方式就会误报一片。
    """
    import re

    return re.search(rf"\b{re.escape(name)}\b", src) is not None
