#!/usr/bin/env python3
"""skill_load.py — 把「每轮先读 SKILL.md」从记性改成闸门。

为什么要有它：本技能的全部细则只长在 `SKILL.md` 一份文件里，脚本只负责执行。
于是「有没有真读」决定了这一轮是在按现行细则跑，还是在凭记忆复刻上一轮的命令。
两件事让它反复失效：一是会话一压缩，`SKILL.md` 原文就被挤出上下文窗口，只剩一句
「须先读」的摘要；二是脚本本身不会因为「细则没读」而报错——旧命令照样能跑通，
改过的选项、新加的硬限被跳过，跑出来的结果看着正常，直到某一张卡被判定成组错。

修法不加提醒，加凭证：
  · `python3 scripts/skill_load.py` 随机取 `SKILL.md` 的一行，**只打行号、不打原文**；
  · 拿到行号必须真去读那一行，把它开头十余字回填 `--quote "…"`，才对得上；
  · 对上即写凭证 `.workbuddy/skill_load.json`（时间戳、文件 sha256、引用行号）；
  · 三个必经入口（`fetch_article` / `publish` / `exit_confirm`）跑前查凭证：没有、
    过期、或 `SKILL.md` 已改（sha256 不再相同）——一律退出 9，不往下跑。

为什么要引原文而不是只打勾：打勾是自述，读没读都能勾；引一行随机原文是举证，
凭记忆复刻猜不中这一行。为什么只拦三个入口：拦到全部只读脚本会逼出「为过检而编
凭证」的动机，而流程真正会断的地方就是抓取、写云、关账三处。凭证有效期默认 12
小时（`FLOMO_SKILL_LOAD_TTL` 可调），一改文档即失效——改完文档正是最该重读的时候。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_NAME = "SKILL.md"
TOKEN_NAME = "skill_load.json"
CHALLENGE_NAME = "skill_load_challenge.json"
DEFAULT_TTL = 12 * 3600
LOAD_BLOCKED = "9"
ROOT_ENV = "FLOMO_SKILL_ROOT"
TTL_ENV = "FLOMO_SKILL_LOAD_TTL"
# 引用长度下限：太短（如「要点：」）能靠猜蒙过去，起不到举证作用
MIN_QUOTE = 8
# 挑行时跳过前若干行：文件头是 front matter 与「先读后做」本身，引它等于没引
MIN_LINE_INDEX = 20


def skill_root(start: str | Path | None = None) -> Path:
    """技能目录：环境变量优先（用例与换挂载点用），否则项目根，再否则本文件上级。"""
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override)
    if start is not None:
        return Path(start)
    return PROJECT_ROOT


def doc_path(root: str | Path | None = None) -> Path:
    return skill_root(root) / DOC_NAME


def _state_dir(root: Path) -> Path:
    return root / ".workbuddy"


def _token_path(root: Path) -> Path:
    return _state_dir(root) / TOKEN_NAME


def _challenge_path(root: Path) -> Path:
    return _state_dir(root) / CHALLENGE_NAME


def _ttl() -> float:
    raw = os.environ.get(TTL_ENV)
    try:
        return float(raw) if raw not in (None, "") else float(DEFAULT_TTL)
    except ValueError:
        return float(DEFAULT_TTL)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SkillDocError(RuntimeError):
    """细则文件读不到或挑不出可引用的行：调用方按失败退出，不用 SystemExit。"""


def _read_doc(root: Path) -> tuple[list[str], str]:
    path = doc_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillDocError(f"读不到 {path}：{exc}") from exc
    return text.splitlines(), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pick_line(lines: list[str]) -> int:
    """挑一行可引用的原文：足够长、不是纯符号或代码围栏；返回 1 起的行号。"""
    candidates = []
    for i, line in enumerate(lines, start=1):
        if i <= MIN_LINE_INDEX:
            continue
        text = line.strip()
        if len(text) < 24:
            continue
        if text.startswith(("```", "---", "|")):
            continue
        candidates.append(i)
    if not candidates:
        raise SkillDocError(f"{DOC_NAME} 里挑不出可引用的行——文档疑似为空或被截断")
    return random.choice(candidates)


def issue(root: str | Path | None = None, *, quiet: bool = False) -> int:
    """发一张挑战：只给行号，不给原文。"""
    base = skill_root(root)
    lines, sha = _read_doc(base)
    line_no = _pick_line(lines)
    payload = {"line": line_no, "sha256": sha, "ts": int(time.time()), "doc": str(doc_path(base))}
    state = _state_dir(base)
    state.mkdir(parents=True, exist_ok=True)
    _challenge_path(base).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if not quiet:
        print(f"[加载] {DOC_NAME} 共 {len(lines)} 行，sha256 {sha[:12]}")
        print(f"[加载] 待引用行：第 {line_no} 行（不打印原文）")
        print('[加载] 读该行后回填：python3 scripts/skill_load.py --quote "<该行开头十余字>"')
    return 0


def _normalize(text: str) -> str:
    return "".join(text.split())


def confirm(root: str | Path | None = None, quote: str = "") -> int:
    """核对引用：对的写凭证，错的退出 1 并提示重新取证。"""
    base = skill_root(root)
    challenge_path = _challenge_path(base)
    if not challenge_path.exists():
        print("[阻塞] 还没有挑战：先跑 python3 scripts/skill_load.py 取行号", file=sys.stderr)
        return 1
    try:
        challenge = json.loads(challenge_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[阻塞] 挑战文件读不出来（{exc}）：重跑 python3 scripts/skill_load.py", file=sys.stderr)
        return 1
    lines, sha = _read_doc(base)
    if sha != challenge.get("sha256"):
        print("[阻塞] SKILL.md 在取证期间改过：重新取挑战再引一行", file=sys.stderr)
        return 1
    line_no = int(challenge.get("line") or 0)
    if not 1 <= line_no <= len(lines):
        print(f"[阻塞] 挑战行号 {line_no} 超出全文 {len(lines)} 行：重新取证", file=sys.stderr)
        return 1
    given = _normalize(quote)
    actual = _normalize(lines[line_no - 1])
    if len(given) < MIN_QUOTE:
        print(f"[阻塞] 引用太短（{len(given)} 字，下限 {MIN_QUOTE}）：引该行开头的连续十余字", file=sys.stderr)
        return 1
    if not actual.startswith(given):
        print(f"[阻塞] 引用与第 {line_no} 行对不上：去读那一行，别按记忆填", file=sys.stderr)
        return 1
    token = {
        "ts": int(time.time()),
        "sha256": sha,
        "line": line_no,
        "quote_len": len(given),
        "doc": str(doc_path(base)),
    }
    _token_path(base).write_text(json.dumps(token, ensure_ascii=False), encoding="utf-8")
    with contextlib.suppress(OSError):
        _challenge_path(base).unlink()
    hours = _ttl() / 3600
    print(f"[加载] 凭证已写入（{DOC_NAME} 第 {line_no} 行核对通过），{hours:g} 小时内有效")
    return 0


def status(root: str | Path | None = None) -> int:
    """只读体检：凭证在不在、过期没、文档改没改。"""
    base = skill_root(root)
    path = _token_path(base)
    if not path.exists():
        print("[加载] 无凭证：先跑 python3 scripts/skill_load.py，再 --quote 回填该行原文")
        return 1
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[加载] 凭证读不出来（{exc}）：重新取证")
        return 1
    age = time.time() - float(token.get("ts") or 0)
    cur = _sha256(doc_path(base))
    same = cur == token.get("sha256")
    if not same:
        print(f"[加载] 凭证已失效：{DOC_NAME} 改过之后没重读")
        return 1
    if age > _ttl():
        print(f"[加载] 凭证过期：{age / 3600:.1f} 小时前取的，上限 {_ttl() / 3600:g} 小时")
        return 1
    print(f"[加载] 凭证有效：{age / 3600:.1f} 小时前核对过 {DOC_NAME} 第 {token.get('line')} 行")
    return 0


def guard(root: str | Path | None = None, *, quiet: bool = False) -> int:
    """入口闸门：`0` 放行，`9` 未加载、拒跑。"""
    base = skill_root(root)
    path = _token_path(base)
    reason = ""
    if not path.exists():
        reason = "没有凭证"
    elif not doc_path(base).exists():
        reason = f"{DOC_NAME} 不存在"
    else:
        try:
            token = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            token = {}
            reason = "凭证读不出来"
        if not reason:
            if _sha256(doc_path(base)) != token.get("sha256"):
                reason = f"{DOC_NAME} 改过之后没重读"
            elif time.time() - float(token.get("ts") or 0) > _ttl():
                reason = f"凭证已过期（上限 {_ttl() / 3600:g} 小时）"
    if not reason:
        return 0
    if not quiet:
        print(
            f"[阻塞] 本轮未加载技能细则（{reason}）：先完整读 {DOC_NAME}，再走两步\n"
            f"  1) python3 scripts/skill_load.py              # 打出待引用的行号\n"
            f'  2) python3 scripts/skill_load.py --quote "<该行开头十余字>"\n'
            f"  取证后重跑本条命令；改过 {DOC_NAME} 的轮次须重新取证",
            file=sys.stderr,
        )
    return int(LOAD_BLOCKED)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip())
        return 0
    root: str | Path | None = None
    quote = ""
    want_status = False
    rest = list(argv)
    i = 0
    while i < len(rest):
        item = rest[i]
        if item == "--quote":
            if i + 1 >= len(rest):
                print("[失败] --quote 后面要跟该行开头的原文", file=sys.stderr)
                return 2
            quote = rest[i + 1]
            i += 2
            continue
        if item == "--root":
            if i + 1 >= len(rest):
                print("[失败] --root 后面要跟技能目录", file=sys.stderr)
                return 2
            root = rest[i + 1]
            i += 2
            continue
        if item == "--status":
            want_status = True
            i += 1
            continue
        print(f"[失败] 未知参数：{item}", file=sys.stderr)
        return 2
    try:
        if want_status:
            return status(root)
        if quote:
            return confirm(root, quote)
        return issue(root)
    except SkillDocError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
