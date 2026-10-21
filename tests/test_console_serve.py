#!/usr/bin/env python3
"""test_console_serve.py — 控制台的运行时冒烟（真起服务、真发请求，全程离线）。

为什么还要这一层：`test_console_web.py` 全是对源码的正则断言——路由表、导航、
按钮事件都能「静态对得上」，但服务**根本起不来**时它一条也不会红。判据不能是
「文件存在」「路由表里有这一项」，只能是「端口真在听、路径真返回该有的东西」。

本用例把服务当黑盒：在随机空闲端口真启动 `serve_console.py`，用 `http.client`
直连（不走系统代理——本机 HTTP_PROXY 会让 localhost 也过代理，curl 会返回代理
自己的 502，看起来像服务坏了），断言六件事：

  1. 静态三件套真能取到（首页是 HTML 外壳，样式与脚本非空）；
  2. `/api/overview` 返回本地 JSON，且只报云端可用性、不把 token 带进响应体；
  3. 未知路径 404；`..` 逃逸的四种写法都拿不到仓库文件（不在 web/ 内即 403）；
  4. 未知写路由 404 且给 JSON 错误体（不能只断连接、让前端干等）；
  5. **通用执行入口 `/api/cloud/tool` 的安全边界**：写工具、空工具名、白名单外
     工具一律被拒，畸形请求体给 400。这一层此前只有单元用例（直接调
     `run_read_tool`）与前端源码正则，HTTP 路由本身一次真实请求都没打过——
     路由若把 body 解析错，界面「执行」按钮会静默失败而所有用例照样绿。
     判据用状态码自证：本机离线，真发出云端请求必因连不上而 502，拿到 422
     即证明拒绝发生在 `_connect()` 之前，一个字节都没出网；
  6. 端口被占时第二个实例以退出码 2 拒绝启动（双实例会让页面内容时新时旧）。

**不测合法读工具的端到端执行**：那必然连云端，用完例换联网依赖不划算；
参数传递与返回形态由 `test_console_cloud.py` 的 `run_read_tool_entry()`（假
client）覆盖，两层各管一段。

用法：python tests/test_console_serve.py     退出码 0=全过，1=有失败。
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


def request(port: int, method: str, path: str, body: bytes | None = None,
            headers: dict | None = None):
    """直连发一个请求，返回 (状态码, Content-Type, 文本)。失败返回 (0, "", 错误)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
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

        # 4b) 通用执行入口的安全边界：写工具不得从 `/api/cloud/tool` 穿透。
        #     这一层此前只有控制台单元用例（`run_read_tool` 直接调用）与前端源码
        #     正则，HTTP 路由本身一次真实请求都没打过——路由若把 body 解析错，
        #     界面「执行」按钮会静默失败而所有用例照样绿。
        #     判据用状态码本身证伪：本机离线，真发出云端请求必因连不上而 502；
        #     拿到 422 即证明拒绝发生在 `_connect()` 之前，一个字节都没出网。
        def post_json(payload):
            return request(port, "POST", "/api/cloud/tool",
                           json.dumps(payload, ensure_ascii=False).encode())

        code, ctype, text = post_json({"name": "memo_create", "arguments": {"content": "x"}})
        check("通用入口拒绝写工具（422 而非 502）",
              code == 422 and "写工具不走通用入口" in text, f"{code} {text[:100]}")

        code, _, text = post_json({"name": "memo_update", "arguments": {"id": "x"}})
        check("写工具 update 同样被拒", code == 422 and "写工具" in text, f"{code} {text[:100]}")

        code, _, text = post_json({"name": "", "arguments": {}})
        check("缺工具名被拒", code == 422 and "缺少工具名" in text, f"{code} {text[:100]}")

        code, _, text = post_json({"name": "delete_everything", "arguments": {}})
        check("白名单外工具被拒", code == 422 and "白名单外工具" in text, f"{code} {text[:100]}")

        # 4c) 畸形请求体：必须是有 JSON 错误体的 400，不能半路掐断连接
        code, ctype, text = request(port, "POST", "/api/cloud/tool", b"{not json")
        check("畸形 JSON 请求体返回 400 与错误体",
              code == 400 and "application/json" in ctype and "error" in text,
              f"{code} {ctype} {text[:80]}")

        # 4d) 请求来源校验：绑定回环 != 只有本机能用
        #     任何网页都能向 127.0.0.1 发请求（同源策略管的是能不能读响应，不管发不发），
        #     而本服务有写入口（建卡 / 改卡 / 批量改标签）。DNS 重绑定还能让攻击者读到
        #     响应。这里逐条验证两道判据真的拦得住，且不误伤正常用法。
        draft = json.dumps({"content": "#AI/RAG\n测试\n"}, ensure_ascii=False).encode()
        evil_host = f"evil.example:{port}"

        code, _, text = request(port, "POST", "/api/cloud/validate", draft,
                                {"Host": evil_host})
        check("伪造 Host 的写请求被拒（拦 DNS 重绑定）",
              code == 403 and "Host" in text, f"{code} {text[:80]}")

        code, _, text = request(port, "GET", "/api/overview", None, {"Host": evil_host})
        check("伪造 Host 的读请求同样被拒（重绑定可读响应，故读也要拦）",
              code == 403, f"{code} {text[:80]}")

        code, _, text = request(port, "POST", "/api/cloud/validate", draft,
                                {"Origin": "http://evil.example"})
        check("跨站 Origin 的写请求被拒（拦浏览器 CSRF）",
              code == 403 and "Origin" in text, f"{code} {text[:80]}")

        code, _, text = request(port, "POST", "/api/cloud/validate", draft,
                                {"Origin": "null"})
        check("Origin: null 同样被拒", code == 403, f"{code} {text[:80]}")

        code, _, text = request(port, "POST", "/api/cloud/validate", draft,
                                {"Origin": f"http://127.0.0.1:{port}"})
        check("同源 Origin 的写请求放行（界面自己的请求）",
              code == 200 and '"ok"' in text, f"{code} {text[:80]}")

        code, _, text = request(port, "GET", "/api/overview", None,
                                {"Origin": f"http://localhost:{port}"})
        check("localhost 与 127.0.0.1 视为同一站", code == 200, f"{code} {text[:60]}")

        code, _, text = request(port, "POST", "/api/cloud/validate", draft)
        check("无 Origin 的命令行客户端不受影响", code == 200 and '"ok"' in text,
              f"{code} {text[:60]}")

        # 4e) 非回环绑定必须显式解锁：本服务无鉴权，绑到 0.0.0.0 等于把云端卡片
        #     （读取全文与批量改标签）开放给同网段任何机器。来源校验拦不住「直接连
        #     上来的另一台机器」，这一层只能靠不绑出去。
        lan = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(free_port()), "--host", "0.0.0.0"],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")},
        )
        try:
            rc_lan = lan.wait(timeout=15)
        except subprocess.TimeoutExpired:
            lan.kill()
            lan.wait()
            rc_lan = None
        err_lan = (lan.stderr.read() or b"").decode("utf-8", "replace")
        check("非回环绑定默认拒绝（退出码 2）", rc_lan == 2, f"rc={rc_lan} {err_lan[:80]}")
        check("拒绝时点明要加 --allow-lan", "--allow-lan" in err_lan, err_lan[:120])

        # 白名单算出来只能是本机名，不得因通配绑定变成「任意名」
        sys.path.insert(0, str(SERVER.parent))
        import serve_console as SC  # noqa: E402
        wild = SC.allowed_hostnames("0.0.0.0")
        check("通配绑定不产出任意宿主名",
              "evil.example" not in wild and all(n for n in wild), str(sorted(wild)[:5]))
        check("通配绑定仍认回环名", {"127.0.0.1", "localhost"} <= wild, str(sorted(wild)[:5]))

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
