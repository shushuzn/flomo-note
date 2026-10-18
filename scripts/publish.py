#!/usr/bin/env python3
"""publish.py — 写云一条链：闸门 → 质检 → 建卡 → 机器项复核（流程第 7 步 + 写云动作）。

为什么要有它：此前写完的卡要人工依次跑 `sop_gate`、`validate_memo`、手拼 create 请求、
从回包里抠 id、再跑 `verify_card`。任何一步手滑都会出事，最典型的是「为了看到输出，
把 create 又跑了一遍」——幂等靠检索索引，索引滞后时挡不住，直接造出重复卡；而 id
手抄错一位，复核就会指向另一张卡（H5 明令禁止）。

本脚本把这条链固定成一次调用，并把三条纪律写进代码而不是靠记性：
  1. 闸门不过，一个字节不发云端；
  2. 质检不是 0 ERR，不发；
  3. **create 只发一次**——失败即停、绝不重发；成功后的 id 只从回包里程序解析。

用法：
  python scripts/publish.py --draft _tmp/memo_body_68.txt --verify _tmp/verify_68.json
  python scripts/publish.py --draft ... --verify ... --dry-run      # 只跑到质检
  python scripts/publish.py --draft ... --verify ... --skip-web     # 透传闸门的 --skip-web
  python scripts/publish.py --draft ... --verify ... --no-verify-card

退出码：
  0  建卡成功且机器项复核全过
  1  前置检查不通过（草稿/留痕文件不存在）
  2  闸门未过（未写云）
  3  文本质检有 ERR（未写云）
  4  建卡失败（**未重发**，须先人工核实再决定）
  5  已建卡但机器项复核失败（卡已写入，不改写、不重发，只报告）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PAYLOAD = PROJECT_ROOT / "_tmp" / "publish_payload.json"
PY = sys.executable

_SUCCESS_RE = re.compile(r"\[成功\] 已写入 memo id=(\S+)")


def _env() -> dict:
    """子进程统一 UTF-8：GBK 控制台下子脚本打印会崩（环境事实见 ENVIRONMENT.md）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _exec(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    """跑一条子命令，返回 (退出码, stdout, stderr)。用例可替换本函数以离线测试。"""
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_env(),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _step(title: str) -> None:
    print(f"\n── {title}")


def main(argv: list[str] | None = None, exec_fn=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    run = exec_fn or _exec

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    draft = ""
    verify = ""
    dry_run = False
    skip_web = False
    verify_card = True
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--draft" and i + 1 < len(args):
            draft = args[i + 1]
            i += 2
        elif a == "--verify" and i + 1 < len(args):
            verify = args[i + 1]
            i += 2
        elif a == "--dry-run":
            dry_run = True
            i += 1
        elif a == "--skip-web":
            skip_web = True
            i += 1
        elif a == "--no-verify-card":
            verify_card = False
            i += 1
        else:
            print(f"[失败] 未知参数：{a}", file=sys.stderr)
            return 1

    draft_path = Path(draft)
    if not draft or not draft_path.exists():
        print(f"[失败] 草稿不存在：{draft or '(未指定 --draft)'}", file=sys.stderr)
        return 1
    if verify and not Path(verify).exists():
        print(f"[失败] 验证留痕不存在：{verify}", file=sys.stderr)
        return 1

    body = draft_path.read_text(encoding="utf-8")

    # ① 流程闸门：不过即停，一个字节不发云端
    _step("① 流程闸门 sop_gate")
    gate_cmd = [PY, str(SCRIPT_DIR / "sop_gate.py"), "--memo", draft]
    if verify:
        gate_cmd += ["--verify", verify]
    if skip_web:
        gate_cmd.append("--skip-web")
    code, out, err = run(gate_cmd)
    if out.strip():
        print(_tail(out))
    if code != 0:
        if err.strip():
            print(_tail(err), file=sys.stderr)
        print("[失败] 闸门未过（退出码非 0），未写云——先按上面的失败项修卡", file=sys.stderr)
        return 2
    print("[通过] 闸门")

    # ② 文本质检：0 ERR 才继续
    _step("② 文本质检 validate_memo")
    code, out, err = run([PY, str(SCRIPT_DIR / "validate_memo.py"), "--file", draft])
    if out.strip():
        print(_tail(out))
    if code != 0:
        if err.strip():
            print(_tail(err), file=sys.stderr)
        print("[失败] 质检存在 ERR，未写云——改正文后须重跑闸门（签名/指纹变化）",
              file=sys.stderr)
        return 3
    print("[通过] 质检 0 ERR")

    if dry_run:
        print("\n[RESULT] dry_run=1")
        print("[完成] 只跑到质检（--dry-run），未写云")
        return 0

    # ③ 建卡：只发一次
    _step("③ 建卡 memo_create（只发一次，失败不重发）")
    PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
    PAYLOAD.write_text(json.dumps({"content": body}, ensure_ascii=False), encoding="utf-8")
    started = time.time()
    code, out, err = run([PY, str(SCRIPT_DIR / "flomo_client.py"), "memo_create",
                          "--file", str(PAYLOAD)], timeout=180)
    elapsed = time.time() - started
    memo_id = ""
    reused = False
    if code != 0:
        if out.strip():
            print(_tail(out), file=sys.stderr)
        if err.strip():
            print(_tail(err), file=sys.stderr)
        print(
            "[失败] 建卡未成功（退出码非 0）。**禁止重发**——先人工核实云端是否已有该卡"
            "（可用 dedup.py 按概念名检索）再决定下一步",
            file=sys.stderr,
        )
        return 4
    m = _SUCCESS_RE.search(err)
    if m:
        memo_id = m.group(1)
        print(f"[成功] id={memo_id}（{elapsed:.1f}s）")
    else:
        # 幂等复用分支：stdout 是 {"idempotent": true, "reused_id": "..."}
        try:
            data = json.loads(out[out.find("{"):])
        except (ValueError, json.JSONDecodeError):
            data = {}
        memo_id = data.get("reused_id") or ""
        reused = bool(data.get("idempotent"))
        if memo_id:
            print(f"[幂等] 云端已有同签名卡，复用 id={memo_id}（未新建）")
        else:
            print("[失败] 回包里既无成功 id 也无幂等复用 id——按失败处理，禁止重发",
                  file=sys.stderr)
            return 4

    # ④ 写后机器项复核：卡已写入，失败只报告，不改写不重发
    if verify_card:
        _step("④ 写后机器项复核 verify_card")
        code, out, err = run([PY, str(SCRIPT_DIR / "verify_card.py"), "--id", memo_id,
                              "--draft", draft, "--json"], timeout=120)
        if code != 0:
            if err.strip():
                print(_tail(err), file=sys.stderr)
            if out.strip():
                print(_tail(out), file=sys.stderr)
            print("[失败] 机器项未全过——卡已写入，**不改写不重发**，须按上面失败项人工处置",
                  file=sys.stderr)
            print(f"[RESULT] id={memo_id}")
            return 5
        try:
            info = json.loads(out[out.find("{"):])
        except (ValueError, json.JSONDecodeError):
            info = {}
        detail = info.get("info") or {}
        checks = info.get("checks") or []
        failed = [c.get("name") for c in checks if not c.get("ok")]
        print(
            f"[通过] 机器项 {len(checks)} 项全过 | 字数 {detail.get('word_count')} | "
            f"标签 {' '.join(detail.get('tags') or []) or '-'}"
        )
        if failed:
            print(f"[告警] 失败项：{failed}")
        human = [h.get("item") for h in (info.get("human_judgment") or [])]
        if human:
            print(f"[须人读] 脚本判不了、别当成已查过：{'；'.join(human)}")

    print("\n[RESULT] id=" + memo_id)
    if reused:
        print("[RESULT] reused=1")
    print(f"[下一步] 复盘定向复查：python scripts/dedup.py --id {memo_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
