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
  python sop_gate.py --memo <正文路径> --verify <JSON> --source-url <素材URL>  # 核对 search_url 不是拿原稿冒充检索
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
       "in_body": ["WLTP 续航 430 公里"]},
      {"term": "某合金相", "verdict": "unverified", "query": "...", "search_url": "...",
       "tried": ["中文名 + 相变温度", "英文名 + phase transition", "DOI 反查"],
       "conclusion": "两轮检索无独立出处，按材料口径保留，不据此删减"}
    ]
  }
  说明：`--skip-web` 仅当卡片主体不是专业术语/机构/产品/模型/定理简称时可用，
  且会在凭证中留痕 `web_skipped=true`，便于事后审计。

**`verdict` 三态（H29）**：缺省 `confirmed`。`refuted` 表示找到相反证据、可据以
改删卡内内容，**必须**同时给 `counter_evidence`（相反原文与链接）——只写 `refuted`
不给反证即阻塞；`unverified` 表示无结论，不要求正文落点，但 `tried` 须列至少两条
不同查询，正文按材料口径保留。检索无果既不是反证，也不是删除依据。

**结论必须落进正文（本闸门的新增阻塞项）**：留痕只证明「搜索做了」，不证明
「搜到的东西写进卡了」——只落一份验证记录、正文却照抄原文，同样能骗过旧版闸门。
故本脚本逐条核对落点，判定顺序：

  1. `in_body`：调用方显式声明的正文落点片段。按**语义一致**判定，不要求逐字相同：
     标点、空白、语序、引号与括号写法不同都算同一段话；但片段里的数字与拉丁专名
     必须原样出现在正文（数值被改就是另一句事实，不能算落点）。片段不得等于术语
     本身（否则是空转声明）；语义对不上即阻塞，并给出最接近的比例供修卡参考。
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
from urllib.parse import urlsplit

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
from tag_index import collect, leaf_names_of, tops_of

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

# 验证结论的三态（H29）。缺省按 confirmed 处理，保持旧留痕可用。
# 存在这三档的理由：检索是抽样——受查询词、索引、语言、付费墙限制，「没搜到」
# 与「事实不成立」之间隔着整整一类情况。旧留痕只有「搜到并写了」一种形态，
# 于是执行者在搜不到时只有两条路：写一个自己不认的 conclusion，或者把卡里
# 那段内容删到「与检索结果一致」。后者看着最干净，代价是卡片开始以检索能力
# 为事实边界。故把「无结论」显式成一档：它不改内容，但要交代试过什么。
_VERDICTS = ("confirmed", "refuted", "unverified")

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


# 语义比对还要抹掉标点、引号、括号与连接符：同一句话在正文里写成
# 「基于最新 LES（前瞻专家调度）推理加速技术」，在验证记录里写成
# 「基于 LES 前瞻专家调度加速技术」，说的是同一件事，逐字判会把后者拦下，
 # 逼执行者把声明改写成正文的复制品——那一层比对就退化成「你有没有抄一遍」。
_SEMANTIC_STRIP = re.compile(
    r"[\s,，。、；;：:！!？?（）()\[\]【】「」『』“”\"'‘’—\-–~～…·/／|]+"
)

# 语义一致的最低「同字覆盖率」：声明里有多少字能在正文找到连续同字片段。
# 只调换虚词、合并分句、改口语化说法也不影响它是同一句话；把「94.5%」改成
# 「94%」这类事实改动由数字与拉丁专名比对单独拦下，不靠这一层。取 0.5 是因为
# 同一事实常被改写成短句（「计算平台采用…芯片，算力达 …」→「平台 …，算力 …」），
# 覆盖率再抬高就把正常改写重新逼回逐字抄写。
_SEMANTIC_COVERAGE = 0.5

# 最长连续同字段落的下限（字）。覆盖率可能由若干短片段凑够，全是「的」「与」
# 这类碎片时不能算同一句话，故要求至少有一段是实打实的片段；取 4 是与
# 「430公里」这类参数短语对齐的量级。
_SEMANTIC_LONGEST = 4


def _core(text):
    """语义比对用核心串：去空白、标点、引号括号，统一小写。"""
    return _SEMANTIC_STRIP.sub("", text or "").lower()


def _coverage(core_a, core_b):
    """core_a 的字有多少落在 core_b 的连续同字片段里，返回 (覆盖率, 最长片段长)。

    用最长公共片段切分：连续同字优先，片段之间的差异（增删虚词、改语序、
    换连接词）不计入覆盖率——这正是「语义一致」与「逐字相同」的差别所在。
    """
    if not core_a or not core_b:
        return 0.0, 0
    sm = difflib.SequenceMatcher(None, core_a, core_b, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    covered = sum(b.size for b in blocks)
    longest = max((b.size for b in blocks), default=0)
    return covered / len(core_a), longest


def _anchor_lands(anchor, body):
    """`in_body` 声明与正文是否语义一致。

    逐字包含只是其中一种情形；标点、语序、括号写法不同不算不一致。数字与拉丁
    专名仍按归一化后逐字比对——它们是事实本体，换了就不是同一句。
    """
    core_a, core_b = _core(anchor), _core(body)
    if not core_a:
        return False, 1.0
    if core_a in core_b:
        return True, 1.0
    digits = re.findall(r"\d+(?:\.\d+)?", anchor or "")
    if any(d not in core_b for d in digits):
        return False, 0.0
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*", anchor or "")
    if any(w.lower() not in core_b for w in latin):
        return False, 0.0
    coverage, longest = _coverage(core_a, core_b)
    return (coverage >= _SEMANTIC_COVERAGE and longest >= _SEMANTIC_LONGEST), coverage


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
                continue
            landed, coverage = _anchor_lands(a, body)
            if not landed:
                ok = False
                hint = (
                    "数字或拉丁专名与正文不符"
                    if coverage <= 0.0
                    else f"与正文连续同字的部分仅覆盖 {coverage:.0%}"
                )
                blockers.append(
                    f"验证术语「{term}」声明的 in_body 落点「{a}」与卡正文语义不一致"
                    f"（{hint}）——落点须与正文里承载该结论的那句话讲同一件事"
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


def _check_tags(tags, tagline, blockers, notes, *, index=None):
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
    if not names and (index is None or index.problems()):
        # 空集合有两种来源，结论相反：**取不到**（无从核对，必须阻塞）与**确实没有**
        # （`prefix` 查询返回空 = 这个顶层在库里不存在，本卡会连顶层一起新建）。
        # 后者是合法动作，报「标签树未取到」会让人以为工具坏了、跑去重采。
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
    """现采 tag_tree，返回 (total, 标签列表)。

    `limit` 传 2000 会被服务端夹到 1000（该参数有效，不传默认只有 200），
    而 `total` 报真实值（1025），所以**靠调大 limit 取全是否定的**，
    是否截断由调用方按 `len(tags) != total` 判，并据此逐名现查补查（见 `_check_dedup`）。
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


def _tag_leaf_names(client, keyword, limit=50):
    """按标签名现查（`tag_search` 子串匹配），返回命中的标签名列表。

    截断补查专用：树缺尾部标签时，用它把本卡要用的那个簇（含父与子）补回来。
    服务端单次上限 50 条，只够补一个簇，不能当全量枚举用。
    """
    sc = tool_data(client.tool("tag_search", {"keywords": keyword, "limit": limit}))
    out = []
    for t in (sc.get("tags") or []):
        name = t.get("name") if isinstance(t, dict) else str(t)
        if name:
            out.append(name)
    return out


# 概念名扫描的翻页上限与页长。服务端 `memo_search` 单次上限 50，且不认 offset，
# 只能按 `end_date` 日期游标往回翻。10 页 = 单簇最多 500 张；取不尽会记 `truncated`，
# 由闸门在 notes 里明说「只扫到这些」，不把「没扫完」写成「没有重复」。
SWEEP_PAGE_LIMIT = 50
SWEEP_MAX_PAGES = 10
# 包含关系视为「疑似同名」时，多出来的部分最长多少字。事件卡常把主体写进概念名
# （「某甲批评 Anthropic：…」），若只看包含关系，任何关于该主体的新卡都会被拦。
# 只认「对象名 + 短后缀」（「Anthropic」/「Anthropic S-1」）这一种形态。
NEAR_EXTRA_MAX = 6


RETIRE_TAG_TEXT = "#待清理"


def _find_same_concept_twin(client, concept, anchor_id):
    """找云端除本卡外、逐字同名的另一张卡；命中返回 "id「概念名」"，否则 None。

    更新场景原本因「本卡即候选」跳过同名判定，于是两张同标题卡各更新一次都能过闸，
    并存状态长期留着。这里只补一条：同名且**仍带正文**的另一张存在就不许写云。
    已挂 #待清理 的骨架不算并存（那是退场后的删除入口，正文按设计已清空）。
    """
    try:
        memos, _trunc = _sweep_memos(client, {"keywords": concept}, max_pages=2)
    except FlomoError:
        return None
    cur = _concept_identity(concept)
    for m in memos:
        if m.get("id") == anchor_id:
            continue
        sig = memo_signature(m.get("content"))
        if not sig:
            continue
        if (sig["tagline"] or "").strip() == RETIRE_TAG_TEXT:
            continue
        if _concept_relation(cur, sig["concept"]) == "same":
            return f"{m.get('id')}「{sig['concept']}」"
    return None


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
    near：一方是另一方的前缀或后缀、短名 ≥4 字，且多出来的部分 ≤ `NEAR_EXTRA_MAX`
          （「Anthropic」/「Anthropic S-1」、`长征十号`/`长征十号乙`）。
          这既可能是同一对象的切片卡，也可能确是两个对象，机器判不了，故同样
          **默认阻塞**，须 `--allow-near-concept` 写明理由放行。多出来的部分更长时
          （事件卡「某甲批评 Anthropic：…」）不算 near——那不是对象名，是句子。
          只认首尾相接，是因为「短名出现在长名中间」多为缩写碰撞而非同一对象的
          切片：`iBOT` 落在 `CASIVIBOT` 末端，二者是不同系统，不能据此阻塞。
          纯拉丁字母的名字再加一道：若加长的部分也全是字母（如 `CASIVIBOT` 相对
          `iBOT` 多出的 `CASV`），按不同术语放行；只有带数字、连字符等版本标记时才
          继续判 near（`Anthropic`/`Anthropic S-1`、`LLAMA`/`LLAMA-2`）。
    """
    a1, a2 = cur
    b1, b2 = _concept_identity(other)
    if a1 and a1 == b1:
        return "same"
    if a2 and a2 == b2:
        return "same"
    for x, y in ((a1, b1), (b1, a1), (a2, b2), (b2, a2)):
        # 两个条件必须同时成立才算 near：前者判「短名与长名首尾相接」（对象名被加长），
        # 后者判加长的部分够短（`某甲批评 Anthropic：…` 是句子，不算同一个对象）。
        if not (x and y and x != y and len(x) >= 4):
            continue
        if y.startswith(x):
            extra = y[len(x):]
        elif y.endswith(x):
            extra = y[:len(y) - len(x)]
        else:
            continue
        if len(extra) > NEAR_EXTRA_MAX:
            continue
        # 纯拉丁字母、且增量也全是字母：视为两个不同缩写/术语，不判 near。
        if x.isascii() and x.isalpha() and y.isascii() and y.isalpha() \
                and extra.isascii() and extra.isalpha():
            continue
        return "near"
    return ""


def _check_dedup(client, sig, blockers, notes, tree=None, anchor_id=None,
                 allow_near="", body=""):
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
    truncated = False
    if tree is None:
        _total, tree = _tag_tree_total_and_count(client)
        truncated = len(_tag_names(tree)) != _total
    names = set(_tag_names(tree))
    leaves = tag_leaves(sig["tagline"])
    # 服务端封顶 1000 条、`total` 报真实值，缺的是排序尾部。尾部簇不在树里时，
    # 「该标签不在树中，本卡将新建簇」会变成误报，这一路的近邻也会漏成 0。
    # 截断后对每个待用标签逐名现查补回（单次上限 50，够补一个簇的父与子）；
    # 补查仍落空的单列出来，改说「无法确认」，绝不写「将新建簇」。
    unresolved: set[str] = set()
    if truncated:
        for top, leaf in leaves:
            target = f"{top}/{leaf}"
            if target in names or any(n.startswith(target + "/") for n in names):
                continue
            filled = _tag_leaf_names(client, target)
            if filled:
                names.update(filled)
            else:
                unresolved.add(target)
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
        target = f"{top}/{leaf}"
        if target in names:
            absent = ""
        elif target in unresolved:
            absent = ("（标签树被服务端封顶、逐名现查也未命中：无法确认，"
                      "不要据此当作新簇）")
        else:
            absent = "（该标签不在树中，本卡将新建簇）"
        notes.append(f"查重·标签路「{target}」近邻 {hit} 个簇{absent}")
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
        if not am or not (am[0].get("content") or "").strip():
            blockers.append(
                f"更新既有卡 {anchor_id} 前必须取到该卡全文，本次 memo_batch_get "
                "没取到正文——未读全文不得更新（H1/H22）")
        elif am[0].get("content_truncated"):
            blockers.append(
                f"更新既有卡 {anchor_id} 前必须取全文，回包仍是截断快照——"
                "不得据截断内容覆盖（H1）")
        old_sig = memo_signature(am[0].get("content")) if am else None
        old_concept = old_sig["concept"] if old_sig else ""
        if old_concept and _concept_relation(_concept_identity(concept), old_concept) == "same":
            enforce = False
            notes.append("查重·更新场景且概念名未改，跳过同名判定（本卡即候选）")
            # 但「跳过」只能跳本卡自身：云端若还并存另一张逐字同名的卡，
            # 两张都带正文就属重复并存，更新时也必须拦下（双卡同名是事故形态）。
            twin = _find_same_concept_twin(client, concept, anchor_id)
            if twin:
                blockers.append(
                    f"同名双卡并存：云端除本卡外还有逐字同名卡 {twin}。"
                    "两张同名卡不得继续并存——须把内容并进较全的一张，"
                    "另一张用 retire.py 退场后再写云（H9/H23）。")
        elif not old_concept:
            notes.append("查重·更新场景但读不到原概念名，仍执行同名判定")

    sweep_rec = {"tag_clusters": [], "queries": [], "scanned": 0,
                 "truncated": [], "same_concepts": [], "near_concepts": [],
                 }
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

        # 同名 / 近名只是线索，判是不是同一对象必须读全文：这里把候选卡正文
        # 全部打印出来，供逐张对照；不做字面相似度打分（公式、定义行天然重合，
        # 机械打分会把不同概念的卡误判成重复）。
        _ids_needing_read = [r["id"] for r in
                             sweep_rec["same_concepts"] + sweep_rec["near_concepts"]]
        if _ids_needing_read:
            print("\n═══ 同名/近名候选全文（逐张对照后再决定新建或更新）═══")
            for _i in range(0, len(_ids_needing_read), 10):
                _chunk = _ids_needing_read[_i:_i + 10]
                _memos = _result_memos(client.tool("memo_batch_get", {"ids": _chunk}))
                for _m in _memos:
                    print(f"── {_m.get('id')} | {_m.get('word_count')} 字")
                    print((_m.get("content") or "").strip())
            notes.append("查重·同名/近名候选已逐张取全文，是否同一对象按正文判断")

        if sweep_rec["same_concepts"]:
            # 同名只提示，不阻塞：改本稿标题、改云端那张卡的标题、两张都改，
            # 都是合法走法，检测层不该替人做选择。
            detail = "；".join(
                f"{r['id']}「{r['concept']}」（{r['tagline']}）"
                for r in sweep_rec["same_concepts"][:5])
            blockers.append(
                f"完全同标题严格禁止：云端已有逐字同名的卡 {detail}。"
                "本稿必须改名后才能新建；确属同一对象就 --update-id 更新该卡，"
                "更新前先取全文核对（H1/H22）。")
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


# 第 2 步「真搜索」的证据只能是**外部检索**：搜索引擎的查询地址，或第三方来源页。
# 素材原稿（本轮抓下来的那篇文章）不算——把素材 URL 抄进 search_url，凭证看起来
# 齐全、实际一次检索都没做，正文仍是照抄原文。这是闸门必须机械区分的东西。
_SEARCH_ENGINE_HOSTS = (
    "baidu.com", "sogou.com", "bing.com", "google.com", "duckduckgo.com",
    "so.com", "sm.cn", "yandex.com", "zhihu.com", "cnki.net", "arxiv.org",
)


def _host(url: str) -> str:
    """取 URL 主机名（去 scheme/路径/端口，去掉 www. 前缀）。解析失败返回空串。"""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _is_engine(host: str) -> bool:
    """主机名是否属于检索入口（含子域名，如 www.baidu.com / cn.bing.com）。"""
    h = host[4:] if host.startswith("www.") else host
    return any(h == e or h.endswith("." + e) for e in _SEARCH_ENGINE_HOSTS)


def _same_host(a: str, b: str) -> bool:
    """两个主机名是否同源（忽略 www. 前缀差异）。"""
    if not (a and b):
        return False
    a = a[4:] if a.startswith("www.") else a
    b = b[4:] if b.startswith("www.") else b
    return a == b


def _open_round_url() -> str:
    """从账本取「最近一条已抓取未关账」轮的 url，作为本轮素材地址。

    取不到就当没有：闸门不该因为读不到账本而阻塞（那会把「缺参数」变成
    「卡有问题」），但会在 notes 里如实说明无法核对同源。
    """
    try:
        data = json.loads(Path(".workbuddy/rounds.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    rounds = data.get("rounds") if isinstance(data, dict) else data
    if not isinstance(rounds, list):
        return ""
    for r in reversed(rounds):
        if isinstance(r, dict) and r.get("url") and not r.get("closed_at"):
            return str(r["url"])
    return ""


def _check_web(verify_path, body, blockers, notes, source_url=None):
    """② 验证：读调用方的网络搜索留痕，校验术语覆盖**且结论已落进卡正文**。

    三件事都做才叫完成：留痕证明「搜索做了」（且证据来自外部检索而非素材本身）、
    落点校验证明「搜到的写进卡了」（见 `_check_term_landed`）、
    素材同源拦截证明「没有拿原稿 URL 冒充检索」。
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
        term = t.get("term")
        if not (term and t.get("conclusion")):
            blockers.append(f"验证记录条目缺 term/conclusion：{t}")
            continue
        verdict = (t.get("verdict") or "confirmed").strip()
        if verdict not in _VERDICTS:
            blockers.append(
                f"术语「{term}」的 verdict 为 {verdict!r}——只认 "
                f"{' / '.join(_VERDICTS)}（H29）"
            )
            continue
        if not t.get("search_url"):
            blockers.append(f"术语「{term}」缺 search_url——必须提供真实搜索来源URL，禁止凭空写conclusion")
            continue
        # 素材本站 URL 不算检索证据：抓原稿是第 1 步，第 2 步要的是「另外查过一遍」。
        # 这一步只拦同源，不拦第三方媒体——用别的站点佐证同样算真检索。
        src_host = _host(str(source_url or ""))
        term_host = _host(str(t["search_url"]))
        if _same_host(term_host, src_host):
            blockers.append(
                f"术语「{term}」的 search_url 指向素材本站（{term_host}）"
                "——抓原稿不是检索留痕；须给出搜索引擎查询地址或第三方来源页"
            )
            continue
        if verdict == "refuted":
            # 改删卡内内容只能凭反证。这是本条最容易被滥用的方向：把「搜不到」
            # 写成 refuted，等于给删除盖一个看起来合规的章，事后无法分辨。
            if not str(t.get("counter_evidence") or "").strip():
                blockers.append(
                    f"术语「{term}」标 refuted 却没给 counter_evidence（相反原文与链接）"
                    "——检索无果不是反证，按 H29 不得据此改删"
                )
                continue
            rec = _check_term_landed(t, body, blockers)
            rec["verdict"] = "refuted"
            landed.append(rec)
            continue
        if verdict == "unverified":
            tried = [str(x).strip() for x in (t.get("tried") or []) if str(x).strip()]
            if len(set(tried)) < 2:
                blockers.append(
                    f"术语「{term}」标 unverified，tried 须列至少 2 条不同查询"
                    "（换词 / 换引擎 / 换语言 / 搜 DOI）——搜一次没中不等于无结论"
                )
                continue
            # 无结论不要求正文落点（搜不到自然没得落），但也不是豁免通道：
            # 正文按材料口径保留，试过什么进凭证，事后可核。
            landed.append(_landed_record(term, True, verdict="unverified",
                                         tried=sorted(set(tried))))
            continue
        landed.append(_check_term_landed(t, body, blockers))
    hosts = sorted({_host(str(t.get("search_url") or "")) for t in terms} - {""})
    engine_n = sum(1 for h in hosts if _is_engine(h))
    if source_url:
        notes.append(
            f"验证·检索来源 {len(hosts)} 个域（引擎 {engine_n} 个），"
            f"已逐个排除与素材 {_host(str(source_url))} 同源"
        )
    # 计数只看分支自己申报的 landed（见 `_landed_record`），不按键名猜——
    # 否则新增分支一改键名就静默漏计，汇总行会与凭证内容相反。
    unver = [r["term"] for r in landed if r.get("verdict") == "unverified"]
    # 只计「反证成立且已落进正文」的那几条：挂了 refuted 却没落点的，已经
    # 进 missed，再计一次会让「凭反证改删 N 条」虚高，审计时无法对账。
    refuted = [r["term"] for r in landed
               if r.get("verdict") == "refuted" and r.get("landed")]
    hit = sum(1 for r in landed
              if r.get("landed") and r.get("verdict") != "unverified")
    missed = [r["term"] for r in landed if not r.get("landed")]
    line = f"验证·已核实 {len(terms)} 个术语，其中 {hit} 条在正文有落点或已豁免"
    if unver:
        line += f"；无结论 {len(unver)} 条按材料口径保留（{'、'.join(unver[:3])}）"
    if refuted:
        line += f"；凭反证改删 {len(refuted)} 条"
    if missed:
        line += f"；未落点 {len(missed)} 条（{'、'.join(missed[:3])}）"
    notes.append(line)
    return {"searched": data.get("searched"), "term_count": len(terms),
            "landed": landed, "unverified": len(unver), "refuted": len(refuted)}


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
    ap.add_argument("--source-url", default="",
                    help="本轮素材（原稿）URL：用于核实 search_url 不是拿原稿冒充检索留痕；"
                         "缺省时从 .workbuddy/rounds.json 最近未关账轮取。")
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
        src_url = args.source_url.strip() or _open_round_url()
        web = _check_web(args.verify, content, blockers, notes, source_url=src_url)

    token, _src = load_token()
    client = FlomoClient(token)
    client.init()

    # 标签按顶层现采，不拉全量：判定路只需要本卡涉及的那几个簇，而全库封顶 1000 条
    # （实测 total 1025），一拉就缺尾部——「该标签不在树中」会被误报成「库里有没这簇」，
    # 近邻也会漏成 0。改成逐顶层 prefix 取后，每簇都是完整数据，两个结论才立得住。
    # 仍只采一次：④ 存在性核对与 ⑤ 标签路查重用同一份，各采一次会让两段结论互相矛盾。
    wanted = [f"{top}/{leaf}" for top, leaf in tag_leaves(sig["tagline"])] or \
        re.findall(r"#([^\s#]+)", sig["tagline"])
    index = collect(client, tops_of(wanted), leaf_names_of(wanted))
    tree = index.names()
    notes.append(
        f"标签树·按顶层现采 {len(index.slices)} 个（{'、'.join(index.slices) or '—'}）、"
        f"{len(tree)} 个标签"
    )
    for problem in index.problems():
        notes.append(f"标签树·{problem}")
    tag_check = _check_tags(tree, sig["tagline"], blockers, notes, index=index)  # ④
    dedup = _check_dedup(client, sig, blockers, notes, tree=tree, anchor_id=args.anchor_id,
                         allow_near=args.allow_near_concept, body=content)  # ⑤
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
