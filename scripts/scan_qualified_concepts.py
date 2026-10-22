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
from flomo_client import FlomoClient, FlomoError, _result_memos  # noqa: E402
from cli_util import cli_entry  # noqa: E402
from memo_util import memo_signature, slice_qualifiers  # noqa: E402  单一来源

# 切片识别**走 memo_util.slice_qualifiers**（与质检拦截 5.8 同源）：
# 两侧各写一套正则时，改一侧会让另一侧静默失配——而失配只表现为「统计数字对不上」，
# 很难被察觉。本脚本只负责「取样 + 计数」，不另立识别口径。


def classify(name):
    """返回命中的切片限定标签（去重保序）。"""
    seen = []
    for lbl, _ in slice_qualifiers(name):
        if lbl not in seen:
            seen.append(lbl)
    return seen


def concept_of(content):
    """取概念名（签名构成部分）。走 memo_util.memo_signature，与幂等查重、闸门凭证同源。"""
    sig = memo_signature(content)
    return sig["concept"] if sig else ""


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
    """按标签簇取卡并按 id 去重。tag 须给完整路径（裸二级名查不到）。

    返回 `(seen, failed)`，`failed` 是取数失败的标签簇。**必须由调用方打印出来**：
    某一簇失败时只留一行 stderr，「占比」类结论就建立在一个悄悄缩水的分母上——
    看起来是「库里只有三成卡带切片」，实际可能是「有三簇根本没取到」。样本量
    偏小但输出正常，正是取证脚本最不该犯的错。
    """
    seen, failed = {}, []
    for t in tags:
        try:
            for m in _result_memos(c.tool("memo_search", {"tag": t, "limit": 50})):
                if m.get("id") and m["id"] not in seen:
                    seen[m["id"]] = m
        except Exception as e:
            print(f"[warn] 标签 {t!r} 失败：{e}", file=sys.stderr)
            failed.append(t)
    return seen, failed


def main():
    from flomo_client import load_token
    c = FlomoClient.__new__(FlomoClient)
    token, _ = load_token()
    c.__init__(token)
    c.init()

    # 按标签簇抽样：裸关键词检索召回低（实测单轮仅 43 张），改按 tag 逐簇取，
    # 覆盖足够大样本才能取证。
    tags = resolve_tags(sys.argv[1:])
    seen, failed = collect_memos(c, tags)
    ok_tags = len(tags) - len(failed)
    print(f"[scan] 取回卡片 {len(seen)} 张（{ok_tags}/{len(tags)} 个标签簇取数成功）",
          file=sys.stderr)
    if failed:
        print(f"[warn] {len(failed)} 个标签簇取数失败：{', '.join(failed)}——"
              f"本次样本量偏小，下面的占比只能当这批卡的占比，不是全库事实", file=sys.stderr)

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


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError,)))
