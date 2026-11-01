#!/usr/bin/env python3
"""技能文档内容纪律自检（H28 落地；离线、只读、无第三方依赖）。

扫描技能文档（技能目录下的 SKILL.md / AGENTS.md / ENVIRONMENT.md / README.md
与 `scripts/`、`tests/` 下的全部 .py/.sh，以及 --root 下的文档与脚本），检测规则
文本里混入的「具体日期」与「历史事件叙事」——二者一律只作抽象陈述。

  ERR  具体日期：YYYY-MM-DD、YYYY/M/D、YYYY.M.D、YYYY年M月[D日]
  ERR  README 漏列脚本：`scripts/` 下每个 .py 都必须在 README 出现
  ERR  用例命名：`tests/` 下的 .py 必须以 test_ 开头，否则 run_tests.sh 不执行它
  ERR  内部交叉引用失效：引用的 H 编号 / 附录小节 / 流程步骤 / 脚本名并不存在
  ERR  命令行选项失效：文档里 `scripts/x.py --flag` 的 `--flag` 不是该脚本认识的选项
  ERR  用例不声称覆盖：每个 `scripts/*.py` 都要出现在某个用例的 `COVERS` 里，
       且每个用例都必须有 `COVERS` 声明（可用空元组表示不针对具体脚本）
  ERR  SKILL.md 超长：总字符数超过 MAX_SKILL_CHARS（技能文件每轮载入，须有上限）
  ERR  操作要素丢失：H 条目 / 流程步骤里登记的记号（脚本、选项、函数名、硬数字）
       在文档中找不到——改写换说法可以，把记号改没了读者就照做不了
  WARN 历史事件叙事：曾经 / 当时 / 当天 / 事后发现 / 上次推送 等回溯措辞

后七条都是**跨文件结构检查**：逐行扫一遍能查措辞，查不出「入口文档漏掉一整个
脚本」。README 是项目入口，清单缺项只表现为「照着 README 找不到这一步」——
不会报错，直到有人手跑一遍才发现少了 publish.py 这类主干脚本。

用例命名同理：`run_tests.sh` 只跑 `tests/test_*.py`，名字写偏的用例永远不执行，
而它「没有失败」，看起来一切正常。

交叉引用同理且更隐蔽：SKILL.md 靠编号互相引用（「见 H11」「见附录 · 标签树现采」
「第 5 步」），删条目、改标题、换脚本名之后，**引用它还留在原处读起来完全通顺**，
只是指不到任何东西。读者按图索骥找不到，不会有人为此报错——所以要让指不到
这件事本身变成 ERR。

选项名又比脚本名低一层：脚本名写错会报「文件不存在」，**选项名写错只会被当成
未知参数**，打一行用法就退出，读者以为是环境问题；而参数改名恰恰是改脚本时最
容易顺手带上的漂移。

脚本与用例的绑定还要再上一层：README 只说得出「有这个脚本」，说不出「谁验证它」。
此前靠「用例文件与脚本同名」推断，而这条约定在本仓库已多处不成立——按名字猜会
把有覆盖的报成没覆盖、把没覆盖的报成有覆盖，两个方向都错，且都是静默的。故覆盖
关系改由用例自己声明（`COVERS`），检查核三件事：每个脚本有人认领、声明指向的
脚本真实存在、声明与文件里确实提到它。**它拦的是「完全没声明 / 声明指空 / 声明
与文件无关」，不冒充覆盖率工具**——断言写得够不够，只能人读。

**任何技能文档都不豁免日期**（H28 是严格禁止，无例外）：ENVIRONMENT.md 记录
环境事实时只写「是什么」——IP、端口、路径、命令，不写「哪天发生的」。

唯一豁免：
  - 技术常量行：命中日期的那一行含 version / protocol 键名时，该日期属协议或
    版本常量（如 MCP protocolVersion），非文档叙事日期，跳过并打印 SKIP。

用法：
  python scripts/check_skill_docs.py [--root DIR] [--quiet]
退出码：0=通过；1=存在 ERR（阻断）；2=用法/路径错误。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import covers
from cleanup import SCAN as _CLEANUP_SCAN

# 日期形态。用 \d 书写，脚本自身不含真实日期字面量，故不会自命中。
# 月 / 日分支一律「两位在前」（1[0-2] 先于 0?[1-9]、3[01]/[12]\d 先于 0?[1-9]），
# 否则交替优先会让 '26' 只匹配到 '2'，命中结果被截短。
DATE_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*[-/.年]\s*(?:1[0-2]|0?[1-9])\s*[-/.月]\s*(?:3[01]|[12]\d|0?[1-9])\s*日?"
    r"|(?<!\d)(?:19|20)\d{2}\s*年\s*(?:1[0-2]|0?[1-9])\s*月"
)

# 历史事件叙事：收窄集合，只收「回溯某次已发生的事」的措辞。
# 刻意不含流程时序术语（当轮 / 本轮 / 每轮）与「实测」——它们在规则文本中合法。
NARRATIVE_RE = re.compile(
    r"曾经|曾因|曾被|当时|当天|那天"
    r"|事后(?:发现|复盘|追查)|回溯(?:发现|检索)"
    r"|上一?次(?:推送|提交|出错|失败|事故)|经验教训|以往的?教训"
)

# 技术常量行豁免：同行含 version / protocol 键名时，其中的日期不是文档叙事日期。
CONST_LINE_RE = re.compile(r"(?i)version|protocol")

# 可照抄的环境地址：IP / 回环主机带端口 / 通配绑定。
# 归属见 AGENTS.md 的文档分工——「随环境变化的细节」只写 ENVIRONMENT.md，SKILL.md
# 只讲流程。判据取**正面形态**（能直接复制到命令行执行的那种串），不做
# 「这段话算不算环境内容」的语义判断：后者没有机械判据，也就等于没有检查。
# 实测命中率：SKILL.md 为 0，故本规则零假报；`curl -x <端口>` 这类占位写法不命中。
ENV_VALUE_RE = re.compile(r"\b127\.0\.0\.1\b|\blocalhost:\d+|\b0\.0\.0\.0\b")

# 只对唯一细则源生效：其余文档（README 讲脚本、ENVIRONMENT 讲环境）出现地址是本职。
ENV_VALUE_TARGET = "SKILL.md"

# ---- 交叉引用（技能文档之间靠编号互指，删改之后引用会静默悬空）----
# H 编号引用：H1 / H6b / H24。前后不得紧邻字母数字，否则 HDR1、H264 之类会误命中。
H_REF_RE = re.compile(r"(?<![A-Za-z0-9])H(\d{1,2}[a-z]?)(?![0-9a-z])")
# H 编号定义：标题行形如 `**H11 · 合并后必须回读全文验收。**` / `**H26（仓库分工部分）· …**`
H_DEF_RE = re.compile(r"\*\*(H\d+[a-z]?)\s*[·（(]")
# 附录小节引用：只认同「附录」之间有分隔符的形态（间隔号或引号）。
# 「附录结论」「附录表」这类是正文里的普通词，不构成对本文档小节的引用。
ANNEX_REF_TRIGGER = re.compile(r"附录\s*(?:末\s*)?(?:[·・]\s*[「『“]?|[「『“])")
ANNEX_REF_STOP = "」』”\"，。、；：）)\n*`"
# 流程步骤引用：第 5 步 / 第 ⑤ 步。只认带「步」字的形态——「第 ⑥ 段」说的是
# publish 的输出段，不是流程步骤，不该被它约束。
STEP_REF_RE = re.compile(r"第\s*([0-9]{1,2}|[①②③④⑤⑥⑦⑧⑨])\s*步")
STEP_TITLE_RE = re.compile(r"^###\s+([1-9][0-9]?)\.", re.M)
CIRCLED_STEPS = {c: str(i) for i, c in enumerate("①②③④⑤⑥⑦⑧⑨", start=1)}
# 脚本引用：**文档里**写的 scripts/x.py / tests/x.py 应当真的存在，否则照着跑会报
# 「文件不存在」。目录名一并捕获、各自到对应目录核——工具与用例分属两层，只认
# scripts/ 会让「用例搬走后文档还写着 scripts/test_x.py」这类悬空引用静默通过。
# 只管 .md：脚本注释与回归夹具里的 `scripts/x.py` 是临时目录里的相对路径或占位名，
# 按仓库文件核它们会成片误报（夹具写的本来就是不存在的样本）。
SCRIPT_REF_RE = re.compile(r"(scripts|tests)/([A-Za-z0-9_]+\.(?:py|sh))")

# README 表格首列（清单项本体）与其中的文件名。与 SCRIPT_REF_RE 的分工：那个管
# 「带目录前缀的行文引用」，这两个管「清单表里点名的裸文件名」——README 的脚本表
# 首列写的是 `git_tunnel.py` 而非 `scripts/git_tunnel.py`，故 SCRIPT_REF_RE 整表
# 绕过去，删脚本留下的陈旧行得由这一对兜住（check_script_inventory ②）。
README_ROW_RE = re.compile(r"^\|([^|\n]+)\|")
README_ITEM_RE = re.compile(r"[A-Za-z0-9_]+\.(?:py|sh)")

# ---- 文档里的命令行 ↔ 脚本真实认识的选项 ----
# 只认**片段开头**的命令（`python scripts/x.py …` / `x.py …`）。
# 不按「离哪个脚本名最近」归属：自然语言句子里「…见 scripts/sop_gate.py…，
# `validate_memo.py --no-gate` 才生效」会把 --no-gate 算到 sop_gate 头上（实测 2 条假报），
# 而脚本名在句中的位置与参数归属没有稳定关系。片段开头则是无歧义的命令形态。
CLI_CMD_RE = re.compile(
    r"^(?:python\s+|bash\s+)?(?:(scripts|tests)/)?([a-z_]+\.py)\b(.*)$")
CLI_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9_-]*)")
# 各脚本的解析风格不同，四种都要覆盖：argparse 的 add_argument、手写的 `a == "--x"`、
# `arg in ("--a", "--b")` 元组、以及 `"--x" in argv`。只认一种会让其余脚本的选项集合
# 取成空集，检查随之静默失效。
_OPT_PATTERNS = (
    r'add_argument\(\s*"(--[a-z0-9-]+)"',
    r'add_argument\(\s*"[^"]*",\s*"(--[a-z0-9-]+)"',
    # 声明式解析（`Opt("--draft", "draft", …)`）：脚本不再逐个写 `== "--x"` 分支，
    # 选项只在这张声明表里出现一次。采不到它，四个改用声明表的脚本会被判成
    # 「文档写了个它不认识的选项」——检查没错，是采集口径落后于实现。
    r'Opt\(\s*"(--[a-z0-9-]+)"',
    r'==\s*"(--[a-z0-9-]+)"',
    r'!=\s*"(--[a-z0-9-]+)"',
    r'"(--[a-z0-9-]+)"\s+(?:not\s+)?in\s',
)
_OPT_TUPLE_RE = re.compile(r"(?:in|==)\s*\(([^)]*)\)")

# 规则实现体豁免：本脚本的叙事词表与回归测试夹具必然逐字包含被检测的词面，
# 属模式定义而非叙事，故对这两个文件只停用叙事扫描（日期扫描照常生效），并打印 SKIP 保持可见。
NARRATIVE_SELF_EXEMPT = {"check_skill_docs.py", "test_check_skill_docs.py"}

DOC_SUFFIXES = {".md", ".py", ".sh"}
SCRIPT_SUFFIXES = {".py", ".sh"}

# SKILL.md 总字数上限。取总字符数（含标点空白）是最严口径，换算法也不会失守。
MAX_SKILL_CHARS = 10000

# 各条目的「操作要素」锚点。只登记**可执行的记号**（脚本名 / 选项名 / 函数名 /
# 键名 / 硬数字），不登记论据句——论据可以重写，锚住它只会逼人改锚点表。
# 判断标准：这一项删掉之后，读者是否还能照做？不能，就该进这张表。
SKILL_ANCHORS = {
    "H1": ("content_truncated", "memo_batch_get"),
    "H2": ("word_count",),
    "H3": ("flomo_client.py", "dedup.py"),
    "H4": ("structuredContent",),
    "H5": ("payload",),
    "H6": ("首行标签", "转义"),
    # 「换个时点」是概念名判据的核心自查：删了它，读者只剩反例可背，没有判法。
    "H6b": ("删动词测试", "换个时点",),
    "H6c": ("退出前必须查重",),
    "H6d": ("_predicate_hits",),
    # 批量轮次里「整批查过一次」被当成逐对象查重，直接造成重复建卡事故。
    "H6e": ("禁以「本批查过一次」代替", "整簇翻页"),
    "H6f": ("产品线卡",),
    "H7": ("进程卡", "盘点"),
    "H8": ("不构成同一事件",),
    "H9": ("覆盖式重组",),
    "H10": ("判例",),
    "H11": ("逐条认领",),
    "H12": ("linked_memos",),
    "H13": ("营销",),
    "H14": ("第二行概念名",),
    "H15": ("参考链接",),
    "H16": ("这张卡从什么载体上抄下来",),
    "H17": ("#顶层/二级",),
    "H18": ("tag_tree",),
    "H19": ("定理",),
    "H20": ("重排",),
    "H21": ("伤亡",),
    "H22": ("memo_update",),
    "H22b": ("全库普查",),
    "H23": ("retire.py", "#待清理", "--ids"),
    "H24": ("当次会话",),
    "H25": ("push_skill.sh", "ls-remote"),
    "H26": (".mcp.json",),
    "H27": ("cleanup.py", ".cleanup_run"),
    "H28": ("check_skill_docs.py", "SKIP"),
}
# 流程各步的命令与选项：步骤标题丢了不会有人发现（下一步照样能读通），
# 但「第 8 步的闸门是哪个脚本」这种信息只在这里出现一次。
STEP_ANCHORS = {
    "1": ("fetch_article.py",),
    "2": ("verify.json", "--skip-reason"),
    "4": ("tag_tree", "tagcheck.py"),
    "5": ("--keywords", "--ids"),
    "6": ("exit_confirm.py", "--search-log"),
    "3": (),  # 只有顺序判据（逐对象走 4-9 步），无可执行记号可锚
    "7": (),  # 只有格式判据（按 H14 写），无可执行记号可锚
    "8": ("publish.py", "--draft", "sop_gate.py"),
    "9": ("memo_create",),
    "10": ("--anchor-id", "--show-body", "max_memos", "tag_rename"),
}
# 既不属某条 H、也不属某一步的散记号（附录里的替代写法与实现出处）。
GLOBAL_ANCHORS = ("a(at)b.c", "_SLICE_RE", '{"limit":2000}')
# 任何技能文档都不豁免日期扫描——H28 是严格禁止，无例外。
# ENVIRONMENT.md 同样受日期（ERR）与叙事词（WARN）双重约束：
# 它记录环境事实时只写「是什么」（IP、端口、路径），不写「哪天发生的"。
EXEMPT_NAMES: set[str] = set()


def _transient_patterns():
    """项目根下的**瞬时产物** glob，取自 `cleanup.SCAN`（唯一实现，不另写一份）。

    这些是抓取件 / 写卡草稿 / 请求回执：按 H24 只在近轮窗口内存活、由 cleanup
    按轮回收，既不入库、也不是我们写下的文档。H28 管的是「技能文档禁写日期」，
    拿抓来的**别人写的**原文（含发布日期、变更日志）当违规报出，是在放一条
    并不存在的违规——它不指任何人，却会让人以为仓库文档违规，真实文档的违规
    反而被这条噪声淹没。故按产物属性排除，而非按文件名开后门。
    """
    out = []
    for base, pats in _CLEANUP_SCAN:
        if Path(base) == Path(__file__).resolve().parent.parent:
            out.extend(pats)
    return tuple(out)


TRANSIENT_PATTERNS = _transient_patterns()


def is_transient(name: str) -> bool:
    """文件名是否属项目根下的瞬时产物（抓取件 / 草稿 / 回执）。"""
    from fnmatch import fnmatch
    return any(fnmatch(name, pat) for pat in TRANSIENT_PATTERNS)


def iter_targets(root: Path):
    """产出待扫描文件：**技能根全量文档** + 仓库根与 scripts 目录下的脚本。

    技能根 = 本脚本所在目录的上一级（`scripts/` 的同级），**按脚本位置推导**，
    不依赖仓库目录名约定（历史教训：曾硬编码 `.kilo/skills/flomo-note`，
    而实际技能目录名不同，导致 SKILL.md/AGENTS.md/README.md 全部漏检，
    H28 自检"0 错"实为假通过）。
    若 `--root` 指向的目录本身即技能根（含 scripts/），同样能正确覆盖。
    项目根下的瞬时产物（`_tmp_*` / `memo_body*.txt`，见 `TRANSIENT_PATTERNS`）
    一律不扫：它们是抓取件与写卡草稿，不是我们写下的文档，拿其中别人写的
    日期当 H28 违规报出属于报假警。
    """
    seen = set()

    def fresh(p: Path) -> bool:
        if not p.is_file() or "__pycache__" in p.parts:
            return False
        if is_transient(p.name):
            return False
        rp = p.resolve()
        if rp in seen:
            return False
        seen.add(rp)
        return True

    # 技能根：脚本自身位置的上一级，与 --root 取并集（两者可能是同一目录）
    skill_roots = {Path(__file__).resolve().parent.parent}
    if root.is_dir():
        skill_roots.add(root)
    for sk in sorted(skill_roots):
        for p in sorted(sk.glob("*")):
            if p.suffix.lower() in DOC_SUFFIXES and p.name not in EXEMPT_NAMES and fresh(p):
                yield p

    # --root 顶层：文档与脚本都要收。此前这里只按 SCRIPT_SUFFIXES 收集，
    # 导致 --root 指向外部目录时其顶层 .md 全部漏检（只扫到该目录的脚本）。
    for p in sorted(root.glob("*")):
        if p.suffix.lower() in DOC_SUFFIXES and p.name not in EXEMPT_NAMES:
            if fresh(p):
                yield p
        elif p.suffix.lower() in SCRIPT_SUFFIXES and fresh(p):
            yield p
    # scripts/ 与 tests/ 分开收：前者是工具，后者是回归用例。两者都算「我们写下的
    # 代码」，日期与叙事口径一致；漏收 tests/ 会让用例里的话绕开内容纪律。
    for sub in ("scripts", "tests"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in SCRIPT_SUFFIXES and fresh(p):
                yield p


def scan_file(path: Path, root: Path):
    """扫描单个文件，返回 (errs, warns, exempts)。"""
    errs: list[str] = []
    warns: list[str] = []
    exempts: list[str] = []
    try:
        relp = path.relative_to(root).as_posix()
    except ValueError:
        relp = path.name
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as e:
        errs.append(f"{relp}: 读取失败 {e}")
        return errs, warns, exempts

    scan_narrative = path.name not in NARRATIVE_SELF_EXEMPT
    if not scan_narrative:
        exempts.append(f"{relp}: 规则实现体，跳过叙事扫描（日期扫描照常）")
    scan_env_value = path.name == ENV_VALUE_TARGET

    for i, ln in enumerate(text.splitlines(), start=1):
        const_line = bool(CONST_LINE_RE.search(ln))
        if scan_env_value:
            for m in ENV_VALUE_RE.finditer(ln):
                errs.append(
                    f"{relp}:{i}: 出现可照抄的环境地址「{m.group(0)}」——IP/端口属环境事实，"
                    f"写 `ENVIRONMENT.md`（文档分工见 `AGENTS.md`）；本文件只讲流程"
                )
        for m in DATE_RE.finditer(ln):
            hit = m.group(0).strip()
            if const_line:
                exempts.append(f"{relp}:{i}: 日期 {hit} 属协议/版本常量，豁免")
                continue
            errs.append(
                f"{relp}:{i}: 具体日期「{hit}」——技能文档禁写日期（H28）；"
                f"理由只作抽象陈述，环境事实只写「是什么」不写「哪天」"
            )
        if not scan_narrative:
            continue
        for m in NARRATIVE_RE.finditer(ln):
            warns.append(
                f"{relp}:{i}: 历史事件叙事「{m.group(0)}」——技能文档禁写具体事件经过（H28）；"
                f"改为抽象陈述（当轮 / 本轮 等流程时序词不受此限）"
            )
    return errs, warns, exempts


def check_script_inventory(root: Path) -> list[str]:
    """三层清单完整性：README 与 `scripts/` 双向核对，`tests/` 下不得有跑不到的用例。

    ① `scripts/` 下每个 .py 都必须在 README 出现一次。判据取正面定义——**全部列出**，
    不设「工具类可以不写」的豁免。留一个口子，下一个脚本就会落在口子里；而缺项只
    表现为「照 README 找不到这一步」，没人会为此报错，直到有人真去手跑才发现少了
    个主干脚本。实测漂移过：`publish.py`、`dedup.py`、`tagcheck.py`、
    `fetch_article.py` 与两个 scan_* 都不在 README，其中前三个是每轮必用的流程脚本。
    用例已全部移入 `tests/`，故这里不再需要 `test_` 豁免——**不可达的分支要删掉**，
    留着它只是让下一个人以为测试还在 scripts/。

    ② 反方向：README 清单里点名的脚本必须真存在。① 只保证「有的都写了」，保证不了
    「写了的都还在」——脚本删掉或改名后，那一行留在表里，读起来完全通顺，只是照着
    一跑就「文件不存在」。这一侧此前没有任何检查覆盖：`check_cross_refs` 的
    `SCRIPT_REF_RE` 要求带 `scripts/` 前缀，而清单表首列是裸文件名，故整表绕过了它。

    ③ `tests/` 下的 .py 必须以 `test_` 开头。`run_tests.sh` 按 `tests/test_*.py`
    glob 收集，名字不符就不会被跑：用例写得再好，也只是一份没人执行的文件，
    而且不报任何错——静默失效比红更坏。
    """
    errs: list[str] = []
    readme = root / "README.md"
    scripts = root / "scripts"
    tests = root / "tests"
    if readme.exists() and scripts.is_dir():
        try:
            text = readme.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as e:
            return [f"README.md: 读取失败 {e}"]
        errs += [
            f"README.md: 未列出 scripts/{n}——`scripts/` 下每个脚本都须在 README "
            f"出现一次（清单缺项等于让人不知道有这一步）"
            for n in sorted(p.name for p in scripts.glob("*.py") if p.name not in text)
        ]
        # 反方向：只核**表格首列**。说明列里出现的脚本名是行文引用（实测 20 余处），
        # 拿它当清单项会把「提到」误判成「列入」；首列才是清单本身。
        # glob 行（`tests/test_*.py`）正则天然匹配不到，故不需要额外的豁免分支。
        have_all: set[str] = set()
        for d in ("scripts", "tests"):
            dirp = root / d
            if dirp.is_dir():
                have_all |= {p.name for p in dirp.glob("*") if p.is_file()}
        for i, ln in enumerate(text.splitlines(), start=1):
            cell = README_ROW_RE.match(ln)
            if not cell:
                continue
            for name in README_ITEM_RE.findall(cell.group(1)):
                if name not in have_all:
                    errs.append(
                        f"README.md:{i}: 清单点名「{name}」在 scripts/ 与 tests/ 下都"
                        f"不存在——脚本已删或改名？行还留着，照着一跑就是「文件不存在」"
                    )
    if tests.is_dir():
        errs += [
            f"tests/{p.name}: 不以 test_ 开头，`run_tests.sh` 不会执行它"
            f"（用例静默失效，比跑红更难发现）"
            for p in sorted(tests.glob("*.py"))
            if not p.name.startswith("test_")
        ]
    return errs


def check_cross_refs(root: Path) -> list[str]:
    """技能文档内部的交叉引用必须指得到东西。

    四类引用各自有唯一事实源：H 编号与流程步骤在 `SKILL.md` 的标题/定义行，
    附录小节在 `SKILL.md` 的 `###` 标题，脚本在 `scripts/` 的真实文件。**只从
    事实源现采一次**，不维护第二份清单——另写一份清单就又多一个漂移点。

    判据取正面定义：**引用即须命中**，不设「历史小节可以留空指」这类豁免。
    豁免面每加一处，下一处失效就落在豁免里；而失效的引用读起来完全通顺
    （「见附录 · 参数速查」），没有任何东西会因此报错，只会让人找不到。
    """
    skill = root / "SKILL.md"
    if not skill.is_file():
        return []  # --root 指向无 SKILL.md 的目录时，没有事实源可核，不臆断
    try:
        stext = skill.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as e:
        return [f"SKILL.md: 读取失败 {e}"]

    hs = set(H_DEF_RE.findall(stext))
    # 附录小节标题**只从附录区取**：拿全文标题当候选，会让「附录 · 查重」命中正文里
    # 「### 5. 查重（阻塞）」，看似通过实则附录里根本没有那节。
    annex_at = re.search(r"^##\s+[^\n]*附录[^\n]*$", stext, re.M)
    annex_text = stext[annex_at.start():] if annex_at else ""
    heads = [h.strip() for h in re.findall(r"^###\s+(.+?)\s*$", annex_text, re.M)]
    steps = set(STEP_TITLE_RE.findall(stext))
    have = {
        d: {p.name for p in (root / d).glob("*") if p.is_file()}
        for d in ("scripts", "tests")
    }

    def annex_hit(name: str) -> bool:
        # 标题常带补充说明（「标签树现采（与 H15 互补：…）」），故按包含双向判，
        # 只要引用的名字能唯一定位到一个标题即可。
        return any(name in h or h.startswith(name) for h in heads)

    errs: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    def note(relp: str, lineno: int, kind: str, name: str, hint: str) -> None:
        key = (relp, kind, name)
        if key in seen:
            return
        seen.add(key)
        errs.append(f"{relp}:{lineno}: {kind}「{name}」指向不存在的目标——{hint}")

    for p in sorted(root.glob("*.md")):
        if is_transient(p.name):
            continue  # 抓取件里别人写的引用不归我们维护
        try:
            text = p.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue  # 读取失败已在 scan_file 里报过，不重复
        relp = p.name

        for i, ln in enumerate(text.splitlines(), start=1):
            for m in SCRIPT_REF_RE.finditer(ln):
                d, name = m.group(1), m.group(2)
                if name not in have[d]:
                    note(relp, i, "脚本", name,
                         f"`{d}/` 下没有这个文件，照着跑只会报文件不存在")
            for m in H_REF_RE.finditer(ln):
                ref = "H" + m.group(1)
                if ref not in hs:
                    note(relp, i, "硬限编号", ref,
                         "`SKILL.md` 第二节里没有该条的定义，引用已悬空")
            for m in STEP_REF_RE.finditer(ln):
                n = CIRCLED_STEPS.get(m.group(1), m.group(1))
                if n not in steps:
                    note(relp, i, "流程步骤", f"第 {n} 步",
                         "`SKILL.md` 第三节没有该编号的步骤标题")
            for m in ANNEX_REF_TRIGGER.finditer(ln):
                rest = ln[m.end():m.end() + 60]
                cut = min([rest.find(c) for c in ANNEX_REF_STOP if c in rest] or [len(rest)])
                name = rest[:cut].strip()
                if name and not annex_hit(name):
                    note(relp, i, "附录小节", name,
                         "`SKILL.md` 附录里没有这个标题，按名字找不到那段")
    return errs


def script_options(path: Path) -> set[str]:
    """从脚本源码里采出它认识的 `--xxx` 选项，不维护第二份清单。

    采集口径跟着实现的写法走：argparse、逐个 `== "--x"` 分支、元组成员、以及声明式的
    `Opt("--x", …)` 都要认。**实现换了写法而这里没跟上，症状是「检查报错、脚本没错」**
    ——四个脚本改用声明表后，这里的九条「不是它认识的选项」全是假报，而假报会教人
    把这个检查整体忽略，比没有检查更坏。

    宁可漏采（取不到就整段跳过）也不虚采：虚采会让检查放行一个脚本根本不认的参数，
    那比没有检查更坏。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return set()
    opts: set[str] = set()
    for pat in _OPT_PATTERNS:
        opts |= set(re.findall(pat, text))
    for grp in _OPT_TUPLE_RE.findall(text):
        opts |= set(re.findall(r'"(--[a-z0-9-]+)"', grp))
    return opts


def check_doc_cli_flags(root: Path) -> list[str]:
    """文档里写的命令行，选项必须是该脚本真认识的。

    与「脚本名存在」是同一件事的两层：名字写错会报「文件不存在」，**选项名写错只会
    被当成未知参数**——脚本自己可能只打印一行用法就退出，读者以为是环境问题；
    而 `--update-id` 改成 `--update_id` 这种漂移，改脚本时最容易顺手改掉，文档那头
    没有任何东西会跟着红。实测文档里这类引用有 29 处。

    只处理**片段开头即命令**的形态（行内反引号、代码块整行），漏掉跨句的自然语言
    描述是可以接受的——判据要求「查到的必须对」，不要求「全部都能查到」。
    """
    if not (root / "scripts").is_dir():
        return []  # 事实源是 scripts/ 里的源码；没有它就没有可核的选项集合
    errs: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    cache: dict[str, set[str]] = {}

    for p in sorted(root.glob("*.md")):
        if is_transient(p.name):
            continue
        try:
            text = p.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        in_fence = False
        for i, ln in enumerate(text.splitlines(), start=1):
            if ln.strip().startswith("```"):
                in_fence = not in_fence
                continue
            segs = re.findall(r"`([^`]+)`", ln) or ([ln] if in_fence else [])
            for seg in segs:
                m = CLI_CMD_RE.match(seg.strip())
                if not m:
                    continue
                name, rest = m.group(2), m.group(3)
                sub = m.group(1) or "scripts"  # 不带目录前缀时按主工具目录解析
                script = root / sub / name
                if not script.is_file():
                    continue  # 脚本不存在由交叉引用检查报，这里不重复
                ckey = f"{sub}/{name}"
                if ckey not in cache:
                    cache[ckey] = script_options(script)
                allowed = cache[ckey]
                if not allowed:
                    continue  # 采不到选项（换了不认识的解析风格）时整段跳过，不臆断
                for flag in CLI_FLAG_RE.findall(rest):
                    if flag in allowed:
                        continue
                    key = (p.name, name, flag)
                    if key in seen:
                        continue
                    seen.add(key)
                    errs.append(
                        f"{p.name}:{i}: 参数「{flag}」不是 {name} 认识的选项——"
                        f"照着这行跑会被当成未知参数；改脚本选项时须同步改文档"
                    )
    return errs


def check_test_coverage(root: Path) -> list[str]:
    """每个脚本都要有能指名道姓的用例，每个用例都要声明它验的是谁。

    起因是可扩展性而非洁癖：`scripts/` 加一个文件，除了 README 那一行（`check_script_inventory`
    已管），没有任何东西会提醒「它还没有回归覆盖」。结果是仓库越大，无覆盖面积越大，
    而全量用例依旧全绿——绿的只是被写到的那几条路径。控制台页面上 `has_test` 会显示
    False，但那是给人看的字段，不是闸门。

    判据取正面定义，四个方向都拦：
      ① 每个 `scripts/*.py` 至少被一个用例的 `COVERS` 提到——新增脚本不写用例即红；
      ② `COVERS` 里的名字必须在 `scripts/` 真实存在——脚本改名后声明不能留悬空；
      ③ 声明覆盖的脚本必须在本文件里留下痕迹（出现该标识符）——防「只声明不测」；
      ④ 每个 `tests/test_*.py` 都必须有 `COVERS`（可为空）——否则新用例又退化成靠名字猜。

    **限度要说清**：③ 只证明「这个用例碰过这个模块」，不证明断言覆盖了它的关键分支；
    「清单写着已覆盖、实际零用例」这类事仍要靠人读用例。本检查拦的是**完全没声明、
    声明到不存在的脚本、声明与文件毫无关系**这三种，不冒充覆盖率工具。
    """
    scripts_dir = root / "scripts"
    tests_dir = root / "tests"
    if not (scripts_dir.is_dir() and tests_dir.is_dir()):
        return []  # 不是本仓库的布局（如 --root 指向外部目录），没有事实源可核，不臆断
    scripts = {
        p.stem for p in scripts_dir.glob("*.py") if not p.name.startswith("test_")
    }
    # `scripts/` 下的 test_*.py 由 `check_script_inventory` 判违规，本检查不重复报同一件事，
    # 也不该要求「有人覆盖一个本就不该存在的文件」。
    if not scripts or not sorted(tests_dir.glob("test_*.py")):
        return []  # 空目录已由 run_tests.sh / 清单检查各自拦下，这里不重复报同一件事

    # 声明解析与「有没有提到」的核对在 covers 模块里（控制台也读同一份，
    # 两处各写一份会出现「闸门说有覆盖、页面说没有」这类相反结论）。
    # 存在性在声明阶段就判：先当假覆盖踢掉的话，「改名后声明悬空」永远不会被报出来。
    existing = {p.stem for p in scripts_dir.iterdir() if p.suffix in (".py", ".sh")}
    covered, errs = covers.coverage_map(tests_dir, known=existing)

    for n in sorted(scripts - set(covered)):
        errs.append(
            f"scripts/{n}.py: 没有任何用例在 `{covers.COVERS_VAR}` 里声明覆盖它——"
            f"新增脚本不写用例也会全绿，回归覆盖跑不过仓库增长；"
            f"补用例（或至少在最相关的用例里把 `{n}` 加进 `{covers.COVERS_VAR}` "
            f"并真断言它的行为）"
        )
    return errs


def check_skill_size(root: Path) -> list[str]:
    """SKILL.md 必须在一万字以内。

    这条限的是**加载成本**：技能文件每轮都会被读进上下文，规则条目只增不减时，
    真正会被执行的那几条会被淹没在边界案例里。此前本文件没有任何长度判据，
    规则一条条往后追加，涨到两万五千字也没人报错——**没有闸门的规则等于没有规则**。

    判据取总字符数（含标点与空白），是各口径里最严的一种，免得换口径时反复。
    """
    p = root / "SKILL.md"
    if not p.is_file():
        return []
    n = len(p.read_text(encoding="utf-8", errors="replace"))
    if n <= MAX_SKILL_CHARS:
        return []
    return [
        f"SKILL.md 共 {n} 字，超过上限 {MAX_SKILL_CHARS} 字（超 {n - MAX_SKILL_CHARS}）"
        f"——技能文件每轮都要载入，规则只增不减会把要执行的那几条淹没。"
        f"新增边界案例时同步删掉已被脚本接管的复述段落，不要只往后追加"
    ]


def check_skill_anchors(root: Path) -> list[str]:
    """每条规则的「操作要素」必须还在——改写可以换说法，不能把记号改没了。

    来由：压缩 SKILL.md 时删掉的不是句子而是**记号**——`_predicate_hits`、
    `_SLICE_RE`、`sop_gate.py`、`a(at)b.c`、H28 的 SKIP 豁免。这些丢了之后全文
    依旧通顺，检查也全绿，只是读者照着做不了：找不到实现函数、不知道第 8 步的
    闸门是哪个脚本、遇到必须写裸邮箱的场合没有替代写法。当时是靠人按条目逐项
    grep 才捞回来的，**靠人记的核对等于没有核对**。

    三向都拦（缺一个方向就有一种漏法）：
      ① 文档里的条目没登记锚点——新增规则时「它掉不得的那几个记号」没人定义；
      ② 锚点表里的条目在文档中已不存在——删条目后锚点悬空，守卫静默失效；
      ③ 登记的记号在文档里找不到——改写时被顺手删掉的正是这种。

    外加一条恒真防线：锚点词若就写在条目自己的标题句里，它永远命中，等于没查
    （命中率≈100% 的判据要先怀疑判据本身）。

    **限度**：锚点只保证「记号还在」，保证不了「论断还对」——判据被改弱的那些
    情况仍要人读。锚点也刻意只取可执行的记号（脚本 / 选项 / 函数 / 键名 / 硬数字），
    不取论据句：论据本来就可以重写，把它锚住只会逼人改锚点表。
    """
    p = root / "SKILL.md"
    if not p.is_file():
        return []
    # 锚点表是**本仓库**技能文档的义务，不是通用文档规则：`--root` 指向外部目录时
    # 没有「这份文档该写哪些记号」的事实源，按布局判断（同 check_test_coverage），
    # 不拿本项目的锚点表去要求别人的文档。
    if not (root / "AGENTS.md").is_file():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    # 条目 -> 它的**标题句**（`**H1 · …**` 这一段，不含后面的正文）。
    # 写在标题句里的词不能当锚点：它随条目名一起被保留，永远命中，等于没查。
    # 粒度必须到标题句而不是整行——本文件的条目是整段成行，拿整行比等于什么都没比。
    oneline: dict[str, str] = {}
    onestmt: dict[str, str] = {}
    for ln in lines:
        m = H_DEF_RE.search(ln)
        if not m:
            continue
        hid = m.group(1)
        oneline.setdefault(hid, ln)
        sm = re.search(r"\*\*" + re.escape(hid) + r"[^*\n]*\*\*", ln)
        onestmt.setdefault(hid, sm.group(0) if sm else "")
    errs = []
    for hid in oneline:
        if hid not in SKILL_ANCHORS:
            errs.append(
                f"SKILL.md: {hid} 未登记操作要素锚点——新增条目时须在 "
                f"check_skill_docs.SKILL_ANCHORS 写明「删掉读者就执行不了」的记号，"
                f"否则它被改写掉时没有任何检查会报"
            )
    for hid, tokens in SKILL_ANCHORS.items():
        if hid not in oneline:
            errs.append(
                f"SKILL.md: 锚点表登记的 {hid} 在文档里已不存在——删条目时同步删锚点，"
                f"悬空锚点会让这条守卫静默失效"
            )
            continue
        errs += _anchor_misses(hid, tokens, onestmt[hid], text)

    # 流程步骤同理：第 8 步丢过 `sop_gate.py`，读起来仍是「闸门 + 质检全过才写云」
    steps: dict[str, str] = {}
    for ln in lines:
        m = STEP_TITLE_RE.search(ln)
        if m:
            steps.setdefault(m.group(1), ln)
    for num in steps:
        if num not in STEP_ANCHORS:
            errs.append(
                f"SKILL.md: 流程第 {num} 步未登记操作要素锚点——新增步骤须在 "
                f"check_skill_docs.STEP_ANCHORS 写明该步的命令 / 选项"
            )
    for num, tokens in STEP_ANCHORS.items():
        if num not in steps:
            errs.append(f"SKILL.md: 锚点表登记的流程第 {num} 步已不存在——同步删锚点")
            continue
        errs += _anchor_misses(f"流程第 {num} 步", tokens, steps[num], text)

    # 文档级记号：散落在附录里、既不属某条 H 也不属某一步的那几个
    errs += _anchor_misses("附录", GLOBAL_ANCHORS, "\x00", text)
    return errs


def _anchor_misses(label, tokens, statement, text):
    out = []
    for t in tokens:
        if t in statement:
            out.append(
                f"SKILL.md: {label} 的锚点 {t!r} 就在它自己的标题句里，永远命中，"
                f"等于没查——改取正文里的记号"
            )
        elif t not in text:
            out.append(
                f"SKILL.md: {label} 丢了操作要素 {t!r}（锚点表登记的必需记号）——"
                f"改写时被删掉了，读者照做不了；确认要删就同步删锚点并说明理由"
            )
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", default=None, help="仓库根目录（默认脚本所在目录的上一级）")
    ap.add_argument("--quiet", action="store_true", help="只打印结论行")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:  # --help 正常退出
        return 2 if e.code else 0

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    if not root.is_dir():
        sys.stderr.write(f"路径不存在：{root}\n")
        return 2

    errs, warns, exempts, n_files = [], [], [], 0
    for p in iter_targets(root):
        n_files += 1
        f_err, f_warn, f_ex = scan_file(p, root)
        errs += f_err
        warns += f_warn
        exempts += f_ex

    # 跨文件结构检查：单文件扫描看不到「README 漏了某个脚本」这类缺项
    errs += check_script_inventory(root)
    # 同理，也看不到「引用的编号/小节/脚本已不存在」——引用读起来通顺，只是指空了
    errs += check_cross_refs(root)
    # 第三层：脚本名对了，选项名未必对——参数改名比脚本改名更常见，且更不容易被发现
    errs += check_doc_cli_flags(root)
    # 第四层：清单里有这个脚本，不代表有人验证它——新增脚本不写用例同样全绿
    errs += check_test_coverage(root)
    # 第五层：长度——条目只增不减时，文件会一路涨到没人读得动，而任何单项检查都不报
    errs += check_skill_size(root)
    # 第六层：记号——压长度时最先掉的不是句子而是脚本名 / 选项名，全文照样通顺
    errs += check_skill_anchors(root)

    if not args.quiet:
        for m in dict.fromkeys(exempts):
            print("SKIP:", m)
        for m in dict.fromkeys(errs):
            print("ERR :", m)
        for m in dict.fromkeys(warns):
            print("WARN:", m)

    print(f"结果：{len(errs)} 错 / {len(warns)} 警，"
          f"合计 {len(errs) + len(warns)} 条提示（扫描 {n_files} 个文件）")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
