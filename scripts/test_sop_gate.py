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


def run_check_web_cases():
    """_check_web 的四类判定（离线，只读文件）。"""
    import json as _json

    def _verify_file(payload):
        tmp = Path(tempfile.mkdtemp()) / "verify.json"
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return tmp

    # 1) 未提供验证记录 → blocker
    b, n = [], []
    GATE._check_web(None, b, n)
    check("_check_web 未提供记录判阻塞", any("未提供第 2 步验证记录" in x for x in b))

    # 2) 文件不存在 → blocker
    b, n = [], []
    GATE._check_web("/nonexistent/verify.json", b, n)
    check("_check_web 文件不存在判阻塞", any("不存在" in x for x in b))

    # 3) searched=false → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": False, "terms": []}), b, n)
    check("_check_web searched=false 判阻塞",
          any("未真正执行网络搜索" in x for x in b))

    # 4) terms 为空 → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": []}), b, n)
    check("_check_web terms 为空判阻塞", any("terms 为空" in x for x in b))

    # 5) 条目缺 conclusion → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True,
                                  "terms": [{"term": "某术语", "query": "q"}]}), b, n)
    check("_check_web 条目缺 conclusion 判阻塞",
          any("缺 term/conclusion" in x for x in b))

    # 6) 合法记录 → 通过且计数正确
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "A", "query": "qa", "conclusion": "ca"},
        {"term": "B", "query": "qb", "conclusion": "cb"},
    ]}), b, n)
    check("_check_web 合法记录通过",
          b == [] and got == {"searched": True, "term_count": 2})

    # 7) 非法 JSON → blocker
    tmp = Path(tempfile.mkdtemp()) / "bad.json"
    tmp.write_text("{不是 JSON", encoding="utf-8")
    b, n = [], []
    GATE._check_web(tmp, b, n)
    check("_check_web 非法 JSON 判阻塞", any("非法 JSON" in x for x in b))


def run_concept_keyword_cases():
    """_concept_keywords 边界（经 memo_util 委托）。"""
    kw = GATE._concept_keywords("德塔智能 Delta 0 双足人形机器人基础模型")
    check("_concept_keywords 派生非空", len(kw) >= 1 and all(kw))
    check("_concept_keywords 限长 2", len(GATE._concept_keywords("某概念名称")) <= 2)


class FakeClient:
    """假 flomo client：按工具名返回预设响应，并可记录调用参数。"""

    def __init__(self, tag_tree=None, search=None, recommended=None):
        self._tag_tree = tag_tree if tag_tree is not None else {}
        self._search = search or {}
        self._recommended = recommended or {}
        self.calls = []

    def tool(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        if name == "tag_tree":
            return self._tag_tree
        if name == "memo_search":
            kw = (args or {}).get("keywords", "")
            return self._search.get(kw, {"structuredContent": {"memos": []}})
        if name == "memo_recommended":
            return self._recommended
        return {}


def _tt(total, names):
    """构造 tag_tree 响应（structuredContent 形态）。"""
    return {"structuredContent": {"total": total,
                                  "tags": [{"name": n} for n in names]}}


def run_tag_tree_net_cases():
    """_tag_tree_total_and_count / _check_tag_tree（假 client，不联网）。"""
    # 1) structuredContent 直取，且 limit 必须传 2000
    c = FakeClient(tag_tree=_tt(600, ["AI/物理AI", "投资/一级市场"]))
    total, tags = GATE._tag_tree_total_and_count(c)
    check("tag_tree 取 structuredContent.total", total == 600)
    check("tag_tree 条数正确", len(tags) == 2)
    check("tag_tree 必须传 limit=2000",
          c.calls and c.calls[0][1].get("limit") == 2000)

    # 2) structuredContent 缺失 → 回落解析 content[].text 内层 JSON
    fallback = {"content": [{"text": json.dumps({"total": 42, "tags": [{"name": "A/b"}]})}]}
    total, tags = GATE._tag_tree_total_and_count(FakeClient(tag_tree=fallback))
    check("tag_tree 回落解析 content[].text", total == 42 and len(tags) == 1)

    # 3) 两者皆无 → (None, [])
    total, tags = GATE._tag_tree_total_and_count(FakeClient(tag_tree={}))
    check("tag_tree 无数据返回 None", total is None and tags == [])

    # 4) total 缺失 → 判阻塞
    b, n = [], []
    GATE._check_tag_tree(FakeClient(tag_tree={"structuredContent": {"tags": []}}), b, n)
    check("_check_tag_tree total 缺失判阻塞",
          any("未取到 structuredContent.total" in x for x in b))

    # 5) 快照自洽（total=3 = 二级行2 + 裸顶层1）→ 无阻塞
    d = Path(tempfile.mkdtemp())
    snap = d / "tag_tree.txt"
    snap.write_text("# total=3\n# AI\n  AI/物理AI\n# 投资\n投资/\n  投资/一级市场\n",
                    encoding="utf-8")
    orig = GATE._local_tag_tree_paths
    try:
        GATE._local_tag_tree_paths = lambda: [snap]
        b, n = [], []
        GATE._check_tag_tree(FakeClient(tag_tree=_tt(3, ["AI/物理AI", "投资/一级市场"])), b, n)
        check("_check_tag_tree 自洽通过", b == [] and any("二级行=2" in x for x in n))

        # 6) 首行 total 与云端不一致 → 判阻塞
        snap.write_text("# total=9\n# AI\n  AI/物理AI\n", encoding="utf-8")
        b, n = [], []
        GATE._check_tag_tree(FakeClient(tag_tree=_tt(3, [])), b, n)
        check("_check_tag_tree total 不一致判阻塞",
              any("与云端 total=3 不一致" in x for x in b))

        # 7) 快照列出数与首行 total 不自洽 → 判阻塞
        snap.write_text("# total=3\n# AI\n  AI/物理AI\n", encoding="utf-8")
        b, n = [], []
        GATE._check_tag_tree(FakeClient(tag_tree=_tt(3, [])), b, n)
        check("_check_tag_tree 列出数不自洽判阻塞",
              any("不自洽" in x for x in b))

        # 8) 找不到任何快照 → 判阻塞
        GATE._local_tag_tree_paths = lambda: [d / "nope.txt"]
        b, n = [], []
        GATE._check_tag_tree(FakeClient(tag_tree=_tt(3, [])), b, n)
        check("_check_tag_tree 无快照判阻塞", any("未找到任何本地 tag_tree 快照" in x for x in b))
    finally:
        GATE._local_tag_tree_paths = orig


def run_dedup_net_cases():
    """_check_dedup 两路查重（假 client，不联网）。"""
    sig = GATE._signature(BODY)
    # 1) 关键词无法派生 → 判阻塞
    b, n = [], []
    got = GATE._check_dedup(FakeClient(), {"concept": "#", "tagline": "#A/b"}, b, n)
    check("_check_dedup 关键词无法派生判阻塞", any("无法从概念名派生查重关键词" in x for x in b))
    check("_check_dedup 关键词缺失败返回空", got == {})

    # 2) 正常两路：关键词命中 + 标签近邻
    c = FakeClient(
        tag_tree=_tt(3, ["科技/机器人", "科技/机器人/人形", "投资/一级市场"]),
        search={"某测试概念名": {"structuredContent": {"memos": [
            {"id": "M1", "content": "已有卡片内容" * 20},
        ]}}},
    )
    b, n = [], []
    got = GATE._check_dedup(c, sig, b, n)
    check("_check_dedup 无阻塞", b == [])
    check("_check_dedup 关键词命中被记录",
          any(v for v in got.get("keyword_hits", {}).values()))
    check("_check_dedup content_head 截断 60", all(
        len(h["content_head"]) <= 60
        for v in got.get("keyword_hits", {}).values() for h in v))
    check("_check_dedup 标签近邻被记录",
          any(x.startswith("科技/机器人") for x in got.get("tag_neighbors", [])))
    check("_check_dedup 两路都跑了",
          any("查重·关键词" in x for x in n) and any("查重·标签路" in x for x in n))


def run_review_net_cases():
    """_check_review 三路复盘补 memo_recommended（假 client，不联网）。"""
    sig = GATE._signature(BODY)

    # 1) 显式给 anchor → 直接调 memo_recommended，且 id 必填
    c = FakeClient(recommended={"structuredContent": {"memos": [{"id": "R1"}, {"id": "R2"}]}})
    b, n = [], []
    got = GATE._check_review(c, sig, b, n, anchor_id="ANCHOR")
    check("_check_review 显式 anchor 通过", b == [] and got["anchor"] == "ANCHOR")
    check("_check_review anchor_source 标明来源", got["anchor_source"] == "调用方提供")
    check("_check_review recommended 收集 id", got["recommended"] == ["R1", "R2"])
    check("_check_review 必传 id 且带 limit",
          c.calls[-1][0] == "memo_recommended" and c.calls[-1][1].get("id") == "ANCHOR")

    # 2) 无 anchor 时从关键词检索取首条
    c = FakeClient(
        search={"某测试概念名": {"structuredContent": {"memos": [{"id": "HIT1"}]}}},
        recommended={"structuredContent": {"memos": []}},
    )
    b, n = [], []
    got = GATE._check_review(c, sig, b, n)
    check("_check_review 关键词命中作锚", b == [] and got and got["anchor"] == "HIT1")
    check("_check_review anchor_source 记命中来源", got and "命中首条" in got["anchor_source"])

    # 3) 既无 id 又无命中 → 判阻塞且返回 None
    b, n = [], []
    got = GATE._check_review(FakeClient(), sig, b, n)
    check("_check_review 无锚判阻塞", any("无法取锚" in x for x in b) and got is None)


if __name__ == "__main__":
    run_signature_cases()
    run_gate_check_cases()
    run_check_web_cases()
    run_concept_keyword_cases()
    run_no_gate_cases()
    run_local_tag_tree_cases()
    run_tag_tree_net_cases()
    run_dedup_net_cases()
    run_review_net_cases()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
