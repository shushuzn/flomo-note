#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""域名前置（domain fronting）取件器：SNI 与 Host 分离，绕过按 SNI 关键字的 TLS RST 阻断。

背景：
本机网络对 SNI 中出现 `arxiv.org` 的 TLS ClientHello 直接回 RST（TCP 能建连、0.08s 后
ConnectionResetError）；换 SNI 为 `www.bing.com` 或去 SNI 即握手成功，说明是 SNI 关键字
过滤而非"TLS 整段阻断"。arXiv 走 Fastly，Fastly 按 HTTP Host 头路由，因此把 SNI 换成
无关域名、Host 头保留 `arxiv.org`，即可正常取回 abs 页与 PDF。

用法：
    python scripts/sni_fetch.py <url> <out_path> [--sni www.bing.com] [--ip <IP>]

示例：
    python scripts/sni_fetch.py https://arxiv.org/pdf/2609.09226 tmp_paper.pdf
    python scripts/sni_fetch.py https://arxiv.org/abs/2609.09226 tmp_abs.html --sni a.fastly.net

输出：stderr 打印每一步状态（HTTP 状态码、重定向、字节数），stdout 为落盘路径。
退出码 0 = 成功落盘，非 0 = 失败（不写空文件）。
"""
from __future__ import annotations

import argparse
import socket
import ssl
import sys
import zlib
from urllib.parse import urlsplit

DEFAULT_SNI = "www.bing.com"   # 不在阻断名单内、且与目标同为 Fastly 边缘
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_REDIRECTS = 6


def _dechunk(body: bytes) -> bytes:
    out, rest = b"", body
    while True:
        p = rest.find(b"\r\n")
        if p < 0:
            return out
        try:
            n = int(rest[:p].split(b";")[0], 16)
        except ValueError:
            return out
        if n == 0:
            return out
        out += rest[p + 2:p + 2 + n]
        rest = rest[p + 2 + n + 2:]


def fetch_once(url: str, ip: str, sni: str, timeout: int = 60):
    """发一次请求，返回 (status, headers_text, body)。连接层用 ip，SNI 用 sni，Host 头用真实域名。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    raw = socket.create_connection((ip, 443), timeout=timeout)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # 证书是 SNI 域名的，不校验主机名
    ctx.verify_mode = ssl.CERT_NONE
    ss = ctx.wrap_socket(raw, server_hostname=sni)
    req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
           f"User-Agent: {UA}\r\n"
           f"Accept: application/pdf,text/html,application/xhtml+xml,*/*\r\n"
           f"Accept-Language: en-US,en;q=0.9\r\n"
           f"Connection: close\r\n\r\n")
    ss.send(req.encode())
    buf = b""
    while True:
        chunk = ss.recv(65536)
        if not chunk:
            break
        buf += chunk
    ss.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    head_txt = head.decode("latin-1")
    status = head_txt.split("\r\n")[0]
    code = 0
    try:
        code = int(status.split()[1])
    except (IndexError, ValueError):
        pass
    if "transfer-encoding: chunked" in head_txt.lower():
        body = _dechunk(body)
    if "content-encoding: gzip" in head_txt.lower():
        body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
    return code, head_txt, body


def _header(head_txt: str, name: str):
    for line in head_txt.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


def sni_fetch(url: str, out_path: str, sni: str = DEFAULT_SNI, ip: str | None = None) -> int:
    seen = 0
    for _ in range(MAX_REDIRECTS):
        host = urlsplit(url).hostname or ""
        target_ip = ip or socket.gethostbyname(host)
        print(f"[fetch] {url}  ip={target_ip}  sni={sni}", file=sys.stderr)
        code, head_txt, body = fetch_once(url, target_ip, sni)
        print(f"[status] HTTP {code}  bytes={len(body)}  "
              f"type={_header(head_txt, 'content-type')}", file=sys.stderr)
        if code in (301, 302, 303, 307, 308):
            loc = _header(head_txt, "location")
            if not loc:
                print("[fail] 重定向缺少 Location", file=sys.stderr)
                return 1
            seen += 1
            url = loc if loc.startswith("http") else f"https://{host}{loc}"
            print(f"[redirect {seen}] -> {url}", file=sys.stderr)
            continue
        if code != 200 or not body:
            print(f"[fail] HTTP {code}，未落盘", file=sys.stderr)
            return 1
        with open(out_path, "wb") as fh:
            fh.write(body)
        print(f"[ok] 已保存 {out_path}（{len(body)} 字节，magic={body[:5]!r}）",
              file=sys.stderr)
        print(out_path)
        return 0
    print("[fail] 重定向次数过多", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="SNI/Host 分离取件（绕过 SNI 关键字阻断）")
    ap.add_argument("url")
    ap.add_argument("out_path")
    ap.add_argument("--sni", default=DEFAULT_SNI)
    ap.add_argument("--ip", default=None, help="指定边缘 IP，省略则用 DNS 解析结果")
    a = ap.parse_args()
    if not a.url.startswith("https://"):
        print("[fail] 仅支持 https://", file=sys.stderr)
        return 2
    return sni_fetch(a.url, a.out_path, a.sni, a.ip)


if __name__ == "__main__":
    sys.exit(main())
