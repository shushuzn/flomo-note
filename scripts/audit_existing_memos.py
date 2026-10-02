#!/usr/bin/env python3
"""存量卡合规普查（H14–H17 + H6b 切片限定）——**读本地快照**，只读。

用途：写卡轮只报发现，**不在写卡轮里顺手重写存量卡**——修一张卡 = 重写其全部
正文并重跑闸门/质检，且同类存量成批存在，单轮只修一张属任意选取、会让「已合规」
变成偶然。本脚本只做普查：逐卡跑质检 → 按违规类型分组 → 报计数，供裁定处置方案。

**读快照而非现采**（`scripts/_snapshot_all_memos.py` 负责取数）：迭代质检口径时
不必反复打云端，且同一批卡可在不同口径下反复比对。快照是瞬时产物，cleanup 回收。

**只读**：不写云端、不改任何卡。

读到的计数**须抽样核验判据本身**再上报：判据误报与漏报同样是缺陷。一张卡命中
多条规则时，汇总数只说明「报了几条」，不说明「该改成什么」——例如概念名既成句
又含切片限定时，两条的处置力度完全不同（重写一段vs 重写一个名词），混在一起
报会让处置方案选错。
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_memo as VM  # noqa: E402
from scan_qualified_concepts import concept_of  # noqa: E402


def violation_kind(msg):
    """把质检文案归一成稳定的违规类型键（**由文案本身派生，不靠猜**）。

    匹配不到就归入「其他（原文照录）」，让漏计**显形**而不是混进总数冒充已覆盖。
    """
    rules = [
        (r"Markdown 链接", "flomo 不渲染的 Markdown 链接"),
        (r"疑似标题语法|引用语法|疑似表格", "flomo 不渲染的 Markdown 语法"),
        (r"来源/通报方", "H16 概念名含来源/通报方"),
        (r"事件来源信息", "H15 事件来源信息"),
        (r"载体/来源信息|取材来源", "H16 卡片载体/来源信息"),
        (r"疑似旧格式状态行", "H15 旧格式状态行"),
        (r"括号注释含载体词|概念名称含载体词", "H16 括号注释含载体词"),
        (r"切片限定", "H6b 概念名含切片限定"),
        (r"取不到概念名称|签名取不到|应以概念名称|概念名称不应为空行|不是概念名",
         "H14 概念名缺失/签名失效"),
        (r"是标签（以 # 开头）", "H14 概念名位置写了标签"),
        (r"必须空一行再接正文", "H14 概念名后缺空行"),
        (r"模板前缀|结论先行|一句话核心结论|一句话结论", "H14 模板指令回显"),
        (r"标签须为两级|标签层级超过两级|疑似裸子标签|标签含非法字符", "H17 标签不合规"),
        (r"以摘要为唯一依据", "预印本摘要依赖"),
        (r"超过上限|超限", "H14 字数超限"),
        (r"正文为空", "H14 正文为空"),
        (r"尖括号|占位符", "H14 占位符未替换"),
    ]
    for pat, key in rules:
        if re.search(pat, msg):
            return key
    return "其他（原文照录）"


def audit_memo(content):
    VM.ERR.clear()
    VM.WARN.clear()
    try:
        VM.check(content or "")
    except Exception as e:
        return [("质检执行异常", f"{type(e).__name__}: {e}")]
    out = [("WARN " + violation_kind(m), m) for m in VM.WARN]
    out += [("ERR " + violation_kind(m), m) for m in VM.ERR]
    return out


def main():
    snap = json.loads((ROOT / "_tmp_audit_snapshot.json").read_text(encoding="utf-8"))
    groups, kind_count, long_concept, clean = defaultdict(list), Counter(), [], 0
    for mid, m in snap.items():
        content = m.get("content", "")
        name = concept_of(content)
        issues = audit_memo(content)
        if len(name) > 50:
            long_concept.append((mid, name))
        if not issues:
            clean += 1
            continue
        for kind, msg in issues:
            kind_count[kind] += 1
            groups[kind].append((mid, name, msg))

    total = len(snap)
    blocking = sorted(k for k in kind_count if k.startswith("ERR "))
    n_block = sum(1 for mid, m in snap.items()
                  if any(k.startswith("ERR ") for k, _ in audit_memo(m.get("content", ""))))

    print("=" * 76)
    print(f"全库 {total} 张｜零提示 {clean} 张｜**有阻断项{ n_block} 张**｜有问题 {total - clean} 张")
    print("=" * 76)
    print("\n【ERR 阻断项】（写云会被拦住，故这些是必须修的）")
    for k in blocking:
        print(f"  {kind_count[k]:5d}  {k}")
    print("\n【WARN 提示项】（不阻断）")
    for k, n in kind_count.most_common():
        if not k.startswith("ERR "):
            print(f"  {n:5d}  {k}")
    print(f"\n另：{len(long_concept)} 张概念名超 50 字（成句，质检未覆盖长度）")

    print("\n【ERR 各类样例（最多 2 张）】")
    for k in blocking:
        print(f"\n  ▸ {k}（{kind_count[k]} 张）")
        for mid, name, msg in groups[k][:2]:
            print(f"      id={mid}  {name[:38]}")
            print(f"        {msg[:110]}")

    (ROOT / "_tmp_audit_report.json").write_text(json.dumps({
        "total": total, "clean": clean, "blocking_memos": n_block,
        "kind_count": dict(kind_count),
        "long_concept": [{"id": i, "name": n} for i, n in long_concept],
        "groups": {k: [{"id": i, "name": n, "msg": msg} for i, n, msg in v]
                   for k, v in groups.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[out] _tmp_audit_report.json")


if __name__ == "__main__":
    main()
