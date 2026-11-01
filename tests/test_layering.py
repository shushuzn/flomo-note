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
    """解析脚本源码；语法错误时报出**哪个文件**，而不是一条裸 SyntaxError。

    本文件扫 scripts/ 下全部 .py，缺文件名时「30 个里哪个写错了」得靠猜——
    而猜的那几分钟里，人往往先怀疑契约本身。
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        raise SyntaxError(f"{path.name} 语法错误（第 {e.lineno} 行）：{e.msg}") from None


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
                    return [*stack[stack.index(nxt):], nxt]
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


def _insert_target_meaning(expr, bindings, seen=frozenset()):
    """静态求值 `sys.path.insert(...)` 第二个参数指向哪里。

    返回 "self_dir"（本文件所在目录）或 "other"。只认**正面定义**的两种形态：
    ① 直接写 `Path(__file__).resolve().parent`（`.resolve()` 可省）；
    ② 写一个模块级名字，而那个名字的绑定就是 ①。
    其余一律 "other"——不靠「像不像路径」猜，猜错就会放行一个假的自备引导。

    `seen` 记已展开过的绑定名，防 `A = B; B = A` 式的自定义环把递归绕死
    （深度上限兜同样的底，但环要靠 seen 才判得准）。
    """
    if len(seen) > 8:
        return "other"
    if isinstance(expr, ast.Call):
        fn = expr.func
        # str(x) / Path(x) 透传内涵
        if isinstance(fn, ast.Name) and fn.id in ("str", "Path") and len(expr.args) == 1:
            return _insert_target_meaning(expr.args[0], bindings, seen)
        # x.resolve() 仍是同一个文件
        if isinstance(fn, ast.Attribute) and fn.attr == "resolve" and not expr.args:
            return _insert_target_meaning(fn.value, bindings, seen)
        return "other"
    if isinstance(expr, ast.Attribute) and expr.attr == "parent":
        if _insert_target_meaning(expr.value, bindings, seen) == "self_file":
            return "self_dir"
        return "other"
    if isinstance(expr, ast.Name):
        if expr.id == "__file__":
            return "self_file"
        if expr.id in bindings and expr.id not in seen:
            return _insert_target_meaning(bindings[expr.id], bindings, seen | {expr.id})
    return "other"


def run_self_bootstrapping_imports():
    """6) 凡导入兄弟模块的 scripts/*.py，必须自备 sys.path 引导。

    直接执行时 Python 会把脚本目录摆进 sys.path，但这条便利**只给直接执行**：
    `runpy.run_path` 与 `importlib` 加载都不摆。于是「靠调用方摆好路径」的脚本
    换一种加载方式就 ModuleNotFoundError，而它在现有用例里一声不响——每个用例
    文件自己插了路径，正好替它兜住。修复前实测：
        runpy.run_path("scripts/rss_store.py")  →  ModuleNotFoundError: cli_util
    四个文件（publish / rss_store / rss_fetch / rss_batch）踩过，已补引导。

    为什么只管 scripts/ 不管 tests/：用例文件缺引导会在收集阶段就炸（响亮的
    ModuleNotFoundError），而脚本缺引导是**静默的**——响声不同，契约就不同。
    判据是正面定义：有兄弟导入，就必须有一条 `sys.path.insert`，其参数可静态
    解析为本文件所在目录，且位置在首次兄弟导入**之前**（摆在后面的引导是死代码）。
    """
    known = _modules()
    offenders = []
    for name, path in known.items():
        tree = _tree(path)
        local = _local_imports(tree, known)
        if not local:
            continue  # 不导兄弟模块的脚本没有这个需求
        first_import_line = min(node.lineno for _mod, _bound, node in local)
        bindings = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                bindings[node.targets[0].id] = node.value
        bootstrapped = False
        for node in ast.walk(tree):
            func = node.func if isinstance(node, ast.Call) else None
            if not (isinstance(func, ast.Attribute) and func.attr == "insert"
                    and len(node.args) == 2):
                continue
            inner = func.value
            if not (isinstance(inner, ast.Attribute) and inner.attr == "path"
                    and isinstance(inner.value, ast.Name) and inner.value.id == "sys"):
                continue
            # insert(0, path)：路径在第二个参数，第一个是插入位置
            if _insert_target_meaning(node.args[1], bindings) != "self_dir":
                continue
            if node.lineno < first_import_line:
                bootstrapped = True
                break
        if not bootstrapped:
            offenders.append(name)
    check("scripts/ 下导入兄弟模块的文件都自备 sys.path 引导", not offenders,
          "；".join(offenders) + "（兄弟导入前须有 sys.path.insert 指向本文件所在目录）")


def _import_indegree():
    """每个项目内模块被多少个**文件** import（入度 = 多少人在用）。

    按文件去重、不按绑定名计数：`from cli_util import a, b` 是**一个**调用方，
    数成两个等于把「谁在用」放大，而放大的那一档不对应任何真实语义
    （实测按绑定计数会把 console_cloud 这类只有 1 个调用方的模块算成 3 个）。
    """
    known = _modules()
    users = {name: set() for name in known}
    for name, path in known.items():
        for mod, _bound, _node in _local_imports(_tree(path), known):
            if mod != name:
                users[mod].add(name)
    return {n: len(u) for n, u in users.items()}


def run_shared_layer_annotated():
    """7) 共享契约层必须 100% 有类型注解。

    mypy 的默认设置下，**只有带注解的函数体才会被检查**。所以「给共享层补注解」
    就是把合约面纳入门禁的唯一办法——另一条路是打开 `check_untyped_defs`，那会让
    全库所有无注解函数一起变红，门禁等于没有（红到没人看之后，真错误也混在里面）。

    名单按**入度**现算（被 3 个以上 import 绑定引用的模块），不手抄：手抄的名单会
    随新模块静默过期，而「过期」的表现是新人写的共享模块不在名单里、没人给它补注解。
    当前名单：cli_util / flomo_client / memo_util / net_util。
    """
    known = _modules()
    indeg = _import_indegree()
    shared = sorted(n for n, d in indeg.items() if d >= 3)
    offenders = []
    for name in shared:
        for fn in ast.walk(_tree(known[name])):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (fn.returns or any(a.annotation for a in fn.args.args)):
                offenders.append(f"{name}.{fn.name}")
    check(f"共享层 100% 有类型注解（{'/'.join(shared)}）", not offenders,
          "；".join(offenders) + "（mypy 默认不检查无注解函数体，补上才进门禁）")


class _ExitInFunctionCollector(ast.NodeVisitor):
    """收集**函数体内**的 `sys.exit(...)` 与 `raise SystemExit(...)`。

    只数 depth > 0 的：模块级和 `if __name__ == "__main__":` 里的调用是进程入口，
    退出码本该在那里落；函数体内的是库层在结束进程，那才是违规。
    """

    def __init__(self):
        self.depth = 0
        self.hits = []

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def _hit(self, node, kind):
        if self.depth:
            self.hits.append((node.lineno, kind))

    def visit_Call(self, node):
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr == "exit"
                and isinstance(fn.value, ast.Name) and fn.value.id == "sys"):
            self._hit(node, "sys.exit")
        self.generic_visit(node)

    def visit_Raise(self, node):
        exc = node.exc
        if isinstance(exc, ast.Name) and exc.id == "SystemExit":
            self._hit(node, "raise SystemExit")
        elif (isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                and exc.func.id == "SystemExit"):
            self._hit(node, "raise SystemExit(...)")
        self.generic_visit(node)


def run_no_library_exit():
    """8) 库层（函数体内）不用 SystemExit / sys.exit 表达调用失败。

    为什么是铁律：`SystemExit` 继承 `BaseException`，`except Exception` 接不住。
    库函数一抛它，调用方就**没有选择**——连「记一笔、跳过这张、继续跑批次」
    都做不到，只能整进程陪葬。正确形态是抛 `FlomoError` / `UsageError` 这类
    `Exception` 子类，由 CLI 入口（`cli_entry`）统一转成退出码。

    判据走 ast 而非正则：各文件的 docstring 与注释里大量出现「SystemExit」字样
    （那是在说明这条契约本身），正则会把它们全算成违规——恒真检查比没有检查更坏。
    """
    offenders = []
    for name, path in _modules().items():
        collector = _ExitInFunctionCollector()
        collector.visit(_tree(path))
        for lineno, kind in collector.hits:
            offenders.append(f"{name}.py:{lineno} {kind}")
    check("库层（函数体内）无 sys.exit / raise SystemExit", not offenders,
          "；".join(offenders))


def _main_blocks(tree):
    """所有 `if __name__ == "__main__":` 块。"""
    return [n for n in tree.body
            if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]


def _is_mainish_call(expr):
    """是不是 `main(...)` / `cli_entry(main, ...)` 这类「结果被丢掉」的调用。

    认 Name 与 Attribute 两种形态：`CLI.cli_entry(main, ...)`（publish / verify_card）
    与 `cli_entry(main, ...)`（其余脚本）都得认。
    """
    if not isinstance(expr, ast.Call):
        return False
    fn = expr.func
    if isinstance(fn, ast.Name):
        return fn.id in ("main", "cli_entry")
    if isinstance(fn, ast.Attribute):
        return fn.attr in ("main", "cli_entry")
    return False


def run_main_exit_propagated():
    """9) `__main__` 里调 main() 必须包进 sys.exit(...)，否则退出码被丢掉。

    事故形态：`if __name__ == "__main__": main()`——main() 里 `return 1` 安安静静
    消失，进程退出码恒 0。下游（CI、`publish.py` 起的子进程、run_tests.sh）按退出码
    判成败，于是**失败被读成成功**。cleanup.py 的 `--verify` 正是如此：有残留时
    main() 返回 1，而入口是裸 `main()`，退出码 0。

    只查「调用结果被直接丢掉的语句」（`Expr` 套 `Call`）：`sys.exit(main())`
    的结果传给了 sys.exit，不算；`print(...)` / `extract(...)` 这类本来就没有退出码
    语义的入口也不该被这条拦（console_data / extract_arxiv_html 两个工具型入口）。
    """
    offenders = []
    for name, path in _modules().items():
        for block in _main_blocks(_tree(path)):
            for stmt in block.body:
                if (isinstance(stmt, ast.Expr) and _is_mainish_call(stmt.value)
                        and not _wrapped_in_exit(stmt)):
                    offenders.append(f"{name}.py:{stmt.lineno}")
    check("__main__ 里调 main() 都包进 sys.exit()（退出码不外丢）", not offenders,
          "；".join(offenders))


def _wrapped_in_exit(stmt):
    """这个语句整体是不是 `sys.exit(...)`——是则结果没被丢。"""
    expr = stmt.value
    fn = expr.func if isinstance(expr, ast.Call) else None
    return (isinstance(fn, ast.Attribute) and fn.attr == "exit"
            and isinstance(fn.value, ast.Name) and fn.value.id == "sys")


def main():
    run_leaf_layer()
    run_no_cycle()
    run_cloud_import_needs_cloud_api()
    run_imports_are_used()
    run_layering_documented()
    run_self_bootstrapping_imports()
    run_shared_layer_annotated()
    run_no_library_exit()
    run_main_exit_propagated()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
