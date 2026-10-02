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
    ("季度", r"(?<![A-Za-z0-9])[Qq]\s?[1-4](?![0-9])"),
    ("相对时间词", r"最新|近期|当前|如今|今日|本日|本周|本月|今年|去年|目前|现阶段"),
    # —— 版本/ 阶段切片 ——
    ("语义化版本号", r"(?<![A-Za-z0-9])[vV]\s?\d+(?:\.\d+)+"),
    ("版本后缀", r"\d+(?:\.\d+)+\s*(?:版|版本)"),
    ("阶段号", r"(?<![A-Za-z0-9])(?:RC|rc|Beta|beta|alpha|Alpha|preview|Preview)\s*\d+(?![0-9])"),
    # —— 代次/轮次/批次切片 ——
    ("代次/轮次", r"第\s*[0-9一二三四五六七八九十]+\s*(?:代|版|阶段|期|轮|届|批)"),
    # —— 地域/语种切片（只拦版本后缀形态，不拦地名本身）——
    ("地域/语种版本", r"(?<![A-Za-z0-9])(?:中文版|英文版|国内版|海外版|欧洲版|亚洲版|美版|日版|韩版)(?![A-Za-z0-9])"),
    # —— 事件切片（只拦结尾动词；概念名中间出现「开源模型」不算）——
    ("事件动作", r"(?:发布|上线|推送|推出|开源|释出|举办|召开|夺冠|夺标|获奖)\s*$"),
]
_SLICE_RE = [(lbl, re.compile(pat)) for lbl, pat in SLICE_PATTERNS]


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
    """返回概念名里命中的**切片限定**标签列表（去重保序）。

    概念名须标识对象本身；命中任一切片即说明它标识的是对象的某个切片。
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


# ---- 标签树快照：渲染与计数（唯一实现，供 sop_gate 与 tag_tree_sync 共用）----
#
# 快照格式（见 SKILL「标签树本地留存」）：
#   # total=N        首行，云端标签总数
#   # 顶层名         顶层分组标题行（不计数）
#     顶层/二级      二级行（计数，缩进）
#   投资/            裸顶层（有顶层无二级，计数，不缩进）
#
# 为什么必须同源：快照由同步脚本渲染、由闸门核对。两边各写一份实现时，
# 任何一处口径漂移（例如某处把分组标题行也算作条目）都会造成
# "重写照做、闸门判不自洽"的死锁——写卡流程直接卡死。


def _snapshot_parts(tags):
    """把云端 tags 归一化为 (顶层 → [二级...], 裸顶层集合)。

    tags 元素可能是字符串或 {"name": ...} 字典；空名忽略。
    """
    tops = {}
    bare = set()
    for t in tags or []:
        name = t.get("name") if isinstance(t, dict) else t
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name.endswith("/"):
            top = name[:-1]
            tops.setdefault(top, [])
            bare.add(top)
        elif "/" in name:
            top, leaf = name.split("/", 1)
            tops.setdefault(top, [])
            if leaf:
                tops[top].append(leaf)
        else:
            tops.setdefault(name, [])
            bare.add(name)
    return tops, bare


def render_tag_tree(tags, total):
    """把云端 `structuredContent.tags` 渲染为快照文本（结尾带换行）。

    `count_snapshot(render_tag_tree(...).splitlines())[0] + [1]` 应恒等于
    云端 `total`——不相等说明云端数据自身异常（重名或畸形名），由调用方报错。
    """
    tops, bare = _snapshot_parts(tags)
    lines = [f"# total={total}"]
    for top in sorted(tops):
        lines.append(f"# {top}")
        if top in bare:
            lines.append(f"{top}/")
        for leaf in sorted(set(tops[top])):
            lines.append(f"  {top}/{leaf}")
    return "\n".join(lines) + "\n"


def count_snapshot(lines):
    """统计快照承载的标签条目数，返回 (二级行数, 裸顶层行数)。

    与 `render_tag_tree` 一一对应：缩进行计二级，未缩进且以 `/` 结尾计裸顶层；
    首行 `# total=N` 与分组标题行 `# 顶层名` 均不计数。
    """
    leaves = bare = 0
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ln.startswith((" ", "\t")):
            leaves += 1
        elif ln.rstrip().endswith("/"):
            bare += 1
    return leaves, bare


def snapshot_total(lines):
    """取快照首行 `# total=N` 的 N；取不到返回 None。"""
    if not lines:
        return None
    m = re.search(r"total\s*=\s*(\d+)", lines[0] or "")
    return int(m.group(1)) if m else None
