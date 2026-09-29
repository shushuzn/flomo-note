#!/usr/bin/env python3
"""serve_console.py — 项目控制台的本地服务（零第三方依赖）。

控制台把云端 MCP 的能力接出来，分四个视图呈现：
  - 云端笔记（主视图）：搜索 / 按标签 / 起止日期 / 来源 / 是否含标签 / 今日回顾，
    点开读全文，抽屉里带「相关笔记」与多选取全文，另有新建与编辑；
  - 标签：本地快照分组速览 + 云端实时标签树（可前缀 / 深度 / 条数）+ 标签名搜索
    + 标签重命名；
  - 参考：记忆文档 / 用户画像 / 格式规范 / 标签规范四份云端文本；
  - 能力：云端 MCP 工具清单（读写工具分别标注是否接出）。

浏览器侧的数据全部来自本服务的 JSON 接口：云端侧委托 `console_cloud`，
本地侧委托 `console_data`；本脚本只做静态文件与 JSON 接口的转发。

**写路径不是直通**：`POST` 是唯一会改动云端的入口，且写请求在 `console_cloud`
那层强制过两道闸门（格式 ERR、流程凭证）并在写入后回读全文验收；
本脚本不替它们做判断，也不提供绕过开关。

**边界**（与项目铁律一致）：
  - 云端工具只走 `console_cloud` 的读 / 写白名单，白名单外的工具名一律拒调用；
  - 正文不落盘（H26）：只在内存与 HTTP 响应之间传递，不写日志、不写缓存；
    唯一落盘是流程闸门要求的当轮草稿（`memo_body*.txt`），由收尾清理回收；
  - token 只在服务进程内用于建连，绝不进入任何响应体；
  - `.sop_gate/`（闸门凭证）不由本脚本读取；仅监听回环地址，静态文件服务做路径逃逸防护。

用法：
  python scripts/serve_console.py                 # 默认 127.0.0.1:8787
  python scripts/serve_console.py --port 9000
  python scripts/serve_console.py --open          # 起服务后自动开浏览器
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_cloud import CloudError, CloudReader, cloud_status  # noqa: E402
from console_data import collect_all, load_tagtree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8787

# 进程级单例：复用与云端的会话，避免每请求重握手。
CLOUD = CloudReader()

# 静态资源白名单后缀（避免误发任意文件）
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".ico", ".png", ".webp", ".woff2"}


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def port_in_use(host: str, port: int, timeout: float = 0.4) -> bool:
    """探测端口是否已有服务在监听。

    必须显式探测：`ThreadingHTTPServer` 默认带地址复用，同一端口上第二个实例
    能「绑成功」而不报错，于是两个版本的服务同时接客——请求落到哪个实例全看运气，
    页面会时新时旧，极难排查。宁可在这里明确拦下，让用户换端口或先停旧实例。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "flomo-console/1.0"

    # ---- 输出助手 ------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        self._send(code, _json_bytes(obj), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 收敛默认访问日志
        sys.stderr.write("[console] %s\n" % (fmt % args))

    # ---- 静态资源 ------------------------------------------------------- #
    def _serve_static(self, rel: str):
        rel = rel.lstrip("/") or "index.html"
        target = (WEB_ROOT / rel).resolve()
        # 路径逃逸防护：解析后必须仍在 web/ 内
        if not target.is_relative_to(WEB_ROOT.resolve()):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if target.suffix.lower() not in STATIC_SUFFIXES:
            self._send(403, b"forbidden suffix", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ---- 云端代理 ------------------------------------------------------- #
    def _cloud(self, fn, err_code: int = 502):
        """云端请求统一出口：白名单与两道写闸门在 `console_cloud` 内强制，本层只做降级。

        写路径的「闸门未过」用 422——请求内容不合规，与「云端连不上」（502）区分开，
        前端据此把待修项直接摆到编辑器里，而不是笼统报一句网络错。
        """
        try:
            self._send_json(fn(CLOUD))
        except CloudError as e:
            self._send_json({"error": str(e), "kind": "cloud"}, err_code)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    @staticmethod
    def _q(query, key, default=None):
        vals = query.get(key) or []
        return vals[0] if vals else default

    # ---- 路由 ----------------------------------------------------------- #
    # 接口只服务界面实际要看的两件事：云端笔记与标签树；不外扩未使用的入口。
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        routes = {
            "/api/overview": lambda: {**collect_all(REPO_ROOT), "cloud": cloud_status()},
            "/api/tagtree": lambda: load_tagtree(REPO_ROOT),
        }
        cloud_routes = {
            "/api/cloud/memos": lambda r: r.list_memos(
                keywords=self._q(q, "keywords"),
                tag=self._q(q, "tag"),
                start_date=self._q(q, "start_date"),
                end_date=self._q(q, "end_date"),
                source=self._q(q, "source"),
                has_tag=self._q(q, "has_tag"),
                limit=self._q(q, "limit", 20),
            ),
            "/api/cloud/memo": lambda r: r.memo_detail(self._q(q, "id", "")),
            "/api/cloud/memos/batch": lambda r: r.memo_batch(
                (self._q(q, "ids", "") or "").split(",")
            ),
            "/api/cloud/review": lambda r: r.daily_review(),
            "/api/cloud/related": lambda r: r.recommended(
                self._q(q, "id", ""),
                limit=self._q(q, "limit", 10),
                no_same_tag=self._q(q, "no_same_tag"),
            ),
            "/api/cloud/tagtree": lambda r: r.tag_tree(
                prefix=self._q(q, "prefix"),
                depth=self._q(q, "depth"),
                limit=self._q(q, "limit", 200),
            ),
            "/api/cloud/tags": lambda r: r.tag_names(
                self._q(q, "keywords", ""), self._q(q, "limit", 20)
            ),
            "/api/cloud/reference": lambda r: r.reference(),
            "/api/cloud/tools": lambda r: r.tool_catalog(),
        }

        if path in cloud_routes:
            self._cloud(cloud_routes[path])
            return
        if path in routes:
            try:
                self._send_json(routes[path]())
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        if path in ("/", ""):
            self._serve_static("index.html")
            return
        self._serve_static(path)

    def _read_json(self) -> dict:
        """读请求体并解析为对象。空体视作空字典，非法 JSON 抛 ValueError。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体顶层须是 JSON 对象")
        return data

    # ---- 写入入口（本服务唯一会改动云端的地方） -------------------------- #
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except ValueError as e:
            self._send_json({"error": f"请求体不是合法 JSON：{e}", "kind": "request"}, 400)
            return

        # 全部写操作：闸门与回读验收都在 console_cloud 内强制，本层不提供跳过开关。
        routes = {
            "/api/cloud/validate": lambda r: r.check_card(body.get("content") or ""),
            "/api/cloud/gate": lambda r: r.run_gate(
                body.get("content") or "",
                skip_web=bool(body.get("skip_web")),
                anchor_id=body.get("anchor_id") or None,
                verify=body.get("verify"),
            ),
            "/api/cloud/memo/create": lambda r: r.create_memo(
                body.get("content") or "", fmt=body.get("format") or None
            ),
            "/api/cloud/memo/update": lambda r: r.update_memo(
                body.get("id") or "", body.get("content") or "", fmt=body.get("format") or None
            ),
            "/api/cloud/tag/rename": lambda r: r.rename_tag(
                body.get("old_tag") or "", body.get("new_tag") or "", max_memos=body.get("max_memos")
            ),
            # 通用只读执行：界面「能力」页按工具清单生成的表单打到这里；
            # 写工具在 console_cloud 里被显式挡在门外，不走此入口。
            "/api/cloud/tool": lambda r: r.run_read_tool(
                body.get("name") or "", body.get("arguments") or {}
            ),
        }
        if path not in routes:
            self._send_json({"error": f"未知写入接口：{path}", "kind": "request"}, 404)
            return
        self._cloud(routes[path], err_code=422)

    def do_HEAD(self):
        self.do_GET()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1", help="默认仅回环，勿改为对外地址")
    ap.add_argument("--open", action="store_true", help="起服务后自动打开浏览器")
    args = ap.parse_args(argv)

    if not WEB_ROOT.is_dir():
        sys.stderr.write(f"未找到前端目录：{WEB_ROOT}\n")
        return 2

    if port_in_use(args.host, args.port):
        sys.stderr.write(
            f"端口 {args.host}:{args.port} 已有服务在监听——\n"
            f"  先停掉它（或改用 --port 指定其他端口）再启动，\n"
            f"  否则两个实例会同时接客，页面内容时新时旧。\n"
        )
        return 2

    httpd = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[console] flomo-note 控制台已启动：{url}")
    print("[console] 只读服务 · 仓库根 %s" % REPO_ROOT)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] 已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
