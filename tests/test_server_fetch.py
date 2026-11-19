#!/usr/bin/env python3
"""远端服务器通道的回归用例（离线，不真的去连那台机器）。

为什么单独一套：这条通道的价值全在「本机被按 IP 拦住时还能取到正文」，而它一旦
悄悄失效，现象与「站点改版」完全一样——人只会去换 UA、换镜像，永远想不到是凭据
过期或 paramiko 没装。故把三件事做成可判定的行为：

  - 配置缺失 → 明确说「本机没配」，不是静默跳过；
  - 原页失败 → 落阅读器代理，并把原页状态码带进通道说明（跳过不写成通过）；
  - 两步都失败 → 报错里同时出现两个状态码，能看出卡在哪一步。

全部离线：`runner` 注入假 SSH 执行器，不连网、不读口令。

用法：python tests/test_server_fetch.py   退出码 0=全过，1=有失败。
"""

COVERS = ("server_fetch", "fetch_article")

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sf = _load("server_fetch", "server_fetch.py")
fa = _load("fetch_article", "fetch_article.py")

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"：{detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


def _clear_env():
    for key in sf._ENV_KEYS.values():
        os.environ.pop(key, None)


def case_config_missing():
    print("[1] 没配通道时要明说，不许静默跳过")
    _clear_env()
    real, sf.CONFIG_PATH = sf.CONFIG_PATH, Path("/nonexistent/remote_fetch.json")
    try:
        cfg = sf.load_config()
        check("缺配置返回 None", cfg is None, repr(cfg))
        try:
            sf.fetch("https://example.com/a", timeout=1)
            check("缺配置调用即报错", False, "没有抛错")
        except sf.ServerFetchError as exc:
            check("报错点名配置", "没配远端通道" in str(exc), str(exc))
    finally:
        sf.CONFIG_PATH = real

    try:
        pass
    except sf.ServerFetchError as exc:
        check("报错点名配置", "没配远端通道" in str(exc), str(exc))


def case_config_env_and_password():
    print("[2] 环境变量优先，且口令/密钥二选一必填")
    _clear_env()
    os.environ["FLOMO_REMOTE_HOST"] = "10.0.0.9"
    os.environ["FLOMO_REMOTE_PORT"] = "2222"
    os.environ["FLOMO_REMOTE_USER"] = "ops"
    try:
        sf.load_config("/nonexistent/remote_fetch.json")
        check("缺口令时报错", False, "没有抛错")
    except sf.ServerFetchError as exc:
        check("报错点名称谓", "password" in str(exc), str(exc))
    os.environ["FLOMO_REMOTE_PASSWORD"] = "p"
    cfg = sf.load_config("/nonexistent/remote_fetch.json")
    check("env 生效", cfg and cfg["host"] == "10.0.0.9" and cfg["port"] == 2222 and cfg["user"] == "ops", repr(cfg))
    _clear_env()


def case_direct_ok():
    print("[3] 原页 200 时用原页（HTML 才能进抽取器）")
    _clear_env()
    cfg = {"host": "h", "port": 22, "user": "u", "password": "p", "max_bytes": 4096}
    calls = []

    def runner(c, cmd, timeout):
        calls.append(cmd)
        return 0, "<html>body</html>\n" + sf._CODE_MARK + "200", ""

    _text, kind, channel = sf.fetch("https://example.com/a", config=cfg, runner=runner)
    check("类型是 html", kind == "html", kind)
    check("只发了一次", len(calls) == 1, str(len(calls)))
    check("通道写明原页", "原页" in channel, channel)


def case_reader_fallback():
    print("[4] 原页 403 时落阅读器，且把 403 带进说明")
    _clear_env()
    cfg = {"host": "h", "port": 22, "user": "u", "password": "p", "max_bytes": 4096}
    calls = []

    def runner(c, cmd, timeout):
        calls.append(cmd)
        if "r.jina.ai" in cmd:
            return 0, "Title: x\n\n正文\n" + sf._CODE_MARK + "200", ""
        return 0, "blocked\n" + sf._CODE_MARK + "403", ""

    _text, kind, channel = sf.fetch("https://example.com/a", config=cfg, runner=runner)
    check("类型是 text", kind == "text", kind)
    check("两步都发过", len(calls) == 2, str(len(calls)))
    check("说明里保留原页状态", "403" in channel, channel)


def case_both_fail():
    print("[5] 两步都失败时报两个状态码，能看出卡在哪")
    _clear_env()
    cfg = {"host": "h", "port": 22, "user": "u", "password": "p", "max_bytes": 4096}

    def runner(c, cmd, timeout):
        return 0, "x\n" + sf._CODE_MARK + ("429" if "r.jina.ai" in cmd else "403"), ""

    try:
        sf.fetch("https://example.com/a", config=cfg, runner=runner)
        check("应当抛错", False, "没有抛错")
    except sf.ServerFetchError as exc:
        msg = str(exc)
        check("带上原页状态", "403" in msg, msg)
        check("带上阅读器状态", "429" in msg, msg)


def case_fetch_article_gate():
    print("[6] 抓取侧：显式关掉远端时不许悄悄用")
    _clear_env()
    os.environ["FLOMO_REMOTE_HOST"] = "10.0.0.9"
    os.environ["FLOMO_REMOTE_USER"] = "ops"
    os.environ["FLOMO_REMOTE_PASSWORD"] = "p"
    import urllib.error

    called = []
    orig_sf, orig_req = sf.fetch, fa.net_util.request

    def spy(*a, **k):
        called.append(1)
        raise sf.ServerFetchError("别用我")

    def blocked(*a, **k):
        raise urllib.error.HTTPError("https://example.com/a", 403, "Forbidden", {}, None)

    sf.fetch, fa.net_util.request = spy, blocked
    try:
        with contextlib.suppress(fa.FetchError):
            fa.fetch("https://example.com/a", allow_server=False)
        check("allow_server=False 时未触发远端", not called, str(called))
    finally:
        sf.fetch, fa.net_util.request = orig_sf, orig_req
        _clear_env()


def main():
    for case in (
        case_config_missing,
        case_config_env_and_password,
        case_direct_ok,
        case_reader_fallback,
        case_both_fail,
        case_fetch_article_gate,
    ):
        case()
    print()
    if FAILS:
        print(f"[失败] {len(FAILS)} 项：{', '.join(FAILS)}")
        return 1
    print("[通过] 远端通道用例全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
