#!/usr/bin/env python3
"""search_web.py — 检索核验的**兜底**执行器（流程第 2 步）。

**定位先说清楚：环境自带搜索工具（如宿主提供的 web_search）能用时，先用它；
本脚本是它不可用时的兜底**——没有搜索工具、或工具因缺凭据报错时，才落到这里。
不要把兜底当成首选：平台搜索给的是结构化结果、可信度判定也由平台负责，本脚本解析的是引擎 HTML，结构一改就得跟着改。

为什么要有它：第 2 步要求「专业术语须真搜索」，但此前没有统一的搜索入口——
调哪个引擎、结果算不算数、留痕的 `search_url` 写什么，全靠调用方临场决定。
实测这一天里最贵的一次返工就出在这里：某引擎对「城中村改造 政策」返回 HTTP 200、
4 条结果、结构完全正常，**内容却全是《城中之城》电视剧与「城中区」楼盘**。
只判「请求成功」的流程会把这种回包照单全收，接着按无关材料写卡——而报错一个都没有。

故本脚本的判据取**正面定义**：查询词块必须在标题或摘要里真的出现，才算这一路可信。
不信就换下一个引擎；全都不信即非 0 退出，绝不返回「空结果 = 没问题」。
这与 `net_util` 的可重发判定同源：正面白名单静默归入「不安全」只是多问一次，
反面定义静默归入「安全」则会写出一张基于无关材料的卡。

**它与抓取的分工**：本脚本只负责「搜到了什么、这一路可不可信、来源 URL 是什么」，
不替调用方写结论。`verify.json` 里的 `conclusion` 必须由读过结果的人（或代理）写，
本脚本给的是可核对的原始结果与真实搜索地址。

用法：
  python scripts/search_web.py "城中村改造 定义"
  python scripts/search_web.py "漫步者 S2000MKIII" --json _tmp/search_s2000.json
  python scripts/search_web.py "非改居" --engines sogou,bing --min-hits 1

退出码：
  0  至少一路引擎给出可信结果（回包里 engine_used 指明是哪一路）
  1  所有引擎都不可信或不可用——**不要**当成「没有这个说法」，先换查询词或换网络
  2  用法错误
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import net_util
from cli_util import Opt, UsageError, cli_entry, parse_options

DEFAULT_ENGINES = ("sogou", "bing", "baidu")
DEFAULT_TIMEOUT = 25.0
# 词块命中数下限。None = 自动取「半数（至少 1 个）」——
# 同一说法在结果里常被缩写或改写，要求全部词块命中会误拦真结果；
# 而实测的假命中里一个词块都不出现，半数足以把它们挡在外面。
DEFAULT_MIN_HITS = None

# 查询里的这些词块不承载「这个对象是什么」，命中它们不构成相关性证据。
# 只收问句尾巴与常识性虚词，不收领域词——「政策」「参数」是实义，命中该算数。
#
# 通用词（generic）：与任何主题搭配都成立的词。它们**不单独构成证据**——「相机」「App」
# 这类词在十万条结果里都能找到，命中它们说明不了答对了问题；但它们与专有名词同时出现时
# 仍是有效佐证。实测查「Chromagraph 相机 App 莱恩约翰逊」时，真实结果只出现品牌名
# 「Chromagraph」，旧判据要求「半数词块命中」于是把它否掉，而通用词恰好是最难中的那些。
_GENERIC_WORDS = frozenset({
    "相机", "App", "app", "应用", "软件", "工具", "官网", "官方", "下载",
    "发布", "推出", "新闻", "资讯", "科技", "数码", "智能", "手机",
    "公司", "企业", "产品", "功能", "使用", "如何", "怎么", "是什么",
})
# 判定「这一路可信」时，至少要有这么长的一个词块命中。
# 实测：查询「PEP Python 增强提案 状态」时某引擎的首条是「人民教育出版社官方网站」
# （其英文简称同样是 PEP），它同时含「PEP」与「状态」，按命中个数算能过，内容却完全无关。
# 短缩写与通用词凑出的命中不计为证据；长词块（4 字及以上）才承载查询的特指信息。
STRONG_TOKEN_MIN = 4

_STOPWORDS = frozenset({
    "定义", "是什么", "什么意思", "含义", "介绍", "怎么", "如何", "为什么", "哪些",
})

_TAG_RE = re.compile(r"<[^>]+>")
_ENT_RE = re.compile(r"&[a-zA-Z#0-9]+;")
_SPLIT_RE = re.compile(r"[\s，。、；：！？,.;:!?()（）\[\]【】「」『』“”\"'/|]+")


def _clean(text: str) -> str:
    """去标签、去实体、压空白——三个解析器共用一份，避免各写一套。"""
    return re.sub(r"\s+", " ", _ENT_RE.sub(" ", _TAG_RE.sub("", text or ""))).strip()


def query_tokens(query: str) -> list:
    """把查询切成用于相关性判定**词块**（不是分词，只切空白与标点）。

    判据要的是「查询里有没有东西真的出现在结果里」，所以切得粗一点更安全：
    切太细会把「城中村」拆出「城中」，而「城中区」正好含这两个字——
    那正是要拦掉的假命中。
    """
    raw = _SPLIT_RE.split(query.strip().strip("「」『』“”\"'"))
    seen, tokens = set(), []
    for token in raw:
        token = token.strip()
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens



def relevance(query: str, results: list, min_hits=None, top_k: int = 3) -> dict:
    """这一路结果是否可信：查询词块是否真的出现在标题/摘要里。

    返回 `{"ok", "tokens", "matched", "need"}`。`need` 默认取词块数的半数（至少 1）：
    全都不命中说明引擎答的是另一个问题（实测假命中正是 0 个）；要求全部命中则会把
    「投融管退一体化」这类被缩写的真结果误拦。判据只数**命中个数**，不做模糊匹配——
    模糊匹配会把「城中区」判成「城中村」。
    """
    tokens = query_tokens(query)
    # 只看引擎自己排在前面的几条，不看整页并集。
    # 实测反例：查「PEP Python 增强提案 状态」时前三条全是「人民教育出版社」（英文简称
    # 也是 PEP），真正对的 PEP 8 文档排在第 7 条。整页取并集会让这一路判成可信——
    # 相关性不能靠「九条里有一条对」凑出来，那等于把排序能力还给调用方人肉承担。
    results = results[:top_k] if top_k and top_k > 0 else results
    # 词块判定用**字符包含**而非整块相等：查询「交警 女子铁骑队」时，真实结果标题写作
    # 「女子交警铁骑队」（同词交错），整块比对会判「只命中 1/1」而否掉一整页对题结果
    # （实测搜狗 6 条全是各地女子铁骑队报道，全被否）。故短词块（2-3 字）退化为子串判断，
    # 长词块仍按整块——长词块才是承载特指信息的部分。
    # 判据以**专有词块**为准：先剔掉通用词，只数能标识主题的那些。
    # 全查询都是通用词时（如「相机 App」）退回全部词块，否则无从判断。
    strong_tokens = [t for t in tokens if t not in _GENERIC_WORDS]
    judging = strong_tokens or tokens
    if not tokens:
        need = 0
    elif min_hits is None:
        need = max(1, (len(judging) + 1) // 2)
    else:
        need = min(min_hits, len(judging))
    haystack = " ".join(
        f"{item.get('title', '')} {item.get('snippet', '')}" for item in results
    ).casefold()
    def _hit(token: str) -> bool:
        t = token.casefold()
        if t in haystack:
            return True
        # 译名/别名的姓部分单独出现也算命中：「莱恩约翰逊」对上标题里的「约翰逊」
        # （实测搜狗首条就是 IT 之家原文，只因写法差一个译名用字被否）。
        # 只认**姓氏级**片段：取词块最后 2-3 个汉字，避免把任意短子串当证据。
        if not token.isascii() and len(token) >= 3:
            for size in (3, 2):
                if len(token) > size and token[-size:] in haystack:
                    return True
        # 中文词块可能被插入修饰语而不再连续（查询「交警 女子铁骑队」，真实标题写作
        # 「女子交警铁骑队」——同五个字换了语序）。此时按**字集合包含**判断：词块的每个字
        # 都出现在结果里，即算命中。这把「换语序」与「答错问题」区分开：
        # 假命中（《城中之城》对上「城中村改造」）缺「改」「造」等字，仍然漏底。
        # 只对 2-5 字的中文词块生效；更长的词块仍要求整块出现，避免把一句话拆成字后误放。
        if not token.isascii() and 2 <= len(token) <= 5:
            return all(ch in haystack for ch in token)
        return False
    matched = [token for token in judging if _hit(token)]
    strong = [token for token in matched if len(token) >= STRONG_TOKEN_MIN]
    has_strong = any(len(token) >= STRONG_TOKEN_MIN for token in tokens)
    return {
        "ok": bool(tokens) and len(matched) >= need and (bool(strong) or not has_strong),
        "tokens": judging,
        "matched": matched,
        "need": need,
    }


# ── 三个引擎的结果解析 ────────────────────────────────────────────────
# 只做一件确定的事：把「标题 / 地址 / 摘要」三样从回包里捞出来。
# 捞不到就是捞不到（block 数为 0），不猜、不把整页当一条结果。

_BING_BLOCK = re.compile(r'<li class="b_algo"[\s\S]*?</li>', re.I)
_SOGOU_BLOCK = re.compile(r'<div class="vrwrap"[\s\S]*?</div>\s*</div>', re.I)
_BAIDU_BLOCK = re.compile(r'<div[^>]+class="[^"]*c-container[^"]*"[\s\S]*?</div>\s*</div>', re.I)

_TITLE_RES = (
    re.compile(r"<h2[^>]*>([\s\S]*?)</h2>", re.I),
    re.compile(r"<h3[^>]*>([\s\S]*?)</h3>", re.I),
)
_SNIPPET_RES = (
    re.compile(r'<p[^>]*>([\s\S]*?)</p>', re.I),
    re.compile(r'<div[^>]+class="[^"]*(?:text-layout|fz-mid|space-txt|c-abstract)[^"]*"[^>]*>([\s\S]*?)</div>', re.I),
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
# 结果块里的第一个 href 常常是样式表或脚本（搜狗百科卡片就是），
# 拿它当「来源地址」会把留痕指向一个 .css 文件。只认非资源类链接。
_ASSET_RE = re.compile(r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?)(?:\?|$)", re.I)


def _first(pattern_list, text):
    for pattern in pattern_list:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return ""


def _result(block: str, base: str) -> dict:
    title = _clean(_first(_TITLE_RES, block))
    snippet = _clean(_first(_SNIPPET_RES, block))
    url = ""
    for href in _HREF_RE.findall(block):
        href = href.replace("&amp;", "&")
        if href.startswith(("javascript:", "#")) or _ASSET_RE.search(href):
            continue
        url = href
        break
    if url.startswith("/"):
        url = base + url
    return {"title": title, "url": url, "snippet": snippet}


def parse_bing(html: str) -> list:
    return [_result(b, "https://www.bing.com") for b in _BING_BLOCK.findall(html)]


def parse_sogou(html: str) -> list:
    return [_result(b, "https://www.sogou.com") for b in _SOGOU_BLOCK.findall(html)]


def parse_baidu(html: str) -> list:
    return [_result(b, "https://www.baidu.com") for b in _BAIDU_BLOCK.findall(html)]


# 引擎名 -> (搜索地址模板, 解析器)。地址即留痕里的 `search_url`：
# 它必须是**真发出过的那一次搜索**，所以由同一张表生成，不另抄一份。
def _quote(query: str) -> str:
    return urllib.parse.quote(query)


ENGINES = {
    "sogou": (lambda q: f"https://www.sogou.com/web?query={_quote(q)}", parse_sogou),
    "bing": (lambda q: f"https://www.bing.com/search?q={_quote(q)}&setlang=zh-CN&cc=CN", parse_bing),
    "baidu": (lambda q: f"https://www.baidu.com/s?wd={_quote(q)}", parse_baidu),
}


def _fetch(url: str, timeout: float, via: str) -> str:
    """默认取页通道：与抓取、写云共用 `net_util`，代理端口由它自己读环境变量。"""
    reply = net_util.request(
        url, headers={"User-Agent": net_util.user_agent()}, timeout=timeout, via=via
    )
    return reply.body.decode("utf-8", errors="replace")


def run_search(query: str, *, engines=DEFAULT_ENGINES, min_hits=DEFAULT_MIN_HITS,
               timeout=DEFAULT_TIMEOUT, via="auto", fetcher=None) -> dict:
    """逐路尝试，返回第一路**可信**的结果；全不可信时 `engine_used` 为 None。

    `fetcher(engine, url)` 可注入，用例据此离线跑完整的换路逻辑（不联网）。
    """
    fetcher = fetcher or (lambda engine, url: _fetch(url, timeout, via))
    rejected = []
    for engine in engines:
        builder, parser = ENGINES[engine]
        url = builder(query)
        try:
            html = fetcher(engine, url)
        except net_util.TransportError as exc:
            # 通道不可用与「结果不可信」分开报：前者换网络，后者换引擎或换查询词。
            rejected.append({"engine": engine, "reason": f"通道不可用：{exc}",
                             "search_url": url, "count": 0})
            continue
        results = parser(html)
        verdict = relevance(query, results, min_hits)
        if verdict["ok"]:
            return {"query": query, "engine_used": engine, "search_url": url,
                    "results": results, "verdict": verdict, "rejected": rejected}
        reason = (f"结果 {len(results)} 条，查询词块只命中 {len(verdict['matched'])}/{verdict['need']}"
                  if results else "回包里没有可解析的结果块")
        sample = results[0]["title"][:40] if results else ""
        rejected.append({"engine": engine, "reason": reason + (f"（首条：{sample}）" if sample else ""),
                         "search_url": url, "count": len(results)})
    return {"query": query, "engine_used": None, "search_url": None, "results": [],
            "verdict": relevance(query, [], min_hits), "rejected": rejected}


_OPTS = [
    Opt("--engines", "engines", default=",".join(DEFAULT_ENGINES)),
    Opt("--min-hits", "min_hits", "int", low=1, default=None),
    Opt("--timeout", "timeout", "float", low=1, default=DEFAULT_TIMEOUT),
    Opt("--via", "via", default="auto"),
    Opt("--json", "json_out", default=None),
    Opt("--max-results", "max_results", "int", low=1, default=6),
]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    ns, pos = parse_options(args, _OPTS, positional_ok=True)
    if not pos:
        raise UsageError("缺查询词：python scripts/search_web.py \"<查询词>\" [选项]")
    if len(pos) > 1:
        raise UsageError(f"只接受一个查询词，多给了 {len(pos) - 1} 个：{' '.join(pos[1:])}")
    query = pos[0]
    engines = [e.strip() for e in str(ns["engines"]).split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        raise UsageError(f"未知引擎 {'、'.join(unknown)}；可选：{'、'.join(ENGINES)}")
    if not engines:
        raise UsageError("--engines 不能为空")
    # 数值已由 parse_options 转好（声明了 kind="int"），此处不再二次转换——
    # 再走一次 int_arg 会把 int 当字符串 strip，报出与参数无关的 AttributeError。
    min_hits = ns["min_hits"]
    max_results = ns["max_results"]

    report = run_search(query, engines=engines, min_hits=min_hits,
                        timeout=ns["timeout"], via=ns["via"])
    print(f"[查询] {query}")
    for item in report["rejected"]:
        print(f"[否决] {item['engine']}：{item['reason']}")
    if report["engine_used"]:
        print(f"[采用] {report['engine_used']}（{len(report['results'])} 条）")
        print(f"[来源] {report['search_url']}")
        for index, item in enumerate(report["results"][:max_results], 1):
            print(f"  {index}. {item['title'][:70]}")
            if item["snippet"]:
                print(f"     {item['snippet'][:160]}")
            if item["url"]:
                print(f"     {item['url'][:110]}")
    else:
        print("[失败] 没有一路引擎给出可信结果——不是「没有这个说法」，"
              "先换查询词或换网络重试", file=sys.stderr)

    if ns["json_out"]:
        out = Path(ns["json_out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(report)
        payload["results"] = report["results"][:max_results]
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[文件] {out}")
    return 0 if report["engine_used"] else 1


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=2))
