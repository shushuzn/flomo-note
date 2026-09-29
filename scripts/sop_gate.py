#!/usr/bin/env python3
"""SOP 流程闸门 —— 把"靠自觉"的检查型步骤变成"必须留痕"。

背景（事故复盘）：SOP 九步里的 ②验证（网络搜索）、④tag_tree 数量核对、
⑤查重两路并查、⑧复盘三路现查，此前全靠执行者自觉，没有任何机械强制。
执行者跳过时，`validate_memo.py` 只校验卡片**文本格式**，完全无从发现
"流程步骤没做"——于是出现"卡写对了但 SOP 漏做"的事故。

本脚本把上述四个检查型步骤收敛为一次**代跑 + 出凭证**：
  - 脚本自己真去调 flomo 只读工具（tag_tree / memo_search / memo_recommended），
    以及校验调用方提供的 search 验证记录；
  - 全部通过后写一份 `.sop_gate/<signature>.json` 凭证；
  - `validate_memo.py` 写云前会检查凭证是否存在、是否覆盖本卡签名、是否在有效期内。

于是"漏做"在物理上不再可能：没跑闸门 → 没有凭证 → 写云被判 ERR。

用法：
  python sop_gate.py --memo <卡片正文路径> --verify <验证记录JSON> [--search-kw k1 --search-kw k2 ...]
  python sop_gate.py --memo <卡片正文路径> --verify <验证记录JSON> --skip-web   # 非术语卡，显式声明无需验证

验证记录 JSON 结构（由调用方在完成第 2 步网络搜索后手工落盘）：
  {
    "searched": true,
    "terms": [
      {"term": "隐式世界-动作模型", "query": "...", "conclusion": "确认为 ... 的标准称谓"},
      {"term": "Geely E5", "query": "...", "conclusion": "纯电动紧凑型 SUV，WLTP 续航 430 公里",
       "in_body": ["WLTP 续航 430 公里"]}
    ]
  }
  说明：`--skip-web` 仅当卡片主体不是专业术语/机构/产品/模型/定理简称时可用，
  且会在凭证中留痕 `web_skipped=true`，便于事后审计。

**结论必须落进正文（本闸门的新增阻塞项）**：留痕只证明「搜索做了」，不证明
「搜到的东西写进卡了」——只落一份验证记录、正文却照抄原文，同样能骗过旧版闸门。
故本脚本逐条核对落点，判定顺序：

  1. `in_body`：调用方显式声明的正文落点片段。每段都必须是正文的真实子串，
     且不得等于术语本身（否则是空转声明）；不符即阻塞。
  2. 结论里能提取到**关键参数**（数字 + 单位，如 `430 公里` / `160 kW` / `31,200 欧元`，
     以及「五星」这类评级）时：至少一项须出现在正文，否则阻塞。
  3. 结论提取不到关键参数（纯命名类核实）时：术语本身的词块至少一个须出现在正文，
     否则阻塞。
  4. 确实不该写进正文的条目（如与卡片主题无关的参数），用
     `"not_in_body": "<理由>"` 显式豁免；豁免会随凭证留痕，供事后审计。

比对前两侧统一做归一化（去空白与千分位逗号、统一小写），避免
`160 kW` / `160kW`、`1,055 公里` / `1055公里` 这类写法差异造成误判。

**凭证自带边界声明**：凭证里的 `human_judgment` 列出本闸门**机器判不了**的项
（如「查重候选是否同一事件」属语义判定、写云后才存在的本卡全文自查）。它是
如实标注闸门的边界，**不参与放行判定**——一份看起来处处有据的凭证比一份
标注了盲区的凭证更危险。

退出码：0 = 闸门全过并已出凭证；1 = 有阻塞项未过（凭证不出）。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient, FlomoError, load_token, _result_memos  # noqa: E402
from memo_util import (  # noqa: E402
    body_hash,
    count_snapshot,
    keywords_of_concept,
    memo_signature,
    signature_key,
    snapshot_total,
    tag_leaves,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
GATE_DIR = PROJECT_ROOT / ".sop_gate"
# 凭证有效期（秒）。同一张卡的闸门凭证在此时限内可反复用于自检，
# 超期须重跑，避免"昨天的凭证给今天的卡用"。
GATE_TTL = 6 * 3600

# 本闸门**机器判不了、只能人工判定**的项。写进凭证是刻意的：
# 一份处处有据、却把盲区藏起来的凭证，比一份如实标注边界的东西更危险——
# 审计者会以为「凭证齐 = 全查过了」。清单只声明边界，不改变任何放行条件。
_HUMAN_ITEMS = [
    {
        "item": "⑤ 查重：命中候选是否同一事件",
        "why": "闸门只保证两路真的跑了、候选被读到；「是否撞卡」须按 H8 三项语义判定，机器判不了",
        "evidence": "dedup.keyword_hits / dedup.tag_neighbors（候选已留痕）",
    },
    {
        "item": "⑧ 本卡全文自查（H11 ①–④）",
        "why": "写云后才存在本卡，闸门运行时无从校验本卡自身",
        "evidence": "写云后回读本卡全文（memo_batch_get）并与落点核对的结论比对",
    },
    {
        "item": "① 抓回原文并读回",
        "why": "属工具侧动作，无落盘凭据可查",
        "evidence": "本轮抓取件与正文草稿（按 H24 保留在近轮窗口，可人工比对）",
    },
]

# 结论里的「关键参数」：数字 + 单位。用于核对搜到的参数有没有落进卡正文。
# 单位表只收可核对的量纲，刻意不含「年/月/日」——事件日期本就写在正文里，
# 拿它当落点会形成空转。**互为前缀的单位必须长者在前**（`kWh` 在 `kW` 前），
# 否则 `60.2 kWh` 会被截成 `60.2 kW`，落点比对随之失真。
_EVIDENCE_RE = re.compile(
    r"\d[\d,.]*\s*(?:kWh|kW|km|kg|GB|TB|MB|GHz|MHz|Hz|nm|"
    r"亿元|万元|欧元|美元|元|吨|公斤|千克|克|"
    r"公里|千米|毫米|厘米|米|小时|分钟|秒|%|％)",
    re.IGNORECASE,
)
# 评级类关键结论（无数字，但同样属「搜到的实质信息」）。
_RATING_RE = re.compile(r"(?:五|四|三|两)星")
# 术语词块：结论无关键参数时，按术语本身的 ASCII / 中文词块比对落点。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_.]{1,}|[\u4e00-\u9fff]{2,}")


def _norm(text):
    """比对用归一化：去空白与千分位逗号、统一小写。

    正文与验证记录的空格 / 大小写写法常不一致（`160 kW` / `160kW`、
    `1,055 公里` / `1055公里`），逐字比对必然误判，故两侧都先过这一层。
    """
    return re.sub(r"[\s,，]", "", text or "").lower()


def _evidence_tokens(text):
    """结论里的关键参数（数字 + 单位、星级）。"""
    return [m.group(0) for m in _EVIDENCE_RE.finditer(text or "")] + [
        m.group(0) for m in _RATING_RE.finditer(text or "")
    ]


def _name_tokens(text):
    """术语本身的 ASCII / 中文词块（结论无关键参数时的兜底落点）。"""
    return _TOKEN_RE.findall(text or "")


def _landed_record(term, landed, **extra):
    """落点审计记录的唯一构造入口——每条都必须显式带 `landed`。

    这个 `landed` 是汇总行与凭证的唯一计数依据。此前汇总行是按手写的键名清单
    （`exempt` / `matched`）反推的，新增一条分支只要键名不同就**静默漏计**：
    `in_body` 锚点分支返回的键是 `anchors`，于是整份都用锚点声明的验证记录会
    显示成「0 条在正文有落点」，而凭证里锚点逐条齐全——不阻塞写入，却让审计
    文案与事实相反。计数不能靠「猜键名」，只能靠分支自己申报。
    """
    rec = {"term": term, "landed": bool(landed)}
    rec.update(extra)
    return rec


def _check_term_landed(item, body, blockers):
    """一条核实结论是否落进了卡正文；返回落点审计记录（写进凭证）。

    判定顺序见模块顶部用法说明。留痕证明「搜了」，本函数证明「写了」——
    缺了它，「搜了不用」与「留痕齐全、正文照抄原文」都能通过闸门。

    每个分支一律经 `_landed_record` 返回（不得裸 dict）：计数只看 `landed`，
    新增分支忘了申报就会被 `test_sop_gate.py` 的结构用例当场拦下。
    """
    term = (item.get("term") or "").strip()
    conclusion = item.get("conclusion") or ""
    norm_body = _norm(body)

    reason = (item.get("not_in_body") or "").strip()
    if reason:
        return _landed_record(term, True, exempt=reason)

    anchors = item.get("in_body") or []
    if anchors:
        ok = True
        for a in anchors:
            na = _norm(a)
            if not na or na == _norm(term) or na in _norm(term):
                ok = False
                blockers.append(
                    f"验证术语「{term}」的 in_body 落点「{a}」无信息量"
                    "（等于或包含于术语本身）——落点须是正文里承载该结论的具体表述"
                )
            elif na not in norm_body:
                ok = False
                blockers.append(
                    f"验证术语「{term}」声明的 in_body 落点「{a}」在卡正文中不存在"
                )
        return _landed_record(term, ok, anchors=anchors)

    evidence = _evidence_tokens(conclusion)
    if evidence:
        matched = [t for t in evidence if _norm(t) in norm_body]
        if not matched:
            blockers.append(
                f"验证术语「{term}」的结论含关键参数（{'、'.join(evidence[:3])}），"
                "但卡正文里一个都没落——「搜到」不等于「写到」，须融入正文对应要点；"
                "确属不该写入正文的，用 not_in_body 显式豁免并给出理由"
            )
        return _landed_record(term, bool(matched), evidence=evidence, matched=matched)

    names = _name_tokens(term)
    matched = [t for t in names if _norm(t) in norm_body]
    if not matched:
        blockers.append(
            f"验证术语「{term}」在卡正文中找不到落点"
            "（结论无关键参数，按术语名比对亦未命中）"
        )
    return _landed_record(term, bool(matched), names=names, matched=matched)


def _tag_tree_total_and_count(client):
    """现采 tag_tree，返回 (total, 覆盖全部标签的集合)。

    limit 必须传大值（服务端默认 200 会截断）；以 structuredContent.total 为准。
    """
    res = client.tool("tag_tree", {"limit": 2000})
    sc = (res or {}).get("structuredContent") or {}
    total = sc.get("total")
    tags = sc.get("tags")
    if total is None or tags is None:
        # 兜底：解析 content[].text 内层 JSON
        for block in (res or {}).get("content") or []:
            try:
                inner = json.loads(block.get("text") or "")
            except Exception:
                continue
            total = total if total is not None else inner.get("total")
            tags = tags if tags is not None else inner.get("tags")
    return total, tags or []


def _local_tag_tree_paths():
    """本地 tag_tree 快照可能存在的路径（历史口径不一，全部纳入比对）。"""
    return [
        PROJECT_ROOT / "tag_tree.txt",
        SCRIPT_DIR / "tag_tree.txt",
    ]


def _check_tag_tree(client, blockers, notes):
    """④ 标签树数量核对：云端 total 与本地快照必须自洽。"""
    total, tags = _tag_tree_total_and_count(client)
    if total is None:
        blockers.append("tag_tree 现采未取到 structuredContent.total（工具返回异常）")
        return
    checked_any = False
    for p in _local_tag_tree_paths():
        if not p.exists():
            continue
        checked_any = True
        head = p.read_text(encoding="utf-8-sig").splitlines()
        local_total = snapshot_total(head)
        leaves, bare = count_snapshot(head)
        listed = leaves + bare
        if local_total != total:
            blockers.append(
                f"{p.name} 首行 total={local_total} 与云端 total={total} 不一致——须现采整体重写"
            )
        if local_total is not None and listed != local_total:
            blockers.append(
                f"{p.name} 列出标签数 {listed}（二级行 {leaves} + 裸顶层 {bare}）"
                f"与首行 total={local_total} 不自洽——须整体重写"
            )
        notes.append(
            f"tag_tree 本地快照 {p.name}: total={local_total}, "
            f"二级行={leaves}, 裸顶层={bare}, 云端 total={total}"
        )
    if not checked_any:
        blockers.append("未找到任何本地 tag_tree 快照（须先现采并整体重写）")


def _check_dedup(client, sig, blockers, notes):
    """⑤ 查重两路并查：memo_search 关键词扫 + tag_tree 同主标签细分比对。

    本函数只负责"两路都真的跑了、且结果被读到"；是否撞卡由调用方在
    正文自检时判断（闸门不做语义合并决策）。命中候选会写进凭证供人工复核。
    """
    concept = sig["concept"]
    kws = keywords_of_concept(concept, limit=2)
    if not kws:
        blockers.append("无法从概念名派生查重关键词")
        return {}
    hits = {}
    for kw in kws:
        res = client.tool("memo_search", {"keywords": kw, "limit": 20})
        memos = _result_memos(res)
        hits[kw] = [
            {"id": m.get("id"), "content_head": (m.get("content") or "")[:60]}
            for m in memos
        ]
        notes.append(f"查重·关键词「{kw}」命中 {len(memos)} 条")
    # 第二路：tag_tree 列同主标签细分
    total, tags = _tag_tree_total_and_count(client)
    leaves = tag_leaves(sig["tagline"])
    near = []
    for top, leaf in leaves:
        hit = 0
        for t in tags:
            name = t.get("name") if isinstance(t, dict) else str(t)
            # 精确匹配 `顶层/二级` 或其下的子路径；不用裸子串匹配，否则
            # 短二级词会命中无关簇（如「机器」命中「其他/机器人架构」）。
            if name and (name == f"{top}/{leaf}" or name.startswith(f"{top}/{leaf}/")):
                near.append(name)
                hit += 1
        notes.append(f"查重·标签路「{top}/{leaf}」近邻 {hit} 个簇")
    return {"keyword_hits": hits, "tag_neighbors": sorted(set(near))}


def _check_review(client, sig, blockers, notes, anchor_id=None):
    """⑧ 复盘三路现查：memo_search + tag_tree 已在查重做过，此处补 memo_recommended。

    服务端 `memo_recommended` 的 `id` 为**必填**（传别的键会报 unexpected
    additional properties，不传 id 会报 missing properties: ["id"]）——
    它按指定卡返回相关推荐。写云前本卡尚不存在，故以关键词检索命中的首条
    卡作锚；确无任何命中（全新主题）则以最近更新的卡兜底，并留痕 anchor 来源。
    """
    anchor, anchor_src = anchor_id, "调用方提供"
    if not anchor:
        for kw in keywords_of_concept(sig["concept"], limit=2):
            res = client.tool("memo_search", {"keywords": kw, "limit": 1})
            memos = _result_memos(res)
            if memos:
                anchor = memos[0].get("id")
                anchor_src = f"关键词「{kw}」命中首条"
                break
    if not anchor:
        blockers.append("复盘第 ⑧ 步无法取锚：既未提供本卡 id，关键词检索也无任何命中")
        return None
    res = client.tool("memo_recommended", {"id": anchor, "limit": 20})
    recs = _result_memos(res)
    notes.append(f"复盘·memo_recommended（锚={anchor}，{anchor_src}）返回 {len(recs)} 条")
    return {"anchor": anchor, "anchor_source": anchor_src, "recommended": [m.get("id") for m in recs]}


def _check_web(verify_path, body, blockers, notes):
    """② 验证：读调用方的网络搜索留痕，校验术语覆盖**且结论已落进卡正文**。

    两件事都做才叫完成：留痕证明「搜索做了」，落点校验证明「搜到的写进卡了」
    （见 `_check_term_landed`）。只查前者会放过「留痕齐全、正文照抄原文」。
    """
    if verify_path is None:
        blockers.append("未提供第 2 步验证记录（--verify）；术语卡必须现查，非术语卡须显式 --skip-web")
        return None
    p = Path(verify_path)
    if not p.exists():
        blockers.append(f"验证记录文件不存在：{p}")
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        blockers.append(f"验证记录非法 JSON：{e}")
        return None
    # 严格判定：必须是 JSON 布尔 true。字符串 "false"/"no" 与数字 0 都是真值，
    # 用真值判定会把「没搜索」的记录判为通过——这是闸门存在的意义所在。
    if data.get("searched") is not True:
        blockers.append(
            f"验证记录 searched 必须为布尔 true（实际为 {data.get('searched')!r}）"
            "——第 2 步未真正执行网络搜索"
        )
    terms = data.get("terms") or []
    if not terms:
        blockers.append("验证记录 terms 为空——未记录任何术语核实结果")
    landed = []
    for t in terms:
        if not (t.get("term") and t.get("conclusion")):
            blockers.append(f"验证记录条目缺 term/conclusion：{t}")
            continue
        landed.append(_check_term_landed(t, body, blockers))
    # 计数只看分支自己申报的 landed（见 _landed_record），不按键名猜——
    # 否则新增分支一改键名就静默漏计，汇总行会与凭证内容相反。
    hit = sum(1 for r in landed if r.get("landed"))
    missed = [r["term"] for r in landed if not r.get("landed")]
    line = f"验证·已核实 {len(terms)} 个术语，其中 {hit} 条在正文有落点或已豁免"
    if missed:
        line += f"；未落点 {len(missed)} 条（{'、'.join(missed[:3])}）"
    notes.append(line)
    return {"searched": data.get("searched"), "term_count": len(terms), "landed": landed}


def assemble_gate(content, sig, web, dedup, review, *, sig_key=None,
                  expires_delta=GATE_TTL, now=None):
    """组装凭证（纯函数：不联网、不落盘），便于用例直接断言凭证内容。

    凭证字段是写云侧 `validate_memo.check_gate` 的判据来源，改动此处
    会同时影响两侧，故单独抽出来可测。
    """
    ts = int(time.time()) if now is None else int(now)
    return {
        "signature": sig,
        "sig_key": sig_key or signature_key(sig),
        "body_hash": body_hash(content),
        "generated_at": ts,
        "expires_at": ts + expires_delta,
        "web": web,
        "dedup": dedup,
        "review": review,
        # 机器盲区随凭证留痕，供事后审计看清闸门的边界（不参与放行判定）。
        "human_judgment": list(_HUMAN_ITEMS),
    }


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--memo", required=True, help="卡片正文文件路径")
    ap.add_argument("--verify", help="第 2 步网络搜索验证记录 JSON 路径")
    ap.add_argument("--skip-web", action="store_true", help="显式声明非术语卡，无需网络验证")
    ap.add_argument("--out", help="凭证输出路径（默认 .sop_gate/<signature>.json）")
    ap.add_argument("--anchor-id", help="可选：本卡 id（更新场景下可作 memo_recommended 锚点）")
    ap.add_argument(
        "--allow-no-gate",
        metavar="REASON",
        help="显式授权本卡在 validate_memo.py 侧可降级闸门（仅限批量处理历史卡）；"
             "理由会写进凭证供事后审计。不加此项时 --no-gate 一律无效。",
    )
    args = ap.parse_args()

    content = Path(args.memo).read_text(encoding="utf-8-sig")
    sig = memo_signature(content)
    if not sig:
        print("[阻塞] 卡片正文不足两行非空内容，无法生成签名——先按 H14 补齐结构", file=sys.stderr)
        return 1

    blockers, notes = [], []
    print(f"== SOP 闸门 · 卡片「{sig['concept']}」 ==")

    # ② 验证
    if args.skip_web:
        web = {"web_skipped": True}
        notes.append("验证·调用方显式声明无需网络验证（--skip-web）")
    else:
        web = _check_web(args.verify, content, blockers, notes)

    token, src = load_token()
    client = FlomoClient(token)
    client.init()

    _check_tag_tree(client, blockers, notes)      # ④
    dedup = _check_dedup(client, sig, blockers, notes)  # ⑤
    review = _check_review(client, sig, blockers, notes, args.anchor_id)  # ⑧

    notes.append(
        f"闸门边界·{len(_HUMAN_ITEMS)} 项机器判不了，须人工判定（已随凭证留痕）："
        + "；".join(i["item"] for i in _HUMAN_ITEMS)
    )
    print()
    for n in notes:
        print("  · " + n)
    print()
    if blockers:
        for b in blockers:
            print(f"[阻塞] {b}", file=sys.stderr)
        print(f"闸门未过：{len(blockers)} 项阻塞，未生成凭证", file=sys.stderr)
        return 1

    sig_key = signature_key(sig)  # 与 validate_memo 侧同源，保证文件名口径一致
    gate = assemble_gate(content, sig, web, dedup, review, sig_key=sig_key)
    if args.allow_no_gate:
        # 降级授权随凭证落盘：validate_memo.py 只在看到此标记时才认 --no-gate。
        # 于是「降级」从调用方口头声明变成凭证里的可审计事实，且绑定本卡签名与正文指纹。
        gate["no_gate_authorized"] = True
        gate["no_gate_reason"] = args.allow_no_gate
        notes.append(f"降级授权·{args.allow_no_gate}")
    out = Path(args.out) if args.out else (GATE_DIR / f"{sig_key}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[通过] 闸门全过，凭证已写入 {out}")
    return 0


if __name__ == "__main__":
    # CLI 入口把库异常转成退出码（非 0，自动化不会据此重试写操作）
    try:
        sys.exit(main())
    except FlomoError as e:
        print(f"[失败] 云端调用出错：{e}", file=sys.stderr)
        sys.exit(2)
