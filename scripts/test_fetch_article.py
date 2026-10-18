#!/usr/bin/env python3
"""fetch_article.py 的离线回归用例（全部不联网）。

只测「解码 / 抽取 / 元信息」这套纯函数；网络路径由 CLI 现场验证。
退出码 0 = 全部通过。
"""
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FA = _load("fetch_article", "fetch_article.py")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


def test_decode():
    raw = "中文编码测试".encode("gb18030")
    out = FA.decode_html(raw, "")
    check("decode：无 charset 时回退 gb18030", out == "中文编码测试", repr(out))
    out = FA.decode_html("<p>hello</p>".encode("utf-8"), "text/html; charset=utf-8")
    check("decode：按 Content-Type 解 UTF-8", "hello" in out, repr(out))


def test_to_text():
    html = (
        "<script>var x=1;</script>"
        "<style>.a{}</style>"
        "<nav>导航</nav>"
        "<p>A &amp; B</p><ul><li>一</li><li>二</li></ul>"
        "<p>下半段<br>换行</p>"
    )
    text = FA.to_text(html)
    check("to_text：去 script/style/nav", "var x" not in text and "导航" not in text, text)
    check("to_text：实体还原", "A & B" in text, text)
    check("to_text：列表项成行", "- 一" in text and "- 二" in text, text)
    check("to_text：br 转换行", "下半段\n换行" in text, text)


def test_extract_site_pattern():
    html = (
        "<html><body><nav>站点头部</nav>"
        '<div id="article-body"><p>' + "正文内容" * 80 + "</p></div>"
        '<div class="author-card">作者卡</div></body></html>'
    )
    text, channel = FA.extract(html)
    check("extract：dev.to 专用选择器", channel == "站内专用选择器", channel)
    check("extract：正文取到且不含后续作者卡",
          "正文内容" in text and "作者卡" not in text, text[:80])


def test_extract_article_and_div():
    html = "<html><body><article><p>" + "文章正文" * 80 + "</p></article></body></html>"
    text, channel = FA.extract(html)
    check("extract：<article> 通道", channel == "<article>" and "文章正文" in text, channel)

    html = (
        "<html><body>"
        '<div class="sidebar"><p>短侧栏</p></div>'
        '<div class="post-content"><p>' + "长正文" * 120 + "</p></div>"
        "</body></html>"
    )
    text, channel = FA.extract(html)
    check("extract：语义 div 取文本量最大者",
          channel == "语义 div 打分" and text.count("长正文") > 100, channel)


def test_balanced_div():
    html = '<div class="content"><div>内层</div>中间</div><div>外层后</div>'
    block = FA._balanced_div(html, 0)
    check("_balanced_div：嵌套配平且不吞后续 div",
          "内层" in block and "中间" in block and "外层后" not in block, block)


def test_inspect():
    html = (
        "<html><head><title>示例标题 - 站点</title>"
        '<meta property="article:published_time" content="PUB-DATE">'
        "</head><body></body></html>"
    )
    meta = FA.inspect(html)
    check("inspect：标题", "示例标题" in meta["title"], meta)
    check("inspect：发布日期", meta["published"] == "PUB-DATE", meta)


def test_short_content_detected():
    html = "<html><body><p>只有一句话</p></body></html>"
    text, _channel = FA.extract(html)
    check("extract：短正文可被阈值判定（JS 页识别的基础）",
          len(text) < FA.DEFAULT_MIN_CHARS, len(text))


if __name__ == "__main__":
    for fn in (test_decode, test_to_text, test_extract_site_pattern,
               test_extract_article_and_div, test_balanced_div, test_inspect,
               test_short_content_detected):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
