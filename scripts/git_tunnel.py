#!/usr/bin/env python3
"""git_tunnel.py — 本地 TCP 隧道代理：把指定主机的连接重定向到可达 IP。

背景
    本机对 github.com 的直连不稳定：DNS 解析出的 IP 与常用美国段 IP 都
    时通时断，且沙箱注入的 https_proxy(http://127.0.0.1:41264) 对该域名
    返回 502，故推送 / 拉取必须绕开环境代理，且不能只认一个固定 IP。

做法
    在本地开一个 HTTP CONNECT 代理，把 github.com:443 的流量在 TCP 层
    透传到可达 IP。TLS 与证书校验仍由 git 与 GitHub 端到端完成
    （SNI=github.com、证书校验 github.com，只是换了落地的 IP），
    因此不需要关校验、不降低安全性。
    候选 IP 全失败时回落系统 DNS：候选表只是快照，会随 GitHub 调整而
    整体失效；缺了回落，隧道会在候选漂移后对每次连接都回 502，而系统
    DNS 解析出的 IP 可能本来是通的——排查时极易误判为「网络不通」。

用法
    python scripts/git_tunnel.py                 # 监听 127.0.0.1:18123
    python scripts/git_tunnel.py 18124           # 指定端口
    python scripts/git_tunnel.py --quiet 18124   # 关闭逐连接日志
    python scripts/git_tunnel.py --print-hosts   # 打印 /etc/hosts 建议行后退出
    git -c http.proxy=http://127.0.0.1:18123 \
        -c https.proxy=http://127.0.0.1:18123 push origin HEAD

    说明：必须显式传 -c http.proxy，否则 git 会读到环境里的
    https_proxy=127.0.0.1:41264（沙箱代理），那条路对 github 是 502。

    本文件是 GitHub 可达 IP 的**唯一事实源**；ENVIRONMENT.md 只描述
    「怎么用」，不复制 IP 列表。IP 漂移时改这里，用 --print-hosts 生成
    hosts 片段（重查手法见 ENVIRONMENT.md）。
"""
import select
import socket
import sys
import threading

HOST = "127.0.0.1"

# 域名 → 可达 IP 候选（按顺序尝试，命中即用）。候选全失败时由 connect_target
# 回落系统 DNS，故本表只需列出「当前更可能通」的快照，不必覆盖全部可能 IP。
# 同段 IP 往往同生共死（路由层面成段不可达），候选取值宜跨段分散。
ROUTES = {
    "github.com": ["140.82.113.3", "20.205.243.166", "140.82.112.3", "140.82.121.4"],
    "api.github.com": ["20.205.243.168", "140.82.113.6", "140.82.112.6"],
    "codeload.github.com": ["20.205.243.165", "140.82.113.9"],
    "objects.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
    "raw.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
    "gist.github.com": ["140.82.113.3", "140.82.112.3"],
}

# 单个候选 IP 的连接超时。取值短一些：候选逐个尝试时，长超时会让每次
# CONNECT 都白等一轮，git 侧先超时，表现出来就成了「隧道不可用」。
CONNECT_TIMEOUT = 6
# 系统 DNS 回落的连接超时（解析 + 建连，留足余量）。
DNS_TIMEOUT = 15

# hosts 片段用第一条候选（权威值），与 ENVIRONMENT.md 的用法说明解耦。
HOSTS_PRIMARY = {h: ips[0] for h, ips in ROUTES.items()}


def _parse_args(argv):
    """解析位置参数与开关，返回 (port, verbose, action)。"""
    port, verbose, action = 18123, True, None
    for a in argv:
        if a in ("--quiet", "-q"):
            verbose = False
        elif a in ("--verbose", "-v"):
            verbose = True
        elif a == "--print-hosts":
            action = "print-hosts"
        elif a.isdigit():
            port = int(a)
        elif a.startswith("-"):
            sys.stderr.write(f"[tunnel] 未知参数 {a}（--quiet/--verbose/--print-hosts/<port>）\n")
            sys.exit(2)
    return port, verbose, action


PORT, VERBOSE, ACTION = _parse_args(sys.argv[1:])


def print_hosts():
    """输出可直接追加到 /etc/hosts 的映射行。"""
    for host in sorted(HOSTS_PRIMARY):
        print(f"{HOSTS_PRIMARY[host]:<18}{host}")


def log(msg):
    if VERBOSE:
        print(f"[tunnel] {msg}", flush=True)


def connect_target(host, port):
    """连到目标：先逐个试 ROUTES 候选 IP，全失败再回落系统 DNS 解析。

    回落是必需的。候选表是硬编码快照，整段漂移时逐个都会失败；若此时直接
    放弃，隧道会对每次连接都回 502，而系统 DNS 解析出的 IP 本来可能可通，
    故障因此被伪装成「网络不通」而难以察觉。
    """
    ips = ROUTES.get(host) or []
    last = None
    for ip in ips:
        try:
            s = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
            return s, ip
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  {host} -> {ip} 失败: {type(e).__name__}")
    if ips:
        log(f"  {host} 候选均失败（{type(last).__name__}），回落系统 DNS")
    s = socket.create_connection((host, port), timeout=DNS_TIMEOUT)
    return s, host


def relay(a, b):
    """双向透传，任一端关闭即收尾。"""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:  # noqa: BLE001
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def handle(client):
    try:
        client.settimeout(20)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
            if len(buf) > 65536:
                client.close()
                return

        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        request_line = lines[0]
        parts = request_line.split()
        if len(parts) < 2:
            client.close()
            return

        method, target = parts[0].upper(), parts[1]
        if method != "CONNECT":
            # 非 CONNECT：按普通 HTTP 代理转发（本项目用不到，简单回绝）
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            client.close()
            return

        host, _, port_s = target.rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443

        try:
            up, ip = connect_target(host, port)
        except Exception as e:  # noqa: BLE001
            log(f"  {host}:{port} 候选与系统 DNS 均不可达: {type(e).__name__}")
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            client.close()
            return

        client.settimeout(None)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            up.sendall(rest)
        log(f"CONNECT {host}:{port} -> {ip}  (bidirectional)")
        relay(client, up)
        log(f"CLOSE   {host}:{port} via {ip}")
    except Exception as e:  # noqa: BLE001
        log(f"  处理连接异常: {type(e).__name__}: {e}")
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    if ACTION == "print-hosts":
        print_hosts()
        return
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(64)
    log(f"listening on {HOST}:{PORT}  routes={sorted(ROUTES)}")
    while True:
        try:
            client, addr = srv.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
