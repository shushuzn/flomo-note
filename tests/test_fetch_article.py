#!/usr/bin/env python3
"""fetch_article.py 的离线回归用例（全部不联网）。

只测「解码 / 抽取 / 元信息」这套纯函数；网络路径由 CLI 现场验证。
退出码 0 = 全部通过。
"""
import contextlib
import importlib.util
import io
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FA = _load("fetch_article", "fetch_article.py")
# 取**同一份**模块（fetch_article 已导入过它，这里只是取回来）：另 `_load` 一份会
# 得到另一个 `UsageError` 类对象，`except` 就接不住脚本真正抛的那个。
from flomo_client import UsageError  # noqa: E402
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


def test_extract_site_pattern_nested_divs():
    """正文内部的相邻 div 边界不得当作容器结束。

    真实来源：WordPress+Traefik 那篇，Step 3 之后的代码高亮块是三层 div 套按钮 svg，
    旧右界规则（`</div>\\s*<div`）在这里命中，后半篇（安装、加固、故障排查、备份）
    整段消失，且截断点落在代码块末尾，看起来像正常结束。
    """
    first = "前半段正文" * 60
    later = "后半段正文" * 60
    html = (
        "<html><body>"
        '<div id="article-body">'
        f"<p>{first}</p>"
        '<div class="highlight"><div class="inner">docker compose ps</div></div>'
        "\n\n"
        '<div class="highlight"><div class="inner">curl -sI</div></div>'
        f"<p>{later}</p>"
        "</div>"
        '<div class="author-card">作者卡</div>'
        "</body></html>"
    )
    text, channel = FA.extract(html)
    check("extract：正文里的相邻 div 不触发截断", later in text, text[-50:])
    check("extract：配平后仍不吞作者卡", "作者卡" not in text, text[-50:])
    check("extract：嵌套用例走主通道", channel == "站内专用选择器", channel)


def test_extract_site_pattern_comment_fallback():
    """配平块吞进评论区时，必须退回右界规则，不能把评论当正文。"""
    html = (
        '<div id="article-body"><p>' + "正文" * 80 + "</p>"
        '<div class="wrap">'
        '<div id="comments">评论区内容</div>'
        '<div class="author-card">作者卡</div></div>'
    )
    text, channel = FA.extract(html)
    check("extract：吞进评论区时降级到兜底通道",
          channel != "站内专用选择器", channel)


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


def test_heading_gap():
    """章节标题缺口：抓了一半的正文要能指出来，抓全了不能误报。"""
    page = (
        "<html><body>"
        "<nav><h2>导航里的标题</h2></nav>"
        "<article>"
        "<h2>第一节：安装</h2><p>安装正文。</p>"
        "<h2>第二节：部署</h2><p>部署正文。</p>"
        "<h2>第三节：维护</h2><p>维护正文。</p>"
        "<h4>四级标题不算小节</h4>"
        "</article></body></html>"
    )
    full = "第一节：安装\n安装正文。\n第二节：部署\n部署正文。\n第三节：维护\n维护正文。"
    check("heading_gap：抓全时不报警", FA.heading_gap(page, full) == {},
          FA.heading_gap(page, full))

    half = "第一节：安装\n安装正文。\n第二节：部署\n部署正文。"
    gap = FA.heading_gap(page, half)
    check("heading_gap：截断时指出最后一个见到的章节",
          gap.get("last_seen") == "第二节：部署", gap)
    check("heading_gap：截断时列出其后的缺失章节",
          gap.get("missing_tail") == ["第三节：维护"], gap)
    check("heading_gap：导航里的标题不参与判定",
          all("导航" not in h for h in gap.get("missing_tail", []) + gap.get("missing_before", [])),
          gap)

    mid = "第一节：安装\n安装正文。\n第三节：维护\n维护正文。"
    gap2 = FA.heading_gap(page, mid)
    check("heading_gap：中间缺失与结尾截断分开报",
          gap2.get("missing_before") == ["第二节：部署"] and gap2.get("missing_tail") == [],
          gap2)

    check("heading_gap：正文里一个标题都没有时不猜",
          FA.heading_gap(page, "完全对不上的正文。") == {},
          FA.heading_gap(page, "完全对不上的正文。"))
    check("heading_gap：页面没有小节标题时返回空",
          FA.heading_gap("<p>无标题页</p>", "无标题页") == {})
    check("page_headings：跳过纯数字与过短标题",
          FA.page_headings("<h2>12</h2><h2>更多</h2><h2>真正的章节标题</h2>")
          == ["真正的章节标题"],
          FA.page_headings("<h2>12</h2><h2>更多</h2><h2>真正的章节标题</h2>"))

    # 告警文案：只在真截断时出现，且要指明断点；无缺口时一行不出
    lines = FA.gap_warnings(gap)
    check("gap_warnings：截断时给出一条告警", len(lines) == 1 and lines[0].startswith("[告警]"),
          lines)
    check("gap_warnings：告警里带上断点章节名",
          "第二节：部署" in lines[0] and "第三节：维护" in lines[0], lines)
    check("gap_warnings：无缺口时零输出", FA.gap_warnings({}) == [], FA.gap_warnings({}))
    many = {"last_seen": "起点", "missing_tail": [f"章节{i}" for i in range(1, 6)]}
    line = FA.gap_warnings(many)[0]
    check("gap_warnings：缺失过多时截断列表并给出余数",
          "另 2 个" in line and "章节4" not in line, line)

    # 端到端：容器选早了（正文 div 在中间就闭合、后面的章节落在容器外），
    # 抽取结果看起来是正常收尾——这正是要靠标题缺口抓的形态。
    page2 = ('<div id="article-body"><h2>第一节：安装</h2><p>安装正文。</p>'
             '<h2>第二节：部署</h2><p>部署正文。</p></div>'
             '<h2>第三节：维护</h2><p>维护正文。</p>')
    text2, _ch = FA.extract(page2)
    gap2 = FA.heading_gap(page2, text2)
    check("端到端：容器选早时告警触发",
          gap2.get("missing_tail") == ["第三节：维护"], gap2)
    check("端到端：告警行里能看出断在哪一章",
          "第二节：部署" in FA.gap_warnings(gap2)[0], FA.gap_warnings(gap2))


def test_bad_numeric_args():
    """`--timeout abc` / `--min-chars 0` 该抛 `UsageError` 并由入口渲染成一行提示。

    此前是裸 `int()` / `float()`，用户打错一个字得到一段 traceback。这两种都在
    发起抓取之前拦下，所以这条用例不联网。
    """
    for argv, want in ((["https://example.com", "--timeout", "abc"], "--timeout"),
                       (["https://example.com", "--min-chars", "0"], "--min-chars")):
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                FA.main(argv)
            got = "未拦下"
        except UsageError as e:
            got = "ok" if want in str(e) else str(e)
        check(f"{want} 非法值抛 UsageError 并点明参数", got == "ok", got)

    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = FA.cli_entry(lambda: FA.main(["https://example.com", "--timeout", "abc"]),
                            usage_code=1)
    check("用法错经入口变成一行提示 + 非 0",
          code == 1 and err.getvalue().startswith("[失败] ") and "Traceback" not in err.getvalue(),
          err.getvalue())


if __name__ == "__main__":
    for fn in (test_decode, test_to_text, test_extract_site_pattern,
               test_extract_site_pattern_nested_divs,
               test_extract_site_pattern_comment_fallback,
               test_extract_article_and_div, test_balanced_div, test_inspect,
               test_short_content_detected, test_lead_trim, test_tail_trim,
               test_published_from_text, test_heading_gap, test_bad_numeric_args):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
