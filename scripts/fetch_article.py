#!/usr/bin/env python3
"""fetch_article.py — 抓取网页并抽成纯文本正文文件（写卡流程第 1 步的抓取通道）。

为什么要有它：写卡每轮第一步都是「抓网页 → 抽正文 → 读回」。此前这步靠 curl 加临时
正则，站内结构一变、临时路径写错就要反复重试，而抽出来的文本干不干净没人把关。
本脚本把「取网页、判编码、去导航与脚本、抽正文、落文件」收敛到一处，并把抽检结果
（标题 / 发布日期 / 字符数 / 输出路径）打成短结论。

**抓回仍须读回全文**：本脚本只负责把文本落到文件，不替调用方阅读——流程第 1 步的
「抓回即读回」纪律不变，脚本末尾会打印下一步该读哪个文件。

**首尾噪音裁掉，且裁了会说**：开头连续的元信息行（面包屑 / 标题行 / 日期 / 来源 /
作者 / 责编 / 评论）与结尾的推荐位、相关文章、APP 下载、声明块，在落文件前裁掉；
裁了几行、从哪句起、长度从多少变到多少，都打进 `[裁剪]` 行——不静默截断。
`--min-chars` 只在抽取阶段判定，裁剪不会把合格页误判成失败页。

**抽了一半也会说**：字数过短只是失败的一种形态，右界提前收束时正文会在某个小节
末尾戛然而止，看起来像自然收尾。故抽取后拿页面自带的小节标题对一遍：正文里有、
页面里没有的标题会打成 `[告警]`，指明断在哪个章节之后——`[告警]` 不改抽取结果，
只提醒先核对原文再动笔。

**发布日期多一条兜底**：meta / `<time>` 取不到时，从正文的日期行里取（中文站常把
发布时间单独成行、页面却不给 meta），并标明来源是「页面 meta」还是「正文兜底」。

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import cli_entry, float_arg, int_arg  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MIN_CHARS = 300

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 站内专用抽取器。
# dev.to 的正文在 `<div id="article-body">`。旧写法以 `</div>\s*<div` 作右界，
# 理由是「用平衡扫描反而会把整页吞进来」——但真正的坑在另一边：正文里只要出现
# 相邻两个 div（代码高亮块就是三层 div 套按钮 svg），那个边界就被当成容器结尾，
# 后半个正文整段丢掉，且截断处落在代码块末尾，看起来像正常结束。WordPress 那篇
# 因此只留下一半（Step 4 之后全丢）。现改为「定位起始标签 + 配平到容器结束」。
_SITE_ANCHORS = [
    re.compile(r'<div[^>]*\bid=["\']article-body["\'][^>]*>', re.S | re.I),
]
# 兜底右界规则：仅在配平块吞进了评论区（容器内出现评论节点）时使用——
# 宁可截断，也不把评论区当正文。
_SITE_PATTERNS = [
    re.compile(r'<div[^>]*\bid=["\']article-body["\'][^>]*>(.*?)</div>\s*<div', re.S | re.I),
]
_COMMENT_NODE = re.compile(
    r'id=["\']comments["\']|class=["\'][^"\']*\bcomment-list\b', re.I
)

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

# ── 首尾噪音 ────────────────────────────────────────────────────────────────
# 实测（IT之家一条手机资讯）：抽出的正文同时带着开头一串元信息（面包屑、标题、日期、
# 来源、作者、责编、「评论：」）和结尾的「下载 APP / 相关文章 / 关键词」及六条无关
# 标题。前者会让人误把站内导航当正文读，后者能整段污染提炼——两条都必须在落文件前
# 裁掉，且**裁剪必须可见**（打印裁掉多少、从哪行起），不做静默截断。
_LEAD_LABEL = re.compile(
    r"^(?:来源|作者|责编|编辑|审核|评论|发布时间|发布日期|时间|日期|字号)\s*[：:]", re.I
)
_PURE_DATE = re.compile(
    r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_TAIL_MARKERS = re.compile(
    r"^(?:下载.{0,24}(?:APP|App|app).*"
    r"|相关(?:文章|阅读|推荐|链接|资讯|新闻)"
    r"|推荐阅读|热门推荐|延伸阅读|猜你喜欢|相关产品"
    r"|关键词\s*[：:]|责任编辑\s*[：:]|责编\s*[：:]"
    r"|(?:免责)?声明\s*[：:]|版权声明.*|转载请注明.*|本文来自.*)$"
)
_TEXT_DATE = re.compile(
    r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T]+(\d{1,2}:\d{2}(?::\d{2})?))?"
)


def _title_core(title: str) -> str:
    """标题去掉站点后缀（`xxx - IT之家` / `xxx | 站点` / `xxx_站点`）。"""
    core = re.split(r"\s+[-–—_|｜]\s+", title or "")[0]
    return core.strip()


def trim_lead_noise(text: str, title: str = "") -> tuple[str, int]:
    """裁掉开头连续的元信息行（面包屑 / 标题行 / 日期 / 来源作者责编 / 评论）。

    只从第一行起连续裁、最多看 15 行，遇到第一行正文即停——不按关键词全文删，
    否则正文里出现「来源」二字的句子会被误伤。返回 (正文, 裁掉的行数)。
    """
    lines = text.split("\n")
    core = _title_core(title)
    idx = 0
    limit = min(len(lines), 15)
    while idx < limit:
        s = lines[idx].strip()
        if not s:
            idx += 1
            continue
        is_meta = bool(
            _LEAD_LABEL.match(s)
            or _PURE_DATE.match(s)
            or (core and (s == core or (len(s) >= 12 and core.startswith(s))))
            or (len(s) <= 40 and (s.startswith("首页") or s.count(">") + s.count("›") >= 2))
        )
        if not is_meta:
            break
        idx += 1
    return "\n".join(lines[idx:]).strip(), idx


def trim_tail_noise(text: str, min_chars: int = DEFAULT_MIN_CHARS) -> tuple[str, str]:
    """裁掉结尾的推荐位 / 相关文章 / APP 下载 / 声明块。

    只在命中行的位置已过全文 40% 时裁（导航型「相关阅读」也可能出现在开头），
    且裁后长度不得低于 `min_chars`——两条护栏都过才动，否则原样返回。
    返回 (正文, 命中标记行)；未裁时标记为空串。
    """
    lines = text.split("\n")
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    if not filled:
        return text, ""
    for pos, i in enumerate(filled):
        if pos < 0.4 * len(filled):
            continue
        if _TAIL_MARKERS.match(lines[i].strip()):
            kept = "\n".join(lines[:i]).strip()
            if len(kept) >= min_chars:
                return kept, lines[i].strip()
            return text, ""
    return text, ""


def find_published_in_text(text: str) -> str:
    """正文里兜底找发布日期（页面无 meta / <time> 时用）。

    先认「整行就是一个日期」的行（多数中文站把发布时间单独成行），再退到前 3 行
    里出现的日期。都找不到返回空串——宁可报未取到，也不猜。
    """
    lines = [ln.strip() for ln in text.split("\n")[:15] if ln.strip()]
    for line in lines:
        if _PURE_DATE.match(line):
            m = _TEXT_DATE.search(line)
            if m:
                return _fmt_date(m)
    for line in lines[:3]:
        m = _TEXT_DATE.search(line)
        if m:
            return _fmt_date(m)
    return ""


def _fmt_date(m: re.Match) -> str:
    day = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return f"{day} {m.group(4)}" if m.group(4) else day


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
    for anchor in _SITE_ANCHORS:
        m = anchor.search(html)
        if m:
            block = _balanced_div(html, m.start())
            if not _COMMENT_NODE.search(block):
                text = to_text(block)
                if text:
                    return text, "站内专用选择器"
            break
    for pattern in _SITE_PATTERNS:
        m = pattern.search(html)
        if m:
            text = to_text(m.group(1))
            if text:
                return text, "站内专用选择器（右界兜底）"
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


_HEADING = re.compile(r"<h([23])\b[^>]*>(.*?)</h\1>", re.S | re.I)
_NOISE_BLOCK = re.compile(r"<(nav|aside|footer|header)\b.*?</\1>", re.S | re.I)
GAP_SHOW = 3


def page_headings(page: str) -> list[str]:
    """页面的二、三级标题（先剔掉导航、侧栏、页眉页脚里的那些）。"""
    cleaned = _NOISE_BLOCK.sub(" ", page or "")
    out: list[str] = []
    for m in _HEADING.finditer(cleaned):
        text = re.sub(r"\s+", " ", to_text(m.group(2))).strip()
        # 太短（「目录」「更多」）与纯数字（页码、年份）没有鉴别力，混进来只会
        # 制造「缺标题」的假警报。
        if len(text) < 4 or text.replace(" ", "").isdigit():
            continue
        out.append(text)
    return out


def heading_gap(page: str, text: str) -> dict:
    """正文是否在某个章节处就断了，返回缺掉的标题（分「结尾截断 / 中间缺失」）。

    只在能定位断点时给结论：正文里一个标题都没出现、或标题全在正文里，都返回
    `{}`——无从判断时不猜。

    【为什么要有它】抽取失败的形态不止「短到触发阈值」，还有「抽到了、但只抽到
    一半」：右界提前收束会让正文在某个小节末尾戛然而止，断口处的文字读起来像是
    自然收尾，字数也未必触发下限，于是按残缺正文起草、事后才发现后半篇没抽到。
    章节标题是页面现成的骨相：页面里有、正文里没有，就是内容丢了。

    **只作告警、不作判据**：标题可能只出现在导航或推荐位里，也可能本就属于被裁掉
    的附录块，故宁可多报一句让人核对，不据此改动抽取结果。
    """
    headings = page_headings(page)
    if not headings:
        return {}
    flat = re.sub(r"\s+", "", text or "").lower()

    def seen(h: str) -> bool:
        return re.sub(r"\s+", "", h).lower() in flat

    marks = [seen(h) for h in headings]
    if all(marks) or not any(marks):
        return {}
    last = max(i for i, ok in enumerate(marks) if ok)
    return {
        "last_seen": headings[last],
        "missing_tail": [h for h, ok in zip(headings[last + 1:], marks[last + 1:]) if not ok],
        "missing_before": [h for h, ok in zip(headings[:last], marks[:last]) if not ok],
    }


def _fmt_titles(items: list[str]) -> str:
    shown = "「" + "」「".join(items[:GAP_SHOW]) + "」"
    if len(items) > GAP_SHOW:
        shown += f"，另 {len(items) - GAP_SHOW} 个"
    return shown


def gap_warnings(gap: dict) -> list[str]:
    """把 `heading_gap` 的结果翻成给人看的告警行（空结果即无告警）。

    单独成函数是为了能离线断言文案：告警只在真的截断时出现，那次出现就是唯一
    一次提醒人的机会，措辞与顺序不能靠下次撞上才验证。
    """
    lines = []
    miss = gap.get("missing_tail") or []
    if miss:
        lines.append(
            f"[告警] 正文止于章节《{gap.get('last_seen', '')}》，其后 {len(miss)} 个"
            f"章节标题未出现在正文里：{_fmt_titles(miss)}——疑似抽取被截断，"
            "先核对页面原文再提炼，别按残缺正文写卡"
        )
    miss_before = gap.get("missing_before") or []
    if miss_before:
        lines.append(
            f"[提请注意] 另有 {len(miss_before)} 个章节标题在正文中找不到（不在结尾处）："
            f"{_fmt_titles(miss_before)}——多来自导航或推荐位，核对后自行判断"
        )
    return lines


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
            min_chars = int_arg(args[i + 1], "--min-chars", low=1)
            i += 2
        elif a == "--timeout" and i + 1 < len(args):
            timeout = float_arg(args[i + 1], "--timeout", low=1)
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

    # 发布日兜底：meta / <time> 都没有时，从正文的日期行取（裁剪会删掉该行，故先取）。
    pub_src = "页面 meta"
    if not meta["published"]:
        found = find_published_in_text(body)
        if found:
            meta["published"], pub_src = found, "正文兜底"
    raw_len = len(body)
    body, lead_cut = trim_lead_noise(body, meta["title"])
    body, tail_cut = trim_tail_noise(body, min_chars)

    if out is None:
        out = PROJECT_ROOT / "_tmp" / f"article_{_slug(url)}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    print(f"[抓取] {url}")
    print(f"[标题] {meta['title'] or '(未取到)'}")
    print(f"[发布] {meta['published'] or '(未取到)'}"
          + (f"（{pub_src}）" if meta["published"] else ""))
    print(f"[正文] {len(body)} 字符（通道：{channel}）")
    if lead_cut or tail_cut:
        cuts = []
        if lead_cut:
            cuts.append(f"开头 {lead_cut} 行元信息")
        if tail_cut:
            cuts.append(f"结尾「{tail_cut[:18]}」起")
        print(f"[裁剪] 原 {raw_len} 字符 → {len(body)}：{'、'.join(cuts)}")
    else:
        print("[裁剪] 无首尾噪音（原样保留）")
    gap = heading_gap(page, body)
    for line in gap_warnings(gap):
        print(line)
    print(f"[文件] {out}")
    print(f"[下一步] 读回全文：Read {out}（流程第 1 步纪律，未读回不得提炼）")
    if echo:
        print("--- 正文 ---")
        print(body)
    return 0


if __name__ == "__main__":
    # 用法错（参数写成非数字等）由入口渲染：`main` 里不再各写一份报错文案。
    sys.exit(cli_entry(main, usage_code=1))
