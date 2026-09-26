#!/usr/bin/env python3
"""技能文档内容纪律自检（H28 落地；离线、只读、无第三方依赖）。

扫描技能文档（技能目录下的 SKILL.md / AGENTS.md 与全部 .py/.sh，以及仓库根
与 scripts 目录下的脚本），检测规则文本里混入的「具体日期」与「历史事件叙事」
——二者只应作抽象陈述，一次性实测记录与环境事实归 ENVIRONMENT.md。

  ERR  具体日期：YYYY-MM-DD、YYYY/M/D、YYYY.M.D、YYYY年M月[D日]
  WARN 历史事件叙事：曾经 / 当时 / 当天 / 事后发现 / 上次推送 等回溯措辞

豁免：
  - ENVIRONMENT.md：环境事实与一次性实测记录的归档处，其中日期属允许内容。
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

# 规则实现体豁免：本脚本的叙事词表与回归测试夹具必然逐字包含被检测的词面，
# 属模式定义而非叙事，故对这两个文件只停用叙事扫描（日期扫描照常生效），并打印 SKIP 保持可见。
NARRATIVE_SELF_EXEMPT = {"check_skill_docs.py", "test_check_skill_docs.py"}

SKILL_DIR_REL = Path(".kilo/skills/flomo-note")
DOC_SUFFIXES = {".md", ".py", ".sh"}
SCRIPT_SUFFIXES = {".py", ".sh"}
EXEMPT_NAMES = {"ENVIRONMENT.md"}


def iter_targets(root: Path):
    """产出待扫描文件：技能目录全量 + 仓库根与 scripts 目录下的脚本。"""
    seen = set()

    def fresh(p: Path) -> bool:
        if not p.is_file() or "__pycache__" in p.parts:
            return False
        rp = p.resolve()
        if rp in seen:
            return False
        seen.add(rp)
        return True

    skill_dir = root / SKILL_DIR_REL
    if skill_dir.is_dir():
        for p in sorted(skill_dir.rglob("*")):
            if p.suffix.lower() in DOC_SUFFIXES and p.name not in EXEMPT_NAMES and fresh(p):
                yield p
    for p in sorted(root.glob("*")):
        if p.suffix.lower() in SCRIPT_SUFFIXES and fresh(p):
            yield p
    scripts = root / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if p.suffix.lower() in SCRIPT_SUFFIXES and fresh(p):
                yield p


def scan_file(path: Path, root: Path):
    """扫描单个文件，返回 (errs, warns, exempts)。"""
    errs, warns, exempts = [], [], []
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

    for i, ln in enumerate(text.splitlines(), start=1):
        const_line = bool(CONST_LINE_RE.search(ln))
        for m in DATE_RE.finditer(ln):
            hit = m.group(0).strip()
            if const_line:
                exempts.append(f"{relp}:{i}: 日期 {hit} 属协议/版本常量，豁免")
                continue
            errs.append(
                f"{relp}:{i}: 具体日期「{hit}」——技能文档禁写日期（H28）；"
                f"理由只作抽象陈述，一次性实测记录与环境事实写 ENVIRONMENT.md"
            )
        if not scan_narrative:
            continue
        for m in NARRATIVE_RE.finditer(ln):
            warns.append(
                f"{relp}:{i}: 历史事件叙事「{m.group(0)}」——技能文档禁写具体事件经过（H28）；"
                f"改为抽象陈述（当轮 / 本轮 等流程时序词不受此限）"
            )
    return errs, warns, exempts


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
        e, w, x = scan_file(p, root)
        errs += e
        warns += w
        exempts += x

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
