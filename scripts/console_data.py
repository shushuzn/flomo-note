#!/usr/bin/env python3
"""console_data.py — 项目控制台的数据抽取（离线、只读、无第三方依赖）。

控制台要把「项目自身」可视化：九步管线、硬限清单、脚本清单与回归状态、标签树概览。
这些数据分散在 `SKILL.md`（管线与硬限）、`tag_tree.txt`（标签快照）与 `scripts/`（脚本），
格式各不相同。解析一律收敛到本模块单一实现——`web/server.py` 只负责 HTTP 与静态文件，
不重复任何解析逻辑（口径只留一处，避免两侧漂移）。

**只读边界**：本模块只读技能文档、脚本目录与标签树快照。
绝不读取 `.mcp.json`（含 token）、`.sop_gate/`（闸门凭证）与任何笔记正文文件；
不发起任何网络请求——控制台不接触云端，也不触碰笔记内容。

用法：
    from console_data import collect_all
    data = collect_all(Path("/path/to/repo"))
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memo_util import count_snapshot, snapshot_total  # noqa: E402

# 控制台展示的技能文档（存在才收）
DOC_FILES = ("SKILL.md", "AGENTS.md", "ENVIRONMENT.md", "README.md")

# `## 一、xxx` 顶层节 / `### A. xxx` 子节 / `**H1 · 标题**` 条目头
_TOP_RE = re.compile(r"^##\s+(.+?)\s*$")
_SUB_RE = re.compile(r"^###\s+(.+?)\s*$")
# 条目 id 后可能带括注（如 `**H26（仓库分工部分）· …**`），括注非标题一部分
_LIMIT_RE = re.compile(r"^\*\*(H\d+)(?:（[^）]*）)?\s*·\s*(.+?)\*\*\s*$")
# 硬限正文的终止标记：分隔线；以及指向治理条目的横向引用注记
_HR_RE = re.compile(r"^-{3,}\s*$")
_XREF_RE = re.compile(r"^>\s*\*\*H\d+")
# 流程小节：`### 1. 抓取`
_STEP_RE = re.compile(r"^###\s+(\d+)\.\s*(.+?)\s*$")
# 顶层标签标题行 / 裸顶层 / 缩进二级
_GROUP_LINE_RE = re.compile(r"^#\s+(.+?)\s*$")
_BARE_RE = re.compile(r"^([^\s#].*?)/\s*$")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def _skill_lines(root: Path):
    p = root / "SKILL.md"
    if not p.is_file():
        return []
    return _read(p).splitlines()


# --------------------------------------------------------------------------- #
# 硬限清单
# --------------------------------------------------------------------------- #
def _group_key(title: str) -> str:
    """`A. 读写工具` → 'A'；治理节无字母，归为 'GOV'。"""
    m = re.match(r"^([A-Z])\.\s", title)
    if m:
        return m.group(1)
    return "GOV"


def load_limits(root: Path):
    """解析 SKILL.md 的硬限清单，按分组归并。

    返回 [{id, title, group, group_name, body, governance}]，顺序即文档顺序。
    """
    limits = []
    cur = None
    group_name = ""

    def flush():
        nonlocal cur
        if cur is not None:
            limits.append(cur)
            cur = None

    for ln in _skill_lines(root):
        if _TOP_RE.match(ln):
            flush()
            group_name = ""
            continue
        m = _SUB_RE.match(ln)
        if m:
            flush()
            group_name = m.group(1)
            continue
        m = _LIMIT_RE.match(ln)
        if m:
            flush()
            cur = {
                "id": m.group(1),
                "title": m.group(2).rstrip("。"),
                "group": _group_key(group_name),
                "group_name": group_name,
                "governance": "技能文档治理" in group_name,
                "body": [],
            }
            continue
        if cur is None:
            continue
        if _HR_RE.match(ln):
            flush()
            continue
        if _XREF_RE.match(ln):
            # 指向治理条目的横向引用注记，不属上一条正文
            continue
        cur["body"].append(ln)

    flush()
    for it in limits:
        body = "\n".join(it["body"]).strip()
        it["body"] = body
    return limits


# --------------------------------------------------------------------------- #
# 九步管线
# --------------------------------------------------------------------------- #
def load_pipeline(root: Path):
    """解析「## 三、流程」下的 `### N. 标题` 小节。

    返回 [{n, title, blocking, body}]；标「阻塞」的步骤在标题或正文任一处出现即标记。
    """
    lines = _skill_lines(root)
    start = next(
        (i for i, l in enumerate(lines) if l.startswith("## 三、")), None
    )
    if start is None:
        return []
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    steps, cur = [], None
    for ln in lines[start + 1:end]:
        m = _STEP_RE.match(ln)
        if m:
            if cur:
                steps.append(cur)
            cur = {
                "n": int(m.group(1)),
                "title": m.group(2),
                "blocking": "阻塞" in m.group(2),
                "lines": [],
            }
            continue
        if cur is not None and ln.strip():
            cur["lines"].append(ln.rstrip())
    if cur:
        steps.append(cur)

    for s in steps:
        s["blocking"] = s["blocking"] or any("阻塞" in l for l in s["lines"])
        s["body"] = "\n".join(s["lines"])
        s.pop("lines", None)
    return steps


# --------------------------------------------------------------------------- #
# 标签树快照
# --------------------------------------------------------------------------- #
def load_tagtree(root: Path):
    """解析标签树本地快照（计数**委托 `memo_util` 唯一口径**，本模块不另实现）。

    格式（见 SKILL「标签树本地留存」）：首行 `# total=N`；其后 `# 顶层名` 起一组，
    缩进行为二级；裸顶层（有顶层无二级）单独成行、不缩进。

    分组结构只用于界面展示；「列出数」一律取 `memo_util.count_snapshot`——
    控制台与闸门、同步脚本同源，避免口径漂移。
    """
    for cand in (root / "tag_tree.txt", root / "scripts" / "tag_tree.txt"):
        if cand.is_file():
            path = cand
            break
    else:
        return {
            "total": None,
            "groups": [],
            "listed": None,
            "leaves": None,
            "bare": [],
            "present": False,
            "consistent": False,
        }

    lines = _read(path).splitlines()
    total = snapshot_total(lines)
    leaves, bare_count = count_snapshot(lines)

    groups = []
    cur = None
    for ln in lines[1:]:
        if not ln.strip():
            continue
        gm = _GROUP_LINE_RE.match(ln)
        if gm:
            cur = {"name": gm.group(1), "children": [], "bare": False}
            groups.append(cur)
            continue
        if ln[:1] in (" ", "\t"):
            if cur is not None:
                cur["children"].append(ln.strip())
            continue
        bm = _BARE_RE.match(ln)
        if bm:
            if cur is not None:
                cur["bare"] = True
            continue
        # 兜底：既非分组也非缩进，按顶层处理
        groups.append({"name": ln.strip(), "children": [], "bare": False})
        cur = groups[-1]

    listed = leaves + bare_count
    return {
        "total": total,
        "groups": groups,
        "leaves": leaves,
        "bare_count": bare_count,
        "bare": [g["name"] for g in groups if g["bare"]],
        "listed": listed,
        "present": True,
        "consistent": total is not None and listed == total,
        "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
    }


# --------------------------------------------------------------------------- #
# 脚本清单
# --------------------------------------------------------------------------- #
def _summary(p: Path) -> str:
    """取脚本的一句话说明：.py 用模块 docstring 首行，.sh 用首个注释行。"""
    try:
        text = _read(p)
    except (OSError, UnicodeDecodeError):
        return ""
    if p.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except SyntaxError:
            doc = None
        if doc:
            for ln in doc.strip().splitlines():
                if ln.strip():
                    return ln.strip()
    for ln in text.splitlines()[1:10]:
        s = ln.strip()
        if s.startswith("#") and not s.startswith("#!") and len(s) > 3:
            return s.lstrip("#").strip()
    return ""


def load_scripts(root: Path):
    """列出 scripts/ 下的工具与用例，标注是否配套回归用例。"""
    sdir = root / "scripts"
    if not sdir.is_dir():
        return []
    out = []
    for p in sorted(sdir.iterdir()):
        if p.suffix not in (".py", ".sh") or p.name == "__init__.py":
            continue
        is_test = p.stem.startswith("test_")
        out.append(
            {
                "name": p.name,
                "kind": "test" if is_test else "tool",
                "bytes": p.stat().st_size,
                "summary": _summary(p),
                "has_test": True
                if is_test
                else (sdir / f"test_{p.stem}.py").is_file(),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 概览
# --------------------------------------------------------------------------- #
def load_docs(root: Path):
    out = []
    for name in DOC_FILES:
        p = root / name
        if p.is_file():
            text = _read(p)
            out.append(
                {
                    "name": name,
                    "bytes": len(text.encode("utf-8")),
                    "lines": len(text.splitlines()),
                }
            )
    return out


def collect_all(root) -> dict:
    """一次性收集控制台所需的全部数据。"""
    root = Path(root)
    limits = load_limits(root)
    steps = load_pipeline(root)
    scripts = load_scripts(root)
    tag = load_tagtree(root)
    docs = load_docs(root)

    return {
        "project": {
            "name": "flomo-note",
            "tagline": "极简云端卡片笔记技能 — 把网页、文章、想法整理成一条条 flomo memo",
        },
        "stats": {
            "tags": tag["total"],
            "tag_groups": len(tag["groups"]),
            "tag_leaves": tag["leaves"],
            "tags_consistent": tag["consistent"],
            "steps": len(steps),
            "steps_blocking": sum(1 for s in steps if s["blocking"]),
            "limits": len(limits),
            "limits_core": sum(1 for l in limits if not l["governance"]),
            "tools": sum(1 for s in scripts if s["kind"] == "tool"),
            "tests": sum(1 for s in scripts if s["kind"] == "test"),
        },
        "docs": docs,
    }


if __name__ == "__main__":  # 便于命令行核对抽取结果
    import json
    import sys

    r = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    print(json.dumps(collect_all(r), ensure_ascii=False, indent=2))
