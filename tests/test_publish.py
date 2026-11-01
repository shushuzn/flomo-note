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


def make_exec(overrides=None, create_ids=None):
    """返回 (exec_fn, calls)。overrides 按脚本名（覆盖其全部子命令）或
    (脚本名, 子命令) 精确指定 (code, stdout, stderr)。

    `create_ids` 给一串 id 时，第 n 次 `memo_create` 返回第 n 个——批量必须能验出
    「id 与草稿配对」错位，而固定 id 会让所有卡看起来都指对了。
    """
    calls = []
    counter = {"create": 0}
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
        if create_ids and name == "flomo_client.py" and tool == "memo_create":
            idx = min(counter["create"], len(create_ids) - 1)
            counter["create"] += 1
            mid = create_ids[idx]
            return (0, json.dumps({"structuredContent": {"id": mid, "word_count": 600}},
                                  ensure_ascii=False),
                    f"[成功] 已写入 memo id={mid}\n")
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


def test_records_last_id():
    """建卡与更新成功后都要把 id 落盘，供 verify_card / dedup 用 `--id -` 读。

    不落盘就得从 stdout 手抄一长串 base64：抄错一位只表现为「memo not found」，
    而那个报错分不清是抄错了还是卡真没写进去。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        PB.LAST_ID_FILE = Path(tmp) / ".sop_gate" / "last_memo_id"
        draft, verify = with_draft(tmp)
        exec_fn, _calls = make_exec()
        run(["--draft", str(draft), "--verify", str(verify)], exec_fn)
        check("建卡后落盘 id", PB.LAST_ID_FILE.exists()
              and PB.LAST_ID_FILE.read_text(encoding="utf-8").strip() == "ABC",
              PB.LAST_ID_FILE.read_text(encoding="utf-8") if PB.LAST_ID_FILE.exists() else "文件不存在")

        # 更新路径同样要落盘：它的 id 来自 --update-id，不是回包，最容易漏
        PB.LAST_ID_FILE.unlink()
        run(["--draft", str(draft), "--verify", str(verify), "--update-id", "XYZ"], exec_fn)
        check("更新后落盘 id", PB.LAST_ID_FILE.exists()
              and PB.LAST_ID_FILE.read_text(encoding="utf-8").strip() == "XYZ",
              PB.LAST_ID_FILE.read_text(encoding="utf-8") if PB.LAST_ID_FILE.exists() else "文件不存在")


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
        exec_fn, _calls = make_exec({
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
    code, _out, _err = run(["--draft", "不存在的草稿.txt"], exec_fn)
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
            code = PB.CLI.cli_entry(lambda a=argv, e=exec_fn: PB.main(a, exec_fn=e),
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
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
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
    check("读数：百分比与亿都算",
          PB.readings("涨 15%，120 亿美元") == {"15%": 1, "12000000000美元": 1},
          PB.readings("涨 15%，120 亿美元"))


def test_lost_readings_counts_multiplicity():
    """同一读数出现两次、改后只剩一次，也算丢了一个——只看「有没有」会放过这类。"""
    check("读数丢失：少一次也报",
          PB.lost_readings("约 320 万人因空气污染死亡，另有 320 万人患病",
                           "约 320 万人因空气污染死亡") == ["320万人"],
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
          PB.lost_readings("正文 8.68 亿元\n- 要点：净利 1.45 亿元", "正文 8.68 亿元") == ["1.45亿元"],
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
    # 状态行紧挨着一段被改写的正文时，diff 会把「删行 + 改行」并成一个 replace 段：
    # 按 opcode 形状认删除就整段漏认，状态行里与正文重复的读数全被误报成丢失
    # （实测工业学大庆卡：改状态行上一行的括注、同轮删状态行，600万/100%/1亿 三个
    # 重复读数被拦，而卡里一个没少）。删除按「草稿里还含不含这一行」认，与形状无关。
    check("读数：改状态行上一行+删状态行，重复读数不误报",
          PB.lost_readings("- 甲：1965 年自给率达 100%，1978 年原油 1 亿吨\n"
                           "- 乙：年产 600 万吨\n"
                           "状态：大会战=建成600万吨级 / 自给率=1965 达100% / 原油=破1亿吨",
                           "- 甲：1965 年自给率达 100%，1978 年原油 1 亿吨\n"
                           "- 乙：年产 600 万吨（较 1965 年 +5 倍）") == [],
          PB.lost_readings("- 甲：1965 年自给率达 100%，1978 年原油 1 亿吨\n"
                           "- 乙：年产 600 万吨\n"
                           "状态：大会战=建成600万吨级 / 自给率=1965 达100% / 原油=破1亿吨",
                           "- 甲：1965 年自给率达 100%，1978 年原油 1 亿吨\n"
                           "- 乙：年产 600 万吨（较 1965 年 +5 倍）"))
    # 同一并段形态下，状态行里**独有**的读数仍须报——豁免只免「保留行里也有同值
    # 读数」的那几份，不能被并段漏认放宽成整行免除（民和股份「2.69 元每只」形态）。
    check("读数：改状态行上一行+删状态行，独有读数仍报",
          PB.lost_readings("- 甲：1965 年自给率达 100%\n"
                           "- 乙：年产 600 万吨\n"
                           "状态：大会战=建成600万吨级 / 鸡苗均价 2.69 元每只",
                           "- 甲：1965 年自给率达 100%\n"
                           "- 乙：年产 600 万吨（较 1965 年 +5 倍）") == ["2.69元"],
          PB.lost_readings("- 甲：1965 年自给率达 100%\n"
                           "- 乙：年产 600 万吨\n"
                           "状态：大会战=建成600万吨级 / 鸡苗均价 2.69 元每只",
                           "- 甲：1965 年自给率达 100%\n"
                           "- 乙：年产 600 万吨（较 1965 年 +5 倍）"))
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
        check("内容丢失：点名少掉的读数", "读数「320万人」" in err, err)
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
        code, _out, err = run(["--draft", str(draft), "--verify", str(verify),
                              "--update-id", "ABC", "--allow-content-loss"], exec_fn)
        check("放行：退出码 0", code == 0, code)
        check("放行：仍打印丢失的读数（stderr，与阻塞同一口径）",
              "读数「320万人」" in err, err[-300:])
        check("放行：打印放行说明", "--allow-content-loss" in err, err[-300:])
        check("放行：照常发 memo_update", "memo_update" in tools(calls), tools(calls))


# ── 批次（--batch）：预检全过才写 ────────────────────────────────────────────
# 批量替代的是「N 张卡各跑一遍 dry-run、再各写一遍」那套手工流程，价值全押在一条
# 不变量上：**预检阶段不写云，任何一张不过就一张都不写**。用例逐条钉住它，外加两个
# 只在批量里才会出现的错：id 与草稿串位、预检后草稿被改动却仍拿旧凭证去写。

def _drafts(tmp, n=3):
    """造 n 份草稿 + 留痕（概念名与正文各不相同）。"""
    rows = []
    for i in range(1, n + 1):
        draft = Path(tmp) / f"draft_b{i}.txt"
        draft.write_text(f"#AI/测试\n概念{i}\n\n正文{i}", encoding="utf-8")
        verify = Path(tmp) / f"verify_b{i}.json"
        verify.write_text('{"searched": true, "terms": []}', encoding="utf-8")
        rows.append((draft, verify))
    return rows


def _manifest(tmp, lines, name="batch.txt"):
    path = Path(tmp) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _manifest_rows(tmp, rows, extra=None):
    return _manifest(tmp, [f"{d} | {v} | -" for d, v in rows] + (extra or []))


def writes(calls):
    return [c for c in calls if len(c) > 1 and Path(c[1]).name == "flomo_client.py"]


def test_batch_precheck_blocks_all():
    """第 2 张预检不过：一张都不写，第 3 张也不用再跑。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp)
        man = _manifest_rows(tmp, rows)

        calls = []

        def exec_fn(cmd, timeout=600):
            calls.append(list(cmd))
            name = Path(cmd[1]).name
            if name == "sop_gate.py":
                if sum(1 for c in calls if Path(c[1]).name == "sop_gate.py") == 2:
                    return 2, "", "[gate] 第 2 张概念名不合规"
                return 0, "[gate] 通过", ""
            if name == "validate_memo.py":
                return 0, "[validate] 0 ERR", ""
            return 0, '{"structuredContent": {"id": "X"}}', "[成功] 已写入 memo id=X\n"

        code, _out, err = run(["--batch", str(man)], exec_fn)
        check("批次预检：退出码 2", code == 2, code)
        check("批次预检：一张都不写", writes(calls) == [], writes(calls))
        check("批次预检：拦下后不再跑第 3 张",
              sum(1 for c in calls if Path(c[1]).name == "sop_gate.py") == 2, names(calls))
        check("批次预检：明说本批一张都没写", "本批一张都没写" in err, err)


def test_batch_all_write():
    """三张全过：逐张写、id 各自解析、复核与草稿逐张配对、汇总表可直接引用。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp)
        man = _manifest_rows(tmp, rows)
        exec_fn, calls = make_exec(create_ids=["M1", "M2", "M3"])
        code, out, _err = run(["--batch", str(man)], exec_fn)

        check("批次写入：退出码 0", code == 0, code)
        check("批次写入：三张各写一次",
              sum(1 for c in writes(calls) if c[2] == "memo_create") == 3, tools(calls))
        verifies = [c for c in calls if Path(c[1]).name == "verify_card.py"]
        pairs = [(v[v.index("--id") + 1], Path(v[v.index("--draft") + 1]).name)
                 for v in verifies]
        check("批次写入：复核的 id 与草稿逐张配对（防串位）",
              pairs == [("M1", "draft_b1.txt"), ("M2", "draft_b2.txt"),
                        ("M3", "draft_b3.txt")], pairs)
        # 写入阶段重跑闸门 = 每张多一次云端检索。这条断言防的是「顺手加一道更保险」——
        # 它会让批量的耗时退回手工逐张跑的规模，而批量存在的理由正是省掉这一遍。
        check("批次写入：闸门只跑预检那一遍",
              sum(1 for c in calls if Path(c[1]).name == "sop_gate.py") == 3, names(calls))
        check("批次写入：汇总表给 id 与实测字数",
              "[批次汇总] 计划 3 张 / 本次写入 3 张" in out
              and "id=M1 600 字" in out, out[-500:])
        check("批次写入：给出可直接引用的已写清单",
              "[批次] 已写 id：M1 M2 M3" in out, out[-300:])


def test_batch_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp)
        man = _manifest_rows(tmp, rows)
        exec_fn, calls = make_exec()
        code, out, _err = run(["--batch", str(man), "--dry-run"], exec_fn)
        check("批次 dry-run：只预检不写", writes(calls) == [], writes(calls))
        check("批次 dry-run：退出码 0 且报全部通过",
              code == 0 and "3 张全部通过预检" in out, out[-200:])


def test_batch_manifest_errors():
    """清单写错一律在跑子脚本之前拦下：错在这里的代价是「跑到一半才发现」。"""
    with tempfile.TemporaryDirectory() as tmp:
        rows = _drafts(tmp, 2)
        # 复制一份草稿改个文件名：路径不同、正文相同。按路径判重会安静放过它，
        # 而这正是「以为在写新的一张卡、其实把同一份内容又发了一遍」的错法。
        dup = Path(tmp) / "draft_copy.txt"
        dup.write_bytes(rows[0][0].read_bytes())
        cases = [
            ("草稿不存在", [f"{tmp}/nope.txt | - | -"], "草稿不存在"),
            ("留痕不存在",
             [f"{rows[0][0]} | {tmp}/nope.json | -"], "验证留痕不存在"),
            ("同一草稿两次",
             [f"{rows[0][0]} | - | -", f"{rows[0][0]} | - | -"], "同一份草稿"),
            ("同内容不同路径",
             [f"{rows[0][0]} | - | -", f"{dup} | - | -"], "正文完全相同"),
            ("同一 id 覆盖两次",
             [f"{rows[0][0]} | - | M9", f"{rows[1][0]} | - | M9"], "覆盖同一张卡"),
            ("字段过多", [f"{rows[0][0]} | - | - | rename | skipweb | 多余"], "字段过多"),
            ("第 5 段不是已知开关", [f"{rows[0][0]} | - | - | - | 多余"], "只接受 `skipweb`"),
            ("skipweb 与留痕同给",
             [f"{rows[0][0]} | {rows[0][1]} | - | - | skipweb"], "互相矛盾"),
            ("第 4 段不是 rename", [f"{rows[0][0]} | - | - | yes"], "只接受 `rename`"),
            ("rename 没有覆盖 id",
             [f"{rows[0][0]} | - | - | rename"], "却没有覆盖 id"),
            # 续跑时把已成功的行全注释掉、剩下的也没了——真实会发生，提示要指回清单本身
            ("全是注释", ["# 都写完了", "# 没有条目"], "没有任何条目"),
        ]
        for idx, (label, lines, want) in enumerate(cases):
            man = _manifest(tmp, lines, name=f"m{idx}.txt")
            exec_fn, calls = make_exec()
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = PB.CLI.cli_entry(
                    lambda m=man, e=exec_fn: PB.main(["--batch", str(m)], exec_fn=e),
                    errors=(PB.FC.FlomoError,), usage_code=1)
            check(f"清单错（{label}）：退出码 1", code == 1, code)
            check(f"清单错（{label}）：提示含「{want}」", want in err.getvalue(),
                  err.getvalue())
            check(f"清单错（{label}）：一个子脚本都没跑", calls == [], calls)


def test_batch_rejects_single_draft_flags():
    """`--batch` 与单卡选项同给：直接拒，不猜「作用于第一条」还是「当默认值」。"""
    with tempfile.TemporaryDirectory() as tmp:
        rows = _drafts(tmp, 1)
        man = _manifest_rows(tmp, rows)
        exec_fn, calls = make_exec()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = PB.CLI.cli_entry(
                lambda: PB.main(["--batch", str(man), "--draft", str(rows[0][0])],
                                exec_fn=exec_fn),
                errors=(PB.FC.FlomoError,), usage_code=1)
        check("--batch + --draft：退出码 1", code == 1, code)
        check("--batch + --draft：说清为何不能同给",
              "--batch 与 --draft" in err.getvalue(), err.getvalue())
        check("--batch + --draft：一个子脚本都没跑", calls == [], calls)


def test_batch_shared_line_blocks():
    """同批两张卡出现同一整行：写云前拦下，并点出是哪两张、哪一行。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 2)
        shared = "参赛资格：市级、国家级体育传统项目学校为基本参赛范围"
        rows[0][0].write_text(f"#教育/基础教育\n学校\n\n要点：\n- {shared}", encoding="utf-8")
        rows[1][0].write_text(f"#体育/赛事\n系列比赛\n\n要点：\n- {shared}",
                              encoding="utf-8")
        man = _manifest_rows(tmp, rows)
        exec_fn, calls = make_exec()
        code, _out, err = run(["--batch", str(man)], exec_fn)
        check("同批整行重复：退出码 3", code == 3, code)
        check("同批整行重复：一个字节都没写", calls == [], calls)
        check("同批整行重复：点出两张卡", "学校 与 系列比赛" in err, err)
        check("同批整行重复：点出那一行", "市级、国家级体育传统项目学校" in err, err)


def test_batch_shared_line_ignores_short_and_tag_lines():
    """标签行、短行不算重复：两张卡用同一套标签、同一句「要点：」是常态。

    标签行要用**够长**的两个标签造（`#` 与 `/` 不在标点表里，一拼就过 10 字），
    否则这条用例只验到「短行不拦」，验不到标签行被排除——它本来就该被排除。
    """
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 2)
        tags = "#AI/安全 #AI/对齐"
        rows[0][0].write_text(f"{tags}\n甲\n\n要点：\n- 一句话\n正文甲", encoding="utf-8")
        rows[1][0].write_text(f"{tags}\n乙\n\n要点：\n- 一句话\n正文乙", encoding="utf-8")
        man = _manifest_rows(tmp, rows)
        exec_fn, _calls = make_exec()
        code, _out, _err = run(["--batch", str(man)], exec_fn)
        check("短行/标签行相同：不拦", code == 0, code)
        check("标签行本身不进比对键",
              PB.shared_lines([f"{tags}\n甲\n\n- 甲的一条独立要点", f"{tags}\n乙\n\n- 乙的一条独立要点"]) == [],
              PB.shared_lines([f"{tags}\n甲\n\n- 甲的一条独立要点", f"{tags}\n乙\n\n- 乙的一条独立要点"]))


def test_batch_overlap_note():
    """整行不同但成大段字面重合：只提示不拦（换个说法抄同一段事实）。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 2)
        para = "北京市体育局与北京市教育委员会共同主办面向全市传统校学生的年度系列赛事"
        rows[0][0].write_text(f"#体育/赛事\n甲\n\n- {para}其一", encoding="utf-8")
        rows[1][0].write_text(f"#体育/赛事\n乙\n\n- {para}其二", encoding="utf-8")
        man = _manifest_rows(tmp, rows)
        exec_fn, _calls = make_exec()
        code, out, _err = run(["--batch", str(man)], exec_fn)
        check("字面重合：只提示不拦", code == 0, code)
        check("字面重合：给出重合比例", "字面重合" in out and "%" in out, out[-400:])


def test_batch_partial_failure():
    """第 2 张写入失败：已写的照实报告、不重发、不继续——重跑时只跑余下的。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp)
        man = _manifest_rows(tmp, rows)

        calls = []

        def exec_fn(cmd, timeout=600):
            calls.append(list(cmd))
            name = Path(cmd[1]).name
            tool = cmd[2] if len(cmd) > 2 and not cmd[2].startswith("--") else ""
            if name in ("sop_gate.py", "validate_memo.py"):
                return 0, "[ok]", ""
            if name == "flomo_client.py" and tool == "memo_create":
                n = sum(1 for c in calls if len(c) > 2 and c[2] == "memo_create")
                if n == 2:
                    return 1, "", "[失败] 超时"
                return 0, '{"structuredContent": {"id": "M1"}}', "[成功] 已写入 memo id=M1\n"
            return 0, VERIFY_JSON, ""

        code, out, err = run(["--batch", str(man)], exec_fn)
        check("批次中途失败：退出码 4", code == 4, code)
        check("批次中途失败：create 只发了 2 次（失败那张不重发）",
              sum(1 for c in calls if len(c) > 2 and c[2] == "memo_create") == 2,
              tools(calls))
        check("批次中途失败：不写第 3 张",
              sum(1 for c in calls if Path(c[1]).name == "sop_gate.py") == 3, names(calls))
        check("批次中途失败：汇总只报已写成的那张",
              "[批次] 已写 id：M1" in out and "计划 3 张 / 本次写入 1 张" in out, out[-400:])
        # 只列已尝试的几张，人会以为这批就这么多；「还剩哪几张没写」正是失败后唯一要知道的
        check("批次中途失败：汇总列出未执行的第 3 张", "3 - 未执行 概念3" in out, out[-400:])
        check("批次中途失败：给出续跑办法", "注释掉" in err, err)


def test_batch_rechecks_changed_draft():
    """预检之后草稿被改动：写入前必须重跑质检，不能拿旧凭证写没被核过的正文。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 2)
        man = _manifest_rows(tmp, rows)

        calls = []

        def exec_fn(cmd, timeout=600):
            calls.append(list(cmd))
            name = Path(cmd[1]).name
            tool = cmd[2] if len(cmd) > 2 and not cmd[2].startswith("--") else ""
            if name in ("sop_gate.py", "validate_memo.py"):
                return 0, "[ok]", ""
            if name == "flomo_client.py" and tool == "memo_create":
                n = sum(1 for c in calls if len(c) > 2 and c[2] == "memo_create")
                if n == 1:  # 写第一张时改掉第二张的草稿
                    rows[1][0].write_text("#AI/测试\n概念2\n\n正文2（预检后改过）",
                                          encoding="utf-8")
                return 0, '{"structuredContent": {"id": "M1"}}', "[成功] 已写入 memo id=M1\n"
            return 0, VERIFY_JSON, ""

        code, out, _err = run(["--batch", str(man)], exec_fn)
        check("草稿改动：被拦下并重跑质检",
              "草稿在预检之后被改动过" in out, out[-600:])
        check("草稿改动：质检一共跑 3 次（2 次预检 + 1 次重跑）",
              sum(1 for c in calls if Path(c[1]).name == "validate_memo.py") == 3,
              names(calls))
        check("草稿改动：重跑通过后仍写入", code == 0, code)


def test_batch_update_route():
    """清单第三段给 id 时走覆盖路：预检期核一次身份、写入前再核一次，绝不误用 memo_create。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 1)
        man = _manifest(tmp, [f"{rows[0][0]} | {rows[0][1]} | ABC"])
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _other_card("概念1"), "")})
        code, _out, _err = run(["--batch", str(man)], exec_fn)
        check("批次覆盖：退出码 0", code == 0, code)
        # 两次读同一张卡，职责不同：预检那次保证「整批不写」成立，写前那次是单张止损点
        check("批次覆盖：batch_get 两次（预检 + 写前确认）",
              tools(calls).count("memo_batch_get") == 2, tools(calls))
        seq = tools(calls)
        check("批次覆盖：update 之前紧邻一次 batch_get",
              seq.index("memo_update") == len(seq) - 2
              and seq[seq.index("memo_update") - 1] == "memo_batch_get", seq)
        check("批次覆盖：不误用 memo_create",
              "memo_create" not in seq, seq)
        last = calls[-1]
        check("批次覆盖：复核指向被覆盖的 id",
              last[last.index("--id") + 1] == "ABC", last)
        # 预检那次闸门必须收到 --anchor-id：闸门的查重会把这行要覆盖的卡本身当成
        # 「同名重复卡」拦下，整批一张都写不出去（真卡实测过）。
        gate_calls = [c for c in calls if Path(c[1]).name == "sop_gate.py"]
        check("批次覆盖：预检闸门带 --anchor-id",
              bool(gate_calls) and "--anchor-id" in gate_calls[0],
              gate_calls)
        check("批次覆盖：--anchor-id 指向被覆盖的 id",
              bool(gate_calls) and gate_calls[0][gate_calls[0].index("--anchor-id") + 1] == "ABC",
              gate_calls)


def test_batch_update_id_mismatch_blocks_all():
    """覆盖 id 抄错：预检期就拦下，且一张都不写——「一张都不写」也包括过了预检的前几张。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 2)
        man = _manifest(tmp, [f"{rows[0][0]} | {rows[0][1]} | -",
                              f"{rows[1][0]} | {rows[1][1]} | ABC"])
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _other_card("别的概念"), "")})
        code, _out, err = run(["--batch", str(man)], exec_fn)
        check("批次 id 抄错：退出码 6", code == 6, code)
        check("批次 id 抄错：预检期拦下，没有 memo_update",
              "memo_update" not in tools(calls), tools(calls))
        check("批次 id 抄错：一张都没写（也没有 memo_create）",
              "memo_create" not in tools(calls), tools(calls))
        check("批次 id 抄错：说清疑似抄错", "疑似 id 抄错" in err, err)
        check("批次 id 抄错：明说本批一张都没写", "本批一张都没写" in err, err)


def test_batch_rename_column():
    """清单第 4 段 rename：概念名不同也照写——这是批量改造存量卡的路，替代逐张 --allow-rename。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp, 1)
        man = _manifest(tmp, [f"{rows[0][0]} | {rows[0][1]} | ABC | rename"])
        exec_fn, calls = make_exec(
            {("flomo_client.py", "memo_batch_get"): (0, _other_card("旧概念"), "")})
        code, out, _err = run(["--batch", str(man)], exec_fn)
        check("批次改名：退出码 0", code == 0, code)
        check("批次改名：打印改名放行", "概念名改名" in out, out[-600:])
        check("批次改名：确实覆盖了目标卡", "memo_update" in tools(calls), tools(calls))


def test_batch_next_steps():
    """收尾给逐张人读命令：批量最容易漏的就是这一项，而 N 个 id 没人愿意手抄 N 次。"""
    with tempfile.TemporaryDirectory() as tmp:
        PB.PAYLOAD = Path(tmp) / "payload.json"
        rows = _drafts(tmp)
        man = _manifest_rows(tmp, rows)
        exec_fn, _calls = make_exec(create_ids=["M1", "M2", "M3"])
        _code, out, _err = run(["--batch", str(man)], exec_fn)
        check("批量收尾：给出逐张 --show-body 的循环",
              "for id in M1 M2 M3; do python scripts/verify_card.py --id $id --show-body; "
              "done" in out, out[-600:])
        check("批量收尾：给出 dedup 定向复查",
              "python scripts/dedup.py --id $id" in out, out[-400:])

        # 复核被关掉时不提示跑 verify_card，也不该冒出「None 字」
        man1 = _manifest(tmp, [f"{rows[0][0]} | {rows[0][1]} | -"], name="one.txt")
        exec_fn, _calls = make_exec(create_ids=["N1"])
        _c, out2, _e = run(["--batch", str(man1), "--no-verify-card"], exec_fn)
        check("批量收尾：关了复核就不提示 verify_card",
              "verify_card.py --id $id" not in out2 and "dedup.py --id $id" in out2,
              out2[-500:])
        check("批量收尾：没复核就不报字数（不打印 None）",
              "None" not in out2, out2[-500:])


def test_readings_scale_equivalence():
    """同一读数换了写法（30,000 台 / 3 万台、109,000 美元 / 10.9 万美元）不判丢失。

    按字面比对时，改写一张卡就会被判「读数丢失」，人只能加 --allow-content-loss
    放行；放行用多了这道闸门就形同虚设。归一后比的是数值 + 真实单位，写法差异
    不该阻塞，真丢的读数照旧拦。
    """
    check("读数归一：30,000 台 == 3 万台",
          PB.readings("交付 30,000 台") == PB.readings("交付 3 万台"),
          f"{PB.readings('交付 30,000 台')} vs {PB.readings('交付 3 万台')}")
    check("读数归一：109,000 美元 == 10.9 万美元",
          PB.readings("售价 109,000 美元") == PB.readings("售价 10.9 万美元"),
          f"{PB.readings('售价 109,000 美元')} vs {PB.readings('售价 10.9 万美元')}")
    check("读数归一：跨写法改写不算丢失",
          PB.lost_readings("交付 30,000 台，售价 109,000 美元",
                           "交付 3 万台，售价 10.9 万美元") == [],
          PB.lost_readings("交付 30,000 台，售价 109,000 美元",
                           "交付 3 万台，售价 10.9 万美元"))
    check("读数归一：少一个数量级仍报丢失",
          PB.lost_readings("交付 30,000 台", "交付 3,000 台") == ["30,000台"],
          PB.lost_readings("交付 30,000 台", "交付 3,000 台"))
    check("读数归一：单位不同不混为一谈",
          PB.readings("30,000 台") != PB.readings("30,000 元"),
          f"{PB.readings('30,000 台')} vs {PB.readings('30,000 元')}")


def test_manifest_per_line_flags():
    """第 5 段 `skipweb` / `loss` 逐行生效：混批里一行跳过网络验证、另一行照常带留痕。"""
    with tempfile.TemporaryDirectory() as tmp:
        rows = _drafts(tmp, 2)
        man = _manifest(tmp, [
            f"{rows[0][0]} | - | - | - | skipweb,loss",
            f"{rows[1][0]} | {rows[1][1]} | -",
        ])
        items = PB.parse_manifest(man)
        check("清单：skipweb,loss 同时解析",
              items[0]["skip_web"] and items[0]["allow_content_loss"],
              items[0])
        check("清单：未标开关的行两项都为 False",
              not items[1]["skip_web"] and not items[1]["allow_content_loss"],
              items[1])

        PB.PAYLOAD = Path(tmp) / "payload.json"
        exec_fn, calls = make_exec()
        code, _out, _err = run(["--batch", str(man)], exec_fn)
        gates = [c for c in calls if len(c) > 1 and Path(c[1]).name == "sop_gate.py"]
        check("混批：退出码 0", code == 0, code)
        check("混批：第 1 行闸门带 --skip-web",
              "--skip-web" in gates[0], gates[0])
        check("混批：第 2 行闸门不带 --skip-web",
              "--skip-web" not in gates[1], gates[1])
        check("混批：第 2 行仍带留痕",
              any("--verify" in c for c in gates[1]), gates[1])


def test_diff_reports_tag_change():
    """覆盖时标签被换掉，差异里要有一行「标签 旧 → 新」——身份确认只比概念名。"""
    rep = PB.diff_report("#AI/测试\n概念名\n\n正文", "#AI/测试 #AI/新标签\n概念名\n\n正文")
    check("差异：给出改前标签", rep["tags_before"] == ["#AI/测试"], rep["tags_before"])
    check("差异：给出改后标签",
          rep["tags_after"] == ["#AI/测试", "#AI/新标签"], rep["tags_after"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        PB._print_diff(rep)
    out = buf.getvalue()
    check("差异：打印标签变化与增删",
          "[标签]" in out and "加上 #AI/新标签" in out, out)

    rep2 = PB.diff_report("#AI/测试\n概念名\n\n正文", "#AI/测试\n概念名\n\n正文")
    check("差异：标签未变时前后一致", rep2["tags_before"] == rep2["tags_after"],
          rep2["tags_before"])


def test_precheck_echoes_concept():
    """预检通过后必须回显第二行：质检只认词表内动词，写卡人得亲眼判一遍（H6b/H6d）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d, _v = with_draft(Path(td), "#AI/测试\n某机首拆\n\n正文")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            PB._echo_concept(str(d))
        out = buf.getvalue()
    check("回显概念名", "某机首拆" in out, out[:120])
    check("回显标签行", "#AI/测试" in out, out[:120])
    check("提示删动词测试", "删动词测试" in out, out[:120])


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
               test_verify_json_unparsable, test_tail_marks_truncation,
               test_batch_precheck_blocks_all, test_batch_all_write,
               test_batch_dry_run, test_batch_manifest_errors,
               test_batch_rejects_single_draft_flags, test_batch_partial_failure,
               test_batch_rechecks_changed_draft, test_batch_update_route,
               test_batch_update_id_mismatch_blocks_all, test_batch_rename_column,
               test_batch_next_steps, test_precheck_echoes_concept,
               test_batch_shared_line_blocks,
               test_batch_shared_line_ignores_short_and_tag_lines,
               test_batch_overlap_note,
               test_records_last_id):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
