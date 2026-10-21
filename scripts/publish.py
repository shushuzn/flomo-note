#!/usr/bin/env python3
"""publish.py — 写云一条链：闸门 → 质检 → 建卡或覆盖更新 → 机器项复核（流程第 7 步 + 写云动作）。

为什么要有它：此前写完的卡要人工依次跑 `sop_gate`、`validate_memo`、手拼 create 请求、
从回包里抠 id、再跑 `verify_card`。任何一步手滑都会出事，最典型的是「为了看到输出，
把 create 又跑了一遍」——幂等靠检索索引，索引滞后时挡不住，直接造出重复卡；而 id
手抄错一位，复核就会指向另一张卡（H5 明令禁止）。

本脚本把这条链固定成一次调用，并把三条纪律写进代码而不是靠记性：
  1. 闸门不过，一个字节不发云端；
  2. 质检不是 0 ERR，不发；
  3. **create 只发一次**——失败即停、绝不重发；成功后的 id 只从回包里程序解析。

覆盖既有卡同样走这一条链：`--update-id <memo id>` 把第 3 步换成「确认目标卡 → 覆盖更新」。
更新比建卡更危险——id 抄错不是多出一张卡，而是把 A 卡正文盖到 B 卡上，B 卡原文即丢。
故更新前先回读目标卡、比对概念名（卡的身份）：不符即阻塞（退出码 6），确属改名要显式
`--allow-rename`。这是本项目里唯一「写之前先读一次」的分支，读的是同一张卡、只比对不动内容。

用法：
  python scripts/publish.py --draft _tmp/memo_body_68.txt --verify _tmp/verify_68.json
  python scripts/publish.py --draft ... --verify ... --update-id MjYxODU0MzI5   # 覆盖既有卡
  python scripts/publish.py --draft ... --verify ... --update-id ... --allow-rename  # 连概念名一起改
  python scripts/publish.py --draft ... --verify ... --dry-run      # 只跑到质检
  python scripts/publish.py --draft ... --verify ... --skip-web     # 透传闸门的 --skip-web
  python scripts/publish.py --draft ... --verify ... --no-verify-card

退出码：
  0  写入成功且机器项复核全过
  1  前置检查不通过（草稿/留痕文件不存在）
  2  闸门未过（未写云）
  3  文本质检有 ERR（未写云）
  4  写入失败（**未重发**，须先人工核实再决定）
  5  已写入但机器项复核失败（卡已写入，不改写、不重发，只报告）
  6  目标卡确认不通过（读不到该卡 / 概念名与草稿不符且未加 --allow-rename），未写云
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


def _cloud_json(out: str) -> dict:
    """从 CLI 输出里抠 JSON：stdout 前面可能带提示行，从第一个 `{` 起解析。"""
    try:
        data = json.loads(out[out.find("{"):])
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _head_lines(text: str, n: int = 2) -> list[str]:
    """取前 n 个非空行。标签段与概念名各自单行，而云端回读会在标签段后补一个空行，
    按非空行取才与本地草稿同一口径（渲染层差异不参与判断）。"""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:n]


def _confirm_target(run, memo_id: str, body: str, allow_rename: bool) -> int:
    """覆盖更新前确认目标卡就是草稿这一张——比对概念名，它是卡的身份。

    为什么必须读一次：建卡的错法是「多出一张卡」，还有机会合并或清空；覆盖的错法是
    「把 A 的正文盖到 B 上」，B 的原文即刻消失、只能凭记忆或历史重建。而 id 是人工
    抄进 `--update-id` 的，抄错一位指向的正是别张卡——写后逐字对账能发现不一致，但那时
    损失已经造成。所以闸门放在写之前。
    """
    _step(f"③ 目标卡确认 memo_batch_get（{memo_id}）")
    req = PAYLOAD.with_name("publish_target.json")
    req.parent.mkdir(parents=True, exist_ok=True)
    req.write_text(json.dumps({"ids": [memo_id]}, ensure_ascii=False), encoding="utf-8")
    code, out, err = run([PY, str(SCRIPT_DIR / "flomo_client.py"), "memo_batch_get",
                          "--file", str(req)], timeout=120)
    if code != 0:
        for stream in (out, err):
            if stream.strip():
                print(_tail(stream), file=sys.stderr)
        print("[失败] 读不到目标卡，未写云——确认 --update-id 指向的卡存在、网络正常",
              file=sys.stderr)
        return 6
    memos = (_cloud_json(out).get("structuredContent") or {}).get("memos") or []
    if not memos:
        print("[失败] 回包里没有这张卡，未写云——确认 --update-id 是否正确", file=sys.stderr)
        return 6
    cloud = _head_lines(memos[0].get("content", ""), 2)
    local = _head_lines(body, 2)
    if len(cloud) >= 2 and len(local) >= 2 and cloud[1] != local[1]:
        if not allow_rename:
            print(f"[阻塞] 目标卡概念名「{cloud[1]}」与草稿「{local[1]}」不同——"
                  f"疑似 id 抄错，硬写会把这张卡的正文盖到另一张卡上；"
                  f"确属改名请显式加 --allow-rename", file=sys.stderr)
            return 6
        print(f"[放行] 概念名改名：「{cloud[1]}」→「{local[1]}」（--allow-rename）")
    print(f"[通过] 目标卡概念名「{cloud[1] if len(cloud) >= 2 else '-'}」")
    return 0


def _create_card(run, body: str) -> tuple[int, str, bool]:
    """建卡：只发一次。返回 (退出码, id, 是否幂等复用)。"""
    _step("③ 建卡 memo_create（只发一次，失败不重发）")
    PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
    PAYLOAD.write_text(json.dumps({"content": body}, ensure_ascii=False), encoding="utf-8")
    started = time.time()
    code, out, err = run([PY, str(SCRIPT_DIR / "flomo_client.py"), "memo_create",
                          "--file", str(PAYLOAD)], timeout=180)
    elapsed = time.time() - started
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
        return 4, "", False
    m = _SUCCESS_RE.search(err)
    if m:
        print(f"[成功] id={m.group(1)}（{elapsed:.1f}s）")
        return 0, m.group(1), False
    # 幂等复用分支：stdout 是 {"idempotent": true, "reused_id": "..."}
    data = _cloud_json(out)
    memo_id = data.get("reused_id") or ""
    reused = bool(data.get("idempotent"))
    if memo_id:
        print(f"[幂等] 云端已有同签名卡，复用 id={memo_id}（未新建）")
        return 0, memo_id, reused
    print("[失败] 回包里既无成功 id 也无幂等复用 id——按失败处理，禁止重发",
          file=sys.stderr)
    return 4, "", False


def _update_card(run, memo_id: str, body: str) -> tuple[int, str, bool]:
    """覆盖更新：同样只发一次。返回 (退出码, id, 是否幂等复用) 以对齐建卡那路的签名。"""
    _step(f"④ 覆盖更新 memo_update（{memo_id}，只发一次）")
    PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
    PAYLOAD.write_text(json.dumps({"id": memo_id, "content": body}, ensure_ascii=False),
                       encoding="utf-8")
    started = time.time()
    code, out, err = run([PY, str(SCRIPT_DIR / "flomo_client.py"), "memo_update",
                          "--file", str(PAYLOAD)], timeout=180)
    elapsed = time.time() - started
    if code != 0:
        for stream in (out, err):
            if stream.strip():
                print(_tail(stream), file=sys.stderr)
        print("[失败] 覆盖更新未成功（退出码非 0）。客户端已做写后回读，先看上面的差异"
              "报告；要重试也得先确认云端当前是什么内容（覆盖写重发不会多出卡，但会盖掉"
              "别人刚改的东西）", file=sys.stderr)
        return 4, "", False
    print(f"[成功] 已覆盖更新 id={memo_id}（{elapsed:.1f}s）")
    return 0, memo_id, False



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
    update_id = ""
    allow_rename = False
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
        elif a == "--update-id" and i + 1 < len(args):
            update_id = args[i + 1].strip()
            i += 2
        elif a == "--allow-rename":
            allow_rename = True
            i += 1
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

    if allow_rename and not update_id:
        print("[失败] --allow-rename 只对 --update-id 有意义（建卡没有既有概念名可比）",
              file=sys.stderr)
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

    if update_id:
        # 覆盖路：先确认目标卡（写前唯一一次读），再覆盖；两条都过了才写
        code = _confirm_target(run, update_id, body, allow_rename)
        if code != 0:
            return code
        code, memo_id, reused = _update_card(run, update_id, body)
        if code != 0:
            return code
    else:
        # 建卡路：只发一次
        code, memo_id, reused = _create_card(run, body)
        if code != 0:
            return code

    # 写后机器项复核：卡已写入，失败只报告，不改写不重发
    if verify_card:
        _step(("⑤" if update_id else "④") + " 写后机器项复核 verify_card")
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
