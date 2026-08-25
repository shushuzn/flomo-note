#!/usr/bin/env python3
"""memo 写入前静态自检（脚本/工具层）。

用法：
  python validate_memo.py memo.txt                    校验纯文本卡片文件
  python validate_memo.py --content "...正文..."       校验命令行传入
  python validate_memo.py --create create.json        校验 memo_create 请求 JSON 里的 content
退出码：0=通过（可写云）；1=存在必须修复的错误；2=参数/用法错误。
警告(WARN)不阻塞，错误(ERR)必须修复。
标签须为两级 `#顶层/二级`（恰一个 /）；三级及以上或裸顶层均判 ERR（对应 SKILL「标签规则」硬限）。
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

    # 2) 标签段独占一行
    if len(lines) > 1 and lines[1].strip() != "":
        warn("标签段后建议空一行再接正文（标签单独成段）")

    # 3) 正文
    body = "\n".join(lines[1:]).strip()
    if not body:
        err("正文为空")
    # 不设字数上限（SKILL 第103行）：长度由内容定，够清楚即可，不检查。

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
            warn(f"第 {i} 行 Markdown 链接 [text](url)（flomo 不渲染；来源用'来源: URL'纯文本行）")

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

    # 5) 来源行（兼容 flomo 渲染后的 [text](url) 形态）
    for ln in lines:
        m = re.match(r"^\s*来源:\s*(.+?)\s*$", ln)
        if m:
            u = m.group(1)
            # 提取真实 URL：优先 Markdown 链接 [..](url)，否则直接 URL
            mm = re.search(r"\((https?://[^)]+)\)", u)
            url = mm.group(1) if mm else u
            if not url.startswith(("http://", "https://")):
                err(f"来源不是合法 http(s)/https URL：{url}")
            break

    # 6) 占位 / 生造来源风险
    # 用 \b 词边界，避免误伤含 todo/lorem 子串的正常词（如 Mastodon、loremipsum 等）
    if re.search(r"example\.(com|org)|待补|\bplaceholder\b|\bTODO\b|\blorem\b", content, re.I):
        err("正文含 example/placeholder/TODO/lorem 占位，疑似未完成或生造内容")


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