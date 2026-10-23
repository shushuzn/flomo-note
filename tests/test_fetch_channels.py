#!/usr/bin/env python3
"""通道选择与回退的回归用例（离线，起本地 HTTP 服务真发请求）。

为什么单独一套：代理端口**每次会话重新分配**，写死端口必然过期，而「端口不对」
与「网络断了」在现象上完全一样（连接被拒 / 502）。此前唯一的应对是让调用方手写
`curl -x http://127.0.0.1:<端口>` 去试，端口猜错就会得出「网络不通」这个错结论。
故本轮给两条出网通道做成可判定的行为：

  - `fetch_article.fetch()` 默认先环境代理、失败直连重试，并把**实际走通的那条**
    和原代理的失败原因一起说明（「跳过」不许写成「通过」）；
  - `netdoctor.probe()` 以「拿到任何 HTTP 响应」为通道可用的判据——403/404 证明包
    已到服务器，把它判成通道坏了会让人去换代理，越换越远。

这些判定都是「能用不能用」，不能靠读代码相信：本地起服务、设一个**死端口**当环境
代理，真发请求看结论。全部离线，不读 token。

用法：python tests/test_fetch_channels.py   退出码 0=全过，1=有失败。
"""

COVERS = ("fetch_article", "netdoctor")

import contextlib
import http.server
import importlib.util
import os
import socket
import sys
import threading
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FA = _load("fetch_article", "fetch_article.py")
ND = _load("netdoctor", "netdoctor.py")
from cli_util import UsageError  # noqa: E402

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
    """临时替换代理相关环境变量，退出时原样恢复。

    必须先清空再设：只设 `http_proxy` 而留着 `no_proxy=127.0.0.1` 的话，环境代理
    通道对本地目标根本不走代理，用例会「通过」但什么都没测到。
    """
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
    """占一个端口再立刻释放，得到「刚被系统回收」的端口号。

    比硬编码 9 之类更可靠：那个端口可能真有服务（discard），也可能被别的进程占着，
    两种都会让用例的「代理不可用」前提不成立。
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    PAYLOAD = ("<html><head><title>通道探针</title></head><body><article>"
               + "本地服务返回的正文。" * 60
               + "</article></body></html>")

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler 的约定命名
        if self.path.startswith("/missing"):
            body = b"not found"
            self.send_response(404)
        else:
            body = self.PAYLOAD.encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    """起一个只在本机监听的临时服务，返回 (base_url, 关闭函数)。"""
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd.shutdown


def _try_fetch(url, **kw):
    """调用 `fetch` 并返回 (html, 通道说明, 异常)。

    用例要能同时断言「取到了」和「没取到时说的是什么」。裸调用会让实现退化以
    traceback 收场——那读起来像环境故障（报错里恰好写着「代理不可用」，而环境里
    的代理其实是好的），而不是「这条用例没通过」。
    """
    try:
        html, _, note = FA.fetch(url, **kw)
        return html, note, None
    except Exception as exc:  # 用例层：任何异常都算「没取到」，由断言分档
        return "", "", exc


def run_fetch_cases(base: str):
    dead = _dead_port()
    proxy_env = {
        "http_proxy": f"http://127.0.0.1:{dead}",
        "https_proxy": f"http://127.0.0.1:{dead}",
    }

    # 1) auto：代理不可用时回退直连，且必须把代理的失败原因写出来
    with _env(**proxy_env):
        html, note, err = _try_fetch(base + "/", timeout=10)
    check("auto 在代理不可用时仍取到页面", err is None, err)
    check("auto 取到的确实是本地服务正文", "本地服务返回的正文" in html, len(html))
    check("auto 的通道说明指明走了直连", note.startswith("直连"), note)
    check("auto 的通道说明保留了代理失败原因", "不可用" in note, note)

    # 2) --via proxy：只走环境代理，不通就该失败，而不是偷偷回退
    with _env(**proxy_env):
        _, _, err = _try_fetch(base + "/", timeout=10, via="proxy")
    hit = isinstance(err, FA.FetchError)
    check("via=proxy 代理不通时抛 FetchError", hit, err)
    check(
        "via=proxy 的报错指明是通道问题",
        hit and "环境代理" in str(err) and "不可用" in str(err),
        str(err),
    )

    # 3) --via direct：清空 ProxyHandler，完全绕开坏代理
    with _env(**proxy_env):
        html, note, err = _try_fetch(base + "/", timeout=10, via="direct")
    check("via=direct 绕开坏代理取到页面", err is None, err)
    check("via=direct 取到了正文", "本地服务返回的正文" in html, len(html))
    check("via=direct 的通道说明是直连", note == "直连", note)

    # 4) 没有代理时 auto 走环境代理那一档（此时 label 里没有具体地址）
    with _env():
        html, note, err = _try_fetch(base + "/", timeout=10)
    check("无代理时 auto 仍能取到页面", err is None, err)
    check("无代理时通道说明标为环境代理", "环境代理" in note, note)

    # 5) 404 不下「通道坏了」的结论：包已到服务器，再换通道只会埋掉真实原因
    with _env(**proxy_env):
        _, _, err = _try_fetch(base + "/missing", timeout=10)
    detail = str(err)
    check("HTTP 404 抛 FetchError", isinstance(err, FA.FetchError), err)
    check("404 的报错说明不是通道问题", "404" in detail and "不是通道问题" in detail, detail)

    # 6) 非法 via 走用法错，不与「调用失败」混为一谈
    _, _, err = _try_fetch(base + "/", via="carrier-pigeon")
    check("非法 --via 抛 UsageError", isinstance(err, UsageError), err)


def run_netdoctor_cases(base: str):
    dead = _dead_port()

    # 1) 判据是「拿到 HTTP 响应」，404 也算通
    row = ND.probe(base + "/missing", timeout=10, direct=True)
    check("probe 把 404 判为通道可用", row["ok"] is True, row)
    check("probe 的详情写明服务器已响应", "已响应" in row["detail"], row["detail"])

    # 2) 连接被拒才算不通
    with _env(http_proxy=f"http://127.0.0.1:{dead}",
              https_proxy=f"http://127.0.0.1:{dead}"):
        row = ND.probe(base + "/", timeout=10, direct=False)
    check("probe 对死代理判为不通", row["ok"] is False, row)
    check("probe 记录了失败原因", bool(row["detail"]), row["detail"])

    # 3) 结论必须落到「下一步敲什么」，三档都要有
    both = [{"channel": "环境代理", "ok": True}, {"channel": "直连", "ok": True}]
    only_direct = [{"channel": "环境代理", "ok": False}, {"channel": "直连", "ok": True}]
    only_proxy = [{"channel": "环境代理", "ok": True}, {"channel": "直连", "ok": False}]
    none = [{"channel": "环境代理", "ok": False}, {"channel": "直连", "ok": False}]

    line, code = ND.verdict(both)
    check("两条都通时退出码 0", code == 0, code)
    check("两条都通的结论指向 fetch_article", "fetch_article.py" in line, line)

    line, code = ND.verdict(only_direct)
    check("仅直连可用时退出码 0", code == 0, code)
    check("仅直连可用时给出 --via direct", "--via direct" in line, line)

    line, code = ND.verdict(only_proxy)
    check("仅代理可用时退出码 0", code == 0, code)
    check("仅代理可用时给出 --via proxy", "--via proxy" in line, line)

    line, code = ND.verdict(none)
    check("两条都不通时退出码 1", code == 1, code)
    check("两条都不通的结论不承诺能用", "都不可用" in line, line)

    # 4) 目标参数校验走用法错
    try:
        ND.main(["--url", "ftp://example.com/x"])
        bad, detail = False, "没有抛错"
    except UsageError as exc:
        bad, detail = True, str(exc)
    check("非 http(s) 目标抛 UsageError", bad, detail)


def run_source_guards():
    """通道脚本里不许出现回环端口字面量。

    这两个脚本抓的是外网、测的是外网，本就不该出现 `127.0.0.1:<端口>`；一旦出现，
    就是有人又把「某个会话的代理端口」抄进了代码。判据取正面定义：直接禁该形态，
    不设豁免——留豁免就得记住豁免理由，而豁免恰恰是下次失效的入口。
    """
    import re

    pattern = re.compile(r"127\.0\.0\.1:\d{2,5}")
    for name in ("fetch_article.py", "netdoctor.py", "net_util.py"):
        source = (SCRIPT_DIR / name).read_text(encoding="utf-8")
        hits = pattern.findall(source)
        check(f"{name} 内无写死的回环端口", not hits, hits)


def main():
    base, stop = _serve()
    try:
        run_fetch_cases(base)
        run_netdoctor_cases(base)
    finally:
        stop()
    run_source_guards()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
