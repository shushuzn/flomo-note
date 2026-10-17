#!/usr/bin/env python3
"""extract_arxiv_html.py 回归用例（公式保真）。

为什么必须锁这一条：该脚本唯一的职责是「把 arXiv 的 LaTeXML HTML 落成可读正文」，
而它最典型的失败形态是**静默丢数据**——`<math>` 整块被换成占位符后若没还原，
公式全部消失，正文只剩成片的 `〈M0〉`。这种残次正文仍能读、仍能写卡，
只是定理陈述、关键参数、上下界全成了空洞，不会有任何报错。

用例覆盖：
  - alttext 中的 TeX 必须还原（不丢公式）
  - 无 alttext 时回落到 `<annotation>` 取 TeX
  - 二者皆无的空公式不得留下占位符（也不得产出 `$$`）
  - 正文不得残留 `〈M数字〉` 占位符（丢还原时的可观测特征）
  - article 容器裁剪、块级标签断行、实体还原

全部离线，临时文件内进行。退出码：0 = 全过。
"""
import contextlib
import io
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import extract_arxiv_html as E  # noqa: E402

RESULTS = []


def check(label, cond):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {label}")


SAMPLE = """<html><body>
<nav>导航噪声</nav>
<article class="ltx_document ltx_authors_1line">
<h1>标题</h1>
<p>先给一个带 alttext 的公式 <math alttext="\\alpha^2 + \\beta" display="inline"><semantics><mrow><msup><mi>α</mi><mn>2</mn></msup></mrow></semantics></math> 收尾。</p>
<p>再来一个只有 annotation 的公式 <math><semantics><annotation encoding="application/x-tex">\\gamma_{k}</annotation></semantics></math>。</p>
<p>空公式在这里 <math><semantics></semantics></math> 不应留占位符。</p>
<p>实体 &amp; 与不等号 &lt; 都应还原。</p>
</article>
<footer>页脚噪声</footer>
</body></html>
"""


def run_cases():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "in.html"
    out = tmp / "out.txt"
    src.write_text(SAMPLE, encoding="utf-8")

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        E.extract(str(src), str(out))
    text = out.read_text(encoding="utf-8")
    diag = buf.getvalue()

    check("alttext 里的 TeX 被还原（不丢公式）", "\\alpha^2 + \\beta" in text)
    check("无 alttext 时回落 annotation 取 TeX", "\\gamma_{k}" in text)
    check("公式以 $…$ 包裹", re.search(r"\$\\alpha\^2", text) is not None)
    check("空公式不留占位符", "〈M" not in text and "M2〉" not in text)
    check("空公式不产出 $$", "$$" not in text)
    check("正文无残留占位符（丢还原的可观测特征）",
          re.search(r"[〈\u3016]M\d+[〉\u3017]", text) is None)
    check("诊断行报出公式数量与空公式数",
          "math_placeholders" in diag and "empty_math" in diag)
    check("article 容器被裁剪（导航/页脚噪声不进入正文）",
          "导航噪声" not in text and "页脚噪声" not in text)
    check("块级标签断行（标题独占一行）", "### 标题" in text or "标题" in text.splitlines()[0])
    check("实体还原（&amp; → &）", "&" in text and "&amp;" not in text)
    check("不等号实体还原（&lt; → <）", "<" in text)

    # 无 article 容器时的兜底路径：仍须保住公式，不得整篇丢公式
    plain = tmp / "plain.html"
    plain.write_text(
        '<div class="ltx_page_main"><p>兜底 <math alttext="x_1">m</math></p></div>',
        encoding="utf-8",
    )
    out2 = tmp / "out2.txt"
    with contextlib.redirect_stderr(io.StringIO()):
        E.extract(str(plain), str(out2))
    text2 = out2.read_text(encoding="utf-8")
    check("无容器兜底路径仍还原公式", "x_1" in text2)


if __name__ == "__main__":
    run_cases()
    print("---")
    ok = all(RESULTS)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)
