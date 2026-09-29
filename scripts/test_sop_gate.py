#!/usr/bin/env python3
"""sop_gate.py 与「闸门接入 validate_memo.py」的离线回归用例。

全部用例**不联网**：涉及 flomo 调用的部分用假 client 桩替换，
只验证"凭证生成 / 校验"这套机械逻辑本身是否正确。
联网侧的实跑由维护者手工执行（见 SKILL「维护约定」）。

退出码 0 = 全部通过。
"""
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


GATE = _load("sop_gate", "sop_gate.py")
VM = _load("validate_memo", "validate_memo.py")

BODY = (
    "#科技/机器人\n"
    "某测试概念名\n"
    "\n"
    "结论句。\n"
    "\n"
    "要点：\n"
    "- 要点一\n"
)

RESULTS = []


def check(label, ok):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def _write_gate(sig, body_hash, *, expires_delta=3600, web=None, key_sig=None):
    """在临时 gate 目录写一张凭证，并让 VM 指向该目录。

    `key_sig` 单独指定落盘用的文件名签名（默认同 `sig`）；用于构造
    "文件名对得上、但凭证内 signature 字段是另一张卡"的签名不符场景。
    """
    tmp = Path(tempfile.mkdtemp())
    VM.GATE_DIR = tmp
    ks = key_sig or sig
    sig_key = hashlib.sha256(
        (ks["tagline"] + "\n" + ks["concept"]).encode("utf-8")
    ).hexdigest()[:16]
    gate = {
        "signature": sig,
        "sig_key": sig_key,
        "body_hash": body_hash,
        "generated_at": int(time.time()),
        "expires_at": int(time.time()) + expires_delta,
        "web": web if web is not None else {"searched": True, "term_count": 2},
        "dedup": {},
        "review": {},
    }
    (tmp / f"{sig_key}.json").write_text(
        json.dumps(gate, ensure_ascii=False), encoding="utf-8"
    )
    return tmp


def run_signature_cases():
    """签名提取：与 flomo_client 同口径（前两个非空行）。"""
    sig = GATE._signature(BODY)
    check("签名取标签行+概念名行",
          sig == {"tagline": "#科技/机器人", "concept": "某测试概念名"})
    check("不足两行返回 None", GATE._signature("#标签\n") is None)
    check("首行非标签返回 None", GATE._signature("普通行\n概念\n") is None)
    check("首行标签、第二行空（H14 变体）仍取到概念名",
          GATE._signature("#科技/机器人\n\n概念名\n正文\n")
          == {"tagline": "#科技/机器人", "concept": "概念名"})


def run_gate_check_cases():
    """validate_memo.check_gate 的四类判定。"""
    sig = VM._signature_of(BODY)

    # 1) 凭证缺失 → ERR
    VM.GATE_DIR = Path(tempfile.mkdtemp())
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("无凭证判 ERR", len(VM.ERR) == 1 and "缺少 SOP 流程闸门凭证" in VM.ERR[0])

    # 2) 凭证正常 → 无错
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest())
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("凭证齐备不报错", len(VM.ERR) == 0)

    # 3) 正文指纹不符 → ERR（防"先跑闸门后改正文"）
    _write_gate(sig, hashlib.sha256(("别的正文" + BODY).encode()).hexdigest())
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("正文指纹不符判 ERR",
          len(VM.ERR) == 1 and "正文指纹" in VM.ERR[0])

    # 4) 凭证过期 → ERR
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(), expires_delta=-10)
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("凭证过期判 ERR", any("已过期" in m for m in VM.ERR))

    # 5) 签名不符 → ERR（文件名对得上，但凭证内 signature 是另一张卡）
    other = {"tagline": "#时政/反腐", "concept": "另一张卡的概念名"}
    _write_gate(other, hashlib.sha256(BODY.encode()).hexdigest(), key_sig=sig)
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("签名不符判 ERR", any("签名与当前卡片不符" in m for m in VM.ERR))

    # 6) 验证未做（web 无 searched 且未 skip）→ ERR
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(),
                web={"searched": False, "term_count": 0})
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("验证未执行判 ERR", any("验证（网络搜索）未执行" in m for m in VM.ERR))

    # 7) 显式 skip-web → 不报验证错
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(),
                web={"web_skipped": True})
    VM.ERR.clear(); VM.WARN.clear()
    VM.check_gate(BODY)
    check("显式跳过网络验证不报错", len(VM.ERR) == 0)


def run_no_gate_cases():
    """--no-gate 降级：闸门问题变 WARN，不阻断。"""
    VM.GATE_DIR = Path(tempfile.mkdtemp())
    orig_argv = sys.argv
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(BODY)
            p = f.name
        sys.argv = ["validate_memo.py", "--no-gate", p]
        rc = VM.main()
    finally:
        sys.argv = orig_argv
    check("--no-gate 不阻断（退出码 0）", rc == 0)


def run_local_tag_tree_cases():
    """本地快照计数口径：二级行 + 裸顶层。"""
    lines = [
        "# total=3",
        "# AI",
        "  AI/物理AI",
        "# 投资",
        "投资/",
        "  投资/一级市场",
    ]
    leaves, bare = GATE._count_local_snapshot(lines)
    check("快照计数=二级行+裸顶层", (leaves, bare) == (2, 1))


if __name__ == "__main__":
    run_signature_cases()
    run_gate_check_cases()
    run_no_gate_cases()
    run_local_tag_tree_cases()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
