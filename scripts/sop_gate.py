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
      {"term": "CoRL 最佳论文", "query": "...", "conclusion": "正式名为 CoRL 2025 杰出论文奖"}
    ]
  }
  说明：`--skip-web` 仅当卡片主体不是专业术语/机构/产品/模型/定理简称时可用，
  且会在凭证中留痕 `web_skipped=true`，便于事后审计。

退出码：0 = 闸门全过并已出凭证；1 = 有阻塞项未过（凭证不出）。
"""
import argparse
import json
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


def _check_web(verify_path, blockers, notes):
    """② 验证：读取调用方的网络搜索留痕，校验术语覆盖。"""
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
    for t in terms:
        if not (t.get("term") and t.get("conclusion")):
            blockers.append(f"验证记录条目缺 term/conclusion：{t}")
    notes.append(f"验证·已核实 {len(terms)} 个术语")
    return {"searched": data.get("searched"), "term_count": len(terms)}


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
        web = _check_web(args.verify, blockers, notes)

    token, src = load_token()
    client = FlomoClient(token)
    client.init()

    _check_tag_tree(client, blockers, notes)      # ④
    dedup = _check_dedup(client, sig, blockers, notes)  # ⑤
    review = _check_review(client, sig, blockers, notes, args.anchor_id)  # ⑧

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
    gate = {
        "signature": sig,
        "sig_key": sig_key,
        "body_hash": body_hash(content),
        "generated_at": int(time.time()),
        "expires_at": int(time.time()) + GATE_TTL,
        "web": web,
        "dedup": dedup,
        "review": review,
    }
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
