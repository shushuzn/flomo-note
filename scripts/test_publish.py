#!/usr/bin/env python3
"""publish.py 的离线回归用例（不联网、不写云）。

用假执行器驱动整条链，钉住三条纪律：闸门/质检不过绝不建卡；create 只发一次、
失败不重发；写后复核失败只报告。
退出码 0 = 全部通过。
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
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


def make_exec(overrides=None):
    """返回 (exec_fn, calls)。overrides 按脚本名替换 (code, stdout, stderr)。"""
    calls = []
    table = {
        "sop_gate.py": (0, "[gate] 通过", ""),
        "validate_memo.py": (0, "[validate] 0 ERR", ""),
        "flomo_client.py": (0, '{"structuredContent": {"id": "ABC", "word_count": 600}}',
                            "[成功] 已写入 memo id=ABC\n"),
        "verify_card.py": (0, VERIFY_JSON, ""),
    }
    table.update(overrides or {})

    def _exec(cmd, timeout=600):
        calls.append(list(cmd))
        name = Path(cmd[1]).name if len(cmd) > 1 else cmd[0]
        return table.get(name, (0, "", ""))

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


if __name__ == "__main__":
    for fn in (test_success_chain, test_gate_blocks, test_validate_blocks,
               test_create_failure_no_retry, test_idempotent_reuse,
               test_verify_card_failure, test_dry_run, test_missing_draft):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
