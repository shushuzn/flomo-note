#!/usr/bin/env python3
"""SOP 流程闸门 —— 把"靠自觉"的检查型步骤变成"必须留痕"。

背景（事故复盘）：SOP 九步里的 ②验证（网络搜索）、⑤查重、⑧复盘现查，
此前全靠执行者自觉，没有任何机械强制。
执行者跳过时，`validate_memo.py` 只校验卡片**文本格式**，完全无从发现
"流程步骤没做"——于是出现"卡写对了但 SOP 漏做"的事故。

本脚本把上述三个检查型步骤收敛为一次**代跑 + 出凭证**：
  - 脚本自己真去调 flomo 只读工具（tag_tree / memo_search / memo_recommended），
    以及校验调用方提供的 search 验证记录；
  - 全部通过后写一份 `.sop_gate/<signature>.json` 凭证；
  - `validate_memo.py` 写云前会检查凭证是否存在、是否覆盖本卡签名、是否在有效期内。

于是"漏做"在物理上不再可能：没跑闸门 → 没有凭证 → 写云被判 ERR。

用法（参数与 argparse 实现保持一致，改动其一必须同步另一处）：
  python sop_gate.py --memo <正文路径> --verify <验证记录JSON>
  python sop_gate.py --memo <正文路径> --skip-web --skip-reason "理由"  # 非术语卡，显式声明无需网络验证
  python sop_gate.py --memo <正文路径> --verify <JSON> --anchor-id <本卡id>  # 更新场景，指定复盘锚点
  python sop_gate.py --memo <正文路径> --verify <JSON> --allow-near-concept "理由"  # 疑似同名但确属不同对象
  python sop_gate.py --memo <正文路径> --verify <JSON> --out <凭证路径>      # 自定义凭证落盘位置
  python sop_gate.py --memo <正文路径> --allow-no-gate "<理由>"  # 仅限批量处理历史卡，须写明理由

  查重关键词由脚本自行从概念名派生（见 `_check_dedup` 的 `keywords_of_concept`），
  无需也无法由调用方传入——本脚本**不提供** `--search-kw` 参数。
  概念名判定不依赖检索排序：每个标签簇整簇翻页 + 概念名关键词翻页取候选，
  逐卡比对归一化概念名；同名直接阻塞，包含关系同名须 `--allow-near-concept "理由"`。

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

**④ 标签核对（本闸门的阻塞项之一）**：首行每个 `#顶层/二级` 都要在现采的标签树里
核一遍。只差空白 / 全半角斜杠 / 大小写即视为**写错**（云端会是同一簇的两种写法，
写入即分裂），直接阻塞；标签不在树中而顶层在，只提示「本卡将新建此二级」并列出
同顶层最接近的现有二级——新建二级是常态动作，相近名是否同一簇属语义判断，机器不
背这个结论。查重的标签路同时点明「0 个簇」是哪一种：簇在但没卡，还是这个名字根本
不在树里（后者意味着这一路没扫到任何同类卡）。

**凭证自带边界声明**：凭证里的 `human_judgment` 列出本闸门**机器判不了**的项
（如「查重候选是否同一事件」属语义判定、写云后才存在的本卡全文自查）。它是
如实标注闸门的边界，**不参与放行判定**——一份看起来处处有据的凭证比一份
标注了盲区的凭证更危险。

退出码：0 = 闸门全过并已出凭证；1 = 有阻塞项未过（凭证不出）。
"""
import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_util import cli_entry
from flomo_client import (
    FlomoClient,
    FlomoError,
    _result_memos,
    load_token,
    tool_data,
)
from memo_util import (
    body_hash,
    keywords_of_concept,
    memo_signature,
    normalize_concept,
    signature_key,
    tag_key,
    tag_leaves,
)
from scan_concept_duplication import normalize as strip_slice_name

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
        "item": "④ 标签：命名是否与主体类别相符",
        "why": "闸门只核「写错了没有」（不在树中 / 只差空白与全半角）；"
               "「该不该复用这个簇」「是不是塞进了纯概念筐」属 H17/H18 的语义判断",
        "evidence": "凭证 tag_check 逐条记录每个标签在树里的状态；"
                    "命名决策见第 4 步 tagcheck.py 的输出",
    },
    {
        "item": "⑤ 查重：命中候选是否同一事件",
        "why": "闸门只保证概念名扫描真的跑了（整簇翻页）、同名候选被读出并阻塞；"
               "「不同名但是否同一事件/同一对象的另一种叫法」仍须按 H8 语义判定",
        "evidence": "dedup.concept_sweep（扫描范围、同名/疑似同名候选、取不尽项）"
                    "与 dedup.keyword_hits / dedup.tag_neighbors",
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
# 符号型术语的落点判定：`M+`、`C++`、`.NET`、`Node.js` 这类名字含连接符号，
# 旧正则只认「字母开头」或「连续汉字」，`+` 既非字母也非汉字，于是 `M+` 被切成
# 裸 `M`——比对上正文里的 `M+` 失败，闸门报「在卡正文中找不到落点」。
# 实测写 M+ 那张卡就是靠人工补 in_body 才过闸，而判据本该自己认得出这个名字。
# 故允许名字内部出现 `+ . _ -`，但**不放在开头**（`+` 开头更像公式或列表符号）。
_TOKEN_RE = re.compile(
    r"[.]?[A-Za-z][A-Za-z0-9\-_.]*(?:[+][A-Za-z0-9\-_.]*)*"
    r"|[\u4e00-\u9fff]{2,}"
)
# 「本卡将新建该簇」时列几个同顶层最相近的现有二级供人判断（只作提示）。
TAG_NEAR_SHOW = 3

# 新建二级标签时的**语义近邻**候选：字符相似度对中文几乎无效
# （`老龄` vs `社会保障` 相似度 0），故对常见易混主题显式给出该并进哪个既有簇。
# 只作提示，不阻断——是否同一簇属语义判断，由人定（误拦比漏检更危险）。
# 新增主题时在此补一行；表里没有的仍按相似度排，不因此失效。
TAG_SEMANTIC_NEIGHBORS = {
    "老龄": ("社会保障", "卫生", "公共卫生", "民政"),
    "养老": ("社会保障", "卫生"),
    "提示词": ("人工智能", "智能体", "大模型"),
    "陶瓷": ("家居", "消费", "建材"),
    "博物馆": ("文化遗产", "文物保护", "艺术"),
    "戏剧": ("文学", "影视", "艺术"),
    "可再生能源": ("新能源", "电力", "中国电力"),
    "小语言模型": ("大模型", "开源模型", "推理部署"),
}


def _tag_names(tags):
    """标签树条目归一成字符串列表（回包有 dict 与裸字符串两种形态）。"""
    out = []
    for t in tags or []:
        name = t.get("name") if isinstance(t, dict) else str(t)
        if name:
            out.append(name)
    return out


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


def _check_tags(tags, tagline, blockers, notes):
    """④ 定标签的机器兜底：卡上每个标签在云端树里到底是什么状态。

    【为什么要它】这一步此前没有任何机器兜底。闸门只在查重第二路里数「同标签
    近邻几个簇」，而**标签根本不存在时那个数同样是 0**——「近邻 0 个簇」既可能是
    「簇在、但没有卡」，也可能是「这个名字是刚编出来的」，两种含义完全不同的情况
    长得一模一样。于是二级名写错（`云原生`→`云原主`）、把已有一簇换个写法重开，
    都不会触发任何提示：卡片照样过闸门，云端静默多出一个单卡簇——H18 要防的近义/
    错字簇，有一半是从这里长出来的。

    分档判定，**只有确定是写错才阻塞**：
      · 归一化后与现有标签相同（只差空白、全半角斜杠、大小写）→ 阻塞：云端会是
        同一簇的两种写法，写入即分裂。
      · 其余不存在 → 只提示「本卡将新建该簇」，并列出同顶层最相近的现有二级。
    新建二级是合法且常见的动作；相近名是否同一簇属语义判断，机器不背这个结论
    （误拦比漏检更危险，与 H6d 同一条原则）。
    """
    names = _tag_names(tags)
    if not names:
        blockers.append(
            "标签树未取到（tag_tree 无内容），第 ④ 步标签核对无从进行"
            "——查重的标签路同样空转，重采通过后再跑"
        )
        return {"tags": [], "checked": 0}
    exact = set(names)
    by_key: dict[str, list[str]] = {}
    for n in names:
        by_key.setdefault(tag_key(n), []).append(n)
    leaves = tag_leaves(tagline)
    if not leaves:
        # 三级、单层、全角斜杠都会落到这里。若它与某个现有标签只差写法，先点出
        # 这一层——那不是「格式写歪了」，而是把一个已有的簇换个写法重开。
        tokens = re.findall(r"#([^\s#]+)", tagline)
        dups = [n for t in tokens for n in by_key.get(tag_key(t), []) if n != t]
        if dups:
            blockers.append(
                f"标签「{tokens[0]}」与现有「{dups[0]}」只差空白/全半角/大小写"
                "——云端会把它们当两个簇，写入即分裂；改回现有写法"
            )
        blockers.append(f"首行标签段里没有合法的两级标签（{tagline!r}）——见 H17")
        return {"tags": [], "checked": 0}
    records = []
    for top, leaf in leaves:
        full = f"{top}/{leaf}"
        rec = {"tag": full, "exists": full in exact}
        if rec["exists"]:
            notes.append(f"标签·「{full}」已存在")
            records.append(rec)
            continue
        dup = [n for n in by_key.get(tag_key(full), []) if n != full]
        # 顶层存在性也按键比：`#AI/新二级` 与树里的 `ai/agent` 只差大小写，
        # 逐字比前缀会报「顶层也不存在」——那句提示会让人连顶层一起新建。
        top_k = tag_key(top)
        rec["top_exists"] = any(tag_key(n) == top_k or tag_key(n).startswith(top_k + "/")
                                for n in names)
        siblings = [n.split("/", 1)[1] for n in names
                    if tag_key(n).startswith(top_k + "/") and n.count("/") == 1]
        # 近邻提示要能真的指出「该并进哪个既有簇」。
        # 旧实现按**字符串相似度**排，而语义近邻往往字形无关——`政策/社会保障`
        # 与要新建的 `政策/老龄` 相似度为 0，提示就退化成「列前三个同顶层二级」，
        # 而排在最前的通常是任意字母序/字典序条目（实测给的是「中小企业、交通、产业」），
        # 对判断毫无帮助。故先按同义词表取语义近邻，相似度只作同顶层内的排序补充。
        related = TAG_SEMANTIC_NEIGHBORS.get(leaf, ())
        semantic = [x for x in siblings if x in related]
        rest = sorted((x for x in siblings if x not in semantic),
                      key=lambda x: -difflib.SequenceMatcher(None, x, leaf).ratio())
        near = (semantic + rest)[:TAG_NEAR_SHOW]
        if near:
            rec["near"] = near
        if dup:
            rec["dup_of"] = dup[0]
            blockers.append(
                f"标签「{full}」与现有「{dup[0]}」只差空白/全半角/大小写——云端会把"
                "它们当两个簇，写入即分裂；改回现有写法"
            )
            records.append(rec)
            continue
        where = "该顶层存在，将新建此二级" if rec["top_exists"] else "顶层也不存在，将连顶层一起新建"
        hint = f"；同顶层最接近的现有二级：{'、'.join(near)}" if near else ""
        notes.append(f"标签·「{full}」不在树中（{where}）{hint}")
        records.append(rec)
    return {"tags": records, "checked": len(records)}


def _tag_tree_total_and_count(client):
    """现采 tag_tree，返回 (total, 覆盖全部标签的集合)。

    limit 必须传大值（服务端默认 200 会截断）；以 `total` 为准。
    回包解析走 `flomo_client.tool_data`（唯一实现）：逐字段兜底那套已删——
    它与 `tagcheck` 的「缺 total 就抛」是同一件事的两种写法，同一个回包会在
    两个脚本里得到相反结论。
    """
    sc = tool_data(client.tool("tag_tree", {"limit": 2000}))
    total = sc.get("total")
    tags = sc.get("tags")
    if total is None:
        raise FlomoError("tag_tree 回包缺 total 字段，无法判断标签树是否被截断")
    return total, tags or []


# 概念名扫描的翻页上限与页长。服务端 `memo_search` 单次上限 50，且不认 offset，
# 只能按 `end_date` 日期游标往回翻。10 页 = 单簇最多 500 张；取不尽会记 `truncated`，
# 由闸门在 notes 里明说「只扫到这些」，不把「没扫完」写成「没有重复」。
SWEEP_PAGE_LIMIT = 50
SWEEP_MAX_PAGES = 10
# 包含关系视为「疑似同名」时，多出来的部分最长多少字。事件卡常把主体写进概念名
# （「某甲批评 Anthropic：…」），若只看包含关系，任何关于该主体的新卡都会被拦。
# 只认「对象名 + 短后缀」（「Anthropic」/「Anthropic S-1」）这一种形态。
NEAR_EXTRA_MAX = 6


def _sweep_memos(client, params, max_pages=SWEEP_MAX_PAGES):
    """按日期游标翻页取卡，返回 (memos, truncated)。

    翻页逻辑与 `scan_qualified_concepts.collect_memos(full=True)` 同口径：
    末页不足一页即到头；同一天卡片数超过一页时游标无法前进，记 `truncated`。
    """
    seen, order, end_date, truncated = {}, [], "", False
    for _ in range(max_pages):
        p = dict(params, limit=SWEEP_PAGE_LIMIT)
        if end_date:
            p["end_date"] = end_date
        memos = _result_memos(client.tool("memo_search", p))
        fresh = [m for m in memos if m.get("id") and m["id"] not in seen]
        for m in fresh:
            seen[m["id"]] = m
            order.append(m["id"])
        if len(memos) < SWEEP_PAGE_LIMIT:
            break
        days = [(m.get("created_at") or "")[:10] for m in fresh]
        days = [d for d in days if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
        if not days:
            truncated = True
            break
        oldest = min(days)
        if oldest == end_date:
            truncated = True
            break
        end_date = oldest
    else:
        truncated = True
    return [seen[i] for i in order], truncated


def _concept_identity(name):
    """概念名的同一性口径：① 外形归一（空白/标点/大小写）② 抹掉切片限定后的对象名。

    两口径都取自既有实现（`memo_util.normalize_concept` 与
    `scan_concept_duplication.normalize`），不另写一套正则——三处各写一套时，
    改一侧会让另两侧静默失配，而失配只表现为「判定结论对不上」。
    """
    return normalize_concept(name), strip_slice_name(name or "").lower()


def _concept_relation(cur, other):
    """返回 'same' / 'near' / ''。

    same：任一同一性口径完全相同——机器可断定是同一对象，直接阻塞。
    near：一方是另一方的真子串、短名 ≥4 字，且多出来的部分 ≤ `NEAR_EXTRA_MAX`
          （「Anthropic」/「Anthropic S-1」、`长征十号`/`长征十号乙`）。
          这既可能是同一对象的切片卡，也可能确是两个对象，机器判不了，故同样
          **默认阻塞**，须 `--allow-near-concept` 写明理由放行。多出来的部分更长时
          （事件卡「某甲批评 Anthropic：…」）不算 near——那不是对象名，是句子。
    """
    a1, a2 = cur
    b1, b2 = _concept_identity(other)
    if a1 and a1 == b1:
        return "same"
    if a2 and a2 == b2:
        return "same"
    for x, y in ((a2, b2), (b2, a2)):
        if x and y and x != y and len(x) >= 4 and x in y:
            if len(y.replace(x, "", 1)) <= NEAR_EXTRA_MAX:
                return "near"
    return ""


def _check_dedup(client, sig, blockers, notes, tree=None, anchor_id=None,
                 allow_near=""):
    """⑤ 查重三路并查：概念名整簇扫描（判定路）+ memo_search 关键词 + tag_tree 标签路。

    **判定路为什么必须整簇翻页**（事故复盘）：旧实现只在「关键词命中的前 20 条」
    里比对概念名。同一概念名「Anthropic」的两张卡，旧卡排在关键词检索 20 名之外，
    闸门遂记录「经自动检查概念名均不重复，确认为误命中，放行新建」——查重是否
    有效取决于检索排序，等于没查。现在改为按本卡每个标签簇整簇翻页（叠加概念名
    关键词翻页）取回候选，逐卡比对归一化概念名；与检索排序、单页条数都无关。

    后两路仍保留：关键词命中会打印全文供人工复核，标签路把「近邻几个簇 /
    本卡将新建簇」写进凭证。证据不足时宁可不放行：取不尽会记为 truncated。
    """
    concept = sig["concept"]
    kws = keywords_of_concept(concept, limit=2)
    if not kws:
        blockers.append("无法从概念名派生查重关键词")
        return {}
    hits = {}
    keyword_memos = {}
    for kw in kws:
        memos = _result_memos(
            client.tool("memo_search", {"keywords": kw, "limit": SWEEP_PAGE_LIMIT})
        )
        hits[kw] = [
            {"id": m.get("id"), "content_head": (m.get("content") or "")[:60]}
            for m in memos
        ]
        for m in memos:
            if m.get("id"):
                keyword_memos[m["id"]] = m
        notes.append(f"查重·关键词「{kw}」命中 {len(memos)} 条")
    # 第二路：tag_tree 列同主标签细分
    if tree is None:
        _total, tree = _tag_tree_total_and_count(client)
    names = set(_tag_names(tree))
    leaves = tag_leaves(sig["tagline"])
    near = []
    for top, leaf in leaves:
        hit = 0
        for name in names:
            # 精确匹配 `顶层/二级` 或其下的子路径；不用裸子串匹配，否则
            # 短二级词会命中无关簇（如「机器」命中「其他/机器人架构」）。
            if name == f"{top}/{leaf}" or name.startswith(f"{top}/{leaf}/"):
                near.append(name)
                hit += 1
        # 「0 个簇」有两种含义，必须说清是哪一种：簇在但没卡，还是这个名字根本
        # 不在树里（后者意味着本卡会新建簇，这一路实际没扫到任何同类卡）。
        absent = "" if f"{top}/{leaf}" in names else "（该标签不在树中，本卡将新建簇）"
        notes.append(f"查重·标签路「{top}/{leaf}」近邻 {hit} 个簇{absent}")
    # 命中>0 自动取全文打印：不看全文就过不了闸门
    all_hit_ids = list(keyword_memos)
    if all_hit_ids:
        print(f"\n═══ 查重命中 {len(all_hit_ids)} 张卡，全文如下（必须逐张确认不是重复）═══")
        # 分批取，每批≤10
        for i in range(0, min(len(all_hit_ids), 20), 10):
            chunk = all_hit_ids[i:i+10]
            res = client.tool("memo_batch_get", {"ids": chunk})
            memos = _result_memos(res)
            for m in memos:
                content = m.get("content") or ""
                lines = content.split("\n")
                # 找概念名行：跳过标签行和空行
                concept_line = "(无概念名行)"
                for i, ln in enumerate(lines):
                    s = ln.strip()
                    if not s:
                        continue
                    if i == 0 and s.startswith("#"):
                        continue
                    concept_line = s
                    break
                print(f"\n── {m.get('id')} | {concept_line}")
                print(content[:300])
        print("═══ 命中卡全文结束 ═══\n")

    # 更新场景：概念名没改时，本卡自己就在候选里，同名判定无从谈起（跳过扫描，
    # 省下一批整簇翻页）；确属改名的更新仍要扫——新名字撞上别的卡同样是重复。
    enforce = True
    if anchor_id:
        try:
            am = _result_memos(client.tool("memo_batch_get", {"ids": [anchor_id]}))
        except FlomoError:
            am = []
        old_sig = memo_signature(am[0].get("content")) if am else None
        old_concept = old_sig["concept"] if old_sig else ""
        if old_concept and _concept_relation(_concept_identity(concept), old_concept) == "same":
            enforce = False
            notes.append("查重·更新场景且概念名未改，跳过同名判定（本卡即候选）")
        elif not old_concept:
            notes.append("查重·更新场景但读不到原概念名，仍执行同名判定")

    sweep_rec = {"tag_clusters": [], "queries": [], "scanned": 0,
                 "truncated": [], "same_concepts": [], "near_concepts": []}
    if enforce:
        candidates = dict(keyword_memos)
        for cluster in dict.fromkeys(near):
            memos, truncated = _sweep_memos(client, {"tag": cluster})
            sweep_rec["tag_clusters"].append(
                f"{cluster}({len(memos)}{'·截断' if truncated else ''})")
            if truncated:
                sweep_rec["truncated"].append(f"标签簇 {cluster}")
            for m in memos:
                candidates.setdefault(m["id"], m)
        # 概念名本身也翻页查一遍：标签换到完全无关的簇时，整簇扫描扫不到，
        # 关键词检索是唯一可能命中的一路（同样翻页，不吃单页 50 条的上限）。
        for q in dict.fromkeys([concept, normalize_concept(concept)]):
            if not q or len(q) < 2:
                continue
            memos, truncated = _sweep_memos(client, {"keywords": q}, max_pages=3)
            sweep_rec["queries"].append(
                f"{q}({len(memos)}{'·截断' if truncated else ''})")
            if truncated:
                sweep_rec["truncated"].append(f"关键词 {q}")
            for m in memos:
                candidates.setdefault(m["id"], m)
        sweep_rec["scanned"] = len(candidates)

        cur_id = _concept_identity(concept)
        for mid, m in candidates.items():
            if mid == anchor_id:
                continue
            csig = memo_signature(m.get("content"))
            if not csig:
                continue
            rel = _concept_relation(cur_id, csig["concept"])
            if not rel:
                continue
            rec = {"id": mid, "concept": csig["concept"],
                   "tagline": (csig["tagline"] or "")[:80]}
            if rel == "same":
                sweep_rec["same_concepts"].append(rec)
            else:
                sweep_rec["near_concepts"].append(rec)

        if sweep_rec["same_concepts"]:
            detail = "；".join(
                f"{r['id']}「{r['concept']}」（{r['tagline']}）"
                for r in sweep_rec["same_concepts"][:5])
            blockers.append(
                f"概念名重复·整簇扫描 {sweep_rec['scanned']} 张候选，发现同一对象已有卡：{detail}。"
                "这是重复卡，必须用 --update-id 合并到该卡，不能新建。"
            )
        if sweep_rec["near_concepts"]:
            detail = "；".join(
                f"{r['id']}「{r['concept']}」" for r in sweep_rec["near_concepts"][:5])
            if allow_near.strip():
                notes.append(f"查重·疑似同名已按理由放行（--allow-near-concept：{allow_near.strip()}）：{detail}")
            else:
                blockers.append(
                    f"概念名疑似重复·整簇扫描 {sweep_rec['scanned']} 张候选，"
                    f"发现包含关系同名卡：{detail}。确属同一对象必须 --update-id 合并；"
                    "确属不同对象（如型号后缀不同）加 --allow-near-concept \"理由\" 重跑。"
                )
        if sweep_rec["truncated"]:
            notes.append("查重·候选取不尽：" + "、".join(sweep_rec["truncated"])
                         + "——同名结论只覆盖已取到的卡")
        notes.append(
            f"查重·概念名整簇扫描 {sweep_rec['scanned']} 张候选"
            f"（标签簇 {len(sweep_rec['tag_clusters'])} 个、概念名查询 {len(sweep_rec['queries'])} 次）："
            f"同名 {len(sweep_rec['same_concepts'])} 张、疑似同名 {len(sweep_rec['near_concepts'])} 张"
        )
    return {"keyword_hits": hits, "tag_neighbors": sorted(set(near)),
            "concept_sweep": sweep_rec}



def _check_review(client, sig, blockers, notes, anchor_id=None):
    """⑧ 复盘三路现查：memo_search + tag_tree 已在查重做过，此处补 memo_recommended。

    服务端 `memo_recommended` 的 `id` 为**必填**（传别的键会报 unexpected
    additional properties，不传 id 会报 missing properties: ["id"]）——
    它按指定卡返回相关推荐。写云前本卡尚不存在，故必须借一张已有卡作锚。

    取锚顺序（每一步都写进 notes，凭证里的 anchor_source 标明实际来源）：
      ① 调用方显式给 `--anchor-id`（更新已有卡时就是本卡 id）；
      ② 概念名派生关键词检索，命中首条；
      ③ 关键词全无命中时，退到本卡标签簇（`#主/子`）取该簇首条——全新主题
         照字面常常搜不到，但同簇里必有语义近邻，这正是人工处置时的做法。
      ④ 以上都取不到才阻塞，并在文案里写明出路。

    **不拿「最近更新的卡」兜底**：那种锚与本卡无关，交回来的邻居也是无关卡的，
    等于把「没做过复盘」记成「复盘过了」——凭证会撒谎，比阻塞更坏。
    """
    anchor, anchor_src = anchor_id, "调用方提供"
    kws = keywords_of_concept(sig["concept"], limit=2)
    tried = []
    if not anchor:
        for kw in kws:
            res = client.tool("memo_search", {"keywords": kw, "limit": 1})
            memos = _result_memos(res)
            tried.append(f"关键词「{kw}」{len(memos)} 条")
            if memos:
                anchor = memos[0].get("id")
                anchor_src = f"关键词「{kw}」命中首条"
                break
        notes.append("复盘·取锚检索：" + "、".join(tried or ["无可派生关键词"]))
    leaves = tag_leaves(sig["tagline"])
    if not anchor:
        for top, leaf in leaves:
            path = f"{top}/{leaf}"
            res = client.tool("memo_search", {"tag": path, "limit": 1})
            memos = _result_memos(res)
            notes.append(f"复盘·取锚退到标签簇「{path}」：{len(memos)} 条")
            if memos:
                anchor = memos[0].get("id")
                anchor_src = f"标签簇「{path}」首条（关键词无命中）"
                break
    if not anchor:
        where = "、".join(f"`{t}/{lf}`" for t, lf in leaves) or "（首行无合法标签）"
        blockers.append(
            "复盘第 ⑧ 步无法取锚：未给 `--anchor-id`，关键词检索无命中，"
            f"本卡标签簇 {where} 里也没有任何卡。"
            "确属全新主题时用 `--anchor-id` 指定最接近的卡（可跨标签）；"
            "否则先确认首行标签是否写错——标签写错会让查重与复盘两路同时空转"
        )
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
        if not t.get("search_url"):
            blockers.append(f"术语「{t.get('term')}」缺 search_url——必须提供真实搜索来源URL，禁止凭空写conclusion")
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
                  expires_delta=GATE_TTL, now=None, tag_check=None):
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
        # ④ 标签存在性核对：每个标签「在树里 / 将新建」逐条留痕。写云侧据此
        # 确认这一步真的跑过（否则「标签不存在」与「簇为空」在凭证里无从分辨）。
        "tag_check": tag_check if tag_check is not None else {"tags": [], "checked": 0},
        "dedup": dedup,
        "review": review,
        # 机器盲区随凭证留痕，供事后审计看清闸门的边界（不参与放行判定）。
        "human_judgment": list(_HUMAN_ITEMS),
    }


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--memo", required=True, help="卡片正文文件路径")
    ap.add_argument("--verify", help="第 2 步网络搜索验证记录 JSON 路径")
    ap.add_argument("--skip-web", action="store_true", help="显式声明非术语卡，无需网络验证（必须配合 --skip-reason）")
    ap.add_argument("--skip-reason", default="", help="跳过网络验证的理由（--skip-web 必填）")
    ap.add_argument("--out", help="凭证输出路径（默认 .sop_gate/<signature>.json）")
    ap.add_argument("--anchor-id", help="可选：本卡 id（更新场景下可作 memo_recommended 锚点）")
    ap.add_argument(
        "--allow-near-concept",
        metavar="REASON",
        default="",
        help="概念名疑似同名（一方是另一方子串，如「长征十号」/「长征十号乙」）时，"
             "确认是不同对象后写明理由放行；理由随凭证留痕。",
    )
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
        if not args.skip_reason.strip():
            blockers.append("--skip-web 必须配合 --skip-reason 写明跳过理由；产品名/技术名/机构名/政策名一律不得跳过搜索")
        else:
            web = {"web_skipped": True, "skip_reason": args.skip_reason}
            notes.append(f"验证·调用方显式声明无需网络验证（--skip-web，理由：{args.skip_reason}）")
    else:
        web = _check_web(args.verify, content, blockers, notes)

    token, _src = load_token()
    client = FlomoClient(token)
    client.init()

    # 标签树只采一次：④ 存在性核对与 ⑤ 标签路查重用的是同一份树，各采一次既
    # 多一次往返，也可能两次之间标签被改名而让两段结论互相矛盾。
    total, tree = _tag_tree_total_and_count(client)
    got = len(_tag_names(tree))
    if total is None:
        notes.append("标签树·未取到（第 ④ 步与查重标签路都将无从核对）")
    else:
        notes.append(f"标签树·现采 {got} 个（total {total}）")
        if got < total:
            notes.append(
                f"标签树·被截断（{got} / {total}）"
                "——「标签不存在」的结论可能不准，但错字类阻塞不受影响"
            )
    tag_check = _check_tags(tree, sig["tagline"], blockers, notes)  # ④
    dedup = _check_dedup(client, sig, blockers, notes, tree=tree, anchor_id=args.anchor_id,
                         allow_near=args.allow_near_concept)  # ⑤
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
    gate = assemble_gate(content, sig, web, dedup, review, sig_key=sig_key,
                         tag_check=tag_check)
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
    sys.exit(cli_entry(main, errors=(FlomoError,), code=2, prefix="[失败] 云端调用出错："))
