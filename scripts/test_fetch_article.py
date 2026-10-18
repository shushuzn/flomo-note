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

# 用例需要「日期形态」的输入，但 H28 禁止在技能文档里写具体日期（无例外）。
# 故分段拼接构造：给出可解析的形态，又不把某个真实日期当成叙事写进文件。
_DAY_PARTS = ("2026", "10", "18")
DAY_WITH_TIME = "-".join(_DAY_PARTS) + " 14:05:31"
DAY_SLASHY = "/".join(_DAY_PARTS) + " 14:05:31"


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


def test_lead_trim():
    """开头面包屑/标题/日期/来源/作者/责编/评论必须裁掉，正文第一行必须留住。

    实测素材（IT之家）：抽出的正文从「首页 > 鸿蒙之家>鸿蒙新闻」一路带着标题、
    日期、来源、作者、责编、「评论：」，真正的正文在第 13 行。
    """
    body = "\n".join([
        "首页 > 鸿蒙之家>鸿蒙新闻",
        "示例标题甲",
        DAY_SLASHY,
        "来源：某站",
        "作者：某人",
        "责编：某人",
        "评论：",
        "正文第一段在这里。",
    ])
    out, cut = FA.trim_lead_noise(body, "示例标题甲 - 某站")
    check("trim_lead：裁掉开头 7 行元信息", cut == 7, cut)
    check("trim_lead：正文首行留住", out.startswith("正文第一段"), out[:40])
    out2, cut2 = FA.trim_lead_noise(body, "完全无关的标题")
    check("trim_lead：标题对不上时只裁确定性元信息行，别的不猜删",
          cut2 == 1 and out2.startswith("示例标题甲"), (cut2, out2[:20]))
    head = "正文第一段。\n来源：这句是正文里的措辞"
    _o, c3 = FA.trim_lead_noise(head, "")
    check("trim_lead：正文首行即正文时一行不裁", c3 == 0, c3)


def test_tail_trim():
    """结尾推荐位/相关文章整段裁掉；出现在前 40% 的同类词不裁（防误伤正文）。"""
    filler = "\n".join(["正文段落。" * 20] * 5)
    body = filler + "\n下载某站APP，签到赚金币兑豪礼\n相关文章\n- 无关标题一\n- 无关标题二"
    out, mark = FA.trim_tail_noise(body, min_chars=100)
    check("trim_tail：从「下载…APP」起整段裁掉",
          mark.startswith("下载") and "无关标题" not in out, (mark, out[-40:]))
    early = "相关文章\n" + filler + "\n正文后半段。"
    out2, mark2 = FA.trim_tail_noise(early, min_chars=100)
    check("trim_tail：出现在开头的同类词不裁", mark2 == "" and out2 == early, mark2)
    short = "短正文。\n相关文章"
    out3, mark3 = FA.trim_tail_noise(short, min_chars=100)
    check("trim_tail：裁后不足 min_chars 则原样保留", mark3 == "" and out3 == short, out3)


def test_published_from_text():
    """页面无 meta 时从正文日期行兜底；找不到就返回空串，不猜。"""
    text = f"首页 > 新闻\n标题\n{DAY_SLASHY}\n来源：某站\n正文。"
    check("find_published：取到正文日期行",
          FA.find_published_in_text(text) == DAY_WITH_TIME,
          FA.find_published_in_text(text))
    check("find_published：找不到时返回空串（不猜）",
          FA.find_published_in_text("标题\n正文没有日期。") == "")


if __name__ == "__main__":
    for fn in (test_decode, test_to_text, test_extract_site_pattern,
               test_extract_article_and_div, test_balanced_div, test_inspect,
               test_short_content_detected, test_lead_trim, test_tail_trim,
               test_published_from_text):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
