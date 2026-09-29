#!/usr/bin/env python3
"""serve_console.py — 项目控制台的本地只读服务（零第三方依赖）。

把仓库自身的可视图景（九步管线、硬限清单、脚本清单与回归状态、标签树概览）
通过一个本地 HTTP 服务提供给浏览器控制台。数据抽取全部委托 `console_data`，
本脚本只做三件事：静态文件、JSON 接口、按需跑离线回归。

**只读边界**（与项目铁律一致）：
  - 不读 `.mcp.json`（含 token）、`.sop_gate/`（凭证）、任何笔记正文文件；
  - 不调用 flomo 云端接口，不发起任何写操作——控制台不接触云端；
  - 仅监听回环地址，不对外暴露；静态文件服务做路径逃逸防护。

用法：
  python scripts/serve_console.py                 # 默认 127.0.0.1:8787
  python scripts/serve_console.py --port 9000
  python scripts/serve_console.py --open          # 起服务后自动开浏览器
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_data import collect_all, load_limits, load_pipeline, load_scripts, load_tagtree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8787

# 静态资源白名单后缀（避免误发任意文件）
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".ico", ".png", ".webp", ".woff2"}


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def run_regression() -> dict:
    """按需跑离线回归（run_tests.sh）。超时 300s；返回结构化结果。"""
    bash = shutil.which("bash")
    if not bash:
        return {"ok": False, "error": "未找到 bash", "lines": []}
    env = dict(os.environ, PYTHON_BIN=sys.executable)
    try:
        proc = subprocess.run(
            [bash, "scripts/run_tests.sh"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "回归超时（>300s）", "lines": []}
    out = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "rc": proc.returncode, "lines": out.splitlines()}


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

    # ---- 路由 ----------------------------------------------------------- #
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/overview": lambda: collect_all(REPO_ROOT),
            "/api/pipeline": lambda: load_pipeline(REPO_ROOT),
            "/api/limits": lambda: load_limits(REPO_ROOT),
            "/api/scripts": lambda: load_scripts(REPO_ROOT),
            "/api/tagtree": lambda: load_tagtree(REPO_ROOT),
        }
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

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/tests/run":
            self._send_json(run_regression())
            return
        self._send_json({"error": "unknown endpoint"}, 404)


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

    httpd = ThreadingHTTPServer((args.host, args.port), ConsoleHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[console] flomo-note 控制台已启动：{url}")
    print(f"[console] 只读服务 · 仓库根 {REPO_ROOT}")
    print("[console] Ctrl+C 停止")
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
