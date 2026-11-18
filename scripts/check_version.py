#!/usr/bin/env python3
"""check_version.py — 版本号与变更日志、git 标签的一致性检查。

版本号只写一处：`pyproject.toml` 的 `[project].version`。另两处是它的**派生表示**——
`CHANGELOG.md` 最新一条的版本、git 标签 `vX.Y.Z`。三处一旦对不上，「这个版本指哪次
提交」就没有答案，而版本号存在的全部意义就是回答这个问题。

为什么不在每个脚本里写一份 `__version__`：抄 N 份等于埋 N-1 份会漂的判据。漂了之后
没人知道该信哪份，于是「版本」从事实退化成传闻。

检查项（前四条是静态一致性，CI 跑）：

  ① `pyproject.toml` 有 `[project].version`，且形如 `X.Y.Z`；
  ② `CHANGELOG.md` 存在，且至少有一条 `## [X.Y.Z]` 条目；
  ③ 首条版本 == pyproject 版本——改能力要补条目，发版要升号，两头必须同步动；
  ④ 条目版本不重复，且自上而下严格递减（新版本在上）。

`--tag` 追加两条，**只在发版时跑**：

  ⑤ 存在 `v<版本>` 标签，且它是 HEAD 的祖先（标签指向的提交仍在当前历史里）；
  ⑥ 没有比 pyproject 版本更高的标签（版本倒挂：pyproject 落后于已发布的标签）。

为什么 `--tag` 不进 CI：GitHub Actions 默认浅检出（fetch-depth 1），检出里没有标签，
在那里跑这条只会得到「标签不存在」的假红——错的是检出深度，不是仓库状态。把一条
只在特定环境下才有意义的检查塞进 CI，等于训练人忽略红。

用法：
  python scripts/check_version.py            # 静态一致性（CI 与本地都用这个）
  python scripts/check_version.py --tag      # 追加 git 标签核对（发版时）
  python scripts/check_version.py --root DIR  # 指向别的仓库副本（用例造夹具用）

退出码：
  0  一致
  1  不一致——输出里每条都带「改哪边」
  2  命令行参数写错
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cli_util import Opt, cli_entry, parse_options  # noqa: E402

_OPTS = [
    Opt("--root", "root", "str", hint="；仓库副本目录，默认本仓库根"),
    Opt("--tag", "check_tag", "flag", hint="；追加 git 标签核对（发版时用）"),
]

# `X.Y.Z` 三段整数。预发布后缀（`1.0.0-rc1`）不支持：本项目的发布是「打标签」这一个
# 动作，没有「先发候选版」的阶段，多支持一种形态只会多一条无人走过的分支。
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# CHANGELOG 条目：`## [1.0.0]` 或 `## 1.0.0`（方括号与 v 前缀都容忍，读的是版本不是排版）。
ENTRY_RE = re.compile(r"^##\s*\[?v?(\d+\.\d+\.\d+)\]?\s*$", re.M)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`1.2.3` / `v1.2.3` → `(1, 2, 3)`；不认识的形态返回 None（不猜）。"""
    m = VERSION_RE.match(text.strip().lstrip("vV"))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def read_pyproject_version(root: Path) -> str:
    """取 `[project].version`；读不到或没这个键都返回空串，由调用方报成一句人话。"""
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    project = data.get("project")
    if not isinstance(project, dict):
        return ""
    return str(project.get("version") or "").strip()


def changelog_versions(text: str) -> list[str]:
    """按**文件顺序**返回条目版本（新版本在上时，这就是从新到旧）。"""
    return ENTRY_RE.findall(text)


def _git(root: Path, *args: str) -> tuple[int, str]:
    """跑一条 git 子命令，返回 `(退出码, 输出)`；git 不存在或超时统一按非 0 处理。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    msg = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    return proc.returncode, (msg + "\n" + err).strip() if err else msg


def _check_tag(root: Path, version: str, tagged: bool) -> list[str]:
    """⑤⑥：标签存在、可达、不倒挂。只在 `--tag` 下被调。"""
    errs: list[str] = []
    rc, out = _git(root, "tag", "--list", "v*")
    if rc != 0:
        return [f"取不到 git 标签（{out or 'git 不可用'}）——本条无法判定，不是「已一致」"]
    tags = [t.strip() for t in out.splitlines() if t.strip()]

    want = f"v{version}"
    if want not in tags:
        errs.append(
            f"没有 {want} 标签——版本号升了却没打标签，这个版本号就指不到任何提交。"
            f"发版动作：git tag -a {want} -m {want} && git push origin {want}")
    elif tagged:
        rc, out = _git(root, "merge-base", "--is-ancestor", want, "HEAD")
        if rc == 1:
            errs.append(f"{want} 不是 HEAD 的祖先——标签指向一条不在当前历史里的提交（被改写过？）")
        elif rc != 0:
            errs.append(f"核 {want} 与 HEAD 的先后失败：{out}")

    cur = parse_version(version)
    higher = [t for t in tags if cur is not None and (parse_version(t) or (0, 0, 0)) > cur]
    if higher:
        errs.append(
            f"标签 {', '.join(sorted(higher))} 比 pyproject 的 {version} 更高——版本倒挂。"
            f"标签一旦推上去就是公开事实，所以改的是 pyproject 的版本号，不是删标签")
    return errs


def check(root: Path, check_tag: bool = False) -> tuple[list[str], list[str]]:
    """返回 `(错误, 提示)`。纯函数、不打印，供用例直接调。错误为空集才算通过。"""
    errs: list[str] = []
    notes: list[str] = []

    version = read_pyproject_version(root)
    if not version:
        return ["pyproject.toml 读不到 [project].version——版本号必须有且只有这一处权威来源"], notes
    cur = parse_version(version)
    if cur is None:
        return [f"pyproject 版本「{version}」不是 X.Y.Z 形态，无法与 CHANGELOG 和标签比对"], notes

    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        return ["缺 CHANGELOG.md——版本号与它脱钩后，读者无从知道这一版有什么"], notes
    versions = changelog_versions(changelog.read_text(encoding="utf-8"))
    if not versions:
        return ["CHANGELOG.md 里没有任何 `## [X.Y.Z]` 条目（标题行须独占一行）"], notes

    if versions[0] != version:
        errs.append(
            f"版本对不上：pyproject 是 {version}，CHANGELOG 首条是 {versions[0]}。"
            f"改的是能力就补条目，发的是版就升 pyproject 的版本号——两头都得动")

    seen: set[str] = set()
    prev: tuple[int, int, int] | None = None
    for v in versions:
        if v in seen:
            errs.append(f"CHANGELOG 版本 {v} 出现两次——同一个版本号只能有一条")
        seen.add(v)
        num = parse_version(v)
        if num is not None and prev is not None and num >= prev:
            errs.append(f"CHANGELOG 顺序应新在上：{v} 排在上一条之后，但版本号没有变小")
        if num is not None:
            prev = num

    notes.append(f"pyproject {version}；CHANGELOG {len(versions)} 条，首条 {versions[0]}")
    if check_tag:
        errs.extend(_check_tag(root, version, tagged=cur is not None))
    return errs, notes


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    # 本脚本没有必需参数，默认动作就是检查：不能「没给参数就打印帮助」，那会让
    # `python scripts/check_version.py` 什么都不查却以 0 退出，看起来像「已一致」。
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    ns, _pos = parse_options(args, _OPTS)
    root = Path(ns["root"]).expanduser().resolve() if ns["root"] else SCRIPT_DIR.parent
    do_tag = bool(ns["check_tag"])

    errs, notes = check(root, check_tag=do_tag)
    for line in notes:
        print(f"[版本] {line}")
    for line in errs:
        print(f"  ✗ {line}", file=sys.stderr)
    if errs:
        print(f"[结论] {len(errs)} 项不一致", file=sys.stderr)
        return 1
    scope = "pyproject / CHANGELOG / git 标签" if do_tag else "pyproject / CHANGELOG"
    print(f"[结论] {scope} 同指一个版本")
    return 0


if __name__ == "__main__":
    # errors=()：本脚本只读本地文件与 git，没有云端失败可预期；用法错由 cli_entry 转 2。
    sys.exit(cli_entry(main, errors=()))
