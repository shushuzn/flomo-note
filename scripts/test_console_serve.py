#!/usr/bin/env python3
"""test_console_serve.py — 控制台的运行时冒烟（真起服务、真发请求，全程离线）。

为什么还要这一层：`test_console_web.py` 全是对源码的正则断言——路由表、导航、
按钮事件都能「静态对得上」，但服务**根本起不来**时它一条也不会红。判据不能是
「文件存在」「路由表里有这一项」，只能是「端口真在听、路径真返回该有的东西」。

本用例把服务当黑盒：在随机空闲端口真启动 `serve_console.py`，用 `http.client`
直连（不走系统代理——本机 HTTP_PROXY 会让 localhost 也过代理，curl 会返回代理
自己的 502，看起来像服务坏了），断言四件事：

  1. 静态三件套真能取到（首页是 HTML 外壳，样式与脚本非空）；
  2. `/api/overview` 返回本地 JSON，且只报云端可用性、不把 token 带进响应体；
  3. 未知路径 404；`..` 逃逸的四种写法都拿不到仓库文件（不在 web/ 内即 403）；
  4. 端口被占时第二个实例以退出码 2 拒绝启动（双实例会让页面内容时新时旧）。

用法：python scripts/test_console_serve.py     退出码 0=全过，1=有失败。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "scripts" / "serve_console.py"
MCP = REPO / ".mcp.json"

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port: int, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def spawn(port: int) -> subprocess.Popen:
    """起服务；清掉代理变量，避免子进程把回环请求交给系统代理。"""
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env["NO_PROXY"] = env["no_proxy"] = "*"
    return subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port)],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def request(port: int, method: str, path: str, body: bytes | None = None):
    """直连发一个请求，返回 (状态码, Content-Type, 文本)。失败返回 (0, "", 错误)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, resp.getheader("Content-Type") or "", raw.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "", f"{type(e).__name__}: {e}"
    finally:
        conn.close()


def main() -> int:
    if not SERVER.is_file():
        print(f"FAIL 服务脚本不存在：{SERVER}")
        return 1

    port = free_port()
    proc = spawn(port)
    try:
        if not wait_listening(port):
            out = ""
            try:
                proc.kill()
                out = (proc.stderr.read() or b"").decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            print(f"FAIL 服务未能在端口 {port} 监听  [{out}]")
            return 1

        # 1) 静态三件套
        code, ctype, text = request(port, "GET", "/")
        check("首页可取且是 HTML", code == 200 and "text/html" in ctype, f"{code} {ctype}")
        check("首页是可挂载的外壳（含 app.js 入口）",
              '<script src="app.js">' in text and "flomo-note" in text, text[:80])

        code, ctype, css = request(port, "GET", "/styles.css")
        check("样式表可取且非空", code == 200 and len(css) > 500, f"{code} {len(css)}B")

        code, _, js = request(port, "GET", "/app.js")
        check("脚本可取且非空", code == 200 and len(js) > 5000, f"{code} {len(js)}B")

        # 2) 本地 JSON 接口 + 不泄 token
        code, ctype, body = request(port, "GET", "/api/overview")
        ok_json = False
        data = {}
        try:
            data = json.loads(body)
            ok_json = isinstance(data, dict)
        except Exception:  # noqa: BLE001
            ok_json = False
        check("/api/overview 返回 JSON 对象", code == 200 and ok_json, f"{code} {body[:120]}")
        check("overview 含项目统计与云端状态",
              "stats" in data and "cloud" in data and isinstance(data.get("cloud"), dict),
              str(list(data)[:8]))
        check("overview 的云端状态只报可用性",
              isinstance(data.get("cloud", {}).get("available"), bool), str(data.get("cloud")))

        leaked = []
        if "Bearer" in body or "Authorization" in body:
            leaked.append("鉴权头字样")
        if MCP.is_file():
            try:
                cfg = json.loads(MCP.read_text(encoding="utf-8"))
                auth = cfg["mcpServers"]["flomo"]["headers"]["Authorization"]
                tok = auth.split()[-1]
                if tok and tok in body:
                    leaked.append("token 原文")
            except Exception:  # noqa: BLE001
                pass
        check("响应体不含 token / 鉴权头", not leaked, "、".join(leaked))

        # 3) 404 与路径逃逸
        code, _, _ = request(port, "GET", "/nope.js")
        check("未知路径返回 404", code == 404, str(code))

        marker = "本组管"  # 只出现在仓库文档里，正常响应体不含
        escapes = ["/../SKILL.md", "/..%2fSKILL.md", "/web/../SKILL.md", "/%2e%2e/SKILL.md"]
        bad = []
        for p in escapes:
            code, _, text = request(port, "GET", p)
            if code == 200 or marker in text:
                bad.append(f"{p} -> {code}")
        check("四种 .. 写法都逃不出 web/", not bad, "、".join(bad))

        # 4) 未知写路由：走 POST 也必须被拒，且是 JSON 错误体
        code, ctype, text = request(port, "POST", "/api/cloud/nope", b"{}")
        ok = code == 404 and "application/json" in ctype and "error" in text
        check("未知写路由 404 且返回 JSON 错误", ok, f"{code} {ctype} {text[:80]}")

        # 5) 双实例：端口已被本进程占用，第二个实例应拒绝启动
        second = spawn(port)
        try:
            rc2 = second.wait(timeout=15)
        except subprocess.TimeoutExpired:
            second.kill()
            second.wait()
            rc2 = None
        err2 = (second.stderr.read() or b"").decode("utf-8", "replace")
        check("端口被占时拒绝起第二个实例（退出码 2）", rc2 == 2, f"rc={rc2} {err2[:80]}")
        check("拒绝时说明原因", "已有服务在监听" in err2, err2[:80])

        # 6) 服务在整个过程后仍存活（没被某个请求拖崩）
        code, _, _ = request(port, "GET", "/")
        check("连续请求后服务仍存活", code == 200, str(code))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("---")
    ok = all(_RESULTS)
    print(f"{'全部通过' if ok else '存在失败'}（{len(_RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
