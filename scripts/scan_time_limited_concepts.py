#!/usr/bin/env python3
"""抽样云端卡片的第二行概念名，统计其中带时间/版本/序数的情况。

用途：为「同概念定义」新规则取证——若概念名常把时间/版本写进名字，同一概念
就会被切成多张卡（签名不同→ 幂等与查重双失效），这正是要禁的形态。

只读脚本：不写云端，不改任何卡。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient  # noqa: E402

# 概念名里出现即视为「带时间限定」的形态
TIME_PATTERNS = [
    (r"20\d{2}\s*年?", "年份"),
    (r"\d{1,2}\s*月\s*\d{1,2}\s*日?", "月日"),
    (r"[QqEe]\s?[1-4]", "季度"),
    (r"[vV]\s?\d+(\.\d+)*", "版本号"),
    (r"\d+(\.\d+)+\s*(版|版本)", "版本号"),
    (r"(19|20)\d{2}\s*[-~到至]\s*(19|20)?\d{2}", "年份区间"),
    (r"(最新|近期|当前|如今|今日|本日|本周|本月|今年|去年)", "相对时间词"),
    (r"(第\s*[0-9一二三四五六七八九十]+\s*(版|代|阶段|期|轮|届))", "序数"),
    (r"(发布|推出|上线|更新|回顾|总结|盘点)\s*$", "动作结尾"),
    (r"[（(]\s*(19|20)\d{2}\s*[)）]", "括号年份"),
]


def classify(name):
    hits = []
    for pat, label in TIME_PATTERNS:
        if re.search(pat, name):
            hits.append(label)
    return hits


def concept_of(content):
    """取第二行概念名（签名构成部分）。"""
    lines = (content or "").split("\n")
    for ln in lines[:4]:
        s = ln.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def resolve_tags(argv):
    """从命令行解析标签列表，支持 "@文件" 模式（200 个标签超命令行长度限制）。

    判据用 startswith("@") 而非 == "@"：后者为假时会把整个 "@xxx.txt" 当成一个
    标签名去搜，memo_search 静默返回 0 张且不报错——**假通过**，比直接报错更坏
    （它会让"抽样没发现问题"被误读成"库里没问题"）。
    """
    tags = list(argv)
    if tags and tags[0].startswith("@"):
        p = Path(tags[0][1:])
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        if not p.exists():
            print(f"[error] 标签文件不存在：{p}", file=sys.stderr)
            sys.exit(2)
        tags = [t.strip() for t in p.read_text(encoding="utf-8").splitlines() if t.strip()]
    return tags or [
        "AI/大模型", "科技/开源", "数学/复几何", "AI/Agent", "AI/开源模型",
        "科技/算力硬件", "AI/RAG", "数学/代数", "科技/Web标准", "AI/多模态",
    ]


def collect_memos(c, tags):
    """按标签簇取卡并按 id 去重。tag 须给完整路径（裸二级名查不到）。"""
    seen = {}
    for t in tags:
        try:
            for m in _memos(c.tool("memo_search", {"tag": t, "limit": 50})):
                if m.get("id") and m["id"] not in seen:
                    seen[m["id"]] = m
        except Exception as e:
            print(f"[warn] 标签 {t!r} 失败：{e}", file=sys.stderr)
    return seen


def main():
    from flomo_client import load_token
    c = FlomoClient.__new__(FlomoClient)
    token, _ = load_token()
    c.__init__(token)
    c.init()

    # 按标签簇抽样：裸关键词检索召回低（实测单轮仅 43 张），改按 tag 逐簇取，
    # 覆盖足够大样本才能取证。
    tags = resolve_tags(sys.argv[1:])
    seen = collect_memos(c, tags)
    print(f"[scan] 取回卡片 {len(seen)} 张（{len(tags)} 个标签簇）", file=sys.stderr)

    flagged = []
    for mid, m in seen.items():
        name = concept_of(m.get("content", ""))
        hits = classify(name)
        if hits:
            flagged.append((mid, name, sorted(set(hits))))

    print(f"\n概念名带时间/版本/序数限定的卡：{len(flagged)} / {len(seen)}")
    print("=" * 70)
    for mid, name, hits in flagged:
        print(f"[{'/'.join(hits)}] id={mid}\n    {name}")
    print("=" * 70)
    out = Path(__file__).resolve().parent.parent / "_tmp_concept_scan.json"
    out.write_text(json.dumps(
        {"total": len(seen), "flagged": [
            {"id": m, "concept": n, "kinds": h} for m, n, h in flagged]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}")


def _memos(result):
    if result.get("structuredContent"):
        sc = result["structuredContent"]
        if isinstance(sc, dict) and "memos" in sc:
            return sc["memos"] or []
    import json as _j
    for blk in result.get("content", []):
        if blk.get("type") == "text":
            try:
                inner = _j.loads(blk["text"])
            except Exception:
                continue
            if isinstance(inner, dict) and "memos" in inner:
                return inner["memos"] or []
    return []


if __name__ == "__main__":
    main()
