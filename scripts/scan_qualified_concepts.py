#!/usr/bin/env python3
"""抽样云端卡片的第二行概念名，统计其中带切片限定（时间/版本/代次/地域/事件动作）的情况。

用途：为H6b「概念名须标识对象本身」取证——若概念名常把某个切片写进名字，同一对象
就会被切成多张卡（签名不同→ 幂等与查重双失效），这正是要禁的形态。

只读脚本：不写云端，不改任何卡。

用法：
  python scripts/scan_qualified_concepts.py                    # 默认标签簇抽样
  python scripts/scan_qualified_concepts.py --tags "AI/RAG,科技/开源"
  python scripts/scan_qualified_concepts.py --tags @tags.txt    # 标签多到超命令行长度
  python scripts/scan_qualified_concepts.py --limit 30 --json _tmp/scan.json

退出码：
  0  扫完，所有标签簇取数成功
  2  参数写错 / 标签文件不存在 / 有标签簇取数失败（样本缩水，占比不能当全库结论）
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient, FlomoError, _result_memos  # noqa: E402
from cli_util import Opt, UsageError, cli_entry, parse_options  # noqa: E402
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


DEFAULT_TAGS = [
    "AI/大模型", "科技/开源", "数学/复几何", "AI/Agent", "AI/开源模型",
    "科技/算力硬件", "AI/RAG", "数学/代数", "科技/Web标准", "AI/多模态",
]

_OPTS = [
    Opt("--tags", "tags_raw", default="",
        hint="；逗号分隔的标签路径；标签多到超命令行长度时用 @文件"),
    Opt("--limit", "limit", "int", low=1, high=50, default=50,
        hint="；每个标签簇取多少条（服务端单次上限 50）"),
    Opt("--json", "json_out", default=""),
]


def resolve_tags(raw):
    """把命令行给的标签串解析成列表，支持 "@文件" 模式（标签多到超命令行长度时用）。

    判据用 startswith("@") 而非 == "@"：后者为假时会把整个 "@xxx.txt" 当成一个
    标签名去搜，memo_search 静默返回 0 张且不报错——**假通过**，比直接报错更坏
    （它会让"抽样没发现问题"被误读成"库里没问题"）。

    文件不存在 / 文件为空一律抛 UsageError（由 cli_entry 收成退出码 2），**不在这里
    sys.exit**：本函数同时被 scan_concept_duplication 调用，库函数直接结束进程会让
    调用方来不及打印任何上下文。
    """
    raw = (raw or "").strip()
    if raw.startswith("@"):
        p = Path(raw[1:].strip())
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        if not p.exists():
            raise UsageError(f"标签文件不存在：{p}")
        tags = [t.strip() for t in p.read_text(encoding="utf-8").splitlines() if t.strip()]
        if not tags:
            raise UsageError(f"标签文件是空的：{p}")
        return tags
    tags = []
    for part in raw.replace("，", ",").replace("、", ",").split(","):
        t = part.strip().lstrip("#").strip()
        if t and t not in tags:
            tags.append(t)
    return tags or list(DEFAULT_TAGS)


def collect_memos(c, tags, limit=50):
    """按标签簇取卡并按 id 去重。tag 须给完整路径（裸二级名查不到）。

    返回 `(seen, failed)`，`failed` 是取数失败的标签簇。**必须由调用方打印出来**：
    某一簇失败时只留一行 stderr，「占比」类结论就建立在一个悄悄缩水的分母上——
    看起来是「库里只有三成卡带切片」，实际可能是「有三簇根本没取到」。样本量
    偏小但输出正常，正是取证脚本最不该犯的错。
    """
    seen, failed, truncated = {}, [], []
    for t in tags:
        try:
            got = 0
            for m in _result_memos(c.tool("memo_search", {"tag": t, "limit": limit})):
                got += 1
                if m.get("id") and m["id"] not in seen:
                    seen[m["id"]] = m
            # 命中数正好等于 limit = 服务端按上限截断，这一簇还有卡没取到。
            # 不报出来，占比的分母就是一个悄悄缩水的样本：看起来「只有 7% 带切片」，
            # 实际可能是「大簇只看了前 50 张」。与取数失败同属样本完整性问题。
            if got >= limit:
                truncated.append(t)
        except Exception as e:
            print(f"[warn] 标签 {t!r} 失败：{e}", file=sys.stderr)
            failed.append(t)
    return seen, failed, truncated


def main(argv=None, client=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    ns, _pos = parse_options(args, _OPTS)
    tags = resolve_tags(ns["tags_raw"])

    if client is None:
        from flomo_client import load_token
        token, _ = load_token()
        c = FlomoClient.__new__(FlomoClient)
        c.__init__(token)
        c.init()
    else:
        c = client

    # 按标签簇抽样：裸关键词检索召回低（实测单轮仅 43 张），改按 tag 逐簇取，
    # 覆盖足够大样本才能取证。
    seen, failed, truncated = collect_memos(c, tags, ns["limit"])
    ok_tags = len(tags) - len(failed)
    print(f"[scan] 取回卡片 {len(seen)} 张（{ok_tags}/{len(tags)} 个标签簇取数成功）",
          file=sys.stderr)
    if failed:
        print(f"[warn] {len(failed)} 个标签簇取数失败：{', '.join(failed)}——"
              f"本次样本量偏小，下面的占比只能当这批卡的占比，不是全库事实", file=sys.stderr)
    if truncated:
        print(f"[warn] {len(truncated)} 个标签簇命中数达到 --limit={ns['limit']}，"
              f"被服务端截断：{', '.join(truncated)}——这些簇只看了前 {ns['limit']} 张，"
              f"要全量请调大 --limit 或分批扫", file=sys.stderr)

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

    if ns["json_out"]:
        out = Path(ns["json_out"])
    else:
        out = Path(__file__).resolve().parent.parent / "_tmp" / "concept_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"total": len(seen), "failed_tags": failed, "truncated_tags": truncated,
         "flagged": [
            {"id": m, "concept": n, "kinds": h} for m, n, h in flagged]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}")

    # 有簇取数失败 = 样本缩水，占比不能当全库结论：按「扫描不完整」非 0 退出，
    # 而不是打印一份看起来正常的报告。
    return 2 if failed else 0

if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError, UsageError)))
