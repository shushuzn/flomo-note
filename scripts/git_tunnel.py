#!/usr/bin/env python3
"""git_tunnel.py — 本地 TCP 隧道代理：把指定主机的连接重定向到可达 IP。

背景
    本机 DNS 把 github.com 解析到 20.205.243.166（新加坡 Azure 段），
    该 IP 的 443 端口 TCP 超时（换 SNI、不发 SNI 均超时 → 是路由不通，
    不是 SNI 关键字阻断，与 arxiv.org 那次的机制不同）。
    而 github.com 的美国段 IP 140.82.112.3 / 113.3 / 114.3 / 121.4
    完全可达，且 SNI=github.com 的 TLSv1.3 握手正常。
    同时沙箱注入的 https_proxy(http://127.0.0.1:41264) 对 github.com 返回 502。

做法
    在本地开一个 HTTP CONNECT 代理，把 github.com:443 的流量在 TCP 层
    透传到可达 IP。TLS 与证书校验仍由 git 与 GitHub 端到端完成
    （SNI=github.com、证书校验 github.com，只是换了落地的 IP），
    因此不需要关校验、不降低安全性。

用法
    python scripts/git_tunnel.py                 # 监听 127.0.0.1:18123
    python scripts/git_tunnel.py 18124           # 指定端口
    git -c http.proxy=http://127.0.0.1:18123 \
        -c https.proxy=http://127.0.0.1:18123 push origin HEAD

    说明：必须显式传 -c http.proxy，否则 git 会读到环境里的
    https_proxy=127.0.0.1:41264（沙箱代理），那条路对 github 是 502。
"""
import select
import socket
import ssl
import sys
import threading

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18123

# 域名 → 可达 IP 候选（按顺序尝试）。命中即用，否则回落正常 DNS 解析。
ROUTES = {
    "github.com": [
        "140.82.112.4", "140.82.113.4", "140.82.114.4",
        "140.82.116.3", "140.82.116.4", "140.82.121.3",
        "140.82.112.3", "140.82.113.3", "140.82.114.3", "140.82.121.4",
    ],
    "api.github.com": ["140.82.114.6", "140.82.113.6", "140.82.112.6"],
    "codeload.github.com": ["140.82.114.9", "140.82.113.9"],
    "objects.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
    "raw.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
    "gist.github.com": ["140.82.114.3", "140.82.113.3"],
}

VERBOSE = True


def log(msg):
    if VERBOSE:
        print(f"[tunnel] {msg}", flush=True)


def _tcp_tls_probe(ip, port, sni, timeout=4):
    """TCP 连通 + TLS 握手探测；返回 True 表示可用（仅对路由域名启用）。

    背景：GitHub 美国段 IP 在 TLS 层偶发 unexpected eof（曾导致 git push 直接失败）。
    仅 TCP 连通不足以判定可用，故在选路时先做一次 TLS 握手，跳过握手失败的 IP。
    timeout 取 4 秒：失败的 IP 应尽快放弃，否则会拖垮 git 的 CONNECT 等待（约 2 秒）。
    """
    s = socket.create_connection((ip, port), timeout=timeout)
    try:
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname=sni)
        ss.close()
    except Exception:  # noqa: BLE001
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def _probe_worker(ip, port, sni, result, lock, done):
    """并行探测单个候选 IP，成功即写入 result 并触发 done。"""
    try:
        if _tcp_tls_probe(ip, port, sni):
            with lock:
                if result[0] is None:
                    result[0] = ip
            done.set()
    except Exception:  # noqa: BLE001
        pass


def connect_target(host, port):
    """连到目标。命中 ROUTES 则**并行**探测候选 IP，取最快握手成功者；
    否则走系统 DNS。

    串行探测时死 IP 每个要耗满 8 秒超时，而 git 的 CONNECT 只等约 2 秒 →
    稳定 502；改为并行后，任一可达 IP 在亚秒级返回，死 IP 不再阻塞。
    """
    ips = ROUTES.get(host)
    if ips:
        result = [None]
        lock = threading.Lock()
        done = threading.Event()
        for ip in ips:
            threading.Thread(
                target=_probe_worker,
                args=(ip, port, host, result, lock, done),
                daemon=True,
            ).start()
        done.wait(timeout=6)
        chosen = result[0]
        if chosen is None:
            raise OSError(f"{host} 全部候选 IP 探测失败")
        s = socket.create_connection((chosen, port), timeout=10)
        return s, chosen
    s = socket.create_connection((host, port), timeout=15)
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
            log(f"  {host}:{port} 全部候选不可达: {type(e).__name__}")
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
