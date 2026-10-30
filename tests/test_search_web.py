#!/usr/bin/env python3
"""search_web.py 的离线回归用例（不联网）。

钉住四件事，每一条都对应一个真实踩过的坑：

1. **HTTP 成功不等于搜对了**——实测某引擎对「城中村改造 政策」返回 200 与 4 条结构
   正常的结果，内容全是《城中之城》电视剧和「城中区」楼盘。用例把这份回包原样喂进去，
   断言它被判为**不可信**；只判「有结果就是通了」的实现会在这里红。
2. **词块切粗一点才安全**——「城中村改造」必须整块比对，切出「城中」就会命中
   「城中区」，把假命中放行。
3. **换路顺序真的生效**：前一路垃圾、后一路正常时，`engine_used` 必须指向后者，
   且 `search_url` 来自同一张引擎表（留痕地址不能另抄一份）。
4. **全不可信 ≠ 没有这个说法**：退出码必须非 0，不能返回空结果当作「查过了」。

全部离线：解析与判据是纯函数，换路逻辑用注入的 fetcher 跑，不建连、不读 token。

退出码 0 = 全部通过。
"""

COVERS = ("search_web",)

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import search_web as SW  # noqa: E402

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


# ── 夹具：三份最小 HTML，结构照真实回包裁到只留解析所需的部分 ──

BING_JUNK = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://baike.baidu.com/item/x">城中之城（2024年电视剧）_百度百科</a></h2>
<p>《城中之城》是由中央广播电视总台出品的金融反腐剧。</p></li>
<li class="b_algo"><h2><a href="https://lzh.fang.anjuke.com/">柳州城中房产网</a></h2>
<p>城中区楼盘信息，城中新开楼盘。</p></li>
</ol>
"""

BING_GOOD = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://www.edifier.com/cn/product/product-418.html">S2000MKIII - Edifier</a></h2>
<p>漫步者 S2000MKIII 采用三颗 TI 数字功放芯片。</p></li>
</ol>
"""

SOGOU_GOOD = """
<div class="vrwrap"><h3><a href="/link?url=abc">投融管退的最佳解释</a></h3>
<div class="text-layout">城市更新语境下的投融管退，指投资、融资、管理、退出四个环节。</div></div>
<div class="vrwrap"><h3><a href="/link?url=def">城市更新项目九大投融资模式</a></h3>
<div class="text-layout">统贷统还、银团贷款等模式常被用于城市更新项目。</div></div>
"""

BAIDU_GOOD = """
<div class="result c-container" id="1"><h3><a href="https://zhidao.baidu.com/q/1">非改居是什么意思</a></h3>
<div class="c-abstract">非改居指把非居住用房改建为居住用途。</div></div>
"""


def test_tokens():
    """词块：短词丢掉、停用词丢掉、去重、标点切开。"""
    got = SW.query_tokens("城中村改造 政策")
    check("两个实义词块", got == ["城中村改造", "政策"], got)
    check("问句尾巴不算证据", SW.query_tokens("非改居 是什么") == ["非改居"],
          SW.query_tokens("非改居 是什么"))
    check("全角标点也切", SW.query_tokens("投融管退一体化，城市更新") == ["投融管退一体化", "城市更新"],
          SW.query_tokens("投融管退一体化，城市更新"))
    check("单字块丢弃", SW.query_tokens("AI 的 用法") == ["AI", "用法"], SW.query_tokens("AI 的 用法"))
    check("重复块去重", SW.query_tokens("城市更新 城市更新") == ["城市更新"],
          SW.query_tokens("城市更新 城市更新"))


def test_junk_is_rejected():
    """核心用例：结构正常但内容无关的回包必须被判不可信。"""
    results = SW.parse_bing(BING_JUNK)
    check("垃圾回包能解析出条目（说明问题不在解析）", len(results) == 2, len(results))
    verdict = SW.relevance("城中村改造 政策", results)
    check("内容无关 → 判不可信", verdict["ok"] is False, verdict)
    check("命中数为 0", verdict["matched"] == [], verdict)

    # 反向：同一个查询碰上真结果就要放行，否则这条判据只是在拦一切。
    good = SW.relevance("S2000MKIII 漫步者", SW.parse_bing(BING_GOOD))
    check("内容相关 → 放行", good["ok"] is True, good)


def test_real_url_landing():
    """留痕地址必须来自同一张引擎表，且真发出过的那一次。"""
    report = SW.run_search("非改居", engines=("baidu",),
                           fetcher=lambda engine, url: BAIDU_GOOD)
    check("百度那一路被采用", report["engine_used"] == "baidu", report["engine_used"])
    check("search_url 与引擎表一致", report["search_url"] == SW.ENGINES["baidu"][0]("非改居"),
          report["search_url"])
    sogou = SW.run_search("城市更新", engines=("sogou",),
                          fetcher=lambda engine, url: SOGOU_GOOD)
    check("相对跳转补成绝对地址",
          sogou["results"][0]["url"].startswith("https://www.sogou.com/link?url="),
          sogou["results"][0]["url"])


def test_fallback_engine():
    """前一路垃圾 → 自动换下一路，并把否决原因留下。"""
    pages = {"bing": BING_JUNK, "sogou": SOGOU_GOOD}
    report = SW.run_search("投融管退一体化 城市更新", engines=("bing", "sogou"),
                           fetcher=lambda engine, url: pages[engine])
    check("改用第二路", report["engine_used"] == "sogou", report["engine_used"])
    check("否决记录留痕", [r["engine"] for r in report["rejected"]] == ["bing"],
          report["rejected"])
    check("否决原因点明命中数", "命中 0" in report["rejected"][0]["reason"],
          report["rejected"][0]["reason"])


def test_all_rejected():
    """全不可信必须报失败，不能返回空结果当作「查过了」。"""
    report = SW.run_search("城中村改造 政策", engines=("bing", "bing"),
                           fetcher=lambda engine, url: BING_JUNK)
    check("engine_used 为空", report["engine_used"] is None, report["engine_used"])
    check("两路都记了否决", len(report["rejected"]) == 2, len(report["rejected"]))
    check("main 退出码非 0", _main_with_stub() != 0, "退出码为 0")


def _main_with_stub():
    """把取页通道换成垃圾页，验证 CLI 层的退出码（不改磁盘、不联网）。"""
    original = SW._fetch
    SW._fetch = lambda url, timeout, via: BING_JUNK
    try:
        return SW.main(["城中村改造 政策", "--engines", "bing"])
    finally:
        SW._fetch = original


def test_transport_split():
    """通道不可用与结果不可信要说成两件事：前者换网络，后者换引擎/查询词。"""
    def boom(engine, url):
        raise SW.net_util.TransportError("连接超时", request_sent=True)

    report = SW.run_search("非改居", engines=("sogou",), fetcher=boom)
    check("通道错误记为通道不可用",
          "通道不可用" in report["rejected"][0]["reason"], report["rejected"][0]["reason"])
    check("通道错误也算否决（不放行）", report["engine_used"] is None, report["engine_used"])


def test_min_hits():
    """need 默认取半数（至少 1）：单词查询不能永远凑不满，双词查询不能要求全中。"""
    verdict = SW.relevance("非改居", SW.parse_baidu(BAIDU_GOOD))
    check("单词查询 need=1", verdict["need"] == 1, verdict)
    check("单词查询放行", verdict["ok"] is True, verdict)
    two = SW.relevance("投融管退一体化 城市更新", SW.parse_sogou(SOGOU_GOOD))
    check("双词查询 need=1（缩写不误拦）", two["need"] == 1 and two["ok"] is True, two)
    check("显式 min-hits 覆盖自动值",
          SW.relevance("投融管退一体化 城市更新", SW.parse_sogou(SOGOU_GOOD), 2)["ok"] is False,
          "显式要求 2 个时应判不可信")


def main():
    test_tokens()
    test_junk_is_rejected()
    test_real_url_landing()
    test_fallback_engine()
    test_all_rejected()
    test_transport_split()
    test_min_hits()
    bad = RESULTS.count(False)
    print(f"\n{len(RESULTS)} 项断言，{len(RESULTS) - bad} 通过，{bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
