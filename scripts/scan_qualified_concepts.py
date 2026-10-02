#!/usr/bin/env python3
"""抽样云端卡片的第二行概念名，统计其中带切片限定（时间/版本/代次/地域/事件动作）的情况。

用途：为H6b「概念名须标识对象本身」取证——若概念名常把某个切片写进名字，同一对象
就会被切成多张卡（签名不同→ 幂等与查重双失效），这正是要禁的形态。

只读脚本：不写云端，不改任何卡。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient  # noqa: E402

# 概念名里出现即视为「带切片限定」的形态（H6b：概念名须标识对象本身，不标识切片）。
# **时间只是切片的一类**，与版本、代次、轮次、地域、事件动作平级——早先这里只列时间类
# 形态、把「限定」窄化成「时间限定」，等于让判据以时间为轴。
SLICE_PATTERNS = [
    # —— 时间切片 ——
    (r"20\d{2}\s*年?", "年份"),
    (r"\d{1,2}\s*月\s*\d{1,2}\s*日?", "月日"),
    (r"(?<![A-Za-z0-9])[QqEe]\s?[1-4](?![0-9])", "季度"),
    (r"(19|20)\d{2}\s*[-~到至]\s*(19|20)?\d{2}", "年份区间"),
    (r"(最新|近期|当前|如今|今日|本日|本周|本月|今年|去年|目前|现阶段)", "相对时间词"),
    (r"[（(]\s*(19|20)\d{2}\s*[)）]", "括号年份"),
    # —— 版本切片 ——
    (r"(?<![A-Za-z0-9])[vV]\s?\d+(\.\d+)+", "版本号"),
    (r"\d+(\.\d+)+\s*(版|版本)", "版本号"),
    (r"(?<![A-Za-z0-9])(?:RC|rc|Beta|beta|alpha|Alpha|preview|Preview)\s*\d+(?![0-9])", "阶段号"),
    # —— 代次/轮次/批次切片 ——
    (r"(第\s*[0-9一二三四五六七八九十]+\s*(版|代|阶段|期|轮|届|批))", "序数"),
    # —— 地域/语种切片 ——
    (r"(?<![A-Za-z0-9])(中文版|英文版|国内版|海外版|欧洲版|亚洲版|美版|日版|韩版)(?![A-Za-z0-9])", "地域版本"),
    # —— 事件切片 ——
    (r"(发布|推出|上线|更新|回顾|总结|盘点|举办|召开|夺冠|夺标|获奖)\s*$", "动作结尾"),
]


def classify(name):
    hits = []
    for pat, label in SLICE_PATTERNS:
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

    print(f"\n概念名带切片限定的卡：{len(flagged)} / {len(seen)}")
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
