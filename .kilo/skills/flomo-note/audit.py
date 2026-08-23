#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flomo 卡片库只读质检（镜像 flomo-note SKILL.md 标尺）。
用法: python audit.py            # 打印报告并写入 audit-report.md
      python audit.py -s         # Strict: 有错误级缺陷则退出码 1
      python audit.py --no-report # 仅打印，不写文件（供 git pre-commit 钩子）
脚本只读，不改任何卡片。
"""
import os
import re
import sys

FLOMO_ROOT = r"D:\OpenClaw\flomo-note"
EXCLUDED_DIRS = {".obsidian", ".kilo", ".workbuddy", ".git", ".githooks", "node_modules"}
EXCLUDED_ROOT = {"AGENTS.md", "README.md"}
ILLEGAL_FNAME = re.compile(r"[\\/:*?\"<>|]")
# 主标签卡片达此数仍未建索引卡则警告
INDEX_THRESHOLD = 6
BODY_MAX_ERROR = 600
BODY_MAX_WARN = 400
BODY_MIN_WARN = 20


def parse_frontmatter(text):
    fm = {}
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return fm
    lines = m.group(1).split("\n")
    for i, line in enumerate(lines):
        mm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not mm:
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if key == "tags" and val == "":
            items = []
            j = i + 1
            while j < len(lines):
                bm = re.match(r"^\s*-\s+(.+)$", lines[j])
                if not bm:
                    break
                items.append(bm.group(1).strip().strip('"').strip("'"))
                j += 1
            fm[key] = items
        else:
            fm[key] = val
    return fm


def body_text(body):
    # 剥离 frontmatter 与一级标题后，去掉空白统计卡片正文长度（字+符号）
    text = re.sub(r"^---\r?\n.*?\r?\n---\r?\n", "", body, count=1, flags=re.S)
    text = re.sub(r"^#\s.*$", "", text, flags=re.M)
    return re.sub(r"\s", "", text)


def main():
    card_files = []
    for root, dirs, files in os.walk(FLOMO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), FLOMO_ROOT)
            parts = rel.split(os.sep)
            # 只认库根（一层）平铺卡片；子目录（含索引）也算，但排除根元文件
            if len(parts) == 1 and fn in EXCLUDED_ROOT:
                continue
            card_files.append(os.path.join(root, fn))

    errors = []
    warnings = []

    cards = {}
    for path in card_files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        rel = os.path.relpath(path, FLOMO_ROOT)
        if content.startswith("\ufeff"):
            errors.append(f"[{rel}] 含 UTF-8 BOM（skill 要求无 BOM UTF-8）")
            content = content.lstrip("\ufeff")
        fm = parse_frontmatter(content)
        ntype = fm.get("type", "memo")
        if ntype not in ("memo", "index"):
            continue
        base = os.path.splitext(os.path.basename(path))[0]
        cards[base.lower()] = {
            "name": base, "path": rel, "type": ntype,
            "fm": fm, "body": content,
        }

    # 主标签 -> memo 卡列表
    main_tag_cards = {}
    for base, c in cards.items():
        if c["type"] != "memo":
            continue
        tags = c["fm"].get("tags")
        if isinstance(tags, list) and tags:
            main_tag_cards.setdefault(tags[0], []).append(base)
        elif isinstance(tags, str) and tags.startswith("["):
            m = re.search(r'"([^"]+)"', tags)
            if m:
                main_tag_cards.setdefault(m.group(1), []).append(base)

    # 索引卡按主标签归类（首 tag 即索引卡的标签）
    index_by_main = {}
    for base, c in cards.items():
        if c["type"] != "index":
            continue
        tags = c["fm"].get("tags")
        if isinstance(tags, list) and tags:
            index_by_main.setdefault(tags[0], []).append(base)

    for base in sorted(cards.keys()):
        c = cards[base]
        rel = c["path"]
        if ILLEGAL_FNAME.search(c["name"]):
            errors.append(f"[{rel}] 文件名含非法字符: {c['name']}")

        if c["type"] != "memo":
            # index 卡：creator/type/tags 即可，不查 source
            continue

        fm = c["fm"]
        tag = f"[{rel}]"

        # source：原创允许留空，否则必须有
        src = fm.get("source", "").strip()
        has_source = bool(src) or bool(re.search(r"(?m)^##\s*来源", c["body"]))
        if not has_source:
            errors.append(f"{tag} 缺 source / ## 来源（原创请写 source: local://原创）")

        if "tags" not in fm:
            warnings.append(f"{tag} frontmatter 缺 tags")
        else:
            tv = fm["tags"]
            if isinstance(tv, list):
                if len(tv) == 0:
                    warnings.append(f"{tag} tags 为空数组")
                if len(tv) > 5:
                    warnings.append(f"{tag} tags 数量>5，建议精简")
            elif isinstance(tv, str):
                if not re.match(r"^\[.*\]$", tv):
                    errors.append(f"{tag} tags 非数组: {tv}")
                else:
                    inner = tv[1:-1].strip()
                    if inner == "":
                        warnings.append(f"{tag} tags 为空数组")
                    elif '"' not in inner:
                        errors.append(f"{tag} tags 含裸词(未引号): {tv}")

        body_len = len(body_text(c["body"]))
        if body_len > BODY_MAX_ERROR:
            errors.append(f"{tag} 正文过长({body_len}字>600)，应拆成多张卡片")
        elif body_len > BODY_MAX_WARN:
            warnings.append(f"{tag} 正文偏长({body_len}字>400)，考虑拆分")
        elif body_len < BODY_MIN_WARN:
            warnings.append(f"{tag} 正文过薄({body_len}字<20)，疑似空洞")

        # 首行一句话定位：正文首段加粗
        if not re.search(r"^\*\*.+?\*\*", c["body"], flags=re.M):
            warnings.append(f"{tag} 缺少首行加粗『一句话定位』")

    # 索引一致性：主标签下 >=INDEX_THRESHOLD 张卡却无索引卡 => 警告
    for maintag, bases in main_tag_cards.items():
        if len(bases) >= INDEX_THRESHOLD and maintag not in index_by_main:
            warnings.append(f"主标签 #{maintag} 已有 {len(bases)} 张卡片，建议建索引卡")

    memo_count = sum(1 for c in cards.values() if c["type"] == "memo")
    index_count = sum(1 for c in cards.values() if c["type"] == "index")

    lines = []
    lines.append("# flomo 卡片质检报告")
    lines.append(f"生成时间: {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"库根: {FLOMO_ROOT}")
    lines.append("")
    lines.append("## 概览")
    lines.append(f"- 卡片总数(memo+index): {len(cards)}")
    lines.append(f"- memo 卡: {memo_count}")
    lines.append(f"- index 卡: {index_count}")
    lines.append(f"- 错误级缺陷: {len(errors)}")
    lines.append(f"- 警告级缺陷: {len(warnings)}")
    lines.append("")
    if errors:
        lines.append("## 错误级缺陷（必须修）")
        for e in sorted(errors):
            lines.append(f"- {e}")
        lines.append("")
    if warnings:
        lines.append("## 警告级缺陷（建议修）")
        for w in sorted(warnings):
            lines.append(f"- {w}")
        lines.append("")

    report = "\n".join(lines)
    if "--no-report" not in sys.argv:
        out_path = os.path.join(FLOMO_ROOT, ".kilo", "skills", "flomo-note", "audit-report.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
    print(report)

    if "-s" in sys.argv or "--strict" in sys.argv:
        sys.exit(1 if errors else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()