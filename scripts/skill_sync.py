#!/usr/bin/env python3
"""skill_sync.py — 让每次开工都跑在当前版本的技能代码上。

为什么要有它：技能目录装在容器的可写层里，镜像自带的那份是**浅克隆**，HEAD 停在镜像
构建时的某个提交。容器一重启（新会话、环境复位），可写层连同本地 `git pull` 的成果
一起被丢掉，目录就退回到那个旧提交——于是同一份细则在两轮里给出两套命令、两套格式，
而**没有任何一步会报错**：旧脚本照样跑，只是跑的不是现在写的这一版。实测症状是
「`SKILL.md` 字数少了几千」「按新格式写的草稿被判成组错」「标签树过千后报截断」，
看起来像文档或代码坏了，其实是版本落后。

修法不能靠「记得先 pull」：那是把一条硬约束挂在记性上。本模块把它收进 CLI 入口
（`cli_util.cli_entry`）——任何脚本开跑前先做一次**只读检查 + 仅快进**，因此：

  · 只快进，不重置：本地有未提交改动就跳过（不覆盖正在改的文件）；
  · 本地有未推送提交就跳过（不吞掉提交，也不替你 push）；
  · 取不到远端（离线、TLS、限流）只提示、不阻塞——旧版本能跑完的活不该因为同步失败停住；
  · 同一进程会话内至多做一次网络往返（落 `.git/flomo_skill_sync` 时间戳，默认 900 秒）。
  · 取的不是最新那一跳而是整段缺失历史：浅克隆里 `fetch --depth=1` 只拿一个孤立提交，
    祖先关系因此不成立，`merge --ff-only` 会报「unrelated histories」——这条实测过。

输出只在真发生动作或真出问题时打一行，不为「一切正常」刷屏。
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

STAMP_NAME = "flomo_skill_sync"
DEFAULT_TTL = 900
DEFAULT_TIMEOUT = 25


def repo_root(start: str | Path | None = None) -> Path | None:
    """向上找带 `.git` 的目录；找不到返回 None（本模块在非仓库里也必须无害）。"""
    here = Path(start or Path(__file__).resolve().parent).resolve()
    for cand in [here, *here.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def _git(args: list[str], cwd: Path, timeout: float):
    """跑一条 git；返回 (退出码, stdout+stderr 去空白)。超时与缺 git 都折算成非 0。"""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _stamp_path(root: Path) -> Path:
    override = os.environ.get("FLOMO_SKILL_SYNC_STAMP")
    return Path(override) if override else (root / ".git" / STAMP_NAME)


def _ttl() -> float:
    raw = os.environ.get("FLOMO_SKILL_SYNC_TTL")
    try:
        return float(raw) if raw not in (None, "") else DEFAULT_TTL
    except ValueError:
        return DEFAULT_TTL


def _fresh(stamp: Path, ttl: float) -> bool:
    try:
        return ttl > 0 and (time.time() - stamp.stat().st_mtime) < ttl
    except OSError:
        return False


def _touch(stamp: Path, status: str, head: str) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{status}\n{head}\n", encoding="utf-8")
    except OSError:
        pass


def ensure_current(start: str | Path | None = None, *,
                   ttl: float | None = None,
                   timeout: float = DEFAULT_TIMEOUT,
                   quiet: bool = False,
                   dry_run: bool = False) -> str:
    """把当前仓库快进到 `origin/master`；返回状态名，**永不抛异常**。

    状态：`no-repo` / `skip` / `current` / `updated` / `dirty` / `ahead` / `offline` /
    `stuck`（fetch 到了但快进失败，需要人工看一眼）。
    """
    root = repo_root(start)
    if root is None:
        return "no-repo"
    stamp = _stamp_path(root)
    if _fresh(stamp, _ttl() if ttl is None else ttl):
        return "skip"

    rc, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], root, timeout)
    if rc != 0:
        return "no-repo"

    # 本地改动：同步会覆盖它，所以先看，脏就一句也不动
    rc, out = _git(["status", "--porcelain", "--untracked-files=no"], root, timeout)
    if rc != 0:
        _touch(stamp, "stuck", "")
        if not quiet:
            print(f"[同步] git status 失败，本轮不检查版本：{out.splitlines()[0] if out else '未知原因'}",
                  file=sys.stderr)
        return "stuck"
    if out:
        _touch(stamp, "dirty", "")
        if not quiet:
            print("[同步] 本地有未提交改动，跳过快进（不覆盖正在改的文件）", file=sys.stderr)
        return "dirty"

    _rc, local = _git(["rev-parse", "HEAD"], root, timeout)
    rc, out = _git(["fetch", "--no-tags", "origin", "master"], root, timeout)
    if rc != 0:
        _touch(stamp, "offline", local)
        if not quiet:
            first = out.splitlines()[0] if out else "未知原因"
            print(f"[同步] 取不到远端，按本地版本继续（{first}）；需要时手工跑 "
                  f"`git -C {root} pull --ff-only origin master`", file=sys.stderr)
        return "offline"

    _rc, remote = _git(["rev-parse", "FETCH_HEAD"], root, timeout)
    if not remote or remote == local:
        _touch(stamp, "current", local)
        return "current"

    rc, out = _git(["rev-list", "--count", "FETCH_HEAD..HEAD"], root, timeout)
    ahead = int(out) if rc == 0 and out.isdigit() else 0

    rc, out = _git(["rev-list", "--count", "HEAD..FETCH_HEAD"], root, timeout)
    behind = int(out) if rc == 0 and out.isdigit() else 0

    if ahead and not behind:
        _touch(stamp, "ahead", local)
        if not quiet:
            print(f"[同步] 本地有 {ahead} 个未推送提交，跳过快进（{local[:7]}）", file=sys.stderr)
        return "ahead"

    if dry_run:
        if not quiet:
            print(f"[同步] 本地落后 {behind} 个提交（{local[:7]} → {remote[:7]}），未快进（--check）",
                  file=sys.stderr)
        return "behind"

    rc, out = _git(["merge", "--ff-only", "FETCH_HEAD"], root, timeout)
    if rc != 0:
        _touch(stamp, "stuck", local)
        if not quiet:
            print(f"[同步] 已取到远端但快进失败，需人工处理（{local[:7]} → {remote[:7]}）："
                  f"{out.splitlines()[0] if out else '未知原因'}", file=sys.stderr)
        return "stuck"

    _touch(stamp, "updated", remote)
    if not quiet:
        print(f"[同步] 环境退回旧提交，已快进 {local[:7]} → {remote[:7]}（落后 {behind} 个提交）",
              file=sys.stderr)
    return "updated"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip())
        return 0
    force = "--force" in argv
    check_only = "--check" in argv
    root = repo_root()
    if force and root is not None:
        with contextlib.suppress(OSError):
            _stamp_path(root).unlink()
    status = ensure_current(ttl=0 if force else None, quiet=check_only, dry_run=check_only)
    print(f"[同步] {status}")
    if status in ("offline", "stuck", "behind", "ahead", "dirty"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
