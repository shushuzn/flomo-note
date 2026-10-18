#!/usr/bin/env python3
"""fetch_article.py — 抓取网页并抽成纯文本正文文件（写卡流程第 1 步的抓取通道）。

为什么要有它：写卡每轮第一步都是「抓网页 → 抽正文 → 读回」。此前这步靠 curl 加临时
正则，站内结构一变、临时路径写错就要反复重试，而抽出来的文本干不干净没人把关。
本脚本把「取网页、判编码、去导航与脚本、抽正文、落文件」收敛到一处，并把抽检结果
（标题 / 发布日期 / 字符数 / 输出路径）打成短结论。

**抓回仍须读回全文**：本脚本只负责把文本落到文件，不替调用方阅读——流程第 1 步的
「抓回即读回」纪律不变，脚本末尾会打印下一步该读哪个文件。

用法：
  python scripts/fetch_article.py <url> [--out PATH] [--min-chars N] [--timeout N]
  python scripts/fetch_article.py <url> --print          # 额外把正文打到 stdout（短文用）

退出码：
  0  抽取成功（已写文件）
  1  网络 / HTTP 失败，或响应不是 HTML
  2  抽取结果过短（多半是 JS 渲染页或反爬页），须换通道（webfetch / 镜像 / 预印本通道）
"""
from __future__ import annotations

import gzip
import html as html_mod
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIN_CHARS = 300

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 站内专用抽取器：按顺序尝试，命中即用。
# dev.to 的正文在 <div id="article-body">，正文容器结束后紧跟作者卡 / 评论区的 <div>，
# 故以 `</div>\s*<div` 作右界——用平衡扫描反而会把整页吞进来（外层 div 未闭合计数）。
_SITE_PATTERNS = [
    re.compile(r'<div[^>]*\bid=["\']article-body["\'][^>]*>(.*?)</div>\s*<div', re.S | re.I),
]

_REMOVE_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|nav|footer|aside|header|form|iframe|template|button)"
    r"\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_BLOCK_END = re.compile(
    r"</(?:p|div|section|article|main|h[1-6]|li|ul|ol|blockquote|pre|tr|table|figure|figcaption)>",
    re.I,
)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI = re.compile(r"<li\b[^>]*>", re.I)
_CELL = re.compile(r"</t[dh]>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_META_TIME = re.compile(
    r'<meta[^>]+(?:property|name)=["\']'
    r"(?:article:published_time|pubdate|publish-date|publish_date|date)"
    r'["\'][^>]*content=["\']([^"\']+)',
    re.I,
)
_META_TIME_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']'
    r"(?:article:published_time|pubdate|publish-date|publish_date|date)"
    r'["\']',
    re.I,
)
_TIME_TAG = re.compile(r"<time\b[^>]*\bdatetime=[\"']([^\"']+)", re.I)
_CANDIDATE_DIV = re.compile(
    r'<div[^>]*\b(?:class|id)=["\'][^"\']*'
    r"(?:article|content|post|entry|detail|main-text|body-text|left_zw|paragraph)"
    r'[^"\']*["\'][^>]*>',
    re.I,
)
_ARTICLE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.S | re.I)
_MAIN = re.compile(r"<main\b[^>]*>(.*?)</main>", re.S | re.I)


def decode_html(body: bytes, content_type: str = "") -> str:
    """按 Content-Type → <meta charset> → utf-8 → gb18030 的顺序解码。

    顺序不能反过来：先试 meta 会把 UTF-8 页里的 `charset=gb2312` 误信
    （cnBeta 一类老站常见），整页中文变乱码，且乱码文本还能"抽得出"正文。
    """
    if body[:2] == b"\x1f\x8b":  # 少数服务器无视 Accept-Encoding: identity
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        try:
            return body.decode(m.group(1), "replace")
        except LookupError:
            pass
    head = body[:4096].decode("ascii", "ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    if m:
        try:
            return body.decode(m.group(1), "replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("gb18030", "replace")


def to_text(fragment: str) -> str:
    """HTML 片段 → 可读纯文本。块级标签转换行，列表项加 `- `，实体还原。"""
    if not fragment:
        return ""
    text = _REMOVE_BLOCKS.sub(" ", fragment)
    text = _LI.sub("\n- ", text)
    text = _BR.sub("\n", text)
    text = _CELL.sub(" ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_mod.unescape(text)
    return _normalize(text)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    blank = False
    for line in text.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            out.append(line)
            blank = False
        elif out and not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def _balanced_div(html: str, start: int) -> str:
    """从 `<div` 起点做深度平衡扫描，返回该 div 的完整外层片段。

    正则无法配平嵌套 div，而"取到下一个 </div>"会把正文切成前半截。
    扫描上限 2 MB，防异常页面把复杂度拖爆。
    """
    token = re.compile(r"<div\b|</div>", re.I)
    depth = 0
    for m in token.finditer(html, start, start + 2_000_000):
        if m.group(0).lower().startswith("</"):
            depth -= 1
            if depth == 0:
                return html[start:m.end()]
        else:
            depth += 1
    return html[start:]


def extract(html: str) -> tuple[str, str]:
    """抽取正文，返回 (正文, 通道说明)。

    通道顺序：站内专用 → <article> → <main> → 含正文语义的 div 里文本量最大者 → 整页。
    每次取块都按「文本量」和 `min_chars` 取舍，避免抽到只有一句导语的侧栏。
    """
    for pattern in _SITE_PATTERNS:
        m = pattern.search(html)
        if m:
            text = to_text(m.group(1))
            if text:
                return text, "站内专用选择器"
    for label, pattern in (("<article>", _ARTICLE), ("<main>", _MAIN)):
        m = pattern.search(html)
        if m:
            text = to_text(m.group(1))
            if len(text) >= DEFAULT_MIN_CHARS:
                return text, label
    best = ("", "")
    for m in _CANDIDATE_DIV.finditer(html):
        block = _balanced_div(html, m.start())
        text = to_text(block)
        if len(text) > len(best[0]):
            best = (text, "语义 div 打分")
    if len(best[0]) >= DEFAULT_MIN_CHARS:
        return best
    return to_text(html), "整页兜底"


def _first(pattern: re.Pattern, html: str) -> str:
    m = pattern.search(html)
    if not m:
        return ""
    return _normalize(html_mod.unescape(_TAG.sub("", m.group(1))))


def inspect(html: str) -> dict:
    """抽取页面元信息（标题 / 发布日期）。只用于打印，不进卡片。"""
    title = _first(_TITLE, html) or _first(_H1, html)
    published = ""
    for pattern in (_META_TIME, _META_TIME_REV):
        m = pattern.search(html)
        if m:
            published = m.group(1).strip()
            break
    if not published:
        m = _TIME_TAG.search(html)
        if m:
            published = m.group(1).strip()
    return {"title": title, "published": published}


def _slug(url: str) -> str:
    path = re.sub(r"[?#].*$", "", url).rstrip("/")
    last = path.rsplit("/", 1)[-1] or "page"
    last = re.sub(r"[^A-Za-z0-9_-]+", "-", last).strip("-")
    return (last or "page")[:48]


def fetch(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """下载页面，返回 (html, content_type)。只发 GET，不改任何远端状态。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return decode_html(body, resp.headers.get("Content-Type", "")), resp.headers.get(
            "Content-Type", ""
        )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    url = args[0]
    out: Path | None = None
    min_chars = DEFAULT_MIN_CHARS
    timeout = 30.0
    echo = False
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--out" and i + 1 < len(args):
            out = Path(args[i + 1])
            i += 2
        elif a == "--min-chars" and i + 1 < len(args):
            min_chars = int(args[i + 1])
            i += 2
        elif a == "--timeout" and i + 1 < len(args):
            timeout = float(args[i + 1])
            i += 2
        elif a == "--print":
            echo = True
            i += 1
        else:
            print(f"[失败] 未知参数：{a}", file=sys.stderr)
            return 1
    if not url.lower().startswith(("http://", "https://")):
        print(f"[失败] 不是 http(s) 链接：{url}", file=sys.stderr)
        return 1

    try:
        page, content_type = fetch(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"[失败] 抓取未完成：{exc}", file=sys.stderr)
        return 1
    if "html" not in (content_type or "").lower() and "<html" not in page[:2000].lower():
        print(f"[失败] 响应不是 HTML（Content-Type: {content_type}）", file=sys.stderr)
        return 1

    body, channel = extract(page)
    meta = inspect(page)
    if len(body) < min_chars:
        print(
            f"[失败] 抽取仅 {len(body)} 字符（阈值 {min_chars}，通道：{channel}）——"
            "多半是 JS 渲染页或反爬页，改走 webfetch / 镜像 / 预印本通道",
            file=sys.stderr,
        )
        if body:
            print(f"[预览] {body[:200]}", file=sys.stderr)
        return 2

    if out is None:
        out = PROJECT_ROOT / "_tmp" / f"article_{_slug(url)}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    print(f"[抓取] {url}")
    print(f"[标题] {meta['title'] or '(未取到)'}")
    print(f"[发布] {meta['published'] or '(未取到)'}")
    print(f"[正文] {len(body)} 字符（通道：{channel}）")
    print(f"[文件] {out}")
    print(f"[下一步] 读回全文：Read {out}（流程第 1 步纪律，未读回不得提炼）")
    if echo:
        print("--- 正文 ---")
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
