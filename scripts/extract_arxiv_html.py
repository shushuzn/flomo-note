"""extract_arxiv_html.py — arXiv LaTeX HTML 正文抽取。

把 arXiv `/html/` 版（LaTeXML 产物）转为纯文本：先保护 MathML 公式
（取 alttext / annotation 中的 TeX，以占位符暂存后原位还原），再按块级
标签断行、还原实体、压缩空白。供抓取 arXiv 全文时把 HTML 落成可读正文。

用法：
  python scripts/extract_arxiv_html.py <input.html> <output.txt>

诊断信息走 stderr（容器匹配情况、公式占位符数量）。
"""
import re, sys, html, os

def extract(path, out):
    s = open(path, encoding='utf-8', errors='replace').read()
    # 1. protect MathML
    math_store = []
    def _m(m):
        tag = m.group(0)
        alt = re.search(r'alttext="([^"]*)"', tag)
        tex = alt.group(1) if alt else ''
        if not tex:
            # try annotation encoding
            a2 = re.search(r'<annotation[^>]*>(.*?)</annotation>', tag, re.S)
            if a2:
                tex = a2.group(1)
        math_store.append(tex.strip())
        return ' \u3016M%d\u3017 ' % (len(math_store) - 1)
    s = re.sub(r'<math\b.*?</math>', _m, s, flags=re.S)

    # 2. extract article container
    m = re.search(r'<article class="ltx_document[^"]*"[^>]*>(.*?)</article>', s, re.S)
    if m:
        body = m.group(1)
    else:
        m = re.search(r'<div class="ltx_page_main">(.*?)</div>\s*</div>', s, re.S)
        body = m.group(1) if m else s
    print('[info] container matched =', bool(m), 'len =', len(body), file=sys.stderr)

    # 3. drop script/style/nav
    body = re.sub(r'<script\b.*?</script>', ' ', body, flags=re.S)
    body = re.sub(r'<style\b.*?</style>', ' ', body, flags=re.S)

    # 4. block -> newline
    body = re.sub(r'</(p|div|section|h1|h2|h3|h4|h5|h6|li|ul|ol|tr|table|figure|figcaption|blockquote|dd|dt|dl)>', '\n', body, flags=re.S)
    body = re.sub(r'<br\s*/?>', '\n', body, flags=re.S)
    body = re.sub(r'<h([1-6])\b[^>]*>', r'\n\n### ', body, flags=re.S)
    body = re.sub(r'<li\b[^>]*>', '\n- ', body, flags=re.S)

    # 5. strip tags
    body = re.sub(r'<[^>]+>', ' ', body)

    # 6. entities
    body = html.unescape(body)

    # 7. restore math
    def _r(m):
        i = int(m.group(1))
        t = math_store[i] if i < len(math_store) else ''
        return ('$' + t + '$') if t else ' '
    body = re.sub('\u3016M(\\d+)\u3017', _r, body)

    # 8. tidy whitespace
    body = re.sub(r'[ \t\u00a0]+', ' ', body)
    body = re.sub(r' *\n *', '\n', body)
    body = re.sub(r'\n{3,}', '\n\n', body)
    body = body.strip()

    open(out, 'w', encoding='utf-8').write(body)
    print('[out]', out, len(body), 'chars', file=sys.stderr)
    print('math_placeholders =', len(math_store), 'empty_math =', sum(1 for x in math_store if not x), file=sys.stderr)

if __name__ == '__main__':
    extract(sys.argv[1], sys.argv[2])
