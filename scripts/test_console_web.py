#!/usr/bin/env python3
"""serve_console.py 与 web/ 前端的对齐回归（离线，不联网、不起服务）。

控制台的前端是纯静态三件套（`web/index.html` / `styles.css` / `app.js`），
与服务的路由表之间没有任何编译期约束——最容易出的错是「拆了一边忘了另一边」：
导航留了项但没有视图、前端还在请求已被删掉的接口、服务里悄悄多出执行入口。
本用例把这些约束写成断言，改完两边任一侧都能立刻发现失配：

  - 导航项 ↔ 视图注册表 ↔ 视图元信息三者一一对应（默认视图也须一致）；
  - 前端请求的每一个 `/api/...` 都必须存在于服务的路由表；
  - 前端不得引入任何外部资源（零依赖、离线可用）；
  - 前端不得出现写云工具名或非 GET 请求；
  - 服务不得出现命令执行入口（无 POST 处理、不引 subprocess/shutil）。

用法：python scripts/test_console_web.py     退出码 0=全过，1=有失败。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = REPO / "web" / "index.html"
CSS = REPO / "web" / "styles.css"
JS = REPO / "web" / "app.js"
SERVER = REPO / "scripts" / "serve_console.py"

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def block(text: str, head: str) -> str:
    """取 `head = { ... };` 这一段（用于 VIEWS / VIEW_META / state）。"""
    m = re.search(re.escape(head) + r"\s*=\s*\{(.*?)\n\};", text, re.S)
    return m.group(1) if m else ""


def main() -> int:
    missing = [p for p in (HTML, CSS, JS, SERVER) if not p.is_file()]
    check("静态三件套与服务脚本齐备", not missing, ", ".join(str(p) for p in missing))
    if missing:
        return 1

    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")

    # ---- 1. 导航 ↔ 视图 ↔ 元信息三表对齐 ---------------------------------- #
    nav = re.findall(r'data-view="([^"]+)"', html)
    views_block = block(js, "const VIEWS")
    meta_block = block(js, "const VIEW_META")
    views = dict(re.findall(r"(\w+)\s*:\s*(\w+)\s*,", views_block))
    metas = set(re.findall(r"(\w+)\s*:\s*\{", meta_block))

    check("导航至少有云端笔记一项", nav and "notes" in nav, str(nav))
    check("导航项无重复", len(nav) == len(set(nav)), str(nav))
    check("导航项都有对应视图", set(nav) <= set(views), f"缺视图 {sorted(set(nav) - set(views))}")
    check("视图都有对应导航项", set(views) <= set(nav), f"孤立视图 {sorted(set(views) - set(nav))}")
    check("视图都有元信息（标题/副标题）", set(views) <= metas, f"缺元信息 {sorted(set(views) - metas)}")

    # 视图函数必须真实存在（VIEWS 里写错函数名会静默变成点击无反应）
    for name, fn in views.items():
        check(f"视图函数存在：{fn}", re.search(rf"function {re.escape(fn)}\s*\(", js) is not None)

    # ---- 2. 默认视图一致 -------------------------------------------------- #
    state_block = block(js, "const state")
    m = re.search(r'view\s*:\s*"(\w+)"', state_block)
    default = m.group(1) if m else ""
    active = re.findall(r'class="nav-item is-active"[^>]*data-view="([^"]+)"', html)
    check("state 默认视图已登记", default in views, default)
    check("导航高亮项与默认视图一致", active == [default], f"高亮 {active} / 默认 {default}")

    # ---- 3. 前端请求的接口必须存在于服务路由表 ----------------------------- #
    routes = set(re.findall(r'"(/api/[A-Za-z0-9_/]+)"', server))
    called = set()
    # 字面量与模板串两种写法都收：取路径部分（模板里 `?${...}` 之后是查询串）
    for raw in re.findall(r'"(/api/[^"]*)"', js) + re.findall(r"`(/api/[^`]*)`", js):
        path = re.split(r"[?`]|\$\{", raw)[0].rstrip("/")
        if path.startswith("/api/") and path != "/api":
            called.add(path)
    called |= {f"/api/{n}" for n in re.findall(r'\bget\("(\w+)"\)', js)}

    check("前端确实在调接口（抽取非空）", bool(called), str(sorted(called)))
    check("前端调用的接口都在服务路由表内", called <= routes, f"服务缺失 {sorted(called - routes)}")
    check("服务路由与前端调用一一对应", routes == called,
          f"服务独有 {sorted(routes - called)} / 前端独有 {sorted(called - routes)}")

    # ---- 4. 零外部依赖 ---------------------------------------------------- #
    ext = re.findall(r'(?:src|href)="(?:https?:)?//[^"]*"', html)
    check("页面不引外部资源", not ext, ", ".join(ext))
    check("样式表不引远程资源", "@import" not in css and "url(http" not in css, "含 @import 或远程 url()")
    check("脚本不直连外部地址", not re.search(r'fetch\(\s*["`]https?://', js), "存在外部 fetch")

    # ---- 5. 前端不做写操作 ------------------------------------------------ #
    write_tools = [t for t in ("memo_create", "memo_update", "tag_rename") if t in js]
    check("前端不出现写云工具名", not write_tools, ", ".join(write_tools))
    check("前端不发非 GET 请求", 'method: "POST"' not in js and "method: 'POST'" not in js)

    # ---- 6. 服务端不得有执行入口 ------------------------------------------ #
    check("服务无 POST 处理", "do_POST" not in server)
    check("服务不引 subprocess / shutil", not re.search(r"^\s*import (subprocess|shutil)", server, re.M))

    total = len(_RESULTS)
    failed = sum(1 for r in _RESULTS if not r)
    print("---")
    print(f"[test_console_web] {'全部通过' if not failed else f'存在失败（{failed}/{total}）'}（{total} 项）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
