#!/usr/bin/env python3
"""test_layering.py — 模块依赖的分层约束（解耦，不是审美）。

为什么需要它：`flomo_client.py` 曾同时承担两件事——**云端通道**（握手、token、MCP
协议、写后回读）与**命令行契约**（选项解析、参数转换、入口出口）。于是
`fetch_article.py`（纯网页抓取，与 flomo 无关）也得 import 这个云端模块，只为拿
四个命令行函数；云端契约一变，它就被牵连，而这件事**不会让任何东西报错**。

一次拆分只解决当下这一处，解决不了下一次：只要「从哪 import 都行」，需要参数解析的人还会
顺手写成 `from flomo_client import Opt`，耦合就长回来，且同样不报错。故把四条约束
做成用例，让「退回去」变成红的：

  1. **叶子层**：`cli_util.py` 不 import 任何项目内模块（能只靠标准库回答的事，
     就不该把别人拖进来）。
  2. **无环**：项目内 import 图不得成环。环一出现，两个模块就没法单独改、单独测。
  3. **借道云端必须真用云端**：import 了 `flomo_client` 的模块，必须用到它**独有的**
     API（名单由两个模块的顶层定义相减得出，不手抄）。只借 `Opt`/`cli_entry` 这类
     已下沉到 `cli_util` 的东西，说明依赖本可以不存在。
  4. **导入即使用**：项目内的 import 必须在该文件里被用到。用不到的 import 就是
     一条没人需要的依赖，只是没人发现。

全部离线、纯源码扫描（ast），不执行被测模块。
"""

COVERS = ()

import ast
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


def _modules():
    """项目内模块名 → 源码路径（scripts/ 下的 .py）。"""
    return {p.stem: p for p in sorted(SCRIPT_DIR.glob("*.py"))}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _local_imports(tree, known):
    """收集一条 import 语句里属于项目内的部分：[(模块名, 绑定名, 语句)]。

    `from x import a, b` → 两个绑定名；`import x as y` → 一个绑定名 y（未起别名时
    绑定名就是模块名本身，引用它会写成 `x.foo`）。
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in known:
                    out.append((root, alias.asname or root, node))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入不参与（本项目无包结构）
                continue
            mod = (node.module or "").split(".")[0]
            if mod in known:
                for alias in node.names:
                    out.append((mod, alias.asname or alias.name, node))
    return out


def _defined_names(tree):
    """模块顶层定义的类 / 函数 / 常量名（判断「独有 API」用）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def run_leaf_layer():
    """1) cli_util 是叶子：不 import 任何项目内模块。"""
    known = _modules()
    tree = _tree(known["cli_util"])
    imports = _local_imports(tree, known)
    check("cli_util.py 不 import 任何项目内模块", not imports,
          "；".join(f"{m}->{n}" for m, n, _ in imports))

    # 它的用途就是「纯本地脚本不必牵出云端」：自己反过来不能依赖任何人
    src = known["cli_util"].read_text(encoding="utf-8")
    check("cli_util.py 不出现云端通道的符号",
          "FlomoClient" not in src and "load_token" not in src)


def run_no_cycle():
    """2) 项目内 import 图无环（含自引用）。"""
    known = _modules()
    graph = {name: {m for m, _n, _node in _local_imports(_tree(path), known)}
             for name, path in known.items()}

    def find_cycle():
        state = {}  # 0=未访问 1=在栈上 2=已完成
        stack = []

        def dfs(node):
            state[node] = 1
            stack.append(node)
            for nxt in sorted(graph.get(node, ())):
                if state.get(nxt, 0) == 0:
                    hit = dfs(nxt)
                    if hit:
                        return hit
                elif state.get(nxt) == 1:
                    return stack[stack.index(nxt):] + [nxt]
            stack.pop()
            state[node] = 2
            return None

        for node in sorted(graph):
            if state.get(node, 0) == 0:
                hit = dfs(node)
                if hit:
                    return hit
        return None

    cyc = find_cycle()
    check("项目内 import 图无环", cyc is None, " -> ".join(cyc or []))


def run_cloud_import_needs_cloud_api():
    """3) 借道云端通道，必须真用它的**独有** API。

    独有 = `flomo_client` 顶层定义 减去 `cli_util` 顶层定义。两边同名的符号说明它
    已被下沉（如 `cli_entry`），从云端模块 import 它只是把依赖留在了原地。
    """
    known = _modules()
    cloud = _defined_names(_tree(known["flomo_client"]))
    cli = _defined_names(_tree(known["cli_util"]))
    cloud_only = cloud - cli

    offenders = []
    for name, path in known.items():
        if name in ("flomo_client", "cli_util"):
            continue
        tree = _tree(path)
        bound = [b for mod, b, _n in _local_imports(tree, known) if mod == "flomo_client"]
        if not bound:
            continue
        src = path.read_text(encoding="utf-8")
        # 直接 import 的名字里有没有云端独有 API；`import flomo_client as FC` 则看
        # 源码里有没有 `FC.<云端独有>` 的属性访问
        used_cloud = any(b in cloud_only for b in bound)
        if not used_cloud:
            attrs = {f"{b}.{sym}" for b in bound for sym in cloud_only}
            used_cloud = any(a in src for a in attrs)
        if not used_cloud:
            offenders.append(f"{name} 只用到了 [{', '.join(sorted(set(bound)))}]")
    check("import flomo_client 的模块都用到云端独有 API（不只借道）", not offenders,
          "；".join(offenders))


def run_imports_are_used():
    """4) 项目内的 import 必须在该文件里被用到。"""
    known = _modules()
    offenders = []
    for name, path in known.items():
        tree = _tree(path)
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        # 把 import 语句自身那几行去掉，避免「import 行里出现过这个名字」被算成使用
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
        rest = "\n".join(
            line for i, line in enumerate(lines, 1)
            if not any(a <= i <= b for a, b in spans)
        )
        for mod, bound, _node in _local_imports(tree, known):
            # 按标识符边界找：`FC` 不该被 `FCX` 命中
            used = bound in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rest)
            if not used:
                offenders.append(f"{name} 导入了 {mod}.{bound} 却没用")
    check("项目内 import 都在文件里被用到", not offenders, "；".join(offenders))


def run_layering_documented():
    """5) 分层不是口头约定：README 得说清 cli_util 的位置与边界。"""
    readme = (SCRIPT_DIR.parent / "README.md").read_text(encoding="utf-8")
    check("README 收录 cli_util.py", "`cli_util.py`" in readme)
    check("README 说明它只依赖标准库", "只依赖标准库" in readme)


def main():
    run_leaf_layer()
    run_no_cycle()
    run_cloud_import_needs_cloud_api()
    run_imports_are_used()
    run_layering_documented()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
