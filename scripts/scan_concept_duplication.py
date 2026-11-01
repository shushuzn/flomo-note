#!/usr/bin/env python3
"""找「同一对象因切片限定不同被切成多张卡」的实证。

判法：把概念名里的**切片限定抹掉**（归一化）——时间、版本、代次、轮次、地域、事件动作
都是同一类切片（对象 + 切片），不是时间专属。若归一化后出现同名簇（≥2 张不同 id），
即说明同一对象被按切片切成了平行卡。这是只读取证脚本，不改云端。

用法：
  python scripts/scan_concept_duplication.py                    # 默认标签簇抽样
  python scripts/scan_concept_duplication.py --tags "AI/RAG,科技/开源"
  python scripts/scan_concept_duplication.py --tags @tags.txt   # 标签多到超命令行长度
  python scripts/scan_concept_duplication.py --tags "商业/企业" --full    # 翻页取尽，大簇不缩水
  python scripts/scan_concept_duplication.py --limit 30 --json _tmp/dupes.json

退出码：
  0  扫完，所有标签簇取数成功
  2  参数写错 / 标签文件不存在 / 有标签簇取数失败（结论只在已取到的卡里成立）
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_util import Opt, UsageError, cli_entry, parse_options
from flomo_client import FlomoClient, FlomoError
from memo_util import slice_qualifiers
from scan_qualified_concepts import collect_memos, concept_of, resolve_tags

_OPTS = [
    Opt("--tags", "tags_raw", default="",
        hint="；逗号分隔的标签路径；标签多到超命令行长度时用 @文件"),
    Opt("--limit", "limit", "int", low=1, high=50, default=50,
        hint="；每个标签簇取多少条（服务端单次上限 50）"),
    Opt("--full", "full", "flag",
        hint="；按日期游标把簇取尽（默认只取最新一页，大簇的「0 组」只覆盖这批卡）"),
    Opt("--json", "json_out", default=""),
]

# 括号包裹形态的剥离（本脚本特有）：切片常被写在括号里，
# 去掉括号后仍要能被共享识别命中，故括号先剥。
_BRACKET_STRIP = [
    (r"[（(]\s*(?:19|20)\d{2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}\s*[)）]", ""),
    (r"[（(]\s*(?:19|20)\d{2}\s*[-/]\s*\d{1,2}(?:\s*[-/]\s*\d{1,2})?\s*[)）]", ""),
    (r"[（(]\s*(?:19|20)\d{2}\s*[)）]", ""),
    (r"[（(][^()（）]{0,40}(?:19|20)\d{2}[^()（）]{0,40}[)）]", ""),
]
# 收尾清理（非切片识别，仅归一化外形）
_TIDY = [
    (r"\s*[-—–~至]+\s*", "-"),
    (r"\s+", " "),
]


def normalize(name):
    """抹掉切片限定，得到同一对象的归一化指称。

    切片识别**走 memo_util.slice_qualifiers**（与质检拦截 5.8、存量统计同源），
    本函数只额外处理「切片被括号包裹」这一形态与外形清理，不另立识别口径——
    三处各写一套正则时，改一侧会让另两侧静默失配，而失配只表现为
    「统计数字对不上」，不会被任何单侧用例发现。
    """
    s = name
    for pat, rep in _BRACKET_STRIP:
        s = re.sub(pat, rep, s)
    # 反复剥离：括号内可能还嵌着切片（("(2026)") 已在上面处理，
    # 但 "(v1.2 RC2)" 这类需靠循环收敛）
    for _ in range(3):
        before = s
        for _lbl, matched in slice_qualifiers(s):
            s = s.replace(matched, " ")
        if s == before:
            break
    for pat, rep in _TIDY:
        s = re.sub(pat, rep, s)
    return s.strip(" -—–·、,，.。:：;；/（()）")


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
    # 标签解析与取卡逻辑同源（scan_qualified_concepts），不另写第二份实现。
    seen, failed, truncated = collect_memos(c, tags, ns["limit"], full=ns["full"])
    if truncated:
        print(f"[warn] {len(truncated)} 个标签簇只取到最新 {ns['limit']} 张"
              f"（{', '.join(truncated)}）——同名簇的结论只覆盖这批卡，"
              "要全量请加 --full", file=sys.stderr)
    if failed:
        print(f"[warn] {len(failed)} 个标签簇取数失败：{', '.join(failed)}——"
              f"下面的同名簇只在已取到的卡里成立，不代表其余簇没有问题", file=sys.stderr)

    clusters = {}
    for mid, m in seen.items():
        name = concept_of(m.get("content", ""))
        key = normalize(name)
        if key:
            clusters.setdefault(key, []).append((mid, name))

    dupes = {k: v for k, v in clusters.items() if len(v) > 1}
    print(f"[scan] 样本 {len(seen)} 张，归一化后同名簇 {len(dupes)} 组，涉及 {sum(len(v) for v in dupes.values())} 张")
    print("=" * 72)
    for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1])):
        print(f"\n【{k}】{len(v)} 张")
        for mid, name in v:
            print(f"    id={mid}  {name[:90]}")

    if ns["json_out"]:
        out = Path(ns["json_out"])
    else:
        out = Path(__file__).resolve().parent.parent / "_tmp" / "concept_dupes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"failed_tags": failed, "dupes": dupes}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"[out] {out}")

    return 2 if failed else 0

if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError, UsageError)))
