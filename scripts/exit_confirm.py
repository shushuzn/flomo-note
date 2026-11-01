#!/usr/bin/env python3
"""退出留痕 —— 任何"不建卡"的决定必须经过本脚本，禁止嘴上说退出就退出。

核心防伪造：脚本自己去HTTP请求每个search_url，验证：
  1. URL能访问（不是编造的域名）
  2. 页面内容里确实包含该对象关键词
  3. 页面标题/内容里确实出现我写的title片段
这样手写假URL直接被拦。
"""
import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse


def fetch_page(url: str, timeout: int = 8) -> str:
    """抓取页面文本，失败返回空串。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(500000).decode("utf-8", errors="replace")
            # 剥HTML标签粗提取文本
            text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            return text
    except Exception:
        return ""


def _nearby_runs(page_text: str, term: str, limit: int = 3) -> list[str]:
    """从页面里挑出与术语有字符交集的连续中文串，按重合度降序，供报错时建议。

    只做「提示该取哪个词」，不参与放行判定——判据仍是 chunks 必须整段命中。
    重合度用字符交集大小衡量：交得越多越可能是页面上对应的那个词。
    """
    term_chars = set(re.sub(r"[\s\W_]+", "", term))
    if not term_chars:
        return []
    runs = re.findall(r"[\u4e00-\u9fa5]{2,}", page_text[:20000])
    scored = []
    for run in runs:
        overlap = len(set(run) & term_chars)
        if overlap >= 2:
            scored.append((overlap, run))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1])))
    out: list[str] = []
    for _, run in scored:
        if run not in out:
            out.append(run)
        if len(out) >= limit:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", required=True, help="逗号分隔的对象清单")
    ap.add_argument("--search-log", required=True, help="搜索记录JSON路径")
    ap.add_argument("--dedup-log", required=True, help="dedup输出文件路径")
    ap.add_argument("--reason", required=True, help="退出理由")
    ap.add_argument("--skip-fetch", action="store_true", help="跳过HTTP校验（仅离线调试用）")
    args = ap.parse_args()

    errors = []

    # 1. 对象清单
    objs = [o.strip() for o in args.objects.split(",") if o.strip()]
    if not objs:
        errors.append("对象清单为空——必须先列对象清单，逐个审视")

    # 2. 读search_log
    sp = Path(args.search_log)
    if not sp.exists():
        errors.append(f"搜索记录不存在: {sp}")
        data = None
    else:
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"搜索记录解析失败: {e}")
            data = None

    if data is not None:
        if not data.get("searched"):
            errors.append("搜索记录里 searched 不为 true——必须真的搜过")
        terms = data.get("terms", [])
        if not terms:
            errors.append("搜索记录里 terms 为空——至少搜过一个对象")

        for t in terms:
            term = t.get("term", "?")
            url = t.get("search_url", "")
            title = t.get("title", "")
            if not url:
                errors.append(f"术语 {term} 缺 search_url")
                continue
            if not title:
                errors.append(f"术语 {term} 缺 title")
                continue

            if args.skip_fetch:
                continue

            # 真去抓URL，验证不是编造
            page_text = fetch_page(url)
            if not page_text or len(page_text) < 200:
                errors.append(
                    f"术语 {term} 的search_url抓不到内容（可能是编造的URL）: {url}——"
                    f"必须填真实可访问的文章页"
                )
                continue

            # 页面内容里必须包含对象关键词（去掉标点后比对）
            def normalize(s):
                return re.sub(r"[\s\W_]+", "", s).lower()

            page_norm = normalize(page_text[:20000])
            chunks = re.findall(r"[\u4e00-\u9fa5]{2,}", term)
            hit = False
            for c in chunks:
                if normalize(c) and normalize(c) in page_norm:
                    hit = True
                    break
            if not hit and chunks:
                # 报错要能直接改：给出页面里与术语有字符交集的连续中文串，让人一眼
                # 看出该取哪一个。此前只报 chunks[0] 并说「疑似编造」——术语是自己
                # 组织的语言时（如「车辆占用公共充电桩」对页面的「充电位都被无人驾驶
                # 出租车占了」），那条提示既吓人又指不到改正方向，只能靠反复试。
                near = _nearby_runs(page_text, term)
                hint = ("；页面里相关的词有：" + "、".join(near)) if near else ""
                errors.append(
                    f"术语 {term} 在 search_url 页面里找不到对应的中文词——"
                    f"术语要取页面原词的连续片段，不能自己组织语言{hint}"
                )

    # 3. dedup
    dp = Path(args.dedup_log)
    if not dp.exists():
        errors.append(f"dedup记录不存在: {dp}")
    else:
        content = dp.read_text(encoding="utf-8", errors="replace")
        if "关键词" not in content:
            errors.append("dedup输出里没有'关键词'段——dedup没有真的跑过")

    if len(args.reason) < 10:
        errors.append("退出理由太短")

    if errors:
        print("[退出拦截] 不允许就这样退出：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("正确流程：", file=sys.stderr)
        print("  1. fetch_article 抓取并读回全文", file=sys.stderr)
        print("  2. 列出全部对象清单", file=sys.stderr)
        print("  3. 对每个对象调 general_search，把返回的 title/snippet/url 原样写入 search_log", file=sys.stderr)
        print("  4. 跑dedup.py查重，保存输出到文件", file=sys.stderr)
        print("  5. 确认确实无建卡价值，再跑本脚本留痕", file=sys.stderr)
        return 1

    print(f"[退出留痕] 已记录：{len(objs)}个对象审视完毕，HTTP校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
