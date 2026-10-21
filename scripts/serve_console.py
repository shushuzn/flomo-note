#!/usr/bin/env python3
"""serve_console.py — 项目控制台的本地服务（零第三方依赖）。

控制台把云端 MCP 的能力接出来，分四个视图呈现：
  - 云端笔记（主视图）：搜索 / 按标签 / 起止日期 / 来源 / 是否含标签 / 今日回顾，
    点开读全文，抽屉里带「相关笔记」与多选取全文，另有新建与编辑；
  - 标签：云端实时标签树（可前缀 / 深度 / 条数）+ 标签名搜索 + 标签重命名；
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
  - `.sop_gate/`（闸门凭证）不由本脚本读取；静态文件服务做路径逃逸防护；
  - **请求来源校验**：Host 的宿主名必须是本机名（拦 DNS 重绑定），带 Origin 时
    须同源（拦浏览器发起的跨站写）。「监听回环」**不等于**「只有本机能用」——
    同源策略管的是能不能读响应，不管请求发不发；本服务有写入口，两道校验缺一不可。

用法：
  python scripts/serve_console.py                 # 默认 127.0.0.1:8787
  python scripts/serve_console.py --port 9000
  python scripts/serve_console.py --open          # 起服务后自动开浏览器
  python scripts/serve_console.py --host 0.0.0.0 --allow-lan   # 局域网访问（须显式解锁）
"""
from __future__ import annotations

import argparse
import ipaddress
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
from console_data import collect_all  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8787

# 进程级单例：复用与云端的会话，避免每请求重握手。
CLOUD = CloudReader()

# 静态资源白名单后缀（避免误发任意文件）
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".ico", ".png", ".webp", ".woff2"}

# 请求来源白名单：本机宿主名。由 main() 按 `--host` 补全（含绑定的具体地址），
# 未初始化时按回环名判——**默认拒绝**而不是默认放行。
LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
ALLOWED_HOSTNAMES: set[str] = set()


def _hostname_of(value: str) -> str:
    """从 Host / Origin 的地址里取宿主名（小写、去端口、去 IPv6 方括号）。"""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v.startswith("["):  # [::1]:8787
        end = v.find("]")
        return v[1:end] if end > 0 else v
    if v.count(":") > 1:  # 裸 IPv6（Host 头规范上带括号，这里只是兜底）
        return v
    return v.rsplit(":", 1)[0] if ":" in v else v


def allowed_hostnames(bound: str) -> set[str]:
    """按绑定地址算出可接受的宿主名集合。

    绑定具体地址时只认它 + 回环名；绑定通配地址（0.0.0.0 / ::）时把本机各接口地址
    也认作本站——否则用户按局域网地址访问会被自己的服务拒掉。**不因此放宽到任意名**：
    通配只是「哪些本机地址算本站」，DNS 重绑定送的仍然是攻击者域名。
    """
    names = set(LOOPBACK_HOSTNAMES)
    b = (bound or "").strip().lower()
    if b and b not in ("0.0.0.0", "::", ""):
        names.add(b)
        return names
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(str(info[4][0]).strip().lower())
    except OSError:
        pass  # 取不到就不加：宁可少认几个本机别名，也不放开任意宿主名
    return names


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


def _reject_body(kind: str, value: str, allowed: set[str]) -> bytes:
    """403 的说明体：写清被拒的是哪一项、允许的是哪些，免得读者以为是服务坏了。"""
    return (
        f"403：请求{kind}「{value}」不是本站地址，已拒绝。\n"
        f"本服务只接受本机访问：{' / '.join(sorted(allowed))}。\n"
        f"浏览器请用 http://127.0.0.1:<端口>/ 打开；命令行工具（无 Origin）不受影响。\n"
    ).encode("utf-8")


def _is_loopback_bind(host: str) -> bool:
    """绑定地址是否落在回环范围（含 127.0.0.0/8 与 ::1；`localhost` 也认）。"""
    h = (host or "").strip().lower()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


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

    # ---- 请求来源校验 ----------------------------------------------------- #
    def _reject_foreign(self) -> bool:
        """来源不合法就回 403 并返回 True（调用方直接 return）。

        **为什么回环绑定还需要这一层**：同源策略管的是「页面能不能读响应」，不管
        「请求发不发」——任何网页都能向 `127.0.0.1:8787` 发一个 `text/plain` 的
        POST，本服务照收；本服务有写入口（建卡 / 改卡 / 批量改标签），请求到达即已
        生效，读不到响应也无所谓。DNS 重绑定更进一步：攻击者域名先解析到自己服务器
        再改解析到 127.0.0.1，浏览器认为同源，连响应都能读——那时 GET 接口里的笔记
        全文与「参考」文本就不只是能写，而是能被抄走。

        两道判据，各拦一类：
          - **Host 的宿主名须在本机名白名单内**：重绑定请求必须带攻击者域名（浏览器
            的 Host 跟着页面走），此关拦它，且连 GET 读取一并拦住；
          - **带 Origin 时其宿主名须同站**：拦浏览器发起的跨站写（含简单请求——它
            不触发预检、CORS 管不到发不发）。`Origin: null` 同样拒。
        Origin 缺失视为非浏览器客户端（curl / 回归用例），放行：强制要求 Origin＝本站
        会把全部命令行用法拒掉，这是可用性与安全的真实权衡，不是漏判。
        """
        allowed = ALLOWED_HOSTNAMES or LOOPBACK_HOSTNAMES
        host = _hostname_of(self.headers.get("Host", ""))
        if host not in allowed:
            self._send(403, _reject_body(
                "Host", self.headers.get("Host", "") or "（缺失）", allowed), "text/plain; charset=utf-8")
            return True
        origin = self.headers.get("Origin")
        if origin is not None:
            if _hostname_of(urlparse(origin).netloc or origin) not in allowed:
                self._send(403, _reject_body("Origin", origin or "（空）", allowed),
                           "text/plain; charset=utf-8")
                return True
        return False

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
        if self._reject_foreign():
            return
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        routes = {
            "/api/overview": lambda: {**collect_all(REPO_ROOT), "cloud": cloud_status()},
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
        if self._reject_foreign():
            return
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
    ap.add_argument("--allow-lan", action="store_true",
                    help="允许绑定非回环地址（本服务无鉴权，局域网内任何人都能读写云端卡片）")
    ap.add_argument("--open", action="store_true", help="起服务后自动打开浏览器")
    args = ap.parse_args(argv)

    # 非回环绑定必须显式解锁：绑定 0.0.0.0 意味着同网段任何人（无需凭据）都能调
    # 建卡 / 改卡 / 批量改标签。来源校验拦的是「浏览器里的网页」，拦不住「直接连上来的
    # 另一台机器」——这一层只能靠不绑出去。与项目「放行只留显式开关」一致。
    if not _is_loopback_bind(args.host) and not args.allow_lan:
        sys.stderr.write(
            f"拒绝绑定 {args.host}：本控制台没有鉴权，绑到回环之外等于把云端卡片\n"
            f"  （含读取全文与批量改标签）开放给同网段任何一台机器。\n"
            f"  确需局域网访问请显式加 --allow-lan，并自行确认网络环境可信。\n"
        )
        return 2

    # 来源白名单按绑定地址算一次，之后每个请求都按它判
    ALLOWED_HOSTNAMES.clear()
    ALLOWED_HOSTNAMES.update(allowed_hostnames(args.host))

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
    print("[console] 本地服务 · 仓库根 %s" % REPO_ROOT)
    print("[console] 只接受本机宿主名：%s" % " / ".join(sorted(ALLOWED_HOSTNAMES)))
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
