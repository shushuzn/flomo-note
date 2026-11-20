#!/usr/bin/env python3
"""baseline_check.py — 判定「本轮跑的是哪一份技能副本」。

为什么要有它：技能目录原本只装在容器可写层，容器一重启就退回镜像里的旧提交，
旧脚本不报错、只是按旧细则跑。把工作副本挪到持久卷之后，同一台机器上会同时存在
两份：持久副本（持续更新）与镜像里的旧副本（永远停在构建时）。谁被加载、谁在跑，
不能靠「我记得刚才拉过」来判断——那正是上一版自愈失效的地方。

纪律：
  - **只读**：只看路径与 git 元数据，不改任何文件；
  - **离线可判**：拿不到远端就用本地 `origin/<分支>` 的引用比，不假装核过；
  - **「未知」不写成「通过」**：没 git、没引用、读不到 HEAD，都要单列状态。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 持久卷上的规范副本；可用环境变量覆盖（换机器 / 换挂载点时不必改代码）
DEFAULT_PERSIST_ROOT = "/home/user/flomo-note"


class BaselineError(RuntimeError):
    """副本状态判不出来：没有 git 元数据、命令失败或输出不可解析。"""


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BaselineError(f"git {' '.join(args)} 跑不起来：{exc}") from exc
    if out.returncode != 0:
        raise BaselineError(f"git {' '.join(args)} 失败：{out.stderr.strip()[:120]}")
    return out.stdout.strip()


def _exists_ref(root: Path, ref: str) -> bool:
    try:
        _git(root, "rev-parse", "--verify", "--quiet", ref)
        return True
    except BaselineError:
        return False


def inspect(root: Path | str = PROJECT_ROOT, persist_root: str | None = None,
            branch: str = "master") -> dict:
    """返回这份副本的可判定状态，字段全部来自实测，判不出的写 `unknown`。"""
    root = Path(root)
    persist = Path(persist_root or DEFAULT_PERSIST_ROOT)
    state: dict = {
        "root": str(root),
        "persist_root": str(persist),
        "persistent": str(root.resolve()) == str(persist.resolve()),
        "head": "unknown",
        "head_subject": "unknown",
        "dirty": "unknown",
        "upstream_ref": f"origin/{branch}",
        "upstream_head": "unknown",
        "behind": "unknown",
        "verdict": "unknown",
        "reason": "",
    }
    if not (root / ".git").exists():
        state["verdict"] = "no-git"
        state["reason"] = "目录里没有 git 元数据，无法判断版本"
        return state
    try:
        state["head"] = _git(root, "rev-parse", "HEAD")
        state["head_subject"] = _git(root, "log", "-1", "--format=%s")
        state["dirty"] = bool(_git(root, "status", "--porcelain"))
        if _exists_ref(root, state["upstream_ref"]):
            state["upstream_head"] = _git(root, "rev-parse", state["upstream_ref"])
    except BaselineError as exc:
        state["verdict"] = "unknown"
        state["reason"] = str(exc)
        return state

    if state["upstream_head"] != "unknown":
        state["behind"] = state["head"] != state["upstream_head"]
    if not state["persistent"]:
        state["verdict"] = "overlay"
        state["reason"] = (
            f"正在跑的是镜像内副本（{root}），不是持久副本（{persist}）；"
            "容器重启会退回构建时提交"
        )
    elif state["behind"] is True:
        state["verdict"] = "behind"
        state["reason"] = "持久副本落后于本地记录的上游引用，先拉取再写云"
    else:
        state["verdict"] = "ok"
    return state


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="判定本轮跑的是哪一份技能副本")
    ap.add_argument("--root", default=str(PROJECT_ROOT), help="要检查的技能目录")
    ap.add_argument("--persist-root", default=None, help="持久副本路径，默认取内置常量或 FLOMO_PERSIST_ROOT")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--json", action="store_true", help="输出 JSON，便于脚本读取")
    args = ap.parse_args(argv)

    import os

    persist = args.persist_root or os.environ.get("FLOMO_PERSIST_ROOT") or DEFAULT_PERSIST_ROOT
    state = inspect(args.root, persist, args.branch)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(f"[副本] {state['root']}")
        print(f"[持久] {'是' if state['persistent'] else '否'}（规范路径 {state['persist_root']}）")
        print(f"[版本] {state['head'][:9] if state['head'] != 'unknown' else 'unknown'} {state['head_subject']}")
        print(f"[判定] {state['verdict']}" + (f"：{state['reason']}" if state["reason"] else ""))
    return 0 if state["verdict"] == "ok" else (3 if state["verdict"] == "overlay" else 4)


if __name__ == "__main__":
    sys.exit(main())
