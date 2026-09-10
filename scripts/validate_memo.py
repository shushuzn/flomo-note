#!/usr/bin/env python3
"""memo 写入前静态自检（脚本/工具层）。

用法：
  python validate_memo.py memo.txt                    校验纯文本卡片文件
  python validate_memo.py --content "...正文..."       校验命令行传入
  python validate_memo.py --create create.json        校验 memo_create 请求 JSON 里的 content
退出码：0=通过（可写云）；1=存在必须修复的错误；2=参数/用法错误。
警告(WARN)不阻塞，错误(ERR)必须修复。
标签须为两级 `#顶层/二级`（恰一个 /）；三级及以上或裸顶层均判 ERR（对应 SKILL「标签规则」硬限）。
卡片格式：首行标签段，第二行概念名称（简明概括卡片主题的名词/概念），空一行接正文；所有卡片均为追踪卡，后续进展用 memo_update 更新。
"""
import json
import re
import sys
from pathlib import Path

ERR, WARN = [], []

TAG_CHAR_OK = re.compile(r"^[\w\u4e00-\u9fff/]+$")
TAG_NO_BAD = re.compile(r"[^\s<>#&]")


def err(msg):
    ERR.append(msg)


def warn(msg):
    WARN.append(msg)


def check(content):
    if not content or not content.strip():
        err("正文为空，无法写入")
        return
    lines = content.split("\n")

    # 1) 首行标签段
    first = lines[0].strip()
    if not first.startswith("#"):
        err("首行必须是标签段（以 # 开头）；无标签的卡片无法检索")
    else:
        tags = first.split()
        has_slash = any("/" in t for t in tags)
        for t in tags:
            if not t.startswith("#"):
                err(f"标签必须以 # 开头：{t}")
                continue
            name = t[1:]
            if not name:
                err("空标签 #")
                continue
            if not TAG_CHAR_OK.match(name):
                err(f"标签含非法字符（仅允许中文/英文/数字/下划线/层级 /）：{t}")
            if not TAG_NO_BAD.search(name):
                err(f"标签包含空格/< /# /& 等终止字符：{t}")
            # 层级严格两级（硬限，对应 SKILL「标签规则」）：只允许 #顶层/二级（恰一个 /）
            # 零个 / = 裸顶层（也非法，须带二级）；≥2 个 / = 三级及以上（非法）
            n_slash = name.count("/")
            if n_slash == 0:
                err(f"标签须为两级 `#顶层/二级` 形式（缺二级）：{t}")
            elif n_slash >= 2:
                err(f"标签层级超过两级（禁止三级及以上）：{t} —— 只允许 #顶层/二级，如 #科技/安全")
            # 子标签前缀检查：存在带 / 的主标签时，裸二级（无 /）即疑似建错顶层
            if has_slash and "/" not in name:
                err(f"疑似裸子标签（无主标签前缀）：{t} —— 应写成 #主/子，否则会建出独立顶层标签")

    # 2) 标签段后第二行应为概念名称标题（非空），第三行空行接正文
    if len(lines) > 1 and lines[1].strip() == "":
        warn("标签段后第二行应为概念名称（简明概括卡片主题的名词/概念），不应为空行")
    elif len(lines) > 2 and lines[2].strip() != "":
        warn("标题行后建议空一行再接正文（标题单独成段）")

    # 3) 正文
    body = "\n".join(lines[1:]).strip()
    if not body:
        err("正文为空")
    # 不设字数上限（见 SKILL「卡片格式」）：长度由内容定，够清楚即可，不检查。


    # 4) flomo 不渲染的 Markdown 语法检测
    for i, ln in enumerate(lines[1:], start=2):
        s = ln.strip()
        if not s:
            continue
        if re.match(r"^#{1,6}\s", s):
            warn(f"第 {i} 行疑似标题语法（flomo 不渲染），建议改为普通文本")
        if s.startswith(">"):
            warn(f"第 {i} 行引用语法 >（flomo 不渲染，建议改为普通文本）")
        if s.startswith("```"):
            err(f"第 {i} 行代码块 ```（flomo 不支持）")
        if s.count("|") >= 2:
            warn(f"第 {i} 行疑似表格 |（flomo 不渲染，成组数字/kpi 改用列表）")
        if re.search(r"!\[[^\]]*\]\(", s):
            err(f"第 {i} 行图片语法 ![（flomo 不支持）")
        if re.search(r"\[[^\]]*\]\(https?://", s):
            warn(f"第 {i} 行 Markdown 链接 [text](url)（flomo 不渲染，建议改为纯文本 URL）")

    # 4.5) 正文内 accidental #tag（flomo 会把任意 #xxx 当标签，造成脏标签）
    #      约定：标签只写在首行；正文任何行内 #词/#数字 都是意外，必须改。
    for i, ln in enumerate(lines[1:], start=2):
        s = ln.strip()
        if not s or "http" in s:
            continue  # 跳过 URL 行（避免误报 #fragment）
        for m in re.finditer(r"#([A-Za-z0-9_\u4e00-\u9fff]{1,40})", s):
            if m.start() == 0:
                continue  # 行首标题已在 4) 处理
            err(f"第 {i} 行正文含 '#{m.group(1)}'（flomo 会把它当标签，造成脏标签）；改作 'No.'/'号' 等写法")

    # 5) 占位 / 生造来源风险
    # 用 \b 词边界，避免误伤含 todo/lorem 子串的正常词（如 Mastodon、loremipsum 等）
    if re.search(r"example\.(com|org)|待补|\bplaceholder\b|\bTODO\b|\blorem\b", content, re.I):
        err("正文含 example/placeholder/TODO/lorem 占位，疑似未完成或生造内容")

    # 5.5) 旧格式遗留检测：状态行 / 来源行（WARN 提示，不阻塞写云）
    # 卡片格式硬限：无状态行、无来源行。为避免误伤正文要点（如「电力来源：火电为主」这类
    # 主语+来源的正常论述句），仅在下列三个条件同时成立时才提示：
    #   a) 行首（允许 -/*/~/• 列表标记与 1. 序号）后紧跟「白名单修饰词 + 状态/来源/报道/出处 + 冒号」；
    #      修饰词不在白名单的一律不报（「电力来源：」是正文要点，不是卡片级来源行）。
    #   b) 该行位于卡片最后两个非空行——卡片级元信息行只可能出现在末尾，正文中间一律不报。
    #   c) 冒号后内容不超过 200 字（或含 http 链接）——冒号后是长论述的视为正文，不报。
    # 命中只判 WARN：这类行多半是数据/参考类要点，交由人工判断，不阻塞写卡。
    meta_source_re = re.compile(
        r"^\s*(?:[-*~\u2022]\s*)?(?:\d{1,2}[.、)]\s*)?"
        r"(?:当前|进展|最新|追踪|更新|既往|信息|内容|数据|新闻|原文|引用|参考|资料|文献|报道|消息|官方|文章|作者与|链接|网址)?"
        r"(状态|来源|报道|出处|资料|文献|参考)"
        r"(?:链接|资料|文献|信息|地址|网址|说明)?\s*[：:]"
    )
    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    last_line_no = (nonempty[-1] + 1) if nonempty else 0
    for i, ln in enumerate(lines[2:], start=3):  # 跳过首行标签段、第二行概念名称
        s = ln.strip()
        if not s:
            continue
        m = meta_source_re.match(s)
        if not m:
            continue
        if i < last_line_no - 1:
            continue  # 非末尾行：视为正文要点，不报
        tail = re.split(r"[：:]", s, maxsplit=1)[-1].strip()
        if len(tail) > 200 and "http" not in tail:
            continue  # 冒号后是长论述：视为正文，不报
        warn(f"第 {i} 行疑似旧格式来源/状态行（卡片格式硬限：无来源行、无状态行）——匹配到「{m.group(0).strip()}」；"
             f"若这是正文要点可忽略，若确为卡片级来源/状态行请删除该行，把信息融入正文对应要点或直接省略")


def load_content(argv):
    arg = argv[1]
    if arg == "--create":
        req = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        return req.get("params", {}).get("arguments", {}).get("content", "")
    if arg == "--content":
        return argv[2]
    return Path(arg).read_text(encoding="utf-8")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    try:
        content = load_content(sys.argv)
    except (IndexError, FileNotFoundError, json.JSONDecodeError) as e:
        sys.stderr.write(f"读取输入失败：{e}\n")
        return 2
    check(content)
    for m in ERR:
        print("ERR :", m)
    for m in WARN:
        print("WARN:", m)
    print(f"结果：{len(ERR)} 错 / {len(WARN)} 警，合计 {len(ERR) + len(WARN)} 条提示")
    return 1 if ERR else 0


if __name__ == "__main__":
    sys.exit(main())