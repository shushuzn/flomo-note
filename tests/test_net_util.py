#!/usr/bin/env python3
"""出网通道的「可重发判定」回归用例（离线，起本地服务真发请求）。

这套用例只钉一件事：**通道失败时，什么情况下可以换条出口重发**。

为什么值得单列一套：写云的动作里，`memo_create` 的签名幂等有索引滞后窗口，重发
就是第二张卡。而「连接被拒」与「超时」在代码里都是 `URLError`，看不出区别——
前者请求确定没发出（换出口重发安全），后者可能已经到达服务端（重发就造脏数据）。
判据取正面白名单：只有连接被拒与 DNS 失败算「没发出」，其余一律按「可能已发出」。

用例分两段：
  ① `net_util.request` 的通道选择与 `request_sent` 判定；
  ② `flomo_client` 把传输失败映射成 `CloudUnreachable` 且**写工具不重试**。

②里用的是「黑洞」代理（接受连接后不回应）：它让第一次尝试**超时**而不是被拒，
这样才能验证「超时也不换通道」。用死端口测不出这一条——死端口会被判为「确定
没发出」，按设计本来就允许换通道。

全部离线：不读 token，端点被替换成本地地址。

用法：python tests/test_net_util.py   退出码 0=全过，1=有失败。
"""

COVERS = ("net_util", "flomo_client")

import contextlib
import http.server
import importlib.util
import os
import socket
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


NU = _load("net_util", "net_util.py")
FC = _load("flomo_client", "flomo_client.py")

RESULTS = []
_PROXY_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


@contextlib.contextmanager
def _env(**kw):
    saved = {k: os.environ.get(k) for k in _PROXY_KEYS}
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)
    for key, value in kw.items():
        os.environ[key] = value
    try:
        yield
    finally:
        for key in _PROXY_KEYS:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]


def _dead_port() -> int:
    """占一个端口再释放：得到「刚被回收」的端口，几乎必然无人监听。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _Echo(http.server.BaseHTTPRequestHandler):
    # 访问计数：用来区分「没换通道」与「换了通道但也失败」——只看有没有抛错分不开，
    # 而这两件事对写操作的含义完全相反。
    hits = 0

    def _handle(self):
        _Echo.hits += 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):
        pass


def _serve(handler):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd.shutdown


def _blackhole():
    """起一个「接受连接但永不回应」的伪代理，返回 (端口, 关闭函数)。

    这是测「超时」的唯一办法：死端口在 TCP 层就被拒（算「确定没发出」），
    只有连上却不回话才会真的超时。写操作的安全性正卡在这里。
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    held = []

    def loop():
        while True:
            try:
                conn, _ = listener.accept()
                held.append(conn)  # 收下就不管：客户端读到超时
            except OSError:
                break

    threading.Thread(target=loop, daemon=True).start()

    def stop():
        try:
            listener.close()
        finally:
            for conn in held:
                try:
                    conn.close()
                except OSError:
                    pass

    return listener.getsockname()[1], stop


def run_sent_judgement():
    """`request_maybe_sent` 的白名单：只有连接阶段失败才算「没发出」。"""
    check(
        "连接被拒判为「确定没发出」",
        NU.request_maybe_sent(ConnectionRefusedError(10061, "refused")) is False,
    )
    check(
        "域名解析失败判为「确定没发出」",
        NU.request_maybe_sent(socket.gaierror(11001, "no such host")) is False,
    )
    check(
        "超时判为「可能已发出」",
        NU.request_maybe_sent(TimeoutError("timed out")) is True,
    )
    check(
        "连接中断判为「可能已发出」",
        NU.request_maybe_sent(ConnectionResetError(10054, "reset")) is True,
    )


def run_request_cases(base: str):
    dead = _dead_port()
    blackhole_port, stop = _blackhole()
    try:
        # 1) 幂等请求：坏代理 → 换直连并说明原因
        with _env(http_proxy=f"http://127.0.0.1:{dead}",
                  https_proxy=f"http://127.0.0.1:{dead}"):
            reply = NU.request(base + "/", timeout=5)
        check("幂等请求在坏代理下回退成功", reply.status == 200, reply.status)
        check("回退后的通道说明含直连", reply.channel.startswith("直连"), reply.channel)
        check("通道说明保留原代理失败原因", "不可用" in reply.channel, reply.channel)

        # 2) 非幂等 + 连接被拒：确定没发出，允许换通道
        with _env(http_proxy=f"http://127.0.0.1:{dead}",
                  https_proxy=f"http://127.0.0.1:{dead}"):
            reply = NU.request(base + "/", timeout=5, idempotent=False)
        check("非幂等在「连接被拒」时换通道成功", reply.status == 200, reply.status)

        # 3) 非幂等 + 超时：可能已发出，绝不许换通道（这条是整套用例的核心）
        with _env(http_proxy=f"http://127.0.0.1:{blackhole_port}",
                  https_proxy=f"http://127.0.0.1:{blackhole_port}"):
            started = time.monotonic()
            try:
                NU.request(base + "/", timeout=1, idempotent=False)
                raised, detail = None, "没有抛错"
            except NU.TransportError as exc:
                raised, detail = exc, str(exc)
            elapsed = time.monotonic() - started
        check("非幂等超时抛 TransportError", isinstance(raised, NU.TransportError), detail)
        check("超时判为请求可能已发出", getattr(raised, "request_sent", None) is True, detail)
        check(
            "超时后没有尝试第二条通道",
            "直连" not in detail,
            detail,
        )
        check("超时确实等到了超时点", elapsed >= 0.8, round(elapsed, 2))

        # 4) 同一次超时，幂等请求则允许换通道（证明差别确实来自 idempotent）。
        #    两条通道的差别要能被观测到：直连这条会真的打到服务上，计数加一。
        _Echo.hits = 0
        with _env(http_proxy=f"http://127.0.0.1:{blackhole_port}",
                  https_proxy=f"http://127.0.0.1:{blackhole_port}"):
            reply = NU.request(base + "/", timeout=1)
        check("幂等请求在超时后仍会换通道", reply.status == 200, reply.status)
        check("幂等请求确实走到了直连", _Echo.hits == 1, f"访问 {_Echo.hits} 次")
    finally:
        stop()

    # 5) 非法 via 属用法错，不该与通道失败混为一谈
    try:
        NU.request(base + "/", via="smoke-signal")
        bad = False
    except ValueError:
        bad = True
    check("非法 via 抛 ValueError", bad)


def run_client_cases():
    """写云通道：传输失败映射成 CloudUnreachable，写工具不重试。"""
    base, stop = _serve(_Echo)
    dead = _dead_port()
    original = FC.ENDPOINT
    try:
        # 1) 端点可达时不报通道错误（验证替换端点本身没破坏协议路径）
        FC.ENDPOINT = base + "/"
        client = FC.FlomoClient("dummy-token", timeout=5)
        result = client.tool("memo_search", {"keywords": "x", "limit": 1})
        check("端点可达时正常返回", result == {"ok": True}, result)

        # 2) 端点被拒：读工具会换通道重试（两条都拒后失败），且判为「没发出」
        FC.ENDPOINT = f"http://127.0.0.1:{dead}/"
        with _env(http_proxy=f"http://127.0.0.1:{dead}",
                  https_proxy=f"http://127.0.0.1:{dead}"):
            client = FC.FlomoClient("dummy-token", timeout=3)
            try:
                client.tool("memo_search", {})
                raised, detail = None, "没有抛错"
            except FC.CloudUnreachable as exc:
                raised, detail = exc, str(exc)
        check("端点不可达抛 CloudUnreachable", isinstance(raised, FC.CloudUnreachable), detail)
        check("连接被拒时判为请求未发出", getattr(raised, "request_sent", None) is False, detail)
        check("错误里带上环境代理信息", "环境代理" in detail, detail)
        check(
            "通道错误不再是裸 URLError（上层能接住）",
            isinstance(raised, FC.FlomoError),
            type(raised).__name__,
        )

        # 3) 写工具遇到超时：不换通道、不重发，且明确标为「可能已发出」。
        #    **端点指向可达的服务、代理才指向黑洞**：这样「偷偷换了通道」会在服务上
        #    留下访问记录。若端点也指向黑洞，两条通道都是超时，用例就分不清
        #    「没重试」与「重试了但也失败」——而后者正是会造出重复卡的情形。
        blackhole_port, stop_hole = _blackhole()
        try:
            FC.ENDPOINT = base + "/"
            _Echo.hits = 0
            with _env(http_proxy=f"http://127.0.0.1:{blackhole_port}",
                      https_proxy=f"http://127.0.0.1:{blackhole_port}"):
                client = FC.FlomoClient("dummy-token", timeout=1)
                try:
                    client.tool("memo_create", {"content": "不该真的写出去"})
                    raised, detail = None, "没有抛错（说明换通道重发了）"
                except FC.CloudUnreachable as exc:
                    raised, detail = exc, str(exc)
            hits = _Echo.hits
        finally:
            stop_hole()
        check("写工具超时抛 CloudUnreachable", isinstance(raised, FC.CloudUnreachable), detail)
        check(
            "写工具超时标为可能已写出（禁止自动重发）",
            getattr(raised, "request_sent", None) is True,
            detail,
        )
        check("写工具超时后没有改走直连重发", hits == 0, f"直连被访问 {hits} 次")

        # 4) 写工具清单与「不幂等」判定同源
        check("memo_create 属写工具", "memo_create" in FC.WRITE_TOOLS)
        check("memo_search 不属写工具", "memo_search" not in FC.WRITE_TOOLS)
        check(
            "tag_rename 也按写工具处理",
            "tag_rename" in FC.WRITE_TOOLS,
        )
    finally:
        FC.ENDPOINT = original
        stop()


class _UaEcho(http.server.BaseHTTPRequestHandler):
    """把收到的 User-Agent 回显出来，用来验证默认头真的发出去了。"""
    seen = ""

    def _handle(self):
        _UaEcho.seen = self.headers.get("User-Agent") or ""
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle

    def log_message(self, *args):
        pass


def _ua_cases():
    url, stop = _serve(_UaEcho)
    try:
        # ① 不传 headers 也必须带浏览器 UA：否则 probe 用浏览器 UA 探通、
        #    request 用 Python 默认 UA 抓取被 403，「探测通过、抓取被拦」
        #    这种假象没人能发现（实测 dev.to/feed 就是这样被拦的）。
        NU.request(url, timeout=10)
        check("不传 headers 也带默认 UA", _UaEcho.seen == NU.user_agent(),
              f"seen={_UaEcho.seen!r}")
        # ② 调用方显式指定时以调用方为准，默认头只垫不抢
        _UaEcho.seen = ""
        NU.request(url, timeout=10, headers={"User-Agent": "my-agent/1.0"})
        check("显式 UA 覆盖默认值", _UaEcho.seen == "my-agent/1.0",
              f"seen={_UaEcho.seen!r}")
        # ③ 探测与抓取同源：probe 发的 UA 必须和 request 的一样
        NU.probe(url, timeout=10)
        check("probe 与 request 用同一个 UA", _UaEcho.seen == NU.user_agent(),
              f"seen={_UaEcho.seen!r}")
    finally:
        stop()


def main():
    base, stop = _serve(_Echo)
    try:
        run_sent_judgement()
        run_request_cases(base)
    finally:
        stop()
    run_client_cases()
    _ua_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
