#!/usr/bin/env python3
"""publish.py 的离线回归用例（不联网、不写云）。

用假执行器驱动整条链，钉住三条纪律：闸门/质检不过绝不建卡；create 只发一次、
失败不重发；写后复核失败只报告。
退出码 0 = 全部通过。
"""

COVERS = ("publish",)

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PB = _load("publish", "publish.py")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


VERIFY_JSON = json.dumps({
    "ok": True,
    "info": {"word_count": 600, "tags": ["AI/测试"]},
    "checks": [{"name": "回读完整（未被截断）", "ok": True, "detail": ""}],
    "human_judgment": [{"item": "首尾是否同一事件", "why": "语义"}],
}, ensure_ascii=False)

# 云端回读形态：标签段后补一个空行（渲染层行为），概念名是第二个非空行
BATCH_JSON = json.dumps({
    "structuredContent": {
        "memos": [{"id": "ABC", "word_count": 30,
                   "content": "#AI/测试\n\n概念名\n\n正文"}]
    }
}, ensure_ascii=False)


def make_exec(overrides=None):
    """返回 (exec_fn, calls)。overrides 按脚本名（覆盖其全部子命令）或
    (脚本名, 子命令) 精确指定 (code, stdout, stderr)。"""
    calls = []
    table = {
        ("sop_gate.py", ""): (0, "[gate] 通过", ""),
        ("validate_memo.py", ""): (0, "[validate] 0 ERR", ""),
        ("flomo_client.py", "memo_create"): (
            0, '{"structuredContent": {"id": "ABC", "word_count": 600}}',
            "[成功] 已写入 memo id=ABC\n"),
        ("flomo_client.py", "memo_batch_get"): (0, BATCH_JSON, ""),
        ("flomo_client.py", "memo_update"): (
            0, '{"structuredContent": {"memos": [{"id": "ABC"}]}}',
            "[成功] 已覆盖更新 memo id=ABC\n"),
        ("verify_card.py", ""): (0, VERIFY_JSON, ""),
    }
    for k, v in (overrides or {}).items():
        if isinstance(k, tuple):
            table[k] = v
            continue
        for key in list(table):
            if key[0] == k:
                table[key] = v
        table[(k, "")] = v

    def _exec(cmd, timeout=600):
        calls.append(list(cmd))
        name = Path(cmd[1]).name if len(cmd) > 1 else cmd[0]
        tool = cmd[2] if len(cmd) > 2 and not cmd[2].startswith("--") else ""
        return table.get((name, tool)) or table.get((name, "")) or (0, "", "")

    return _exec, calls


def run(argv, exec_fn):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = PB.main(argv, exec_fn=exec_fn)
    return code, out.getvalue(), err.getvalue()


def with_draft(tmp, content="#AI/测试\n概念名\n\n正文"):
    draft = Path(tmp) / "memo_body_99.txt"
    draft.write_text(content, encoding="utf-8")
    verify = Path(tmp) / "verify_99.json"
    verify.write_text('{"searched": true, "terms": []}', encoding="utf-8")
    return draft, verify


def names(calls):
    return [Path(c[1]).name if len(c) > 1 else c[0] for c in calls]


def test_success_chain():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec()
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("成功链：四步按序执行",
              names(calls) == ["sop_gate.py", "validate_memo.py", "flomo_client.py",
                               "verify_card.py"], names(calls))
        check("成功链：create 只发一次",
              names(calls).count("flomo_client.py") == 1, names(calls))
        check("成功链：退出码 0", code == 0, code)
        check("成功链：程序解析出 id", "[RESULT] id=ABC" in out, out[-200:])
        check("成功链：复核带上草稿逐字比对",
              "--draft" in calls[-1], calls[-1])
        check("成功链：列出须人读项", "须人读" in out, out[-300:])


def test_gate_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec({"sop_gate.py": (2, "", "[gate] 失败")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("闸门不过：退出码 2", code == 2, code)
        check("闸门不过：一个字节不发云端", names(calls) == ["sop_gate.py"], names(calls))
        check("闸门不过：提示未写云", "未写云" in err, err)


def test_validate_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec({"validate_memo.py": (1, "[ERR] 1 项", "")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("质检有 ERR：退出码 3", code == 3, code)
        check("质检有 ERR：不建卡",
              "flomo_client.py" not in names(calls), names(calls))
        check("质检有 ERR：提示改后重跑闸门", "重跑闸门" in err, err)


def test_create_failure_no_retry():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec({"flomo_client.py": (1, "", "[失败] 超时")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("建卡失败：退出码 4", code == 4, code)
        check("建卡失败：create 只调用一次、不重发",
              names(calls).count("flomo_client.py") == 1, names(calls))
        check("建卡失败：明确禁止重发", "禁止重发" in err, err)


def test_idempotent_reuse():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec({
            "flomo_client.py": (0, '{"idempotent": true, "reused_id": "ABC"}',
                                "[注意] memo_create 是写操作\n"),
        })
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("幂等复用：退出码 0", code == 0, code)
        check("幂等复用：报告复用 id", "复用 id=ABC" in out, out[-200:])
        check("幂等复用：标记 reused", "[RESULT] reused=1" in out, out[-200:])


def test_verify_card_failure():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec({"verify_card.py": (1, "", "[verify] 回读失败")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("复核失败：退出码 5", code == 5, code)
        check("复核失败：卡已写入仍报 id", "[RESULT] id=ABC" in out, out[-200:])
        check("复核失败：不改写不重发",
              names(calls).count("flomo_client.py") == 1, names(calls))
        check("复核失败：提示人工处置", "不改写不重发" in err, err)


def test_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec()
        code, out, _err = run(
            ["--draft", str(draft), "--verify", str(verify), "--dry-run"], exec_fn)
        check("dry-run：只跑到质检", names(calls) == ["sop_gate.py", "validate_memo.py"],
              names(calls))
        check("dry-run：退出码 0 且标记 dry_run", code == 0 and "dry_run=1" in out, out)


def test_missing_draft():
    exec_fn, calls = make_exec()
    code, _out, err = run(["--draft", "不存在的草稿.txt"], exec_fn)
    check("草稿不存在：退出码 1", code == 1, code)
    check("草稿不存在：不调用任何子脚本", calls == [], calls)


def test_option_syntax():
    """参数写法错：缺值 / 重复 / 未知，一律在跑任何子脚本之前拦下。

    `--draft` 落在末尾时旧循环报「未知参数：--draft」——用户会以为选项名写错；
    `--draft a --draft b` 则静默用 b，闸门核的是 b，用户以为核的是 a。
    """
    for argv, want, label in (
        (["--draft"], "缺少参数值", "缺值"),
        (["--draft", "a.txt", "--draft", "b.txt"], "只能给一次", "重复 --draft"),
        (["--draft", "a.txt", "--bogus"], "未知参数：--bogus", "未知选项"),
        (["a.txt"], "多余的参数", "值写在选项前面"),
    ):
        exec_fn, calls = make_exec()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            # 走真实入口：用法错由 cli_entry 渲染成一行，`main` 不再自己打印
            code = PB.CLI.cli_entry(lambda: PB.main(argv, exec_fn=exec_fn),
                                     errors=(PB.FC.FlomoError,), usage_code=1)
        msg = err.getvalue()
        check(f"参数错（{label}）：退出码 1", code == 1, code)
        check(f"参数错（{label}）：提示含「{want}」", want in msg, msg)
        check(f"参数错（{label}）：不调用任何子脚本", calls == [], calls)


# ── 覆盖既有卡（--update-id）：比建卡更危险，写前必须确认目标卡 ──────────────

def tools(calls):
    return [c[2] if len(c) > 2 and not c[2].startswith("--") else "" for c in calls]


def _other_card(concept="另一张卡", tag="#AI/测试"):
    return json.dumps({"structuredContent": {"memos": [
        {"id": "ABC", "content": f"{tag}\n\n{concept}\n\n别的正文"}]}}, ensure_ascii=False)


def test_update_success():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec()
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("覆盖路：五步按序（含写前确认）",
              names(calls) == ["sop_gate.py", "validate_memo.py", "flomo_client.py",
                               "flomo_client.py", "verify_card.py"], names(calls))
        check("覆盖路：先 batch_get 再 memo_update",
              tools(calls)[2:4] == ["memo_batch_get", "memo_update"], tools(calls))
        check("覆盖路：不误用 memo_create",
              "memo_create" not in tools(calls), tools(calls))
        payload = PB.PAYLOAD.read_text(encoding="utf-8")
        check("覆盖路：update 请求带目标 id 与正文",
              '"id": "ABC"' in payload and "概念名" in payload, payload[:120])
        last = calls[-1]
        check("覆盖路：复核指向同一 id",
              last[last.index("--id") + 1] == "ABC", last)
        check("覆盖路：退出码 0 且不误报幂等",
              code == 0 and "[RESULT] reused=1" not in out, f"{code} {out[-160:]}")
        check("覆盖路：复核步骤编号为 ⑤", "⑤ 写后机器项复核" in out, out[-400:])


def test_update_rename_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _other_card(), "")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify),
                              "--update-id", "ABC"], exec_fn)
        check("概念名不符：退出码 6（未写云）", code == 6, code)
        check("概念名不符：一个字节不发 update",
              "memo_update" not in tools(calls), tools(calls))
        check("概念名不符：也不跑复核", "verify_card.py" not in names(calls), names(calls))
        check("概念名不符：提示疑似 id 抄错", "疑似 id 抄错" in err, err)
        check("概念名不符：给出放行办法", "--allow-rename" in err, err)


def test_update_rename_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _other_card(), "")})
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC", "--allow-rename"], exec_fn)
        check("显式改名：退出码 0", code == 0, code)
        check("显式改名：确实发了 update", "memo_update" in tools(calls), tools(calls))
        check("显式改名：打印改名放行", "[放行] 概念名改名" in out, out[-400:])


def test_update_target_missing():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        empty = json.dumps({"structuredContent": {"memos": []}}, ensure_ascii=False)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, empty, "")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("目标卡不存在：退出码 6", code == 6, code)
        check("目标卡不存在：不写云", "memo_update" not in tools(calls), tools(calls))
        check("目标卡不存在：提示核对 id", "确认 --update-id" in err, err)


def test_update_read_unparseable():
    """回包读不出结构 ≠ 卡不存在：两者都退 6，但提示必须分开。

    合成一句「回包里没有这张卡」会把人引向「换 id 再试」；真正的原因是协议形态
    变了，重试一次还是同一句，而对照的草稿可能已经改成另一张卡的内容。
    """
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, "不是 JSON 的回包", "")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("回包读不出结构：退出码 6", code == 6, code)
        check("回包读不出结构：不写云", "memo_update" not in tools(calls), tools(calls))
        check("回包读不出结构：提示指向协议形态", "无法解析" in err, err)
        check("回包读不出结构：不误报「没有这张卡」", "没有这张卡" not in err, err)


def test_update_read_failure():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (1, "", "[失败] 网络")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("读目标卡失败：退出码 6", code == 6, code)
        check("读目标卡失败：不写云", "memo_update" not in tools(calls), tools(calls))
        check("读目标卡失败：提示未写云", "未写云" in err, err)


def test_update_failure_no_dup():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_update"): (1, "", "[失败] 超时")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("覆盖失败：退出码 4", code == 4, code)
        check("覆盖失败：只发一次、不自动重试",
              tools(calls).count("memo_update") == 1, tools(calls))
        check("覆盖失败：提示重试前先确认云端现状",
              "先确认云端当前是什么内容" in err, err)


def test_allow_rename_without_update():
    exec_fn, calls = make_exec()
    code, _out, err = run(["--draft", "x.txt", "--allow-rename"], exec_fn)
    check("单独 --allow-rename：退出码 1", code == 1, code)
    check("单独 --allow-rename：不调用任何子脚本", calls == [], calls)
    check("单独 --allow-rename：说明只对 update 有效",
          "只对 --update-id 有意义" in err, err)


def test_update_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, calls = make_exec()
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC", "--dry-run"], exec_fn)
        check("覆盖路 dry-run：只跑到质检",
              names(calls) == ["sop_gate.py", "validate_memo.py"], names(calls))
        check("覆盖路 dry-run：退出码 0", code == 0 and "dry_run=1" in out, out)


# ── 覆盖差异：整篇替换下「草稿少抄一段」写后对账永远查不出，故比改前快照 ────────

def _card(body: str, concept: str = "概念名") -> str:
    return json.dumps({"structuredContent": {"memos": [
        {"id": "ABC", "content": f"#AI/测试\n\n{concept}\n\n{body}"}]}}, ensure_ascii=False)


def test_update_diff_reported():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n第一段\n改写段")
        exec_fn, _calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _card("第一段\n第二段"), "")})
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("差异：覆盖成功后打印差异段", "⑥ 覆盖差异" in out, out[-600:])
        check("差异：列出被删的行", "- 第二段" in out, out[-600:])
        check("差异：列出新增的行", "+ 改写段" in out, out[-600:])
        check("差异：统计删除/新增/未动",
              "删除 1 行、新增 1 行、未动 3 行" in out, out[-600:])
        check("差异：删了行就点名提示", "本次覆盖删除了 1 行" in out, out[-600:])
        snap = Path(tmp) / "update_before_ABC.txt"
        check("差异：改前快照已落盘且是云端原文",
              snap.exists() and "第二段" in snap.read_text(encoding="utf-8"),
              snap)
        check("差异：退出码仍为 0（差异不是失败）", code == 0, code)


def test_update_diff_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        # 云端形态标签段后多一个空行；归一化后与草稿一致，不该报成「新增一行」
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n正文")
        exec_fn, _calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _card("正文"), "")})
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("差异：无变化时报无实质变化", "无实质变化" in out, out[-500:])
        check("差异：无变化时不误报删除提示", "本次覆盖删除了" not in out, out[-500:])
        check("差异：无变化仍退出码 0", code == 0, code)


def test_diff_truncated_and_large_delete():
    old = "\n".join(f"旧行{i}" for i in range(100))
    rep = PB.diff_report("#AI/测试\n概念名\n\n" + old, "#AI/测试\n概念名\n\n新的一行")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        PB._print_diff(rep, limit=10)
    out = buf.getvalue()
    check("差异：删除行数按实际统计", "删除 100 行、新增 1 行" in out, out[:300])
    check("差异：超上限只列前 N 行并说明剩余",
          out.count("  - 旧行") == 10 and "…另 91 行未列出" in out, out[-300:])
    check("差异：大比例删除额外提示",
          "达到两成以上" in out, out[-300:])


def test_update_diff_on_verify_failure():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n第一段\n改写段")
        exec_fn, _calls = make_exec({
            ("flomo_client.py", "memo_batch_get"): (0, _card("第一段\n第二段"), ""),
            "verify_card.py": (1, "", "[verify] 回读不一致"),
        })
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("差异：复核失败也要打差异（正是要人看的时候）",
              code == 5 and "⑥ 覆盖差异" in out, out[-600:])


def test_create_has_no_diff():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, _calls = make_exec()
        _code, out, _err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("建卡路：不打覆盖差异段", "覆盖差异" not in out, out[-400:])
        check("建卡路：不落改前快照",
              not list(Path(tmp).glob("update_before_*.txt")), tmp)


def test_update_truncated_read_no_diff():
    """回读被截断时快照只是前半截，拿它 diff 会把没取到的部分报成删除——假警报更坏。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        cut = json.dumps({"structuredContent": {"memos": [
            {"id": "ABC", "content": "#AI/测试\n\n概念名\n\n正文",
             "content_truncated": True}]}}, ensure_ascii=False)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, cut, "")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify),
                              "--update-id", "ABC"], exec_fn)
        check("截断回读：仍继续覆盖（截断不影响写入）",
              code == 0 and "memo_update" in tools(calls), f"{code} {tools(calls)}")
        check("截断回读：不打差异段", "⑥ 覆盖差异" not in out, out[-400:])
        check("截断回读：不落不可信快照",
              not list(Path(tmp).glob("update_before_*.txt")), tmp)
        check("截断回读：说明跳过原因", "跳过差异" in err, err)


def test_update_target_without_concept():
    """目标卡读得到、但只有一行非空：概念名无从比对 → 必须阻塞，不许「跳过当通过」。

    旧判据是 `len(cloud) >= 2 and len(local) >= 2 and cloud[1] != local[1]`——少一行就
    整条短路，然后照样打印「[通过] 目标卡概念名「-」」并覆盖。回包形态一变（`content`
    换位置、卡里只有一行），这道唯一的写前止损点就无声失效，失效时正好是最危险的时候。
    """
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        one_line = json.dumps({"structuredContent": {"memos": [
            {"id": "ABC", "content": "#AI/测试"}]}}, ensure_ascii=False)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, one_line, "")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify),
                              "--update-id", "ABC"], exec_fn)
        check("目标卡无概念名：退出码 6（未写云）", code == 6, code)
        check("目标卡无概念名：一个字节不发 update",
              "memo_update" not in tools(calls), tools(calls))
        check("目标卡无概念名：提示读不到概念名", "取不到概念名" in err, err)
        check("目标卡无概念名：给出放行办法", "--allow-rename" in err, err)
        check("目标卡无概念名：不谎报通过", "目标卡概念名" not in out, out[-300:])


def test_update_target_without_concept_allowed():
    """同一情形加 --allow-rename：放行，且注明是「跳过比对」而不是「比对通过」。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        one_line = json.dumps({"structuredContent": {"memos": [
            {"id": "ABC", "content": "#AI/测试"}]}}, ensure_ascii=False)
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, one_line, "")})
        code, out, _err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC", "--allow-rename"], exec_fn)
        check("无概念名 + 显式放行：退出码 0", code == 0, code)
        check("无概念名 + 显式放行：确实发了 update",
              "memo_update" in tools(calls), tools(calls))
        check("无概念名 + 显式放行：说明跳过比对而非通过",
              "[放行]" in out and "跳过概念名比对" in out, out[-400:])


def test_verify_json_unparsable():
    """复核退出码 0 但回包解析不出明细：不许说成「机器项 0 项全过」。

    「0 项」与「过了 0 项」是两件事。旧写法把 `checks` 缺省成 `[]` 后照常打印
    「[通过] 机器项 0 项全过」，读起来像复核跑过且全过，实际一条明细都没拿到。
    """
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp)
        exec_fn, _calls = make_exec(
            {"verify_card.py": (0, "not json at all", "")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("复核明细不可解析：链仍算成功（卡已写入）", code == 0, code)
        check("复核明细不可解析：不谎称 0 项全过",
              "0 项全过" not in out, out[-300:])
        check("复核明细不可解析：给出告警并指向人工核对",
              "解析不出机器项明细" in err and "--show-body" in err, err)


def test_tail_marks_truncation():
    """子脚本输出被截断时要报「还有几行没显示」，不能悄悄切掉。"""
    short = "\n".join(f"行{i}" for i in range(5))
    check("不超上限时原样转发", PB._tail(short, 12) == short, PB._tail(short, 12))

    long = "\n".join(f"行{i}" for i in range(30))
    got = PB._tail(long, 12)
    check("超上限时声明被切掉的行数", "还有 18 行未显示" in got, got.splitlines()[0])
    check("超上限时仍保留最后 12 行",
          got.splitlines()[-12:] == [f"行{i}" for i in range(18, 30)],
          got.splitlines()[-1])


def test_readings_ignore_spaces_not_dates():
    """读数按「数字+单位」取，平台补的空格不影响；日期不算读数（随来源一起删是常规）。

    这两个口径必须分开：把日期算成读数，删「据 X 某年某月某日报道」天天被拦，
    闸门一旦吵就会被绕过；而不归一化空格，云端回读的「320 万人」与快照「320万人」
    会被算成两个不同读数，同样天天误报。
    """
    same = PB.readings("约320万人") == PB.readings("约 320 万人")
    check("读数：空格不影响同一读数", same,
          f"{PB.readings('约320万人')} vs {PB.readings('约 320 万人')}")
    check("读数：日期不计入", not any("年" in t or "月" in t or "日" in t
                                   for t in PB.readings("某年 1 月 2 日报道，")),
          PB.readings("某年 1 月 2 日报道，"))
    check("读数：百分比与亿都算", set(PB.readings("涨 15%，120 亿美元")) == {"15%", "120亿"},
          PB.readings("涨 15%，120 亿美元"))


def test_lost_readings_counts_multiplicity():
    """同一读数出现两次、改后只剩一次，也算丢了一个——只看「有没有」会放过这类。"""
    check("读数丢失：少一次也报",
          PB.lost_readings("约 320 万人因空气污染死亡，另有 320 万人患病",
                           "约 320 万人因空气污染死亡") == ["320万"],
          PB.lost_readings("约 320 万人，另有 320 万人", "约 320 万人"))
    check("读数丢失：读数没少就不报",
          PB.lost_readings("约 320 万人", "约 320 万人，其余相同") == [],
          PB.lost_readings("约 320 万人", "约 320 万人，其余相同"))
    # 删来源行会带走行内复述的读数（公司卡末尾来源行普遍如此），这不算丢正文读数；
    # 但整行删掉的若是要点行，里面的读数就是真丢了，仍须报。
    check("读数：删来源行带走同值读数不算丢失",
          PB.lost_readings("正文 8.68 亿元\n来源: 报道称营收 8.68 亿元", "正文 8.68 亿元") == [],
          PB.lost_readings("正文 8.68 亿元\n来源: 报道称营收 8.68 亿元", "正文 8.68 亿元"))
    check("读数：整行删要点仍报",
          PB.lost_readings("正文 8.68 亿元\n- 要点：净利 1.45 亿元", "正文 8.68 亿元") == ["1.45亿"],
          PB.lost_readings("正文 8.68 亿元\n- 要点：净利 1.45 亿元", "正文 8.68 亿元"))
    # 旧格式状态行把定价复述一遍（工具卡「商业模式=订阅 17 美元/月」），删状态行是
    # 格式硬限要求，行里的同值读数不算丢正文读数（实测 CoolPlugz 卡）。
    check("读数：删状态行带走同值读数不算丢失",
          PB.lost_readings("- 定价：Pro 17 美元/月\n状态：订阅 17 美元/月 + 企业定制",
                           "- 定价：Pro 17 美元/月") == [],
          PB.lost_readings("- 定价：Pro 17 美元/月\n状态：订阅 17 美元/月 + 企业定制",
                           "- 定价：Pro 17 美元/月"))
    check("读数：删状态行里独有的读数仍报",
          PB.lost_readings("- 定价：Pro 17 美元/月\n状态：累计 3 家客户",
                           "- 定价：Pro 17 美元/月") == ["3家"],
          PB.lost_readings("- 定价：Pro 17 美元/月\n状态：累计 3 家客户",
                           "- 定价：Pro 17 美元/月"))
    # 来源行里的数字是引文标题的一部分（「品牌价值 2756 亿」「每日研究第 130 家」），
    # 不是卡片内容，整行免除——即便正文没有同值读数（实测通威/大北农两卡）。
    check("读数：删来源行里独有的读数不算丢失",
          PB.lost_readings("正文写营收\n来源: [链接]（官网 品牌价值 2756 亿）",
                           "正文写营收") == [],
          PB.lost_readings("正文写营收\n来源: [链接]（官网 品牌价值 2756 亿）", "正文写营收"))
    # 同一个读数两种写法（12.70% / 12.7%）是同一个数，尾零不承载信息。
    # 不归一化时，删掉末尾来源行里那个重复读数会被判成「读数丢失」（实测 6 张公司卡）。
    check("读数归一：小数尾零不算不同读数",
          PB.lost_readings("同比 -12.70%", "同比 -12.7%") == [],
          PB.lost_readings("同比 -12.70%", "同比 -12.7%"))
    check("读数归一后仍能抓真丢失",
          PB.lost_readings("同比 -12.70% 与 +3.27%", "同比 -12.70%") == ["3.27%"],
          PB.lost_readings("同比 -12.70% 与 +3.27%", "同比 -12.70%"))


def test_misaligned_deletions():
    """断口判据：删除片段必须从行首或标点后开始，括号/引号要么整对删要么不删。"""
    before = "世界卫生组织数据显示全球每年约有320万人死亡，这些污染源于烹饪"
    after = "世界卫生组织数这些污染源于烹饪"
    frags = PB.misaligned_deletions(before, after)
    check("断口：从「数」后面起删被抓出", bool(frags) and frags[0][0] == "数", frags)
    check("断口：片段内容一并给出", frags and "据显示全球每年约有320万人死亡，" in frags[0][1],
          frags)
    ok = PB.misaligned_deletions("正文（据路透社报道）继续", "正文继续")
    check("断口：整对括号删除不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("第一段\n旧内容", "第一段")
    check("断口：整行删除不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("正文；据某报 3 月 4 日报道，后续",
                                 "正文；后续")
    check("断口：标点后起删不算断口", ok == [], ok)
    ok = PB.misaligned_deletions(
        "#A/B\n\nClaude Code 桌面端新增 /resume 命令，支持跨端续接与检查点回退。\n\n要点：",
        "#A/B\n\nClaude Code /resume 命令\n\n"
        "Claude Code 桌面端新增 /resume 命令，支持跨端续接与检查点回退。\n\n要点：")
    check("断口：原概念名整句移到正文首段不算断口（改名场景）", ok == [], ok)
    ok = PB.misaligned_deletions("某基金新增68亿美元外汇额度（作者署名，发布日期）",
                                 "某基金新增68亿美元外汇额度")
    check("断口：删括号来源时把正文一起框进片段不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("陆续有大批玩家发文称此前被 Ban 的 Switch 2 已自动恢复正常",
                                 "陆续有此前被 Ban 的 Switch 2 已自动恢复正常")
    check("断口：删在「有」后面（真事故）仍抓", ok and ok[0][0] == "有", ok)
    ok = PB.misaligned_deletions("安装时选**全盘加密**并记口令", "安装时选全盘加密并记口令")
    check("断口：剥掉强调标记不算断口（内容逐字仍在）", ok == [], ok)
    ok = PB.misaligned_deletions("入口写 if __name__ == \"**main**\":", "入口写 if __name__ == \"main\":")
    check("断口：行内代码剥掉反斜粗体不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("需电信业务资质的服务（电子邮件、语音信箱、信息发布平台）不在范围",
                                 "需电信业务资质的服务（电子邮件、语音信箱、信息服务）不在范围")
    check("断口：定点替换（中段换成新文字）不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("报告全文1.5万余字，以中英文通过网站、期刊等平台发布。报告系统梳理",
                                 "报告全文1.5万余字。报告系统梳理")
    check("断口：删掉逗号后的整句不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("性能稳健。性质：厂商发布博客，含可验证规格。后续要点",
                                 "性能稳健。后续要点")
    check("断口：删掉句号后的整条注行不算断口", ok == [], ok)
    ok = PB.misaligned_deletions("数据显示，全球每年约有320万人死亡，这些污染源于烹饪",
                                 "数据显示这些污染源于烹饪")
    check("断口：删掉逗号后的句中分句仍抓", ok and ok[0][1].startswith("，全球"), ok)


def test_update_content_loss_blocks():
    """写前拦：草稿比原卡少了读数就停，一个字节不发 update。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n- 结论：全球每年约有 90 万人因此患病")
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"):
             (0, _card("- 结论：全球每年约有 320 万人因此患病"), "")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("内容丢失：退出码 7（未写云）", code == 7, code)
        check("内容丢失：不发 memo_update", "memo_update" not in tools(calls), tools(calls))
        check("内容丢失：点名少掉的读数", "读数「320万」" in err, err)
        check("内容丢失：给出放行开关", "--allow-content-loss" in err, err)


def test_update_fragment_blocks():
    """写前拦：删出的断口同样阻塞——整篇替换下这类断口在写后对账里永远看不见。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n- 数千寻智能安排千人")
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"):
             (0, _card("- 数据与成绩：千寻智能安排千人"), "")})
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                               "--update-id", "ABC"], exec_fn)
        check("断口：退出码 7（未写云）", code == 7, code)
        check("断口：不发 memo_update", "memo_update" not in tools(calls), tools(calls))
        check("断口：点名片段与前一字符", "「据与成绩：」" in err and "「数」" in err, err)


def test_update_content_loss_allowed():
    """有意重写（如把长卡压成机制卡）显式放行：仍打印丢失项，让回读能逐条认领。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        draft, verify = with_draft(tmp, "#AI/测试\n概念名\n\n- 重写后的机制要点")
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"):
             (0, _card("- 原读数：约 320 万人"), "")})
        code, out, err = run(["--draft", str(draft), "--verify", str(verify),
                              "--update-id", "ABC", "--allow-content-loss"], exec_fn)
        check("放行：退出码 0", code == 0, code)
        check("放行：仍打印丢失的读数（stderr，与阻塞同一口径）",
              "读数「320万」" in err, err[-300:])
        check("放行：打印放行说明", "--allow-content-loss" in err, err[-300:])
        check("放行：照常发 memo_update", "memo_update" in tools(calls), tools(calls))


if __name__ == "__main__":
    for fn in (test_success_chain, test_gate_blocks, test_validate_blocks,
               test_create_failure_no_retry, test_idempotent_reuse,
               test_verify_card_failure, test_dry_run, test_missing_draft,
               test_option_syntax,
               test_update_success, test_update_rename_blocked,
               test_update_rename_allowed, test_update_target_missing,
               test_update_read_unparseable,
               test_update_read_failure, test_update_failure_no_dup,
               test_allow_rename_without_update, test_update_dry_run,
               test_update_diff_reported, test_update_diff_unchanged,
               test_diff_truncated_and_large_delete,
               test_update_diff_on_verify_failure, test_create_has_no_diff,
               test_update_truncated_read_no_diff,
               test_update_target_without_concept,
               test_update_target_without_concept_allowed,
               test_readings_ignore_spaces_not_dates,
               test_lost_readings_counts_multiplicity,
               test_misaligned_deletions,
               test_update_content_loss_blocks,
               test_update_fragment_blocks,
               test_update_content_loss_allowed,
               test_verify_json_unparsable, test_tail_marks_truncation):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
