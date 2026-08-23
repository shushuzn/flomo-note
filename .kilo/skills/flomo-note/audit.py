#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flomo 卡片库只读质检（镜像 flomo-note SKILL.md 标尺）。
卡片无 frontmatter：一条 MEMO，标签 `#标签/子标签` 写在正文行内（flomo 官方规则）。
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

# flomo 标签只允许：汉字 / 字母 / 数字 / 下划线，层级用 `/`，段内不含空格与其它字符
TAG_BODY = r"[\u4e00-\u9fffA-Za-z0-9_]+"
ALLOW_TAG = re.compile(r"^" + TAG_BODY + r"(/" + TAG_BODY + r")*$")
# 提取：`#` 后紧跟标签，且 `#` 前不是标签字符（避免句子里普通 `#` 干扰 markdown 标题/井号）
TAG_RE = re.compile(r"(?<![A-Za-z0-9_\u4e00-\u9fff#])#([^\s#:]+)")
SRC_RE = re.compile(r"来源\s*[:：]\s*(\S.*)", re.M)

BODY_MAX_ERROR = 600
BODY_MAX_WARN = 400
FILE_MAX_TAGS = 7
OVERVIEW_THRESHOLD = 6


def main():
    card_files = []
    for root, dirs, files in os.walk(FLOMO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), FLOMO_ROOT)
            parts = rel.split(os.sep)
            if len(parts) == 1 and fn in EXCLUDED_ROOT:
                continue
            card_files.append(os.path.join(root, fn))

    errors = []
    warnings = []

    overviews = set()          # 已存在的概览卡文件名（去扩展名，含"概览"）
    main_tag_cards = {}        # 主标签(首标签第一段) -> 卡名列表
    cards = {}

    for path in card_files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        rel = os.path.relpath(path, FLOMO_ROOT)
        if content.startswith("\ufeff"):
            errors.append(f"[{rel}] 含 UTF-8 BOM（skill 要求无 BOM UTF-8）")
            content = content.lstrip("\ufeff")
        base = os.path.splitext(os.path.basename(path))[0]
        cards[base.lower()] = {"name": base, "path": rel, "body": content}
        if "概览" in base and content.count("#") == 0:
            overviews.add(base)

    for base_l, c in cards.items():
        cw = cards[base_l]
        rel = cw["path"]
        if ILLEGAL_FNAME.search(cw["name"]):
            errors.append(f"[{rel}] 文件名含非法字符: {cw['name']}")

        lines_ = cw["body"].splitlines()
        first_line = lines_[0] if lines_ else ""
        first_tags = TAG_RE.findall(first_line)              # 标签须集中在第一行单独成段
        all_tags = TAG_RE.findall(cw["body"])                # 仅用于判"标签未放首行"

        if not first_tags:
            warnings.append(f"[{rel}] 首行无标签——#标签 应单独放第一行成段，空一行再接正文")
        else:
            remain = re.sub(r"#[^\s#:]+", "", first_line).strip()
            if remain:
                errors.append(f"[{rel}] 首行标签未单独成段——第一行应只放 #标签，正文从第三行起（当前混有：{remain[:20]}）")
            if len(all_tags) > len(first_tags):
                errors.append(f"[{rel}] 除首行外还有标签——标签应全部集中在第一行标签段")

        if not all_tags:
            warnings.append(f"[{rel}] 无任何 #标签")
            continue

        # 标签格式校验（flomo：无 emoji/特殊字符、段内无空格、层级 `<...>/<...>` 非空）
        for body in first_tags:
            if not ALLOW_TAG.match(body):
                errors.append(f"[{rel}] 非法标签 #/ {body} —— 仅允许汉字/字母/数字/下划线，层级用 / ，禁 emoji、&、空格等")
        if len(first_tags) > FILE_MAX_TAGS:
            warnings.append(f"[{rel}] 标签数量 {len(first_tags)}>7，建议精简")

        # 外部资料出处：有"来源:"时其同行须带内容
        src_m = SRC_RE.search(cw["body"])
        if src_m and not src_m.group(1).strip():
            warnings.append(f"[{rel}] 来源: 后为空")

        # 计数主标签（取首行首个标签的第一段）用于概览一致性
        first = first_tags[0]
        maintag = first.split("/")[0]
        main_tag_cards.setdefault(maintag, []).append(cw["name"])

        # 长度
        body_len = len(re.sub(r"\s", "", cw["body"]))
        if body_len > BODY_MAX_ERROR:
            errors.append(f"[{rel}] 正文过长({body_len}字>600)，应拆成多张卡片")
        elif body_len > BODY_MAX_WARN:
            warnings.append(f"[{rel}] 正文偏长({body_len}字>400)，考虑拆分")

    # 概览一致性：某主标签下 >=OVERVIEW_THRESHOLD 张卡且无对应概览卡 => 建议
    for maintag, names in main_tag_cards.items():
        if len(names) >= OVERVIEW_THRESHOLD and maintag not in overviews:
            warnings.append(f"主标签 #{maintag} 已有 {len(names)} 张卡片，可建同名概览卡")

    of = os.path.join(FLOMO_ROOT, ".kilo", "skills", "flomo-note", "audit-report.md")
    all_cards = [c for c in card_files if c not in (EXCLUDED_ROOT or {})]
    lines = []
    lines.append("# flomo 卡片质检报告")
    lines.append(f"生成时间: {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"库根: {FLOMO_ROOT}")
    lines.append("")
    lines.append("## 概览")
    lines.append(f"- 卡片总数: {len(cards)}")
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
        with open(of, "w", encoding="utf-8") as f:
            f.write(report)
    print(report)

    if "-s" in sys.argv or "--strict" in sys.argv:
        sys.exit(1 if errors else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()