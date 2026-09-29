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


def concept_keywords(content, limit=2):
    """从卡片正文派生检索用短关键词。

    背景：整行概念名含全角标点时 flomo 全文检索常返回 0，必须退到短核心词。
    产出（去重保序，截断到 limit 个）：
      1. 概念名内最长的连续中文段（≥4 字）
      2. 概念名去除标点后的前 8 个字符
    content 无效时返回 []。
    """
    sig = memo_signature(content)
    if not sig:
        return []
    concept = sig["concept"]
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


def tag_leaves(tagline):
    """取标签行里**合法两级**的 (顶层, 二级) 对。

    如 `#科技/机器人 #AI/物理AI` → [(科技,机器人),(AI,物理AI)]。
    严格两级：二级部分不得再含 `/`，且**必须以空白或行尾收束**——
    否则 `#科技/安全/邮件` 会被误当成合法的 `(科技, 安全)`。
    二级名收尾的句读标点须剔除：`#科技/机器人。` 显然是 `科技/机器人`
    被句号收尾，若把句号吃进二级名，近邻比对会指向不存在的簇。
    **只剔行尾句读，不剔成对括号**——`#科技/机器人（人形）` 的括号属名字本身。
    """
    if not tagline:
        return []
    raw = re.findall(r"#([^/\s#]+)/([^\s#/]+)(?=\s|$)", tagline)
    tail = "，。、；：！？,.;:!?）】》」』"
    return [(top, leaf.rstrip(tail)) for top, leaf in raw if leaf.rstrip(tail)]
