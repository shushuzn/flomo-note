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
  python scripts/verify_card.py --id MjYxMzk1MDg0 --draft _tmp/memo_body_37.txt
    # 追加一项机械比对：云端回读与本地草稿按写路径同口径（FC._norm_body：
    # 丢空行 / 还原转义 / 去行内水平空白）逐字比。不传草稿时只报数字，
    # 不假装对过账——「数字相等」曾靠两层平台差异相互抵消蒙对过（卡 37）。
  python scripts/verify_card.py --id MjYxMzk1MDg0 --show-body
    # 打印云端全文（还原渲染层补的空行）。存在理由是那几条「须人读」项：
    # 早先为了看一眼全文，要临时写一个 memo_batch_get 请求文件、再把 id 手抄进去、
    # 再用内联 python 抠 content——H5 明令 id 不手抄，而这一步每轮都做一次。
    # --show-body 把「回读 → 判定 → 摆出原文」收成一次调用，人读项就在手上这段里查。

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

import cli_util as CLI  # noqa: E402
import flomo_client as FC  # noqa: E402
import validate_memo as VM  # noqa: E402
from memo_util import find_truncation  # noqa: E402

# 抽取器丢公式时留下的占位符（arXiv HTML 抽取的已知失败形态）：
# 正文里成片出现即说明抽取坏了，卡片只能靠摘要写成，属降级例外。
PLACEHOLDER = re.compile(r"[〈〖＜]\s*M\d+\s*[〉〗＞]")

# 脚本判不了、必须由人通读确认的项。列出来是为了让「自查报告」不虚报覆盖面。
HUMAN_JUDGMENT = [
    ("首尾是否同一事件", "跨事件拼装只有读懂内容才看得出，正则判不出"),
    ("是否串入他卡内容", "同名主题的近邻卡内容是否混入，需要与近邻卡比对"),
    ("第二行是否标识对象本身（不是事件/切片）", "名词短语只是形式：`某车撞报废气囊没弹` 也成短语，说的却是一件事故；事件名词表外的一律要人判（见 H6b/H6d）"),
]

STRUCT_LABELS = ("要点", "结构", "标签")


def _undo_render_blank(content: str):
    """还原渲染层在标签段后补的那一个空行。返回 (正文, 是否还原过)。

    flomo 存储端会在标签行后插一个空行（本地草稿没有），于是云端回读的
    `lines[1]` 是空串、概念名落到 `lines[2]`。若直接按「第二行」判断，每张
    合规卡都会被判成缺概念名——这不是内容问题，是**不由我们控制的那一层**。

    只删这一处，且三个条件同时成立才动手（首行是标签段 / 第二行空 / 第三行
    非空），避免把正文里的空行误当成渲染填充删掉。

    **渲染层还会在「要点：」这类列表引导行后补空行**（见
    `flomo_client._norm_body` 的 docstring）。这里刻意不还原它：草稿可能本来
    就写了那个空行，回读里两种形态无法区分，删了就会把「草稿有的空行」删掉。
    结构判断只需要概念名落在第二行，与这一处无关；**字符对账因此不能用
    「还原后的长度」**——那会把平台差异算进内容（卡 37：数字靠「要点：补行
    +1」与「末尾换行被修 −1」相互抵消蒙对，分解也是错的）。对账口径统一走
    `FC._norm_body`：丢空行、还原转义、去行内水平空白，与写路径
    `readback_check` 同源。
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
    """取回卡片全文。`client` 可注入（用例传假 client，避免联网）。

    回包解析走 `FC.tool_data`（唯一实现）。此前是 `structuredContent or res`——
    形态一变就把**整个回包信封**当卡片，`memos` 取不到即报「回读失败：未返回卡片」，
    把「协议变了」误诊成「云端没有这张卡」，接着就会有人去重建一张重复卡。
    """
    client = client or _default_client()
    sc = FC.tool_data(client.tool("memo_batch_get", {"ids": [memo_id]}))
    memos = sc.get("memos") or []
    if not memos:
        raise RuntimeError(f"回读失败：未返回卡片 {memo_id}")
    return memos[0]


def run(memo_id: str, client=None, draft_path=None):
    """返回 (检查项列表, 卡片元信息)。检查项为 (名称, 是否通过, 详情)。

    `draft_path` 给本地草稿路径时，追加一项机械比对：把云端回读与草稿按
    `FC._norm_body` 同口径归一化后逐字比（丢空行 / 还原转义 / 去行内水平
    空白）——这三类差异都由平台层决定，不参与比对；行界与所有非转义字符
    不宽容。不传草稿时只报数字，不假装对过账。
    """
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

    # 截断占位符与上一条不同源：那边是「抽取器丢公式」，这边是「正文取自检索回包的
    # 截断版本」。一旦如此，云端正文的中段就是永久缺失，且此前的判据都看不出来
    # （结尾完好、字数够、逐字对账两边同缺），只能靠标记本身识别。
    trunc_marks = find_truncation(norm)
    checks.append(("无截断占位符（正文未取自检索回包）", not trunc_marks,
                   f"命中 {len(trunc_marks)} 处：{trunc_marks[:3]}，正文中段已缺失"
                   if trunc_marks else ""))

    # 正文常以换行收尾，直接取 lines[-1] 会得到空串——「末行」是人读首尾时的
    # 落点，给空串等于误导（曾据此误判末句为逗号收尾）。取最后一个非空行。
    nonempty = [ln.strip() for ln in lines if ln.strip()]

    # 字数对账：H11 ④ 要的是「实测字数」，比的是一份本地草稿。而云端回读的字符数
    # 天然含四层平台自有差异——渲染层补的空行（标签段后 + 「要点：」后）、全角标点
    # 与行内代码之间插入的空格、存储层对 Markdown 元字符的反斜杠转义
    # （`[Z][I]=[V]` 存成 `\[Z\]\[I\]\[V\]`）、以及草稿文件末尾换行被修掉。
    # 直接报 `len(原始回读)` 会系统性地大于草稿，差额由这几层决定而非内容——实测卡 34
    # 差 7 个字符、卡 37 差 1 个，只能靠人当场算账，且旧口径只还原了第一层，卡 37 的
    # 分解是错的（数字靠两层差异抵消蒙对）。故对账口径与写路径 `readback_check`
    # 同源：`FC._norm_body`（丢空行 / 还原转义 / 去行内水平空白），四层差异分别列出。
    escapes = FC._MD_ESCAPES.findall(norm)
    plain = FC._MD_ESCAPES.sub(r"\1", norm)
    kept_joined = "\n".join(ln for ln in plain.split("\n") if ln.strip())
    net = FC._norm_body(raw)
    # 空行 = 丢掉的空行占的换行（草稿自有的 + 渲染层补的，此处已分不清，也不该分）；
    # 行内空白 = 口径去掉的水平空白（ mostly 是草稿自带的中英混排空格，两侧同去，
    # 不是平台插入的——标注时不得写成「平台差异」）。
    blank_chars = len(plain) - len(kept_joined)
    space_chars = len(kept_joined) - len(net)

    info = {
        "id": memo.get("id", memo_id),
        "word_count": memo.get("word_count"),
        "chars": len(plain),
        "net": len(net),
        "chars_raw": len(raw),
        "escapes": len(escapes),
        "blanks": blank_chars,
        "spaces": space_chars,
        "render_pad": len(raw) - len(norm),
        "lines": len(nonempty),
        "tags": memo.get("tags") or [],
        "first": first,
        "second": second,
        "tail": nonempty[-1] if nonempty else "",
        "render_padded": render_padded,
        "draft_compared": bool(draft_path),
        "warnings": list(dict.fromkeys(VM.WARN)),
        # 人读全文用：已还原渲染层补的空行，形态与本地草稿一致（不是 _norm_body 那个
        # 丢空行/去空白的比对口径——那个只用来判等，拿去打印会把排版压成一坨）。
        # main 会把它从 --json 输出里摘掉，免得自动化消费方平白多背一份全文。
        "content": norm,
    }

    if draft_path:
        draft = Path(draft_path).read_text(encoding="utf-8")
        a, b = FC._norm_body(draft), net
        if a == b:
            checks.append(("与本地草稿逐字比对（同写路径口径）", True, f"{len(b)} 字"))
        else:
            i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]),
                     min(len(a), len(b)))
            checks.append(("与本地草稿逐字比对（同写路径口径）", False,
                           f"首个差异在第 {i + 1} 个字符（草稿 {len(a)} 字 / 云端 {len(b)} 字）"))
    return checks, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="写云后回读全文，做 H11 可机械判定的自查")
    ap.add_argument("--id", required=True, help="卡片 id（写云回包里的 id）")
    ap.add_argument("--draft", help="本地草稿路径；给了就追加与草稿的逐字比对（同写路径口径）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供自动化消费）")
    ap.add_argument("--show-body", action="store_true",
                    help="打印云端全文（还原渲染层补的空行），供「须人读」项当场核对；"
                         "与 --json 不同时生效")
    args = ap.parse_args(argv)

    try:
        checks, info = run(args.id, draft_path=args.draft)
    except Exception as e:  # noqa: BLE001 — 回读失败只影响本次自查，转成退出码
        print(f"[verify] 回读失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    ok = all(c[1] for c in checks)
    # 全文只供人眼核对，不进 JSON：--json 是给程序消费的，多塞一份正文既没用到，
    # 又让人误以为机器已经查过「首尾是否同一事件」这类语义项。
    body_text = info.pop("content", "")
    if args.json:
        if args.show_body:
            print("[提示] --json 供程序消费，不与 --show-body 同时生效（未打印全文）",
                  file=sys.stderr)
        print(json.dumps({
            "ok": ok,
            "info": info,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in checks],
            "human_judgment": [{"item": n, "why": w} for n, w in HUMAN_JUDGMENT],
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    print(f"[verify] {info['id']} | 标签 {' '.join(info['tags']) or '-'} | "
          f"字数 {info['word_count']} | 净字符 {info['net']} | 行数 {info['lines']}")
    if info["chars_raw"] != info["net"]:
        parts = []
        if info["blanks"]:
            parts.append(f"空行 {info['blanks']}")
        if info["spaces"]:
            parts.append(f"行内空白 {info['spaces']}（两侧同去）")
        if info["escapes"]:
            parts.append(f"存储转义 {info['escapes']} 处")
        print(f"  （云端原样 {info['chars_raw']} 字符 = 净内容 {info['net']} + "
              f"{' + '.join(parts)}；净内容口径 = 丢空行/去行内空白/还原转义后）")
    if info["draft_compared"]:
        print("  （已与本地草稿逐字比对：行界与非转义字符不宽容，空行/行内空白/转义不参与）")
    else:
        print("  （未传 --draft：只报数字，未与草稿对账；要机械对账请加 --draft <草稿路径>）")
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
    if args.show_body and body_text:
        print("[云端全文——上面人读项就在这段里查]")
        for line in body_text.split("\n"):
            print(f"  {line}" if line.strip() else "")
    print("---")
    print(f"[verify] {'机械项全过' if ok else '存在失败项'}（{len(checks)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    # 与其他命令行脚本同一出口：`FlomoError`（凭据缺失/握手失败）收成一行 + 退出码。
    # `main` 里只兜了 `run()` 这一段，argparse 之后还有 `--draft` 读文件等分支。
    sys.exit(CLI.cli_entry(main, errors=(FC.FlomoError,)))
