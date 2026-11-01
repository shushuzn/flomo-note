#!/usr/bin/env python3
"""netdoctor.py — 出网通道体检：不问你端口，直接告诉你哪条通道能通。

为什么要有它：沙箱注入的 HTTP 代理端口**每次会话重新分配**（实测见过 15191 →
2286 → 29178 一路换），环境变量里的 `*_proxy` 与新会话不总同步。于是「抓取失败」
有一半不是网络断，而是拿旧端口去连——现象完全一样（连接被拒 / 502），靠肉眼看
分不出。此前判断方式是在命令行手写 `curl -x http://127.0.0.1:<端口>`，端口一旦
猜错，得到的结论就是「网络不通」，然后按错的结论去找原因。

本脚本把判断收敛成一个动作：

    python scripts/netdoctor.py                      # 体检出口（默认基线目标）
    python scripts/netdoctor.py --url <要抓的链接>    # 体检「这条链接」能不能抓

**判据是正面定义**：拿到任何 HTTP 响应（含 403/404）都算该通道可用——包已经到了
服务器，通道是通的；只有连接被拒、超时、DNS 失败才算该通道不通。把 403 判成
「通道坏了」会让人去换代理，越换越远。

**不写死端口**：代理地址一律从环境变量现读并原样打印，脚本与文档都不存具体端口。

用法：
  python scripts/netdoctor.py [--url URL ...] [--timeout N] [--quiet]

退出码：
  0  至少一条通道可用
  1  两条通道都不可用（此时才可下「出网有问题」的结论）
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import net_util
from cli_util import Opt, UsageError, cli_entry, force_utf8, parse_options

_OPTS = [
    Opt("--url", "urls", default=None, hint="可重复，或用逗号分隔"),
    Opt("--timeout", "timeout", "float", low=1, default=15.0),
    Opt("--quiet", "quiet", "flag"),
]

# 基线目标：只用来回答「出口本身通不通」，不代表任何业务站点可达。
BASELINE_URL = "https://www.baidu.com/"


def proxy_snapshot() -> tuple[dict, str]:
    """现读环境里的代理设置，返回 (代理键值, NO_PROXY 原值)。只看，不改。"""
    proxies = urllib.request.getproxies()
    bypass = proxies.pop("no", "") or ""
    return {k: v for k, v in sorted(proxies.items()) if k != "no"}, bypass


def probe(url: str, timeout: float, direct: bool) -> dict:
    """转调 `net_util.probe`。

    本脚本只负责「把结论讲给人听」，通道判据（什么算通）留在唯一实现里——
    判据抄两份，就会出现一个说通、一个说不通，而没人知道该信哪个。
    """
    return net_util.probe(url, timeout=timeout, direct=direct)


def verdict(rows: list[dict]) -> tuple[str, int]:
    """把一组探测结果翻成一句可执行的结论，返回 (结论行, 退出码)。

    结论必须落到「下一步敲什么命令」，否则读的人还得再猜一次——那正是本脚本要
    消灭的循环。
    """
    ok = [r["channel"] for r in rows if r["ok"]]
    if not rows:
        return "没有可探测的通道", 1
    if not ok:
        return (
            "两条通道都不可用——先确认网络/代理进程是否重启，别在同一通道上反复重试；"
            "仍不通再按 ENVIRONMENT.md 的通道清单换路子",
            1,
        )
    if "环境代理" in ok and "直连" in ok:
        return ("两条通道均可用 → 直接跑 fetch_article.py（默认 --via auto 即可）", 0)
    if "环境代理" in ok:
        return (
            "仅环境代理可用、直连不通 → 仍可直接跑 fetch_article.py；"
            "若抓取报通道错误，加 --via proxy 强制走代理",
            0,
        )
    return (
        "仅直连可用、环境代理不通 → fetch_article.py 默认会自动回退；"
        "也可显式加 --via direct",
        0,
    )


def _targets(ns) -> list[str]:
    raw = ns.get("urls")
    if not raw:
        return [BASELINE_URL]
    items = [part.strip() for part in re.split(r"[,\n]", raw) if part.strip()]
    for item in items:
        if not item.lower().startswith(("http://", "https://")):
            raise UsageError(f"--url 需要 http(s) 链接，收到 {item!r}")
    return items


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    # 与 fetch_article 不同：本脚本的目标**不是必需参数**，故无参数时直接跑基线体检
    # ——「一条命令就能得到结论」是它存在的理由；打帮助反而多一步。
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    ns, pos = parse_options(args, _OPTS, positional_ok=True)
    if pos:
        # 位置参数收成 `--url`：`netdoctor.py <链接>` 是最自然的写法，不该报用法错。
        extra = " ".join(pos)
        ns["urls"] = f"{ns['urls']},{extra}" if ns["urls"] else extra
    urls = _targets(ns)
    timeout = ns["timeout"]
    quiet = ns["quiet"]

    proxies, bypass = proxy_snapshot()
    if not quiet:
        shown = "，".join(f"{k}={v}" for k, v in proxies.items()) or "无"
        print(f"[环境] 代理变量：{shown}")
        print(f"[环境] NO_PROXY：{bypass or '（未设置）'}")
        print("[环境] 端口每次会话重新分配，故脚本与文档都不写死端口，一律现读")

    all_rows: list[dict] = []
    for url in urls:
        if not quiet:
            print(f"\n[目标] {url}")
        rows = []
        for direct in (False, True):
            row = probe(url, timeout, direct)
            rows.append(row)
            all_rows.append(row)
            if not quiet:
                mark = "PASS" if row["ok"] else "FAIL"
                print(f"  {row['channel']}  {mark}  {row['detail']}  ({row['seconds']:.2f}s)")
        if not quiet:
            line, _ = verdict(rows)
            print(f"  → {line}")

    line, code = verdict(all_rows)
    print(f"\n[结论] {line}")
    return code


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=1))
