#!/usr/bin/env python3
"""server_fetch.py — 远端服务器代理取页：站点按出口 IP 拦截时的第三条通道。

为什么要有它：有些站点不看 UA、不看请求频率，只按出口 IP 判风险。实测同一篇雅虎
文章，本机直接请求是 403，换 UA / 加 `?guccounter=1` / `/amp` / news 子域全不通；
借一台自备云主机去取，原页是 429，改从那台机器走阅读器代理才拿到 200。也就是说
「换网络」不是一个开关，而是三件事：换出口、换 UA、换取页方式（原页 / 阅读器）。

纪律：
  - **凭据不入库**。主机、端口、用户名、口令只放 `.workbuddy/remote_fetch.json`
    （`scripts/../.workbuddy/`，已被 gitignore），也可用环境变量覆盖；
  - **只读**。远端只执行 `curl` 打到 stdout，不在那台机器上落文件、不改任何状态；
  - **「跳过」不写成「通过」**。没配、没装、连不上、两次都非 200，都要抛
    `ServerFetchError` 并说明卡在哪一步，不许静默退化成「取到了」。
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / ".workbuddy" / "remote_fetch.json"

# 远端取页的两步：先原页（拿 HTML，后续抽取器照旧可用），失败再阅读器代理（拿正文）。
DIRECT_TIMEOUT = 25
READER_TIMEOUT = 40
READER_PREFIX = "https://r.jina.ai/"
_CODE_MARK = "__HTTP_CODE__"

_ENV_KEYS = {
    "host": "FLOMO_REMOTE_HOST",
    "port": "FLOMO_REMOTE_PORT",
    "user": "FLOMO_REMOTE_USER",
    "password": "FLOMO_REMOTE_PASSWORD",
    "key_path": "FLOMO_REMOTE_KEY",
}


class ServerFetchError(RuntimeError):
    """服务器代理不可用：配置缺失、依赖缺装、连接失败或两次取页都非 200。"""


def load_config(path: Path | str | None = None) -> dict | None:
    """读远端配置；文件不存在返回 None（表示「本机没配这条通道」）。

    字段：`host`（必填）、`port`（默认 22）、`user`（默认 root）、
    `password` 或 `key_path`（二选一）、`max_bytes`（可选，默认 512KB）。
    环境变量优先于文件：口令不该被写进任何能提交的地方。
    """
    cfg: dict = {}
    p = Path(path) if path else CONFIG_PATH
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServerFetchError(f"远端配置读不出来：{p}（{exc}）") from exc
        if not isinstance(cfg, dict):
            raise ServerFetchError(f"远端配置必须是 JSON 对象：{p}")
    for key, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = int(val) if key == "port" and str(val).isdigit() else val
    if not cfg:
        return None
    host = str(cfg.get("host") or "").strip()
    if not host:
        raise ServerFetchError("远端配置缺 host")
    cfg["host"] = host
    cfg["port"] = int(cfg.get("port") or 22)
    cfg["user"] = str(cfg.get("user") or "root")
    if not cfg.get("password") and not cfg.get("key_path"):
        raise ServerFetchError("远端配置缺 password 或 key_path，无法登录")
    cfg["max_bytes"] = int(cfg.get("max_bytes") or 524288)
    return cfg


def _ssh_run(cfg: dict, command: str, timeout: float) -> tuple[int, str, str]:
    """在一台机器上跑一条只读命令，返回 (退出码, stdout, stderr)。"""
    try:
        import paramiko  # 延迟导入：没装这条通道就整条不可用，而不是整个技能起不来
    except ImportError as exc:
        raise ServerFetchError(
            "本机没装 paramiko（远端通道需要它）：pip install paramiko"
        ) from exc
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            cfg["host"],
            port=cfg["port"],
            username=cfg["user"],
            password=cfg.get("password") or None,
            key_filename=cfg.get("key_path") or None,
            timeout=min(timeout, 20),
            banner_timeout=min(timeout, 20),
            auth_timeout=min(timeout, 20),
        )
    except Exception as exc:  # 认证失败与网络失败都归「这条通道没打通」
        raise ServerFetchError(
            f"连不上远端 {cfg['user']}@{cfg['host']}:{cfg['port']}（{type(exc).__name__}: {exc}）"
        ) from exc
    try:
        _in, out, err = client.exec_command(command, timeout=timeout)
        return (
            out.channel.recv_exit_status(),
            out.read().decode("utf-8", "replace"),
            err.read().decode("utf-8", "replace"),
        )
    finally:
        client.close()


def _curl(cfg: dict, target: str, timeout: int, runner) -> tuple[int, str, str]:
    """远端 curl 一个地址并带回状态码：正文末尾拼 `__HTTP_CODE__<code>`。"""
    cmd = (
        "curl -sS -m " + str(timeout) + " -A " + shlex.quote(_UA)
        + " -w " + shlex.quote("\\n" + _CODE_MARK + "%{http_code}")
        + " " + shlex.quote(target)
    )
    rc, out, err = runner(cfg, cmd, timeout + 10)
    code = 0
    if _CODE_MARK in out:
        body, _, tail = out.rpartition(_CODE_MARK)
        tail = tail.strip()
        code = int(tail) if tail.isdigit() else 0
    else:
        body = out
    # 超量截断：远端取页是「拿正文」，不是镜像整站，超出上限只留前缀。
    if len(body) > cfg["max_bytes"]:
        body = body[: cfg["max_bytes"]]
    return code, body, err


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch(url: str, timeout: float = 45.0, config: dict | None = None, runner=None) -> tuple[str, str, str]:
    """经远端服务器取页，返回 (正文, 类型, 通道说明)；类型为 `html` 或 `text`。

    顺序：原页 → 阅读器代理。两次都失败才抛错，并把两次的状态码一起写进报错——
    只报「都失败」等于让人自己去猜是哪一步被拦。
    """
    cfg = config if config is not None else load_config()
    if not cfg:
        raise ServerFetchError(
            "本机没配远端通道：写 .workbuddy/remote_fetch.json（host/user/password），"
            "或用 FLOMO_REMOTE_HOST / FLOMO_REMOTE_USER / FLOMO_REMOTE_PASSWORD 覆盖"
        )
    run = runner or _ssh_run

    code, body, err = _curl(cfg, url, DIRECT_TIMEOUT, run)
    if code == 200 and body.strip():
        return body, "html", f"远端 {cfg['host']}·原页"
    first = f"原页 HTTP {code or '无响应'}" + (f"（{err.strip()[:60]}）" if err.strip() else "")

    code2, body2, err2 = _curl(cfg, READER_PREFIX + url, READER_TIMEOUT, run)
    if code2 == 200 and body2.strip():
        return body2, "text", f"远端 {cfg['host']}·阅读器代理（{first}）"
    raise ServerFetchError(
        f"远端两次都没取到：{first}；阅读器 HTTP {code2 or '无响应'}"
        + (f"（{err2.strip()[:60]}）" if err2.strip() else "")
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="经远端服务器代理取页")
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default=None, help="保存路径，默认打印到 stdout")
    ap.add_argument("--config", default=None, help="远端配置路径")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args(argv)
    try:
        text, kind, channel = fetch(args.url, args.timeout, load_config(args.config))
    except ServerFetchError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    print(f"[出网] {channel}")
    print(f"[类型] {kind}｜{len(text)} 字符")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[文件] {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
