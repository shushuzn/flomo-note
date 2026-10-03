#!/usr/bin/env python3
"""memo_util.py — 卡片签名的**唯一实现**（供 flomo_client / sop_gate / validate_memo 共用）。

为什么要有这个模块：
卡片"签名"（首行标签行 + 第二行概念名行）是**幂等查重、闸门凭证、写云前校验**三处的
共同判定依据。此前三处各自实现一份，返回类型还不一致（有的 tuple、有的 dict），
任何一处口径漂移（比如改取行规则）都会导致"查重通过但凭证对不上"这类幽灵故障。
现收敛到本模块单一实现，三处一律 import 使用。

签名口径：取正文的**前两个非空行**。
- 首行应为标签段（以 `#` 开头）——不满足则返回 None（视为非卡片/格式异常）。
- 第二非空行应为概念名（非空，且不以 `#` 开头）。
  "前两个非空行"而非"前两行"，是为了兼容 H14 的两种写法：
  标签段后直接跟概念名，或标签段与概念名之间夹一个空行（flomo 存储层会自动插空行）。
"""

from __future__ import annotations

import hashlib
import re


def memo_signature(content):
    """返回 {"tagline","concept"} 或 None。

    content 为空 / None / 非字符串时返回 None，绝不抛异常
    （调用方可能是缺失 content 的请求体或已清空的卡片）。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    tagline, concept = lines[0], lines[1]
    if not tagline.startswith("#") or not concept or concept.startswith("#"):
        return None
    return {"tagline": tagline, "concept": concept}


def signature_key(sig):
    """签名 → 稳定短键（用于凭证文件名等）。sig 为 None 时返回 None。"""
    if not sig:
        return None
    raw = (sig["tagline"] + "\n" + sig["concept"]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def body_hash(content):
    """正文整体指纹（用于凭证，防"先跑闸门、后改正文"）。

    非字符串入参（None / 数字等）加类型前缀，使 `None` 与 `""` **不产生同指纹**——
    否则"取不到正文"与"正文恰好为空"在凭证层无法区分，会掩盖调用侧的空值缺陷。
    """
    if not isinstance(content, str):
        return hashlib.sha256(f"<non-str:{type(content).__name__}>".encode("utf-8")).hexdigest()
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def keywords_of_concept(concept, limit=2):
    """从**概念名本身**派生检索用短关键词（不经卡片结构解析）。

    背景：整行概念名含全角标点时 flomo 全文检索常返回 0，必须退到短核心词。
    产出（去重保序，截断到 limit 个）：
      1. 概念名内最长的连续中文段（≥4 字）
      2. 概念名去除标点后的前 8 个字符

    调用方常只握有概念名（如闸门拿到的是 `sig["concept"]`），故本函数直接
    接受概念名；`concept_keywords` 只负责从完整卡片里取概念名再转发。
    """
    if not isinstance(concept, str) or not concept.strip():
        return []
    kws = []
    zh = re.findall(r"[\u4e00-\u9fff]{4,}", concept)
    if zh:
        kws.append(max(zh, key=len))
    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", concept)
    if clean:
        kws.append(clean[:8])
    out = []
    for k in kws:
        if k and k not in out:
            out.append(k)
    return out[:limit]


def concept_keywords(content, limit=2):
    """从卡片正文派生检索用短关键词（先取签名，再委托 keywords_of_concept）。

    content 不是合法卡片（签名取不到）时返回 []。
    """
    sig = memo_signature(content)
    if not sig:
        return []
    return keywords_of_concept(sig["concept"], limit=limit)


# ── 概念名的「切片限定」识别（H6b：概念名须标识对象本身，不标识对象的某个切片）──
# **单一来源**：质检拦截（validate_memo 5.8）与存量取证（scan_qualified_concepts）
# 都调用本实现。两处各写一套正则时，改一侧会让另一侧静默失配——而失配的表征
# 只是「统计数字对不上」，很难被察觉，正是「计数不许靠猜键名」那条纪律的同类风险。
#
# 判据不看单词本身，看**是否可剥离的限定结构**：切片能独立剥离出去，对象名不能。
# 故阶段词只拦「阶段词+数字」，裸阶段词放行（`Step 5 Preview大模型` 的 Preview 是
# 型号的一部分，`某模型 Preview 3` 才是切片）。
SLICE_PATTERNS = [
    # —— 时间切片 ——
    ("年份", r"(?<!\d)(?:19|20)\d{2}(?:\s*[-/年]\s*(?:\d{1,2}(?:\s*[-/月]\s*\d{1,2})?)?)?"),
    # 季度：三条形态，均为期间切片。
    # ① 紧贴年份/财年（`2026Q2`、`2027 财年 Q2`）——最明确的一类。
    # ② 裸Q 且**前有边界**（`比特币 Q3 市场格局`、句首 `Q1 财报`）——Q 是期间标记。
    # ③ 裸 Q 但**紧贴中文/字母**（`旺旺Q1利润暴跌`）——Q 是产品代号的一部分，放行；
    #    连字符形态（`IQuest-Q1`）由②的前瞻一并放行。
    # 早期只写 `(?<![A-Za-z0-9])[Qq][1-4]`，把产品代号 IQuest-Q1、旺旺Q1 一并误拦。
    ("季度", r"(?:19|20)\d{2}\s*(?:财年)?\s*[Qq]\s?[1-4](?![0-9])"
              r"|(?<![\u4e00-\u9fffA-Za-z0-9-])[Qq]\s?[1-4](?![\u4e00-\u9fffA-Za-z0-9-])"),
    ("相对时间词", r"最新|近期|当前|如今|今日|本日|本周|本月|今年|去年|目前|现阶段"),
    # 时间形态词：与「年份」平级的同一种切片，只是没写成数字（`某公司年度财报`）。
    # 不拦 `Q1`/`Q2` 单独出现——那是产品代号（IQuest-Q1、旺旺Q1），不是期间切片；
    # 带年份的季度（2026Q2）已由「年份」条命中。
    # 前置限定词要求「该/本/上/下」等指示词，避免拦下普通名词组合（`上半年行情`）。
    ("时间形态词", r"年度|半年度|财年|(?:财|技术|产品|业务|市场)(?:季度|月)|"
                    r"(?:本|该|上|下|首|末)季度|(?:上半|下半)年(?!行情)"),
    # —— 版本/ 阶段切片 ——
    ("语义化版本号", r"(?<![A-Za-z0-9])[vV]\s?\d+(?:\.\d+)+"),
    ("版本后缀", r"\d+(?:\.\d+)+\s*(?:版|版本)"),
    ("阶段号", r"(?<![A-Za-z0-9])(?:RC|rc|Beta|beta|alpha|Alpha|preview|Preview)\s*\d+(?![0-9])"),
    # —— 代次/轮次/批次切片 ——
    ("代次/轮次", r"第\s*[0-9一二三四五六七八九十]+\s*(?:代|版|阶段|期|轮|届|批)"),
    # —— 地域/语种切片（只拦版本后缀形态，不拦地名本身）——
    ("地域/语种版本", r"(?<![A-Za-z0-9])(?:中文版|英文版|国内版|海外版|欧洲版|亚洲版|美版|日版|韩版)(?![A-Za-z0-9])"),
    # —— 谓语动作（**全位置**检测）——
    #
    # 早期版本只匹配结尾（`...发布$`），漏了两类真实违规：
    #   ① 词表外的动作（`蔚来ET9 上市`、`某公司挂牌`）；
    #   ② 位于名字中间的谓语（`Anthropic 为 Claude Code 新增命令`）。
    # 实测全库：结尾匹配命中 409 张，另有 157 张含动词却漏检。
    # 谓语出现在**任何位置**都违规，故不再按位置收窄。
    #
    # 关键：**谓语 vs 构词**。`开源模型`/`发布会`/`上线率` 里动词是**构词成分**
    # （组成一个新名词，对象仍是那个东西）；`GPT-6 发布` 里动词是**谓语**
    # （动作的对象不是名字本身标识的对象）。判据不能只看有没有这个动词——
    # 先前实测把构词一并拦掉（发布会/开源模型/获奖情况全被误判），误拦比漏检更危险。
    # 判法见`_predicate_hits`——谓语 vs 构词靠结构判定，不靠枚举后接词。
]
_SLICE_RE = [(lbl, re.compile(pat)) for lbl, pat in SLICE_PATTERNS]


# ── 谓语动作检测（H6b：概念名是名词短语，不含「谁做了什么」）──────────────
#
# 为什么单列：H6b 的两类错因里，「含谓语」与「含切片」判据完全不同。
# 切片靠模式表就能穷举；谓语不行——动词是**开放词类**，且同一个动词在概念名里
# 可能是谓语（违规）、也可能是构词成分（合规）：
#   `GPT-6 发布`  谓语 → 标识「一次发布事件」，违规
#   `发布会`构词 → 标识「发布会」这个场合，合规
# 所以不能「见动词就拦」。
#
# 判据用**语法位置**而非词表枚举。三条按优先级：
#   ① 动词前为空、或只有抽象修饰语（`增量`、`模型`…）→ **构词**，放行。
#      这条语法依据可靠：中文「动+名」构词里动词必在成分开头（`发布会`），
#      动词前面一旦出现实义成分，它就从构词成分变成了连接主谓的谓语。
#   ② 动词后接型号数字 / 代词 / 介词短语 / 另一动词 → 谓语，拦。
#   ③ 其余（后接具体事物如 `新增命令`、`发布信号`）→ 谓语，拦。
#
# **为什么不枚举后接词**：早前试过，列了「所有可能后接词」后`发布量`/`上线仪式`/
# `降价潮` 全漏，补上去又漏下一批——枚举法无解。①用语法位置②③④用封闭类
# （宾语标志、构词抽象修饰语），都不是开放词表。
_VERB_RE = re.compile(
    r"发布|上线|推送|推出|开源|释出|举办|召开|夺冠|夺标|获奖|上市|挂牌"
    r"|涨薪|降价|召回|扩建|裁员|关停|中标|签约|获批|核准|试行|印发|启动|启用"
    r"|突破|登顶|刷新|首发|亮相|新增|开放|接入|部署"
)
# 宾语标志：代词、方位、介词短语开头——后面跟的是「动作的对象」而非构词成分
_BA_RE = re.compile(r"^(该|本|此|其|这项|该项目|各|每|某|面向|针对|基于|用于|支持|覆盖|包含|采用)")
# 型号/数字开头：动词后跟的是产品代号（`发布 K2.8`），必是谓语
_OBJ_RE = re.compile(r'^[0-9A-Za-z（("《\[「」]')
# 构词抽象修饰语（**封闭类**）：定语而非主语，动词在它之后仍是构词成分
# （`增量发布`、`模型发布`）。这类是「抽象属性词」，语法上数量有限可穷举，
# 与「所有后接词」那种开放枚举不同。
_MODIFIER = {
    "增量", "模型", "线上", "线下", "全量", "存量", "批量", "定向", "定制", "通用",
    "专用", "私有", "公有", "前端", "后端", "客户端", "服务端", "基础", "核心",
    "高级", "低级", "完整", "简易", "快速", "实时", "离线", "智能", "传统", "现代",
    "一致", "分布式", "云原生", "端到端", "自动化", "可视化", "三维", "二维",
    "多维", "统一", "复合", "新型", "老旧", "默认", "官方", "闭源", "商业",
    "消费级", "企业级", "工业级", "开源", "闭门", "标准", "参考", "示例", "典型",
}


def _predicate_hits(head):
    """概念名的标题部分里是否存在**谓语动作**（H6b 第一类错因）。

    返回命中的动词列表（去重保序）。判据是语法位置而非词表枚举，见上方注释。
    """
    hits = []
    for m in _VERB_RE.finditer(head):
        verb = m.group(0)
        before = head[:m.start()].strip()
        tail = head[m.end():].strip()
        # ① 构词：动词前无成分，或只有抽象修饰语
        if before == "" or before in _MODIFIER:
            continue
        # ② 强谓语标志：后接型号 / 代词介词 / 另一动词
        strong = (
            tail == ""
            or _OBJ_RE.match(tail) is not None
            or _BA_RE.match(tail) is not None
            or _VERB_RE.match(tail) is not None
        )
        # ③ 其余一律判谓语：后接的是具体事物（新增命令 / 发布信号 / 开源框架）
        if strong or tail:
            if verb not in hits:
                hits.append(verb)
    return hits


def concept_head(name):
    """取概念名的**标题部分**：遇「冒号 / 破折号 / 句号」即截断。

    背景：概念名写成长形态时是「标题：摘要」（这本身另由 H14「概念名不得写成整句」
    判定），但切片检测只该看**标题本体**。若扫整行，摘要里的「当前」「近日」等词
    会被误判成概念名的切片——判据作用域错了，会让合规卡因摘要用词而被拦。
    截断后仍按整名识别（标题里的切片照样命中）。
    """
    if not isinstance(name, str):
        return ""
    m = re.search(r"[：:—–]|。", name)
    return name[:m.start()] if m else name


def slice_qualifiers(name):
    """返回概念名里命中的**切片限定 / 谓语动作**标签列表（去重保序）。

    概念名须是**名词短语**：标识对象本身、不含谓语（见 H6b 的两类错因）。
    命中任一即说明它标识的是对象的某个切片，或说的是一件事而非一个东西。
    只在**标题部分**（concept_head）内识别，不扫「标题：摘要」里的摘要内容。
    空/非字符串入参返回 []（不抛异常——调用方可能是缺失 content 的请求体）。
    """
    head = concept_head(name)
    if not head:
        return []
    hits = []
    for lbl, rx in _SLICE_RE:
        m = rx.search(head)
        if m:
            hits.append((lbl, m.group(0)))
    for verb in _predicate_hits(head):
        hits.append(("谓语动作", verb))
    return hits


_TAIL_PUNCT = "，。、；：！？,.;:!?"
# 成对括号：出现在二级名**末尾**时，若左侧有配对的开括号，则该括号属名字本身；
# 若无配对开括号，则是标签串里的收尾括号，须剔除。
_PAIRS = {"）": "（", ")": "(", "】": "【", "》": "《", "」": "「", "』": "『"}


def _strip_tail_punct(name: str) -> str:
    """剔除二级名末尾的**句读标点**与**无配对收尾括号**。

    `#科技/机器人。`      → `科技/机器人`（句号是标点）
    `#科技/机器人（人形）` → 原样保留（右括号有配对左括号，属名字）
    `#科技/机器人）`      → `科技/机器人`（右括号无配对，是收尾符）
    一刀切 rstrip 会把 `（人形）` 剥成残缺的 `（人形`——语法不成立的名字，
    用于近邻比对必然指向不存在的簇。
    """
    s = name
    while s:
        c = s[-1]
        if c in _TAIL_PUNCT:
            s = s[:-1]
            continue
        opener = _PAIRS.get(c)
        if opener is not None:
            # 该右括号在本串内是否有配对左括号？有则属名字，停止；无则剔除。
            if opener in s[:-1]:
                break
            s = s[:-1]
            continue
        break
    return s


def tag_leaves(tagline):
    """取标签行里**合法两级**的 (顶层, 二级) 对。

    如 `#科技/机器人 #AI/物理AI` → [(科技,机器人),(AI,物理AI)]。
    严格两级：二级部分不得再含 `/`，且**必须以空白或行尾收束**——
    否则 `#科技/安全/邮件` 会被误当成合法的 `(科技, 安全)`。
    二级名收尾的句读标点须剔除（`#科技/机器人。` 的句号不是名字的一部分），
    否则近邻比对会指向不存在的簇；成对括号则原样保留（见 `_strip_tail_punct`）。
    """
    if not tagline:
        return []
    raw = re.findall(r"#([^/\s#]+)/([^\s#/]+)(?=\s|$)", tagline)
    out = []
    for top, leaf in raw:
        cleaned = _strip_tail_punct(leaf)
        if cleaned:
            out.append((top, cleaned))
    return out
