#!/usr/bin/env python3
"""verify_card.py — 写云后回读全文，做 H11 里**可机械判定**的那部分自查。

为什么需要这个脚本：H11 要求写云后回读全文核对四件事，其中「实测字数」「载体
素材混入」「结构残留」都能机械判，但它们过去写在每轮的临时代码里——扫描词表
随当场想到什么而变（某张卡扫了公式占位符 `〈M12〉`，另一张没扫；某次比了
`word_count`，另一次忘了）。**同一件事每次口径不同**，于是「自查通过」实际只
说明「当场记得查的那几项通过了」。

本脚本把可机械判的部分固定下来，并且**显式列出脚本判不了的两项**（首尾是否同一
事件、是否串入他卡内容）——自查报告必须如实标注盲区，不能让人误以为「脚本绿了
= 四条都查过了」。

形态检查直接复用 `validate_memo.check()`，不另写一套判据：写前与写后必须同源，
否则两头口径一漂，谁也不知道该信哪边。

用法：
  python scripts/verify_card.py --id MjYxMzk1MDg0
  python scripts/verify_card.py --id MjYxMzk1MDg0 --json   # 供自动化消费

退出码：0 = 机械项全过；1 = 有机械项不过或回读失败。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
REPO = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

import flomo_client as FC  # noqa: E402
import validate_memo as VM  # noqa: E402

# 抽取器丢公式时留下的占位符（arXiv HTML 抽取的已知失败形态）：
# 正文里成片出现即说明抽取坏了，卡片只能靠摘要写成，属降级例外。
PLACEHOLDER = re.compile(r"[〈〖＜]\s*M\d+\s*[〉〗＞]")

# 脚本判不了、必须由人通读确认的项。列出来是为了让「自查报告」不虚报覆盖面。
HUMAN_JUDGMENT = [
    ("首尾是否同一事件", "跨事件拼装只有读懂内容才看得出，正则判不出"),
    ("是否串入他卡内容", "同名主题的近邻卡内容是否混入，需要与近邻卡比对"),
]

STRUCT_LABELS = ("要点", "结构", "标签")


def _undo_render_blank(content: str):
    """还原渲染层在标签段后补的那一个空行。返回 (正文, 是否还原过)。

    flomo 存储端会在标签行后插一个空行（本地草稿没有），于是云端回读的
    `lines[1]` 是空串、概念名落到 `lines[2]`。若直接按「第二行」判断，每张
    合规卡都会被判成缺概念名——这不是内容问题，是**不由我们控制的那一层**。

    只删这一处，且三个条件同时成立才动手（首行是标签段 / 第二行空 / 第三行
    非空），避免把正文里的空行误当成渲染填充删掉。
    """
    lines = content.split("\n")
    if (len(lines) > 2 and lines[0].strip().startswith("#")
            and not lines[1].strip() and lines[2].strip()):
        return "\n".join(lines[:1] + lines[2:]), True
    return content, False


def _default_client():
    """默认构造：`load_token()` 返回 (token, 来源) 元组，先解包再交给客户端。

    直接 `FlomoClient(load_token())` 会把元组当 token 拼进请求头，报
    `can only concatenate str (not "tuple") to str`——离线用例若把客户端整个替换掉，
    这条路径根本不会被执行，所以它被单独抽成一个函数以便用例钉住。
    """
    token, _src = FC.load_token()
    return FC.FlomoClient(token)


def fetch(memo_id: str, client=None):
    """取回卡片全文。`client` 可注入（用例传假 client，避免联网）。"""
    client = client or _default_client()
    res = client.tool("memo_batch_get", {"ids": [memo_id]})
    sc = res.get("structuredContent") or res
    memos = sc.get("memos") or []
    if not memos:
        raise RuntimeError(f"回读失败：未返回卡片 {memo_id}")
    return memos[0]


def run(memo_id: str, client=None):
    """返回 (检查项列表, 卡片元信息)。检查项为 (名称, 是否通过, 详情)。"""
    memo = fetch(memo_id, client)
    content = memo.get("content") or ""
    raw = content.replace("\r\n", "\n").replace("\r", "\n")
    norm, render_padded = _undo_render_blank(raw)
    lines = norm.split("\n")
    checks = []

    truncated = bool(memo.get("content_truncated"))
    checks.append(("回读完整（未被截断）", not truncated,
                   "截断时字数与结构判断都不可信，须换 memo_batch_get 重取"))

    first = lines[0].strip() if lines else ""
    second = lines[1].strip() if len(lines) > 1 else ""
    checks.append(("首行是完整标签段", first.startswith("#") and "/" in first, first[:60]))
    checks.append(("第二行是概念名",
                   bool(second) and not second.startswith("#")
                   and not second.startswith(STRUCT_LABELS),
                   second[:60]))

    # 形态检查同源于写前校验器：清空模块级列表后跑一遍。
    # 喂的是还原过渲染空行的正文——校验器按本地草稿形态写，标签行后紧跟概念名，
    # 直接喂云端原文会让每张合规卡都吃一条「第二行不应为空」的假提示。
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check(norm)
    checks.append(("形态检查（复用 validate_memo）", not VM.ERR,
                   "；".join(VM.ERR[:3]) if VM.ERR else ""))

    hits = PLACEHOLDER.findall(norm)
    checks.append(("无内容占位符残留（抽取未丢正文）", not hits,
                   f"命中 {len(hits)} 处：{hits[:3]}"))

    # 正文常以换行收尾，直接取 lines[-1] 会得到空串——「末行」是人读首尾时的
    # 落点，给空串等于误导（曾据此误判末句为逗号收尾）。取最后一个非空行。
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    info = {
        "id": memo.get("id", memo_id),
        "word_count": memo.get("word_count"),
        "chars": len(raw),
        "lines": len(nonempty),
        "tags": memo.get("tags") or [],
        "first": first,
        "second": second,
        "tail": nonempty[-1] if nonempty else "",
        "render_padded": render_padded,
        "warnings": list(dict.fromkeys(VM.WARN)),
    }
    return checks, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="写云后回读全文，做 H11 可机械判定的自查")
    ap.add_argument("--id", required=True, help="卡片 id（写云回包里的 id）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供自动化消费）")
    args = ap.parse_args(argv)

    try:
        checks, info = run(args.id)
    except Exception as e:  # noqa: BLE001 — 回读失败只影响本次自查，转成退出码
        print(f"[verify] 回读失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    ok = all(c[1] for c in checks)
    if args.json:
        print(json.dumps({
            "ok": ok,
            "info": info,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
            "human_judgment": [{"item": n, "why": w} for n, w in HUMAN_JUDGMENT],
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    print(f"[verify] {info['id']} | 标签 {' '.join(info['tags']) or '-'} | "
          f"字数 {info['word_count']} | 字符 {info['chars']} | 行数 {info['lines']}")
    if info["render_padded"]:
        print("  （云端回读在标签段后多一个空行，属渲染层行为，结构判断已按本地形态还原）")
    print(f"  首行 {info['first']}")
    print(f"  次行 {info['second']}")
    print(f"  末行 {info['tail'][:60]}")
    print("[机器判定]")
    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  {mark} {name}" + (f"  [{detail}]" if detail and not passed else ""))
    if info["warnings"]:
        print("[提示（WARN，不阻断，但须看一眼）]")
        for w in info["warnings"][:5]:
            print(f"  ! {w}")
    print("[须人读——脚本判不了，别当成已查过]")
    for name, why in HUMAN_JUDGMENT:
        print(f"  ? {name}（{why}）")
    print("---")
    print(f"[verify] {'机械项全过' if ok else '存在失败项'}（{len(checks)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
