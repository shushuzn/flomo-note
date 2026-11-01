#!/usr/bin/env python3
"""net_util.py — 出网通道的唯一实现：双通道选择、错误分类、可重发判定。

为什么要有它：沙箱注入的代理端口**每次会话重新分配**（同一台机器实测 9 分钟内
从 `29178` 变成 `3898`），而「端口不对」与「网络真的断了」现象完全一样（连接被拒 /
502）。此前抓取侧已改成自动回退，但**写云通道仍然裸调 `urlopen`**——代理一换，
整轮写卡直接失败，而且报出来的是 `URLError`，`except FlomoError` 接不住，读的人
只看到 traceback，以为是云端出了事。

**通道选择只能有一份实现**：`fetch_article`、`netdoctor`、`flomo_client` 若各写一套，
判据一分叉就会出现「一个说通、一个说不通」，而没有人知道该信哪个。

**写操作不许盲重试**：`memo_create` 有签名幂等，但存在索引滞后窗口，重发即重复卡。
故本模块不只回答「换哪条通道」，还要回答「请求是否可能已到达服务端」——
`TransportError.request_sent`。判据取**正面白名单**：

  - 只有连接被拒（`ConnectionRefusedError`）与域名解析失败（`socket.gaierror`）
    才判为「确定没发出」——两者都发生在建立连接之前，服务端不可能收到；
  - 超时 / 连接中断 / TLS 失败一律判为「可能已发出」。写操作遇到它只能报
    「需人工确认」，绝不能自动换条通道再发一次。

不做「反面定义」（列举哪些情况算 unsafe）：新增一种失败形态时，反面定义会静默
把它归进「安全」，而正面白名单会静默把它归进「不安全」——后者只是多问一次，
前者会造出重复卡。
"""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

VIA_CHOICES = ("auto", "proxy", "direct")

# 「确定未发出」的传输失败：发生在建立连接之前，服务端不可能收到请求体。
_UNSENT_ERRORS = (ConnectionRefusedError, socket.gaierror)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TransportError(RuntimeError):
    """所有通道都不可用（连接被拒 / 超时 / DNS / 中断）。HTTP 状态码不归这里。

    `request_sent` 是给写操作看的：False 表示可以安全换通道重发（请求确定没发出），
    True 表示可能已经到达服务端，只能报「需人工确认」。调用方**不要**自己猜这个值，
    也不要在 True 时重发——那正是重复卡的来源。
    """

    def __init__(self, message: str, *, request_sent: bool) -> None:
        super().__init__(message)
        self.request_sent = bool(request_sent)


class Reply(NamedTuple):
    body: bytes
    # urllib 回包是 email.message.Message：有 .get() 但不是 Mapping，
    # 标 dict 是谎报，标 object 又让每个调用点都要先断言——按实际用途标 Any。
    headers: Any
    status: int
    channel: str


def proxy_label() -> str:
    """环境代理的展示标签（只用于打印，不参与决策）。

    决策一律交给 `ProxyHandler` 自己读环境变量——把端口抄进代码或文档，正是要
    消灭的那类缺陷：抄下来的那个必然在某天变成「连接被拒」。
    """
    proxies = urllib.request.getproxies()
    parts = [f"{k}={v}" for k, v in sorted(proxies.items()) if k.lower() != "no"]
    return "，".join(parts) if parts else "无"


def request_maybe_sent(exc: BaseException) -> bool:
    """这次传输失败是否可能已经把请求送到了服务端（判据见模块注释）。"""
    reason = getattr(exc, "reason", exc)
    return not isinstance(reason, _UNSENT_ERRORS)


def _opener(direct: bool):
    # 空 ProxyHandler 才是真直连；仅 unset 环境变量不够——调用方 shell 里的 `*_proxy`
    # 还在，且 `no_proxy` 可能把目标排除在外，造成「以为直连、其实走代理」。
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({} if direct else None)
    )


def _channel(direct: bool) -> str:
    return "直连" if direct else f"环境代理（{proxy_label()}）"


def _default_headers() -> dict[str, str]:
    """抓取与探测共用的默认请求头。

    两者必须同源：`probe` 拿浏览器 UA 探通道、`request` 拿 Python 默认 UA 抓内容时，
    会出现「探测通过、抓取被拦」的假象——人以为通道没问题，转而去查别的原因，而
    真实原因只是 UA 被站点拒了。此前 `user_agent()` 的文档写着要避免这种假象，
    但 `request()` 从没把它加进去，等于那句承诺没有实现。
    """
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }


def request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    via: str = "auto",
    idempotent: bool = True,
) -> Reply:
    """发一次 HTTP 请求，返回 `Reply`（正文、响应头、状态码、实际通道）。

    `via`：
      auto   先环境代理，失败再直连
      proxy  只走环境代理（`NO_PROXY` 仍生效）
      direct 只直连（空 `ProxyHandler`，完全不读 `*_proxy`）

    `idempotent` 决定「换个通道重发」是否允许：
      True（GET / 查询类 POST）——任何传输失败都换通道；重复读没有副作用
      False（建卡 / 覆盖 / 改名）——只有 `request_sent is False` 时才换通道，
            超时与中断直接抛 `TransportError(request_sent=True)`

    HTTP 4xx/5xx 原样抛 `urllib.error.HTTPError`：那是**服务端已应答**，换通道
    只会把真实原因埋掉。调用方据「两种异常 = 两个出口」分别处置。
    """
    if via not in VIA_CHOICES:
        raise ValueError(f"via 只能是 {'/'.join(VIA_CHOICES)}，收到 {via!r}")
    plan = [False, True] if via == "auto" else [via == "direct"]
    tried: list[str] = []
    sent_last = False
    for index, direct in enumerate(plan):
        label = _channel(direct)
        # 默认头先垫上、调用方的再盖：既让不传 headers 的抓取也带浏览器 UA，又不
        # 挡住调用方显式指定（如 flomo 写云要的 Content-Type、部分站点要换 UA）。
        req = urllib.request.Request(
            url,
            data=data,
            headers={**_default_headers(), **(headers or {})},
            method="POST" if data is not None else "GET",
        )
        try:
            with _opener(direct).open(req, timeout=timeout) as resp:
                body = resp.read()
                note = label
                if tried:
                    # 「跳过」不写成「通过」：把原通道为何失败一并带出。
                    note = f"{label}；{tried[-1]}"
                return Reply(body, resp.headers, resp.status, note)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            sent_last = request_maybe_sent(exc)
            reason = getattr(exc, "reason", exc)
            tried.append(f"{label}不可用：{reason}")
            has_next = index < len(plan) - 1
            if not (has_next and (idempotent or not sent_last)):
                raise TransportError("；".join(tried), request_sent=sent_last) from exc
    raise TransportError("；".join(tried), request_sent=sent_last)


def probe(url: str, *, timeout: float = 15.0, direct: bool = True) -> dict:
    """探一条通道能不能用，返回结论字典（给连通体检用）。

    判据取正面定义：**拿到任何 HTTP 响应都算可用**，403/404 也算——包已经到了
    服务器，通道是通的。把 403 判成「通道坏了」会让人去换代理，越换越远。
    只有连接被拒 / 超时 / DNS 失败才算不通。
    """
    label = "直连" if direct else "环境代理"
    req = urllib.request.Request(url, headers=_default_headers())
    started = time.monotonic()
    try:
        with _opener(direct).open(req, timeout=timeout) as resp:
            resp.read(1024)
            return {
                "channel": label,
                "ok": True,
                "detail": f"HTTP {resp.status}",
                "seconds": time.monotonic() - started,
            }
    except urllib.error.HTTPError as exc:
        return {
            "channel": label,
            "ok": True,
            "detail": f"HTTP {exc.code}（服务器已响应，通道可用）",
            "seconds": time.monotonic() - started,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "channel": label,
            "ok": False,
            "detail": str(getattr(exc, "reason", exc)),
            "seconds": time.monotonic() - started,
        }


def user_agent() -> str:
    """抓取与探测共用同一个 UA，避免「探测通过、抓取被拦」的假象。"""
    return _USER_AGENT
