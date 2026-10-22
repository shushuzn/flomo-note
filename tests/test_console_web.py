#!/usr/bin/env python3
"""serve_console.py 与 web/ 前端的对齐回归（离线，不联网、不起服务）。

控制台的前端是纯静态三件套（`web/index.html` / `styles.css` / `app.js`），
与服务的路由表之间没有任何编译期约束——最容易出的错是「拆了一边忘了另一边」：
导航留了项但没有视图、前端还在请求已被删掉的接口、按钮没有事件处理。
本用例把这些约束写成断言，改完两边任一侧都能立刻发现失配：

  - 导航项 ↔ 视图注册表 ↔ 视图元信息三者一一对应（默认视图也须一致）；
  - 前端请求的每一个 `/api/...` 都必须存在于服务的路由表，且服务路由都被用到；
  - 改动云端一律经本地 POST；读工具的执行入口也走 POST（只因为它要带参数体），
    但写工具名不得挂在可执行按钮上、也不得被直接送去执行入口；
  - 编辑器与重命名区的按钮都必须有事件处理；
  - 前端不得引入任何外部资源（零依赖、离线可用）；
  - 服务不得出现命令执行入口（不引 subprocess/shutil）。

用法：python tests/test_console_web.py     退出码 0=全过，1=有失败。
"""

from __future__ import annotations

COVERS = ("serve_console", "console_cloud",)

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
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

    # ---- 5. 写通道：一律走本地 POST，不直连云端 --------------------------- #
    cloudpost = set(re.findall(r'cloudPost\(\s*"(/api/[^"]+)"', js))
    cloudget = {
        re.split(r"[?`]|\$\{", raw)[0].rstrip("/")
        for raw in re.findall(r'cloudApi\(\s*["`](/api/[^"`]*)', js)
    }
    check("写调用一律走 cloudPost（抽取非空）", bool(cloudpost), str(sorted(cloudpost)))
    check("写路径不出现在只读调用里", not (cloudpost & cloudget), f"重合 {sorted(cloudpost & cloudget)}")
    # 写工具名可以出现在界面上——但只作「指路牌」，把用户引到过闸门的专属入口；
    # 它们绝不能被挂到可执行按钮上，也不得作为参数发给通用执行入口。
    write_body = re.search(r"function writePanel\(t\)\s*\{(.*?)\n\}", js, re.S)
    check("写工具面板不含执行按钮（改写只能经专属入口过闸门）",
          bool(write_body) and "data-run" not in write_body.group(1) and "data-goto" in write_body.group(1),
          "writePanel 里出现 data-run 或缺 data-goto")
    check("只读面板挂执行按钮、写面板只指路",
          "reads.map(toolPanel)" in js and "writes.map(writePanel)" in js,
          "工具台未按读写分组渲染")
    check("写工具名不进入通用执行入口",
          not re.search(r'cloudPost\(\s*"/api/cloud/tool"\s*,\s*\{\s*name:\s*"(?:memo_create|memo_update|tag_rename)"', js),
          "写工具名被直接送去执行")
    check("前端不直连外部地址", not re.search(r'fetch\(\s*["`]https?://', js), "存在外部 fetch")

    # ---- 6. 交互完整性：按钮必须有事件处理 -------------------------------- #
    btns = set(re.findall(r'id="(btn-ed-[a-z-]+|btn-tag-rename|btn-new-memo)"', html))
    handled = set(re.findall(r'closest\("#(btn-ed-[a-z-]+|btn-tag-rename|btn-new-memo)"\)', js))
    check("按钮都有事件处理（渲染了但点不动是最难查的失配）",
          btns <= handled, f"无处理 {sorted(btns - handled)}")

    # ---- 7. 服务端：POST = 写接口 + 通用读工具执行入口，无命令执行位置 ------- #
    # 只取 do_POST 方法体——do_GET 里的云端只读路由写法相同，不圈定范围会误收
    m = re.search(r"def do_POST\(self\):(.*?)\n    def ", server, re.S)
    post_body = m.group(1) if m else ""
    post_routes = set(re.findall(r'"(/api/[A-Za-z0-9_/]+)":\s*lambda r:', post_body))
    # POST 上只有两类：改动云端的写接口，与「带参数去跑一个读工具」的通用执行入口。
    # 除执行入口外，其余一律是写接口——不给「执行命令」「跑脚本」留任何位置。
    exec_entry = "/api/cloud/tool"
    post_write = post_routes - {exec_entry}
    check("服务有 POST 入口", "def do_POST" in server)
    check("POST 写路由都是云端写接口",
          bool(post_write) and all(p.startswith("/api/cloud/") for p in post_write),
          str(sorted(post_write)))
    check("POST 上唯一的只读执行入口是通用工具入口",
          post_routes - post_write == {exec_entry}, str(sorted(post_routes - post_write)))
    check("前端写调用与服务 POST 路由一致", cloudpost == post_routes,
          f"服务独有 {sorted(post_routes - cloudpost)} / 前端独有 {sorted(cloudpost - post_routes)}")
    check("服务不引 subprocess / shutil（执行外部程序不在服务层）",
          not re.search(r"^\s*import (subprocess|shutil)", server, re.M))

    # ---- 8. 截断必须显式声明（上限可以设，静默截断不行） ------------------- #
    # 同一页的其他位置都标了「已截断」（列表卡、标签树）；工具台与多选取全文若只切数字
    # 不提示（JSON 兜底切字符数、批量取全文切条数），用户会把半份结果当全份。
    # 故这里钉三件事：上限是具名常量、提示与上限同时存在、提示挂在真实的判据上。
    tool_result = re.search(r"function toolResult\(r\)\s*\{(.*?)\n\}", js, re.S)
    tr_body = tool_result.group(1) if tool_result else ""
    check("工具台 JSON 兜底有具名上限常量", re.search(r"const TOOL_JSON_LIMIT = \d+;", js) is not None)
    check("工具台超限时显式标注已截断并给出总数",
          re.search(r"cut \?", tr_body) is not None and "已截断" in tr_body
          and "TOOL_JSON_LIMIT" in tr_body and "jsonText.length" in tr_body,
          tr_body[-160:].replace("\n", " "))
    check("工具台不再裸切 4000（硬编码 slice 清零）", "slice(0, 4000)" not in js)

    batch_open = re.search(r"async function openBatch\(\)\s*\{(.*?)\n\}", js, re.S)
    ob_body = batch_open.group(1) if batch_open else ""
    check("批量取全文使用具名上限常量（无魔法数）",
          "slice(0, BATCH_LIMIT)" in ob_body and "slice(0, 10)" not in ob_body, ob_body[:120].replace("\n", " "))
    check("批量超上限时告知用户少取了几条",
          re.search(r"data\.dropped\s*\?", ob_body) is not None and "本次未取" in ob_body,
          ob_body[:200].replace("\n", " "))
    # 跨层：前端上限必须与服务侧 console_cloud.BATCH_MAX 一致，
    # 否则服务侧改到 12 而前端仍按 10 切，界面会声称「都取回了」而实际少两张。
    import console_cloud as _CC
    m_limit = re.search(r"const BATCH_LIMIT = (\d+);", js)
    js_limit = int(m_limit.group(1)) if m_limit else -1
    check("前端批量上限与服务侧 BATCH_MAX 一致",
          js_limit == _CC.BATCH_MAX, f"前端 {js_limit} / 服务 {_CC.BATCH_MAX}")

    # ---- 9. 重命名的结局要分开显示（不写在回执里也算没达成） ---------------- #
    # 云端回执里 matched / updated 两个数才决定写没写成：一条没写是「等规模」，
    # 只写一部分是「标签已分裂」。界面若只按「有没有 error」判断，两种都会被显示
    # 成「重命名已下发」，用户照此收工，线上就留下一半旧标签。
    ren = re.search(r"const ren = state\.renameResult;(.*?)const renameBlock", js, re.S)
    ren_body = ren.group(1) if ren else ""
    check("重命名结果按 ok / hint 呈现", "ren.ok" in ren_body and "ren.hint" in ren_body,
          ren_body[:80].replace("\n", " "))
    check("重命名规模读 max_memos_requested（旧字段名已改，写错会静默显示默认值）",
          "max_memos_requested" in ren_body and not re.search(r"ren\.max_memos\b", ren_body),
          ren_body[:80].replace("\n", " "))
    check("重命名未达成不用成功样式", 'ren.ok ? "ok-note"' in ren_body,
          ren_body[:80].replace("\n", " "))
    # 提示语必须按结局分叉——只钉「文件里出现过 needs_scale 字样」是不够的：
    # 该字符串也出现在「把匹配数填回输入框」的条件里，退回一句「已下发」照样能过。
    ren_fn = re.search(r"async function runTagRename\(\)\s*\{(.*?)\n\}", js, re.S)
    fn_body = ren_fn.group(1) if ren_fn else ""
    toast_m = re.search(r"toast\((r\.error.*?)\);", fn_body, re.S)
    toast_expr = toast_m.group(1) if toast_m else ""
    check("重命名的提示按结局分叉（不是一句「已下发」）",
          "r.ok" in toast_expr and '"needs_scale"' in toast_expr and '"partial"' in toast_expr,
          toast_expr[:120].replace("\n", " "))
    check("未写入 / 部分写入时把匹配数填回规模输入框",
          "state.renMax = String(r.matched_memos)" in js, "没有回填规模")
    check("规模输入本地校验（NaN 会序列化成 null，等于没声明规模）",
          "/^\\d+$/.test(state.renMax)" in js, "缺规模校验")

    total = len(_RESULTS)
    failed = sum(1 for r in _RESULTS if not r)
    print("---")
    print(f"[test_console_web] {'全部通过' if not failed else f'存在失败（{failed}/{total}）'}（{total} 项）")
    return 1 if failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
