#!/usr/bin/env python3
"""memo 写入前静态自检（脚本/工具层）。

用法：
  python validate_memo.py memo.txt                    校验纯文本卡片文件
  python validate_memo.py --file memo.txt             同上（别名）
  python validate_memo.py --file create.json          文件是 JSON 时自动抽取 content
  python validate_memo.py --content "...正文..."       校验命令行传入
  python validate_memo.py --create create.json        校验 JSON 里的 content
  python validate_memo.py --no-gate ...               降级闸门为警告（须凭证带授权标记）
退出码：0=通过（可写云）；1=存在必须修复的错误；2=参数/用法错误。
警告(WARN)不阻塞，错误(ERR)必须修复。
`--no-gate` 降级须配套 `sop_gate.py --allow-no-gate <理由>` 先在凭证中留下授权标记，
仅供批量处理历史卡等特殊场景；无授权的 --no-gate 一律判 ERR（防止绕过整套 SOP 闸门）。
标签须为两级 `#顶层/二级`（恰一个 /）；三级及以上或裸顶层均判 ERR（对应 SKILL「标签规则」硬限）。
卡片格式：首行标签段，第二行概念名称（简明概括卡片主题的名词/概念），空一行接正文；所有卡片均为追踪卡，后续进展用 memo_update 更新。
模板前缀回显（ERR）：「结论先行」等 SKILL 条目名/格式指令词写进正文即判错——结论直接作为正文首段第一句，不加引导词。

输入健壮性：
  - 读取文件统一走 utf-8-sig，容忍 BOM。
  - CRLF/CR 归一化为 LF，避免行内容被 \r 污染。
  - --create/--file 同时兼容两种 JSON 形态：flomo_client.py 实际发送的
    `{"content": ...}`（顶层），以及 JSON-RPC 信封 `params.arguments.content`。
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memo_util import body_hash, memo_signature, signature_key  # noqa: E402

ERR, WARN = [], []

# 单卡正文（含标签段）字数上限，与 SKILL「卡片格式」承诺的硬限一致。
MAX_MEMO_CHARS = 20000

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
GATE_DIR = PROJECT_ROOT / ".sop_gate"

TAG_CHAR_OK = re.compile(r"^[\w\u4e00-\u9fff/]+$")

# 表格：分隔行（|---|---|）、被竖线包裹的行（| a | b |）、空格包围的竖线成组（a | b | c）
# 半角 `|` 与全角 `｜`（U+FF5C）同等认定——中文输入法下常打出全角，漏认会造成漏报。
_P = "|｜"
TABLE_SEP = re.compile(rf"^\s*[{_P}]?\s*:?-{{2,}}:?\s*(?:[{_P}]\s*:?-{{2,}}:?\s*)+[{_P}]?\s*$")
TABLE_WRAPPED = re.compile(rf"^\s*[{_P}].*[{_P}]\s*$")
SPACED_PIPE = re.compile(rf"(?:^|\s)[{_P}](?=\s|$)")


def err(msg):
    ERR.append(msg)


def warn(msg):
    WARN.append(msg)


def _is_table_row(s):
    """判定是否真的是 Markdown 表格行。

    旧实现用 `s.count('|') >= 2`，把数学绝对值 `|p|≤r`、集合势 `|F(s)−F(t)|<2|s−t|`
    一律误判为表格。改为只认三种真表格形态：
    分隔行、竖线包裹行、空格包围的竖线成组；紧贴文字的竖线（绝对值/条件概率/势）放行。
    判定前剥掉列表标记，使「- | a | b |」这类列表内的表格也能识别。
    竖线半角与全角同等认定（中文输入法常打出全角）。
    """
    core = re.sub(r"^(?:[-*~\u2022]|\d{1,2}[.、)])\s+", "", s).strip()
    if TABLE_SEP.match(core):
        return True
    if TABLE_WRAPPED.match(core) and core.count("|") + core.count("｜") >= 2:
        return True
    return (len(SPACED_PIPE.findall(core)) >= 2
            and core.count("|") + core.count("｜") >= 2)


def check(content):
    if not content or not content.strip():
        err("正文为空，无法写入")
        return
    # CRLF / CR 归一化：不归一化时 \r 会留在行尾，影响行首行尾判定
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
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
                bad = "".join(sorted(set(re.findall(r"[^\w\u4e00-\u9fff/]", name))))
                err(f"标签含非法字符（仅允许中文/英文/数字/下划线/层级 /）：{t}"
                    + (f" —— 非法字符：{bad}" if bad else ""))
                continue
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
    # 注意：标签段后空行（line 2 = 空）是已知误报（flomo 存储层自动插空行），只提示不阻断
    if len(lines) > 1 and lines[1].strip() == "":
        warn("标签段后第二行应为概念名称（简明概括卡片主题的名词/概念），不应为空行")
    elif len(lines) > 2 and lines[2].strip() != "":
        err("标题行后必须空一行再接正文（标题单独成段）——缺少空行时 flomo 会将标题与正文首段合并为一大段，无法辨识标题")
    # 概念名行不得以 # 开头：否则卡片签名（首行标签段 + 第二行概念名）不成立，
    # 会导致签名取不到 → 幂等查重失效 + SOP 闸门凭证无从校验，一路静默放行。
    # 此检查必须在「结构本身不合规」层就报错，不能依赖下游以缺签名为由兜底。
    for i in range(1, min(3, len(lines))):
        s = lines[i].strip()
        if s and s.startswith("#"):
            err(f"第 {i + 1} 行是标签（以 # 开头），但它应为概念名称——"
                f"标签只能出现在首行标签段，多写一行标签会让卡片签名失效"
                f"（连带使写前幂等查重与 SOP 闸门校验同时失效）")
            break

    # 3) 正文
    body = "\n".join(lines[1:]).strip()
    if not body:
        err("正文为空")
    # 字数上限：与 SKILL「卡片格式」承诺的硬限一致（含标签段，按字符数计）。
    # 上限存在的意义是防膨胀——超限往往是该拆卡或多事件混装（见 H7）的信号。
    total_len = len(content)
    if total_len > MAX_MEMO_CHARS:
        err(f"卡片正文含标签段共 {total_len} 字，超过上限 {MAX_MEMO_CHARS} 字"
            f"——超出须精简，或按 H7 拆分多事件")


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
        if _is_table_row(s):
            warn(f"第 {i} 行疑似表格 |（flomo 不渲染，成组数字/kpi 改用列表）")
        if re.search(r"!\[[^\]]*\]\(", s):
            err(f"第 {i} 行图片语法 ![（flomo 不支持）")
        if re.search(r"\[[^\]]*\]\(https?://", s):
            warn(f"第 {i} 行 Markdown 链接 [text](url)（flomo 不渲染，建议改为纯文本 URL）")


    # 4.5) 正文内 accidental #tag（flomo 会把任意 #xxx 当标签，造成脏标签）
    #      约定：标签只写在首行；正文行内的 #词 / #数字 / #( 等属意外，必须改。
    #      含 http 的行整行跳过（URL 里的 #fragment 不构成脏标签）。
    #      注意：正文里的 / 斜杠（如「Mori/Siu–Yau」「消失/刚性/估计」）是正常书写，
    #      不当作脏标签扫描；脏标签认定只针对标签段本身与正文 # 形态。
    #      判定放宽为「# 后紧跟任意非空白字符」（原实现漏掉 '#(' 形态）。
    for i, ln in enumerate(lines[1:], start=2):
        s = ln.strip()
        if not s or "http" in s:
            continue
        # 原正则只认 # 后跟 [A-Za-z0-9_中文]，漏掉 '#(' 这类形态。
        # flomo 的判定只看 # 后是否紧跟非空白字符，故放宽为 \S。
        for m in re.finditer(r"#(\S{1,40})", s):
            if m.start() == 0:
                continue  # 行首标题已在 4) 处理
            err(f"第 {i} 行正文含 '#{m.group(1)}'（flomo 会把它当标签，造成脏标签）；"
                f"数学里的球面导数等一并写作 ♯（U+266F）或改写成文字，勿用 ASCII 的 #")

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
    # 5.5) 旧格式遗留检测：状态行（WARN 提示，不阻塞写云）
    # 说明：**来源类**行（来源：/ 报道：/ 参考：…）已由 5.8) 以 ERR 硬阻断，此处不再重复提示，
    # 只保留「状态」类的 WARN——状态行（状态：/ 当前状态：/ 进展状态：）是历史卡遗留，属人工判断项。
    meta_source_re = re.compile(
        r"^\s*(?:[-*~\u2022]\s*)?(?:\d{1,2}[.、)]\s*)?"
        r"(?:当前|进展|最新|追踪|更新|既往)?"
        r"(状态)"
        r"(?:说明|信息|描述)?\s*[：:]"
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
        warn(f"第 {i} 行疑似旧格式状态行（卡片格式硬限：无来源行、无状态行）——匹配到「{m.group(0).strip()}」；"
             f"若这是正文要点可忽略，若确为卡片级状态行请删除该行，把信息融入正文对应要点或直接省略")

    # 5.6) 卡片载体元信息检测（ERR）
    # 把"B 站数学直播截图，UP 主…""某课程讲义，定义 3"这类
    # **卡片取自什么载体**的信息写进概念卡正文（含首段括号与要点句内），与概念无关。
    # 5.5) 只认"行首白名单词 + 冒号 + 位于末两行"，括号式 / 句中式载体标注全部漏检，故补本项。
    #
    # 分三层，防止误伤：
    #   a) 行内强载体词（carrier_strong）：截图 / 讲义 / 课件 / 板书 / 字幕 / 批注 / 页码 / UP主 / B站 /
    #      如图 等，几乎只可能用于交代取材方式，正文出现即 ERR。
    #   b) 取材标注词（carrier_ctx）：公众号 / 视频号 / 哔哩哔哩 / 抖音 / 直播 / 课程 / 讲座 / 专栏 等，
    #      本身可能是卡片主体（如"微信公众号"是产品概念），故只在**括号式注释**里出现才 ERR。
    #   c) 歧义词（carrier_weak）：视频 / 音频 / 直播 / 演讲 / 发布会 / 访谈 等，括号内出现只 WARN
    #      人工判断。"直播"原在 ctx 层，因可能是业务名词（如 "4D 直播"），降级到 weak：
    #      真载体语境（直播截图 / 直播间）必有 strong 词（截图 / UP主 / B站）兜底判 ERR，
    #      不靠 "直播" 单独阻断——专业名词不得为过检而改写。
    # 注意：本项 early-return 的字面量只针对"材料载体"（截图/讲义/课件）。
    # "报道 / 消息 / 通报 / 通报方 / 官网 / 作者"这类**事件自身的来源信息**不在此处判，
    # 而是由 5.8) 单独以 ERR 硬阻断（见 H15/H16：一切来源字样一律删除，无豁免）。
    carrier_strong = re.compile(
        r"截图|截屏|录屏|屏摄|讲义|课件|幻灯片|(?<![A-Za-z/\.])[Pp][Pp][Tt](?![A-Za-z])"
        r"|板书|字幕|批注|听课|课程记录|课程笔记|续页|原图|配图"
        r"|[Uu][Pp]\s*主|[Bb]\s*站|bili\s*bili"
        r"|第\s*\d+(?:\s*[-–—~]\s*\d+)?\s*页|\d+\s*[-–—~]?\s*\d+\s*/\s*\d+\s*页|\d+\s*[/／]\s*\d+\s*页"
        r"|如图|见图|图中所?示|上图|下图|该图|这张图"
    )
    carrier_ctx = re.compile(
        r"公众号|视频号|小程序|哔哩哔哩|抖音|快手|小红书|录播|讲座|课堂|课程|订阅号|推文"
    )
    carrier_weak = re.compile(r"视频|音频|回放|演讲|发布会|访谈|播客|直播")
    bracket_re = re.compile(r"[（(][^（()）]*[）)]")

    def _carrier_scan(line_no, text, only_brackets):
        """扫描一行，返回 (强/标注类命中列表, 歧义类命中列表)；only_brackets=True 时只查括号注释。"""
        hard, soft = [], []
        if not text.strip():
            return hard, soft
        if not only_brackets:
            hard += [m.group(0).strip() for m in carrier_strong.finditer(text)]
        for grp in bracket_re.findall(text):
            hard += [m.group(0).strip() for m in carrier_ctx.finditer(grp)]
            if not only_brackets:
                hard += [m.group(0).strip() for m in carrier_strong.finditer(grp)]
            soft += [m.group(0).strip() for m in carrier_weak.finditer(grp)]
        return list(dict.fromkeys(hard)), [w for w in dict.fromkeys(soft) if w not in hard]

    for i, ln in enumerate(lines[2:], start=3):  # 只扫正文：首行为标签段
        hard, soft = _carrier_scan(i, ln, only_brackets=False)
        if hard:
            err(f"第 {i} 行含卡片载体/来源信息「{'、'.join(hard)}」——概念卡正文只写概念本身，"
                f"禁止交代内容取自何种载体（截图 / 讲义 / 课件 / 字幕 / 板书 / 页码 / UP主 / 直播间 / 公众号 等），"
                f"无论放在首段括号、要点句内还是行首都一律禁止；请删除这类表述，"
                f"载体上没有的信息直接省略，禁止用「截图未展开」之类说法保留")
            continue  # 已判错的行不再叠加同主题提示
        for w in soft:
            warn(f"第 {i} 行括号注释含载体词「{w}」——若这是交代卡片取材来源（而非概念自身内容），"
                 f"按卡片格式硬限删除")

    # 概念名称行（第二行）不得含强载体词；平台 / 企业名（哔哩哔哩、微信公众号）作概念名合法，
    # 故此处只查 carrier_strong，且只提示不阻塞。
    name_hit = [m.group(0).strip()
                for m in carrier_strong.finditer(lines[1] if len(lines) > 1 else "")]
    if name_hit:
        warn(f"第 2 行概念名称含载体词「{'、'.join(name_hit)}」——概念名应只写概念本身，"
             f"不要用取材方式（截图 / 讲义 / 课件 等）命名")

    # 5.8) 事件来源信息检测（ERR，硬阻断）——对应 SKILL H15 / H16。
    # 背景：5.5) 只在「末两行 + 冒号式 + 200 字内」才 WARN，正文首句的
    # 「据沈阳市纪委监委消息，…」「据路透社报道，…」「中央纪委国家监委网站 9 月 18 日发布」
    # 全部落在盲区；旧版更在载体检测里显式豁免"报道/消息/通报"，等于开后门。
    # 本项把「事件自身的报道方 / 通报方 / 发布方」按 H16 与材料载体同等对待，一律 ERR：
    #   卡片只陈述事件与概念本身，不署来源。违规出现在哪一行都判，不限行位与长度。
    #
    # 三层规则（从严设计，避免误伤正文里的普通词）：
    #   a) 来源引导式（source_lead）：行内任意位置，「据/根据/援引/源自 + 来源主体 + 消息/报道/
    #      通报/披露/介绍/称」或「据 + 来源主体」。来源主体限定为机构/媒体/平台后缀词，
    #      避免「据研究」「据报道」以外的普通论述被误伤——但「据报道 / 据消息」无主体也判。
    #   b) 发布/通报动作式（source_act）：「X 网站/纪委监委/… 发布（消息）」「通报称」「发文称」
    #      「官方发布渠道」「通报来源」「发布渠道」「消息来源」「转自/转载自 X」「原文链接」。
    #   c) 行首标签式（source_label）：行首（允许列表标记与序号）直接写
    #      「来源/报道/通报/发布/消息/出处/链接 + 冒号」——即 5.5) 的加强版，全行位生效。
    #
    # 豁免（避免误伤，均可解释为事件内容自身而非"交代来源"）：
    #   - URL 行整行跳过（http/https 出现即跳过，链接本身另有 4) 的 WARN 提示）。
    #   - 「据…称」后接的是**事件内容**而非来源主体时，来源主体正则不匹配即不判。
    # 来源主体：机构 / 媒体 / 平台名。三选一即匹配——
    # ① 带引号（含中英文引号、书名号、直角引号）的任意名称：「汕头纪检监察」「科创板日报」「海阳发布」
    # ② 以机构后缀结尾的连续串：纪委监委 / 监委 / 纪检监察组 / 网站 / 官网 / 公众号 / 视频号 /
    #    报社 / 通讯社 / 新闻 / 日报 / 时报 / 周报 / 网 / 社 / 部 / 厅 / 局 / 委 / 办
    # ③ 已知媒体清单（中文全称 + 常见英文站名）：路透/新华社/澎湃/The Information/NikonRumors 等
    # 反面：不为穷举名称而写死，故用结构匹配，避免「路透及多家媒体」这类变体漏检。
    _org_quoted = r"[「『“\"《][^」』”\"》]{1,30}[」』”\"》]"
    _org_suffix = (
        r"[^，。；：\s（()）「」『』“”\"《》]{1,30}?"
        r"(?:纪委监委|纪检(?:监察)?组|监委|派驻[^，。；：\s]{0,12}纪检监察组|"
        r"网站|官网|公众号|视频号|订阅号|"
        r"报社|通讯社|新闻社|日报|时报|周报|晚报|新闻网|"
        r"新闻|监察|纪委|纪检)"
    )
    # 载体后缀：专指「承载内容发布的平台/刊物」，用于无日期的「X 发布 Y」句式。
    # 不含裸「新闻/监察/纪委」——那些是机构词，单独出现时更像事件主体而非发布渠道。
    _org_carrier = (
        r"[^，。；：\s（()）「」『』“”\"《》]{1,30}?"
        r"(?:网站|官网|公众号|视频号|订阅号|"
        r"报社|通讯社|新闻社|新闻网|"
        r"日报|时报|周报|晚报)"
    )
    # 裸载体词：允许「公众号 5 月 1 日发布公告」这类无前缀写法（主体就是载体名本身）。
    _org_bare = r"(?:网站|官网|公众号|视频号|订阅号|纪委监委)"
    # 日月段：阿拉伯数字与中文数字同等认定（「9 月 18 日」/「九月十八日」/「九 月 十八 日」）。
    # 中文数字是制度类文本的常见写法，只认阿拉伯数字会造成真实漏检。
    _cnum = r"[一二三四五六七八九十〇零两]{1,4}"
    _day = r"(?:\d{1,2}|" + _cnum + r")"
    _month = r"(?:\d{1,2}|" + _cnum + r")"
    _date_part = (
        r"(?:(?:\d{1,4}|" + _cnum + r")\s*年\s*)?"
        r"\s*" + _month + r"\s*月\s*" + _day + r"\s*日"
    )
    _org_known = (
        r"(?:路透社|路透|新华社|中新社|中新网|美联社|法新社|彭博社|纽约时报|华尔街日报|金融时报|"
        r"泰晤士报|卫报|澎湃新闻|界面新闻|财新|第一财经|科创板日报|每日经济新闻|中国新闻周刊|"
        r"南方都市报|南方周末|南都晨报|内蒙古法制报|光明网|央广网|中国新闻网|人民网|央视|"
        r"人民日报|经济日报|The\s+Information|NikonRumors|TechCrunch|The\s+Verge|Ars\s+Technica|"
        r"清风中原|清风|惠州清风|汕头纪检监察|长安街知事|党建头条|安徽先锋|海阳发布|"
        r"路透及多家媒体|多家媒体|外媒|港媒|台媒)"
    )
    source_org = r"(?:" + _org_quoted + r"|" + _org_suffix + r"|" + _org_known + r")"
    source_lead = re.compile(
        r"(?:据|根据|援引|源自|转自|转载自|引自)\s*"
        r"(?:" + source_org + r")"
        r"(?:\s*(?:消息|报道|通报|披露|介绍|通稿|发布|发布的消息|电))?"
    )
    source_bare = re.compile(r"(?:据|根据)\s*(?:报道|消息|通报|披露|介绍|外媒|港媒|台媒)\s*[，,：:，]?")

    # 豁免：法条 / 党内法规 / 制度名称——「依据《中国共产党纪律处分条例》…」是处分决定的
    # 法定依据（事件自身程序要素），不是交代来源。命中即整行跳过来源检测。
    law_exempt = re.compile(
        r"《[^》]{2,60}?(?:条例|法|规定|办法|准则|意见|决定|通知|规则|章程|司法解释)》"
    )
    # 豁免：来源主体实为法规/文书名时的引导语（据《…条例》/ 根据《…法》）
    law_lead_only = re.compile(r"^(?:据|根据|依据|按照)\s*《[^》]+》")
    source_act = re.compile(
        r"(?:通报|发文)\s*称"
        r"|(?:官方)?发布(?:渠道|平台)"
        r"|(?:通报|发布)(?:来源|渠道)"
        r"|消息来源\s*[：:]"
        r"|(?:通过|由|经)[^，。；]{0,25}(?:网站|官网|公众号|平台|渠道)[^，。；]{0,10}(?:发布|刊发|刊出|通报)"
        r"|(?:由|经|系由)[^，。；]{0,30}(?:纪委监委|纪检(?:监察)?组|监委|网站|官网|公众号)"
        r"[^，。；]{0,10}(?:发布|刊发|刊出|通报|披露)"
        r"|(?:原文|参考|资料|新闻|引用|来源)\s*(?:链接|地址|网址)?\s*[：:]"
        r"|(?:转自|转载自|引自)\s*\S"
        # 「机构主体 +（日期）+ 发布/刊发/通报」直接作主语的独立句式。旧版只认
        # 「通过/由/经 + 机构 + 发布」的介词引导式，凡机构名直接作主语即漏检
        # （「中央纪委国家监委网站 9 月 18 日发布消息」「某网站 9 月 18 日发布新规」）。
        # 两支口径均要求主体落到**来源载体特征词**上，以免误伤事件内容：
        #   - 支一（带日期）：日期是来源署时特征，主体只需是来源主体（含媒体名）；
        #   - 支二（无日期）：单独一个「X 发布 Y」太像事件本身（「该公司发布新品」），
        #     故主体必须带**载体后缀**（网站/官网/公众号/报社/通讯社/新闻社/新闻网），
        #     且动作词后须紧跟载体化宾语（消息/通报/通稿/公告/通知/报道）。
        #     「新闻报道中…」这类论述因主体是裸「新闻」+「报道」连读，不落载体后缀，放行。
        r"|(?:" + source_org + r"|" + _org_bare + r")"
        r"\s*" + _date_part + r"\s*"
        r"(?:发布|刊发|刊出|通报|披露|公布|发文)"
        r"|(?:" + _org_carrier + r")\s*"
        r"(?:发布|刊发|刊出|通报|披露|公布)\s*(?:消息|通报|通稿|公告|通知|报道)"
    )
    # 机构名直接作主语 + 报道/消息/通报 + 称/指出/披露：如「新华社报道称…」
    # （无「据/根据」引导的变体，source_lead / source_bare 覆盖不到，单列本项）
    source_subj = re.compile(
        r"(?:" + source_org + r")\s*"
        r"(?:报道|消息|通报|通稿|披露)"
        r"\s*(?:称|指出|提到|显示|披露|介绍|发布)?"
    )
    source_label = re.compile(
        r"^\s*(?:[-*~\u2022]\s*)?(?:\d{1,2}[.、)]\s*)?"
        r"(?:来源|报道|通报|发布|消息|出处|链接|作者|机构|参考|资料)"
        r"(?:主体|方|来源|渠道|机构|部门|单位|信息|时间)?\s*[：:]"
    )

    for i, ln in enumerate(lines[2:], start=3):  # 正文从第 3 行起（跳过标签段与概念名）
        s = ln.strip()
        if not s or "http" in s:
            continue
        hit = None
        for probe in (source_lead, source_bare, source_subj, source_act):
            m = probe.search(s)
            if not m:
                continue
            cand = m.group(0).strip()
            # 法规豁免：命中的引导语实际引导的是法条名（「据《中国共产党纪律处分条例》」），
            # 属处分决定的法定依据，不是交代来源；以及「依据《…》」式行整体跳过。
            tail_after_lead = re.sub(r"^(?:据|根据|援引|源自|转自|转载自|引自)\s*", "", cand)
            if law_exempt.fullmatch(tail_after_lead):
                continue
            hit = cand
            break
        if hit is None:
            m = source_label.match(s)
            if m:
                hit = m.group(0).strip()
        if hit:
            err(f"第 {i} 行含事件来源信息「{hit}」——卡片只陈述事件与概念本身，不署来源"
                f"（H15/H16：事件自身的报道方 / 通报方 / 发布方，与截图、讲义等材料载体同等对待，"
                f"一律不得写入正文，无豁免位置）。请删除该来源表述，事件内容本身保留；"
                f"来源性要点整条删除，禁止改写成句子形式并入正文变相保留")

    # 概念名称行（第 2 行）同样禁来源：捕获「XXX（江苏“风腐一体”通报）」
    # 「XXX（中央纪委国家监委通报）」这类把通报方写进概念名的形态（H16）。
    # 注意：flomo 云端回读的正文第 2 行常为空行（存储层自动插空行），概念名实际落在第 3 行；
    # 故这里取「标签段之后、首个非空行」作为概念名候选，兼容本地草稿（无空行）与云端（有空行）两种形态。
    if len(lines) > 1:
        name_idx = None
        for j in range(1, min(len(lines), 4)):
            if lines[j].strip():
                name_idx = j
                break
        if name_idx is not None:
            name_line = lines[name_idx].strip()
            nm = source_lead.search(name_line) or source_bare.search(name_line)
            if not nm:
                for grp in bracket_re.findall(name_line):
                    inner = grp.strip("（）()").strip()
                    if not inner:
                        continue
                    # 括号内是来源主体（中央纪委国家监委 / 济南纪委监委 …）
                    mm = re.search(source_org, inner)
                    if mm:
                        nm = mm
                        break
                    # 括号内以来源类词结尾（广西通报 / 江苏“风腐一体”通报 / 媒体披露 …）
                    # 概念名括号只应写概念补充说明，出现这类词即视为标注通报方/来源。
                    if re.search(r"(?:通报|报道|消息|披露|公布|发布|来源)$", inner):
                        nm = re.match(r".*", inner)
                        break
            if nm:
                err(f"第 {name_idx + 1} 行概念名称含来源/通报方「{nm.group(0).strip()}」——"
                    f"概念名只写概念或事件本身，禁止在括号或任何位置标注报道方 / 通报方（H16）。"
                    f"请删除括号内来源后只留概念名")

    # 5.7) 模板前缀回显检测（ERR）
    # 「结论先行：」是 SKILL H14 的条目名（写给执行者的格式指令），被原样回显即成为卡片内容；
    # 占位/载体/来源行检测无法覆盖这类「模板指令词回显」，故单列本项。
    #
    # 两层规则（从严设计，防误伤）：
    #   a) 「结论先行」四字连写：正文任意位置出现即 ERR。该词组是 SKILL 条目名，知识内容
    #      几乎不可能合法使用（确需表达时写「结论先于」「先给结论」）。无位置/标点豁免。
    #   b) SKILL 点名的首段行首前缀「一句话核心结论：」「一句话结论：」：ERR。
    #      仅匹配行首（允许 -/*/• 列表标记与 1. 序号前缀）+ 紧跟冒号的形态。
    #      注意「核心结论：」不带「一句话」时**不判错**——它是既有合规卡常见的要点小标题
    #      （如 #AI/训练规划 卡「核心结论：Paul Graham 称…」），判错会误伤历史卡。
    echo_prefix_re = re.compile(
        r"^\s*(?:[-*~\u2022]\s*)?(?:\d{1,2}[.、)]\s*)?"
        r"(?:一句话核心结论|一句话结论)\s*[：:]"
    )
    if "结论先行" in content:
        for i, ln in enumerate(lines[1:], start=2):
            if "结论先行" in ln:
                err(f"第 {i} 行正文含「结论先行」——这是 SKILL H14 的条目名（写给执行者的指令），"
                    f"不是卡片内容；结论必须直接作为正文首段第一句，不加任何引导词。"
                    f"请删除该字面前缀再写云")
    for i, ln in enumerate(lines[1:], start=2):
        m = echo_prefix_re.match(ln.strip())
        if m:
            err(f"第 {i} 行正文以模板前缀「{m.group(0).strip()}」开头——模板指令不得回显进卡片，"
                f"结论直接写成正文第一句即可")

    # 5.8) 概念名不得带**切片限定**（H6b）
    # 签名（首行标签 + 第二行概念名）是幂等查重与闸门凭证的共同依据。
    # 概念名一旦带上时点，同一对象就会因名字不同被切成平行卡：签名不等 → 幂等与查重
    # 对它同时失效。故概念名只命名「什么东西」，不命名「什么时候的它」。
    # 实测（2026-10 全库 1206 张）：带此类限定者238 张（19.7%），其中 103 张概念名
    # 本身简短却带限定（如「Hermes Agent v0.21.4 发布」「Claude Opus 5.5 发布」）。
    #
    # 误伤防护（这三条是本项能否进写的关键，逐一说明理由）：
    #   a) 4 位年份：只拦 19xx/20xx 形态的数字串。对象名里的数字（Qwen3、Grok 2.0、
    #      iPhone 17、MiMo-V2.6）不含 4 位年份，不受影响。
    #   b) 版本号：只拦紧跟字母/中文的语义化版本（v0.21.4、2.0 版、V2.1），
    #      且要求形如「v+数字(.数字)*」或「数字.数字 + 版」；纯数字型号（Transcribe 2.0）
    #      若无 v/版 字样不判——它可能是对象名的一部分。
    #   c) 发布动作：只拦**结尾**的「发布/上线/推送/推出/开源/发布」等动词。
    #      概念名中间出现这些词（如「开源模型」「发布计划」）不判。
    #   d) 相对时间词（最新/近期/当前）：概念名用它们必然随时间失效，一律 ERR。
    #   e) 序数（第七代/第五代/第六代）：代次是产品对象的稳定识别符，但对「同一对象
    #      的不同代」会切开签名。判WARN 而非 ERR：代次常是对象名必需部分
    #      （LPDDR6、第七代 TPU），不容易改写。
    #   f) 括号内的年份限定（如「双曲熵（Ramírez-Belman 等 2026）」）：判 ERR——
    #      来源型括号已在 5.6 判 ERR，这里补的是括号内纯时间限定（切片的一类）。
    if len(lines) > 1:
        cname_idx = None
        for j in range(1, min(len(lines), 4)):
            if lines[j].strip():
                cname_idx = j
                break
        if cname_idx is not None:
            cname = lines[cname_idx].strip()
            hits = []

            # 判据（H6b）：概念名须标识**对象本身**，不得标识对象的某个**切片**。
            # 「切片限定」是一类，不按类型分级——时间只是其中最常见的一种，
            # 与版本、代次、批次、地域同属「对象 + 切片」这一结构。
            # 早先版本把年份/季度判ERR 而代次只 WARN，那是**以时间为轴**：
            # 同属切片却待遇不同，且时间一被特判，判据就绑死在时间上，
            # 下一种切片（公测批次、轮次、地域变体）出现时规则即失效。
            # 现按统一口径：可机械识别的切片一律 ERR。
            #
            # 误伤防护（逐条给出理由，均经真实样本实测）：
            #  - 4 位年份只拦 19xx/20xx：对象名里的数字（Qwen3、Transcribe 2.0、iPhone 17）
            #    不含4 位年份，不受影响。
            #  - 语义化版本号只拦 v+数字(.数字)* 与「数字.数字 + 版」：纯数字型号若不带
            #    v/版 字样，可能是对象名的一部分（Transcribe 2.0、Grok 2.0），不判。
            #  - 代次序数与轮次同样 ERR（不再给代次开 WARN 例外）：它们同样是切片。
            #    若某对象名确实以代次为稳定识别（如 LPDDR6），该对象本身应以其产品线
            #    命名（"LPDDR 内存标准"），把代次写进要点。
            #  - 地域/语种切片只拦「中文版/英文版/欧洲版/国内版」这类**版本后缀形态**，
            #    不拦概念词本身含地名的（如"京东物流""北交所"——地名是对象的一部分）。

            # 切片 1：时间（年份、年月、年月日、季度、相对时间词）
            m_year = re.search(
                r"(?<!\d)(?:19|20)\d{2}"
                r"(?:\s*[-/年]\s*(?:\d{1,2}(?:\s*[-/月]\s*\d{1,2})?)?)?", cname)
            if m_year:
                hits.append(("年份/日期", m_year.group(0)))
            m_q = re.search(r"(?<![A-Za-z0-9])[Qq]\s?[1-4](?![0-9])", cname)
            if m_q:
                hits.append(("季度", m_q.group(0)))
            m_rel = re.search(
                r"最新|近期|当前|如今|今日|本日|本周|本月|今年|去年|目前|现阶段", cname)
            if m_rel:
                hits.append(("相对时间词", m_rel.group(0)))

            # 切片 2：版本（语义化版本号、RC/公测/beta/preview 阶段词）
            # 阶段词只拦「数字 + 阶段词」这一**可剥离的限定结构**（v2 / RC2 / Preview 3），
            # 不拦裸阶段词：`Step 5 Preview 大模型`（阶跃星辰的模型代号含 Preview）、
            # `Grok Voice Transcribe` 等形态里，阶段词是**对象名自身的一部分**。
            # 依据：切片限定能独立剥离而对象名不能——判据看结构，不看单词本身。
            m_ver = re.search(
                r"(?<![A-Za-z0-9])[vV]\s?\d+(?:\.\d+)+|"
                r"\d+(?:\.\d+)+\s*(?:版|版本)|"
                r"(?<![A-Za-z0-9])(?:RC|rc|Beta|beta|alpha|Alpha|preview|Preview)"
                r"\s*\d+(?![0-9])", cname)
            if m_ver:
                hits.append(("版本/阶段", m_ver.group(0).strip()))

            # 切片 3：代次 / 轮次 / 期次
            m_ord = re.search(
                r"第\s*[0-9一二三四五六七八九十]+\s*(?:代|版|阶段|期|轮|届|批|期数)", cname)
            if m_ord:
                hits.append(("代次/轮次", m_ord.group(0)))

            # 切片 4：地域 / 语种版本后缀（只拦明确的后缀形态）
            m_geo = re.search(
                r"(?<![A-Za-z0-9])(?:中文版|英文版|国内版|海外版|欧洲版|亚洲版|美版|日版|韩版)"
                r"(?![A-Za-z0-9])", cname)
            if m_geo:
                hits.append(("地域/语种版本", m_geo.group(0)))

            # 切片 5：事件动作（结尾动词——标识「某次动作」而非对象）
            m_act = re.search(
                r"(?:发布|上线|推送|推出|开源|释出|发布新版|举办|召开|夺冠|夺标|获奖|上线开源)\s*$",
                cname)
            if m_act:
                hits.append(("事件动作", m_act.group(0)))

            if hits:
                detail = "、".join(f"{k}「{v}」" for k, v in hits)
                err(f"第 {cname_idx + 1} 行概念名称含切片限定（{detail}）——"
                    f"概念名须标识**对象本身**，不标识对象的某个切片。"
                    f"时间、版本、代次、批次、轮次、地域语种、事件动作都是同一类切片，"
                    f"不是特例。取该对象的稳定指称作概念名，切片信息写进正文要点（H6b）")

    # 6) 预印本摘要依赖（WARN 提示，不阻塞写云）
    # 预印本卡须基于正文写作（SKILL 流程第 1 步「预印本正文优先」）：只抓 arXiv /abs/ 之类摘要页，
    # 要点会停留在摘要泛述。此处仅在正文出现"以摘要为唯一依据"的措辞时提示，回查是否漏抓正文；
    # 正文级陈述不会命中，故不阻塞写卡。
    abstract_dep = re.compile(
        r"(?:据|根据|依据|按|援引)\s*摘\s*要"
        r"|摘\s*要\s*(?:称|显示|指出|提到|中|里|仅|只|如下|表明|介绍)"
    )
    for i, ln in enumerate(lines[1:], start=2):
        s = ln.strip()
        if not s:
            continue
        hit = abstract_dep.search(s)
        if hit:
            warn(f"第 {i} 行出现以摘要为唯一依据的表述「{hit.group(0)}」——预印本卡须基于正文写作"
                 f"（见 SKILL 流程第 1 步「预印本正文优先」），请回查是否漏抓 /html/ 或 PDF 正文；"
                 f"若该句确为正文级陈述可忽略")


def _signature_of(content):
    """签名（委托 memo_util.memo_signature，返回 dict，与 sop_gate 同源）。"""
    return memo_signature(content)


def _no_gate_authorized(content):
    """凭证里是否带 sop_gate.py --allow-no-gate 的降级授权标记。

    授权与凭证同源（同签名、同正文指纹），因此不可被"另写一份凭证"绕过；
    凭证缺失/损坏时返回 False，走正常闸门校验路径（并报缺凭证）。
    """
    sig = _signature_of(content)
    if not sig:
        return False
    gate_path = GATE_DIR / f"{signature_key(sig)}.json"
    if not gate_path.exists():
        return False
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return False
    if gate.get("no_gate_authorized") is not True:
        return False
    # 授权必须锚在同一份正文上，防止"给 A 卡授权的凭证"拿来给 B 卡降级
    return gate.get("body_hash") == body_hash(content)


def check_gate(content):
    """流程闸门校验：确认 ②验证 / ④tag_tree 核对 / ⑤查重两路 / ⑧复盘三路 都已执行。

    这是"漏做 SOP 步骤"事故（卡写对了但流程没走）的机械兜底——
    此前 validate_memo.py 只查文本格式，对"步骤没做"完全无感。
    凭证由 scripts/sop_gate.py 生成，落在 .sop_gate/<sig_key>.json。
    无凭证 / 凭证过期 / 签名不匹配 / 术语验证缺失，一律判 ERR 阻断写云。
    """
    sig = _signature_of(content)
    if not sig:
        # 不得静默 return：签名取不到时凭证文件名无从计算，若放行即等于
        # 「结构不合规」顺便豁免了整套 SOP 闸门，形成静默绕过路径。
        err("卡片签名取不到（首行须为标签段、第二行须为不以 # 开头的概念名称），"
            "无法核对 SOP 流程闸门凭证——请先修正卡片结构")
        return
    sig_key = signature_key(sig)
    gate_path = GATE_DIR / f"{sig_key}.json"
    if not gate_path.exists():
        err(f"缺少 SOP 流程闸门凭证（{GATE_DIR.name}/{sig_key}.json）——"
            f"说明第 ②验证 / ④标签树核对 / ⑤查重两路 / ⑧复盘三路 至少一步没跑。"
            f"先执行 scripts/sop_gate.py 再写云（见 SKILL「流程」各阻塞项）")
        return
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        err(f"SOP 闸门凭证非法 JSON（{gate_path.name}）：{e}")
        return
    if gate.get("signature") is not None:
        gs = gate["signature"]
        if (gs.get("tagline"), gs.get("concept")) != (sig["tagline"], sig["concept"]):
            err(f"SOP 闸门凭证签名与当前卡片不符（凭证针对「{gs.get('concept')}」，"
                f"当前卡是「{sig['concept']}」）——凭证被复用或卡已改动，须重跑 sop_gate.py")
    # 正文指纹：杜绝"先跑闸门、后改正文"绕过（签名只锚卡片标识，锚不住正文改动）
    if gate.get("body_hash"):
        if gate["body_hash"] != body_hash(content):
            err("SOP 闸门凭证的正文指纹与当前卡片不符——闸门跑完后正文又被改过，"
                "须重跑 scripts/sop_gate.py 再写云")
    else:
        err("SOP 闸门凭证缺少正文指纹（body_hash）——凭证格式过旧，须重跑 sop_gate.py")
    exp = gate.get("expires_at")
    if isinstance(exp, int) and exp < int(time.time()):
        err("SOP 闸门凭证已过期——须重新执行 scripts/sop_gate.py 现查")
    web = gate.get("web") or {}
    if not web.get("web_skipped") and not web.get("searched"):
        err("SOP 闸门凭证显示第 ② 步验证（网络搜索）未执行——术语卡必须现查后再写云")


def _content_from_json(obj):
    """从 JSON 对象抽取 content，兼容 flomo_client 请求体与 JSON-RPC 信封。"""
    if not isinstance(obj, dict):
        return ""
    if isinstance(obj.get("content"), str):
        return obj["content"]
    args = (obj.get("params") or {}).get("arguments")
    if isinstance(args, dict) and isinstance(args.get("content"), str):
        return args["content"]
    args = obj.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("content"), str):
        return args["content"]
    return ""


def read_text(path):
    """读文件；utf-8-sig 去 BOM。若是 JSON（以 { 开头）则尝试抽取 content。

    注意：validate 与 flomo_client.py 必须共用同一份请求体文件——flomo_client.py
    以 `--file xx.json` 直接发送文件内容为 arguments，故此处必须优先识别顶层 content，
    否则会出现「校验通过的是空串、发送的是正文」或反之的错位。
    """
    raw = Path(path).read_text(encoding="utf-8-sig")
    if raw.lstrip().startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        c = _content_from_json(obj)
        if c:
            return c
    return raw


def load_content(argv):
    """从参数列表取卡片正文。

    兼容两种入参：`main()` 传的是已剥掉脚本名的参数（`["--file", p]`），
    回归测试传的是完整 `sys.argv`（`["validate_memo.py", "--file", p]`）。

    判据曾用「首元素是否存在的文件路径」，但该判据在 CWD 恰为 `scripts/`
    时会失效——`Path("validate_memo.py").exists()` 为真，脚本名被当成输入文件，
    于是把整个脚本源码读成了卡片正文（回归测试从 `scripts/` 目录运行时必现）。
    改为按**脚本名后缀**判定：首元素不以 `-` 开头且以 `.py` 结尾即为脚本名。
    """
    if not argv:
        raise IndexError("缺少输入路径")
    if not argv[0].startswith("-") and argv[0].endswith(".py"):
        argv = argv[1:]
    if not argv:
        raise IndexError("缺少输入路径")
    arg = argv[0]
    if arg in ("--create", "--file", "--content"):
        if len(argv) < 2:
            raise IndexError(f"{arg} 需要一个参数（文件路径或正文内容）")
        return read_text(argv[1]) if arg != "--content" else argv[1]
    return read_text(arg)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    # ERR/WARN 是模块级列表：主流程入口统一清空，避免同进程多次调用时
    # 上一轮的判错污染本轮退出码（测试里连着调 main() 会踩到）。
    ERR.clear()
    WARN.clear()
    argv = sys.argv[1:]
    no_gate = False
    if "--no-gate" in argv:
        no_gate = True
        argv = [a for a in argv if a != "--no-gate"]
    try:
        content = load_content(argv)
    except (IndexError, FileNotFoundError, json.JSONDecodeError) as e:
        sys.stderr.write(f"读取输入失败：{e}\n")
        return 2
    check(content)
    # 流程闸门降级（--no-gate）不再是无条件开关：须凭证里带 sop_gate.py --allow-no-gate 的
    # 授权标记才生效，否则照常判 ERR。避免"谁加个参数谁就绕过整套 SOP"。
    if no_gate and _no_gate_authorized(content):
        _gate_err, _gate_warn = ERR[:], WARN[:]
        ERR.clear(); WARN.clear()
        check_gate(content)
        for m in ERR:
            warn(f"（闸门·未阻断）{m}")
        ERR[:] = _gate_err
        WARN[:] = _gate_warn + WARN
    else:
        if no_gate:
            err("使用了 --no-gate 但该项降级未经授权——凭证中缺少 sop_gate.py "
                "--allow-no-gate 的授权标记。降级仅限批量处理历史卡，且须由 sop_gate.py "
                "携理由生成凭证；常规写卡请直接跑 sop_gate.py 走完整闸门")
        check_gate(content)
    # 去重：同一问题可能被多条规则命中，重复提示无信息增量（dict 保序）
    for m in dict.fromkeys(ERR):
        print("ERR :", m)
    for m in dict.fromkeys(WARN):
        print("WARN:", m)
    print(f"结果：{len(ERR)} 错 / {len(WARN)} 警，合计 {len(ERR) + len(WARN)} 条提示")
    return 1 if ERR else 0


if __name__ == "__main__":
    sys.exit(main())
