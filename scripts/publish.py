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
`--allow-rename`。**读不到概念名同样阻塞**——「无法比对」不等于「比对通过」，静默跳过会让
回包形态一改、这道止损点就无声失效。这是本项目里唯一「写之前先读一次」的分支，读的是同一张卡、只比对不动内容。

同一次回读还兼作**改前快照**：`memo_update` 是整篇替换，写后逐字对账的基准是被改后的草稿，
所以「草稿漏抄了一段」「编辑时误删两行」在一致的口径下完全隐形——对账越严格越看不见。
故覆盖路在写前把云端原文落一份快照，写后逐行比对快照与草稿，把删除/新增行原样列出来；
净删行数不为零时点名提示，逐条由人认出「这条是不是有意删的」。

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
  6  目标卡确认不通过（读不到该卡 / 读不到概念名 / 概念名与草稿不符），未写云；
     后两种（读不到概念名、确属改名）可显式加 --allow-rename 放行
"""
from __future__ import annotations

import difflib
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
# 快照目录随 PAYLOAD 走（不预先绑定 `PAYLOAD.parent`：那样用例替换 PAYLOAD 后
# 快照仍写进真实 _tmp，测试会「通过」，而注入点等于没有）。

# 正文归一化必须与回读对账同源：那是「平台渲染差异不参与比对」的唯一口径
# （丢空行 / 还原 `_` `|` `[` 转义 / 去行内水平空白）。自己再写一份，两处迟早
# 不同步，diff 会把渲染层补的空行当成「新增了一行」——噪声会训练出跳过习惯。
sys.path.insert(0, str(SCRIPT_DIR))
import flomo_client as FC  # noqa: E402

# 覆盖差异逐行列出，超过这个行数只列前 N 行 + 提示剩余
DIFF_SHOW = 80

_SUCCESS_RE = re.compile(r"\[成功\] 已写入 memo id=(\S+)")

_OPTS = [
    FC.Opt("--draft", "draft", default=""),
    FC.Opt("--verify", "verify", default=""),
    FC.Opt("--update-id", "update_id", default=""),
    FC.Opt("--allow-rename", "allow_rename", "flag"),
    FC.Opt("--dry-run", "dry_run", "flag"),
    FC.Opt("--skip-web", "skip_web", "flag"),
    FC.Opt("--no-verify-card", "no_verify_card", "flag"),
]


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
    """转发子脚本输出的末尾 n 行。**截断必须说出来**：被切掉的常是最先报的失败项，
    读者看不到就会当成「只有这些错」，改完再跑又冒出新的，白跑一轮。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) <= n:
        return "\n".join(lines)
    return (f"…（前面还有 {len(lines) - n} 行未显示，完整输出请单独跑该脚本）\n"
            + "\n".join(lines[-n:]))


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


def _norm_lines(text: str) -> list[str]:
    """按回读对账的口径取行序列（与 `FC._norm_body` 同源，不另写一份）。"""
    return [ln for ln in FC._norm_body(text).splitlines() if ln.strip()]


def diff_report(before: str, after: str) -> dict:
    """改前快照 vs 改后正文的行级差异（纯函数，不联网不落盘，便于用例断言）。

    比的对象是「云端原文」与「本轮草稿」，不是「草稿 vs 草稿」——写后逐字对账只能
    证明云端存了草稿，证明不了草稿没少抄；漏抄的那段在对账里两边都不存在，永不报错。
    行序参与比对（`SequenceMatcher` 按序列判），故段落搬动会显示成删+增，属预期。
    """
    old, new = _norm_lines(before), _norm_lines(after)
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        removed.extend(old[i1:i2])
        added.extend(new[j1:j2])
    return {
        "old_lines": len(old),
        "new_lines": len(new),
        "removed": removed,
        "added": added,
        "unchanged": len(old) - len(removed),
    }


def _print_diff(rep: dict | None, limit: int = DIFF_SHOW) -> None:
    """打印覆盖差异。删除行永远是重点：整篇替换下误删不会触发任何别的检查。"""
    if rep is None:
        return
    _step("⑥ 覆盖差异（写前云端 → 写后云端）")
    rm, ad = rep["removed"], rep["added"]
    if not rm and not ad:
        print(f"[差异] 无实质变化（改前改后同为 {rep['old_lines']} 行，"
              "按归一化口径逐行一致）")
        return
    print(f"[差异] 改前 {rep['old_lines']} 行 → 改后 {rep['new_lines']} 行；"
          f"删除 {len(rm)} 行、新增 {len(ad)} 行、未动 {rep['unchanged']} 行")
    shown = 0
    for ln in rm:
        if shown >= limit:
            break
        print(f"  - {ln}")
        shown += 1
    for ln in ad:
        if shown >= limit:
            break
        print(f"  + {ln}")
        shown += 1
    total = len(rm) + len(ad)
    if total > shown:
        print(f"  …另 {total - shown} 行未列出（逐行清单上限 {limit} 行，"
              f"要看全量请自行比草稿与快照）")
    if rm:
        print(f"[注意] 本次覆盖删除了 {len(rm)} 行——整篇替换下误删不报错，"
              "逐条确认都是有意的再收工")
    if rep["old_lines"] and len(rm) / rep["old_lines"] >= 0.2 and len(rm) >= 3:
        print(f"[注意] 删除占改前 {len(rm) * 100 // rep['old_lines']}%"
              "（达到两成以上）——若这不是一次有意的重写，先别急着关掉这轮")


def _write_snapshot(memo_id: str, before: str) -> Path | None:
    """落改前快照：写入失败、复核失败、事后想核对「原来写了什么」都靠它。"""
    try:
        PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
        path = PAYLOAD.parent / f"update_before_{memo_id}.txt"
        path.write_text(before, encoding="utf-8")
        return path
    except OSError as exc:
        print(f"[告警] 改前快照未落盘：{exc}（差异比对仍可继续，但事后无原文可查）",
              file=sys.stderr)
        return None


def _confirm_target(run, memo_id: str, body: str, allow_rename: bool) -> tuple[int, str]:
    """覆盖更新前确认目标卡就是草稿这一张——比对概念名，它是卡的身份。

    为什么必须读一次：建卡的错法是「多出一张卡」，还有机会合并或清空；覆盖的错法是
    「把 A 的正文盖到 B 上」，B 的原文即刻消失、只能凭记忆或历史重建。而 id 是人工
    抄进 `--update-id` 的，抄错一位指向的正是别张卡——写后逐字对账能发现不一致，但那时
    损失已经造成。所以闸门放在写之前。

    返回 `(退出码, 目标卡改前全文)`：同一个回包顺手当改前快照，不为 diff 再读一次。
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
        return 6, ""
    # 回包解析走唯一实现（`FC.tool_data_or_none`）；解析不出与「没有这张卡」必须分开说：
    # 前者是协议形态问题（重试无用、更不能据此重建卡），后者才是 --update-id 抄错。
    sc = FC.tool_data_or_none(_cloud_json(out))
    if sc is None:
        print("[失败] 回读回包无法解析（既无 structuredContent，也无可解析的内层 JSON），"
              "未写云——先核对协议形态，别把它读成「卡不存在」", file=sys.stderr)
        return 6, ""
    memos = sc.get("memos") or []
    if not memos:
        print("[失败] 回包里没有这张卡，未写云——确认 --update-id 是否正确", file=sys.stderr)
        return 6, ""
    before = memos[0].get("content", "") or ""
    cloud = _head_lines(before, 2)
    local = _head_lines(body, 2)
    # 判身份的前提是**两侧都读得到概念名**。读不到就跳过比对，等于「回包形态一变、
    # 唯一的止损点就静默失效」——而它失效时恰恰是最危险的时候（id 抄错即覆盖别人）。
    # 故读不到一律阻塞，放行只留 --allow-rename 这一个显式口子。
    if len(cloud) < 2 or len(local) < 2:
        why = (f"目标卡只读到 {len(cloud)} 行非空内容，取不到概念名"
               if len(cloud) < 2 else "草稿前两行没有概念名")
        if not allow_rename:
            print(f"[阻塞] {why}，无法确认目标卡就是草稿这一张——未写云。"
                  f"草稿缺概念名先按 H14 补；若该卡本身没有概念名、确属待改的卡，"
                  f"显式加 --allow-rename 放行", file=sys.stderr)
            return 6, ""
        print(f"[放行] {why}——按 --allow-rename 跳过概念名比对")
    else:
        if cloud[1] != local[1]:
            if not allow_rename:
                print(f"[阻塞] 目标卡概念名「{cloud[1]}」与草稿「{local[1]}」不同——"
                      f"疑似 id 抄错，硬写会把这张卡的正文盖到另一张卡上；"
                      f"确属改名请显式加 --allow-rename", file=sys.stderr)
                return 6, ""
            print(f"[放行] 概念名改名：「{cloud[1]}」→「{local[1]}」（--allow-rename）")
        print(f"[通过] 目标卡概念名「{cloud[1]}」")
    # 回读被截断时，快照只是原文的前半截：拿它做 diff 会把「没取到的后半段」
    # 整片报成删除，比不报更坏（人会以为真的误删了）。故宁可显式放弃这一项。
    if memos[0].get("content_truncated"):
        print("[告警] 目标卡回读被截断（content_truncated）——改前快照与差异比对"
              "均不可信，本轮跳过差异；改后须人工比对原卡，不要据「无删除行」"
              "判定没丢内容", file=sys.stderr)
        return 0, ""
    snap = _write_snapshot(memo_id, before)
    if snap:
        print(f"[快照] 改前全文已落盘：{snap}")
    return 0, before


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

    ns, _pos = FC.parse_options(args, _OPTS)
    draft = ns["draft"]
    verify = ns["verify"]
    # id 是人工抄来的，首尾空白一律不算内容：`--update-id " ABC"` 否则会带空格去查
    update_id = ns["update_id"].strip()
    allow_rename = ns["allow_rename"]
    dry_run = ns["dry_run"]
    skip_web = ns["skip_web"]
    verify_card = not ns["no_verify_card"]

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

    before = ""
    if update_id:
        # 覆盖路：先确认目标卡（写前唯一一次读，兼作改前快照），再覆盖；两条都过了才写
        code, before = _confirm_target(run, update_id, body, allow_rename)
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

    # 覆盖差异先算好、后打印：复核失败也照样要打，而那时已经在 return 的路上
    diff = diff_report(before, body) if (update_id and before) else None

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
            _print_diff(diff)
            print(f"[RESULT] id={memo_id}")
            return 5
        try:
            info = json.loads(out[out.find("{"):])
        except (ValueError, json.JSONDecodeError):
            info = {}
        detail = info.get("info") or {}
        checks = info.get("checks") or []
        # 这里不再判「有没有失败项」：verify_card 有任何 FAIL 就退出码非 0，上面已经
        # return 5 了。能走到这里的必然全过——再写一次 `if failed:` 是条不可达分支，
        # 只会让人以为「失败也能走到这里」。
        if checks:
            print(
                f"[通过] 机器项 {len(checks)} 项全过 | 字数 {detail.get('word_count')} | "
                f"标签 {' '.join(detail.get('tags') or []) or '-'}"
            )
        else:
            print("[告警] 复核回包解析不出机器项明细——退出码 0 只代表它自己判过，"
                  "本轮没有逐项清单可核；请自己跑一遍 verify_card --show-body 读全文",
                  file=sys.stderr)
        human = [h.get("item") for h in (info.get("human_judgment") or [])]
        if human:
            print(f"[须人读] 脚本判不了、别当成已查过：{'；'.join(human)}")

    _print_diff(diff)

    print("\n[RESULT] id=" + memo_id)
    if reused:
        print("[RESULT] reused=1")
    print(f"[下一步] 人读全文自查：python scripts/verify_card.py --id {memo_id} --show-body")
    print(f"[下一步] 复盘定向复查：python scripts/dedup.py --id {memo_id}")
    return 0


if __name__ == "__main__":
    # 与其余命令行脚本同一出口：`FlomoError` / `UsageError` 收成一行 + 退出码，
    # 不让「没配 token」变成 traceback（`main` 里子进程的失败是靠退出码判的，
    # 但本脚本自己也会解 JSON、读文件，兜底不能省）。
    sys.exit(FC.cli_entry(main, usage_code=1))
