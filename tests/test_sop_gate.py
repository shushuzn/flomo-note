#!/usr/bin/env python3
"""sop_gate.py 与「闸门接入 validate_memo.py」的离线回归用例。

全部用例**不联网**：涉及 flomo 调用的部分用假 client 桩替换，
只验证"凭证生成 / 校验"这套机械逻辑本身是否正确。
联网侧的实跑由维护者手工执行（见 SKILL「维护约定」）。

退出码 0 = 全部通过。
"""

COVERS = ("sop_gate", "validate_memo",)

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"


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


# 供 _check_web 落点校验用的正文：只含 `430 公里` 这一个关键参数，
# 便于构造「结论参数在正文 / 不在正文」两侧。
WEB_BODY = (
    "#科技/机器人\n"
    "某测试概念名\n"
    "\n"
    "结论句。\n"
    "\n"
    "要点：\n"
    "- WLTP 续航 430 公里\n"
)

# 供汇总计数用例使用：含两处可作锚点的真实片段（均不等于术语本身）。
SUMMARY_BODY = (
    "#科技/机器人\n"
    "某测试概念名\n"
    "\n"
    "结论句。\n"
    "\n"
    "要点：\n"
    "- 该车 WLTP 续航 430 公里，起售价 30,000 欧元\n"
)


def check(label, ok, detail=""):
    """`detail` 只在失败时打印，用于回显实际值（成功时保持单行输出）。"""
    RESULTS.append(ok)
    if ok:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}" + (f"\n      实际：{detail}" if detail else ""))


def _write_gate(sig, body_hash, *, expires_delta=3600, web=None, key_sig=None,
                allow_no_gate=False):
    """在临时 gate 目录写一张凭证，并让 VM 指向该目录。

    `key_sig` 单独指定落盘用的文件名签名（默认同 `sig`）；用于构造
    "文件名对得上、但凭证内 signature 字段是另一张卡"的签名不符场景。
    `allow_no_gate=True` 模拟 sop_gate.py --allow-no-gate 写入的降级授权标记。
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
        "tag_check": {"tags": [{"tag": "科技/机器人", "exists": True}], "checked": 1},
        "dedup": {},
        "review": {},
    }
    if allow_no_gate:
        gate["no_gate_authorized"] = True
        gate["no_gate_reason"] = "测试：批量处理历史卡"
    (tmp / f"{sig_key}.json").write_text(
        json.dumps(gate, ensure_ascii=False), encoding="utf-8"
    )
    return tmp


def run_signature_cases():
    """签名提取：直达 memo_util 单一实现（前两个非空行）。"""
    sig = GATE.memo_signature(BODY)
    check("签名取标签行+概念名行",
          sig == {"tagline": "#科技/机器人", "concept": "某测试概念名"})
    check("不足两行返回 None", GATE.memo_signature("#标签\n") is None)
    check("首行非标签返回 None", GATE.memo_signature("普通行\n概念\n") is None)
    check("首行标签、第二行空（H14 变体）仍取到概念名",
          GATE.memo_signature("#科技/机器人\n\n概念名\n正文\n")
          == {"tagline": "#科技/机器人", "concept": "概念名"})


def run_gate_check_cases():
    """validate_memo.check_gate 的四类判定。"""
    sig = VM._signature_of(BODY)

    # 1) 凭证缺失 → ERR
    VM.GATE_DIR = Path(tempfile.mkdtemp())
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("无凭证判 ERR", len(VM.ERR) == 1 and "缺少 SOP 流程闸门凭证" in VM.ERR[0])

    # 2) 凭证正常 → 无错
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest())
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("凭证齐备不报错", len(VM.ERR) == 0)

    # 3) 正文指纹不符 → ERR（防"先跑闸门后改正文"）
    _write_gate(sig, hashlib.sha256(("别的正文" + BODY).encode()).hexdigest())
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("正文指纹不符判 ERR",
          len(VM.ERR) == 1 and "正文指纹" in VM.ERR[0])

    # 4) 凭证过期 → ERR
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(), expires_delta=-10)
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("凭证过期判 ERR", any("已过期" in m for m in VM.ERR))

    # 5) 签名不符 → ERR（文件名对得上，但凭证内 signature 是另一张卡）
    other = {"tagline": "#时政/反腐", "concept": "另一张卡的概念名"}
    _write_gate(other, hashlib.sha256(BODY.encode()).hexdigest(), key_sig=sig)
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("签名不符判 ERR", any("签名与当前卡片不符" in m for m in VM.ERR))

    # 6) 验证未做（web 无 searched 且未 skip）→ ERR
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(),
                web={"searched": False, "term_count": 0})
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("验证未执行判 ERR", any("验证（网络搜索）未执行" in m for m in VM.ERR))

    # 7) 显式 skip-web → 不报验证错
    _write_gate(sig, hashlib.sha256(BODY.encode()).hexdigest(),
                web={"web_skipped": True})
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("显式跳过网络验证不报错", len(VM.ERR) == 0)


def run_no_gate_cases():
    """--no-gate 降级须凭证带 --allow-no-gate 授权标记，否则不生效。"""
    orig_argv = sys.argv

    def _run():
        # main() 不做跨轮清理，ERR/WARN 是模块级列表；连着调必须手动清，
        # 否则上一轮的判错会污染下一轮退出码。
        VM.ERR.clear()
        VM.WARN.clear()
        sys.argv = ["validate_memo.py", "--no-gate", _p]
        try:
            return VM.main()
        finally:
            sys.argv = orig_argv

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(BODY)
        _p = f.name
    sig = VM._signature_of(BODY)
    bh = hashlib.sha256(BODY.encode()).hexdigest()

    # 1) 无授权标记 → 降级不生效，闸门缺失照常判 ERR
    _write_gate(sig, bh, expires_delta=-10, web={"searched": True})
    check("无授权的 --no-gate 判 ERR（退出码 1）", _run() == 1)

    # 2) 凭证带授权标记 + 闸门问题存疑 → 降级为 WARN，不阻断
    _write_gate(sig, bh, expires_delta=-10, web={"searched": True},
                allow_no_gate=True)
    check("有授权的 --no-gate 降级不阻断（退出码 0）", _run() == 0)

    # 3) 有授权但正文指纹对不上（凭证是旧版正文的）→ 授权失效，判 ERR
    _write_gate(sig, "deadbeef" * 8, expires_delta=-10, web={"searched": True},
                allow_no_gate=True)
    check("授权凭证正文指纹不符时降级失效（退出码 1）", _run() == 1)


def run_symbolic_term_cases():
    """符号型术语的落点判定：`M+`、`C++`、`.NET` 这类名字含连接符号。

    实测写「M+、蓬皮杜中心与 MoMA 文化联盟」那张卡时，旧词块正则只认
    「字母开头」与「连续汉字」，`M+` 里的 `+` 既非字母也非汉字，被切成裸 `M`，
    于是正文里明明白白写着 `M+`，闸门却报「在卡正文中找不到落点」，
    只能靠人工补 `in_body` 才过闸。判据本该自己认得出这个名字。
    """
    # 1) 词块切分：符号是名字的一部分，不是切分点
    for term in ("M+", "C++", ".NET", "Node.js", "MoMA"):
        toks = GATE._TOKEN_RE.findall(term)
        check(f"词块保留完整符号：{term}", toks == [term], toks)

    # 2) 落点比对：正文里有 `M+`，术语就是 `M+`，必须算命中
    body = (
        "#文化/艺术\n"
        "M+、蓬皮杜中心与 MoMA 文化联盟\n"
        "\n"
        "这是 M+ 香港、蓬皮杜中心与 MoMA 三方成立的文化联盟。\n"
    )
    hit = [t for t in GATE._name_tokens("M+") if GATE._norm(t) in GATE._norm(body)]
    check("M+ 落点在正文中命中", hit == ["M+"], hit)

    # 3) 反向：正文里没有该符号时仍要报未命中（不能靠放宽到「有字母就算」）
    empty = "#文化/艺术\n\n蓬皮杜中心与纽约现代艺术博物馆三方联盟。\n"
    miss = [t for t in GATE._name_tokens("M+") if GATE._norm(t) in GATE._norm(empty)]
    check("正文无 M+ 时判未命中", miss == [], miss)

    # 4) 端到端：这个术语的验证条目不再被闸门拦下
    import json as _json
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp()) / "v.json"
    tmp.write_text(_json.dumps({"searched": True, "terms": [
        {"term": "M+", "query": "q",
         "conclusion": "香港的当代视觉文化博物馆",
         "search_url": "https://example.com/q"},
    ]}, ensure_ascii=False), encoding="utf-8")
    b, n = [], []
    GATE._check_web(str(tmp), body, b, n)
    check("M+ 验证条目过闸（不再被误拦）", not b, b)



def run_check_web_cases():
    """_check_web：留痕校验 + 「结论已落进正文」的落点校验（离线，只读文件）。"""
    import json as _json

    def _verify_file(payload):
        tmp = Path(tempfile.mkdtemp()) / "verify.json"
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return tmp

    # 1) 未提供验证记录 → blocker
    b, n = [], []
    GATE._check_web(None, WEB_BODY, b, n)
    check("_check_web 未提供记录判阻塞", any("未提供第 2 步验证记录" in x for x in b))

    # 2) 文件不存在 → blocker
    b, n = [], []
    GATE._check_web("/nonexistent/verify.json", WEB_BODY, b, n)
    check("_check_web 文件不存在判阻塞", any("不存在" in x for x in b))

    # 3) searched=false → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": False, "terms": []}), WEB_BODY, b, n)
    check("_check_web searched=false 判阻塞",
          any("未真正执行网络搜索" in x for x in b))

    # 4) terms 为空 → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": []}), WEB_BODY, b, n)
    check("_check_web terms 为空判阻塞", any("terms 为空" in x for x in b))

    # 5) 条目缺 conclusion → blocker
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True,
                                  "terms": [{"term": "某术语", "query": "q"}]}), WEB_BODY, b, n)
    check("_check_web 条目缺 conclusion 判阻塞",
          any("缺 term/conclusion" in x for x in b))

    # 6) 合法记录：一条按关键参数命中、一条按术语名命中 → 通过且计数正确
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某测试概念名", "query": "qa", "conclusion": "确认为该概念的标准称谓", "search_url": "https://example.com"},
        {"term": "续航参数", "query": "qb", "conclusion": "WLTP 续航 430 公里", "search_url": "https://example.com"},
    ]}), WEB_BODY, b, n)
    check("_check_web 合法记录通过",
          b == [] and got["searched"] is True and got["term_count"] == 2, str(b))

    # 7) 非法 JSON → blocker
    tmp = Path(tempfile.mkdtemp()) / "bad.json"
    tmp.write_text("{不是 JSON", encoding="utf-8")
    b, n = [], []
    GATE._check_web(tmp, WEB_BODY, b, n)
    check("_check_web 非法 JSON 判阻塞", any("非法 JSON" in x for x in b))

    # 8) searched 必须是布尔 true：字符串/数字都是真值，用真值判定会放行未搜索的记录
    for bad_val in ("no", "false", 1, 0, "true"):
        b, n = [], []
        GATE._check_web(_verify_file({"searched": bad_val, "terms": [
            {"term": "某测试概念名", "query": "q", "conclusion": "确认为该概念的标准称谓", "search_url": "https://example.com"}]}),
            WEB_BODY, b, n)
        check(f"_check_web searched={bad_val!r} 判阻塞",
              any("必须为布尔 true" in x for x in b))

    # 9) 核心新规：留痕齐全、但结论里的关键参数一个都没落进正文 → 必须阻塞。
    #    这正是旧版闸门的缺口——「搜了不用」与「正文照抄原文」都能骗过旧版。
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q",
         "conclusion": "该车 WLTP 续航 500 公里，起售价 9,999 欧元", "search_url": "https://example.com"}]}), WEB_BODY, b, n)
    check("_check_web 结论关键参数未落正文判阻塞",
          any("一个都没落" in x for x in b), str(b))
    check("_check_web 未落条目在凭证里如实记为未命中",
          got["landed"][0].get("matched") == [], str(got["landed"]))

    # 10) 结论关键参数已落正文 → 通过，且凭证记下命中项
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q", "conclusion": "WLTP 续航 430 公里", "search_url": "https://example.com"}]}),
        WEB_BODY, b, n)
    check("_check_web 结论关键参数已落正文通过",
          b == [] and bool(got["landed"][0]["matched"]), str(b))

    # 11) 无数字的评级结论（五星）同属关键参数，未落正文照样阻塞
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某评级", "query": "q", "conclusion": "获 Euro NCAP 五星安全评级", "search_url": "https://example.com"}]}),
        WEB_BODY, b, n)
    check("_check_web 星级结论未落正文判阻塞", any("一个都没落" in x for x in b), str(b))

    # 12) 纯命名结论：按术语名比对，不在正文即阻塞
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某无关术语", "query": "q", "conclusion": "确认为标准称谓", "search_url": "https://example.com"}]}),
        WEB_BODY, b, n)
    check("_check_web 纯命名结论术语名未落正文判阻塞",
          any("找不到落点" in x for x in b), str(b))

    # 13) in_body 显式落点：不存在 / 等于术语本身都要阻塞，真实落点才放行
    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q", "conclusion": "c", "in_body": ["正文里没有的句子"], "search_url": "https://example.com"}]}),
        WEB_BODY, b, n)
    check("_check_web in_body 落点不在正文判阻塞",
          any("在卡正文中不存在" in x for x in b), str(b))

    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某测试概念名", "query": "q", "conclusion": "c",
         "in_body": ["某测试概念名"], "search_url": "https://example.com"}]}), WEB_BODY, b, n)
    check("_check_web in_body 落点等于术语本身判阻塞",
          any("无信息量" in x for x in b), str(b))

    b, n = [], []
    GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q", "conclusion": "c",
         "in_body": ["WLTP 续航 430 公里"], "search_url": "https://example.com"}]}), WEB_BODY, b, n)
    check("_check_web in_body 落点真实存在则放行", b == [], str(b))

    # 14) not_in_body 显式豁免：放行，但豁免理由必须随凭证留痕（可审计）
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某无关参数", "query": "q", "conclusion": "WLTP 续航 500 公里",
         "not_in_body": "该参数与卡片主题无关，按 H13 不并入", "search_url": "https://example.com"}]}), WEB_BODY, b, n)
    check("_check_web not_in_body 显式豁免放行且留痕",
          b == [] and got["landed"][0]["exempt"], str(b) + str(got["landed"]))

    # 15) 关键参数提取本身的口径（防回归）
    check("_evidence_tokens 不截断 kWh（前缀单位须长者在前）",
          GATE._evidence_tokens("电池 60.2 kWh") == ["60.2 kWh"],
          str(GATE._evidence_tokens("电池 60.2 kWh")))
    check("_evidence_tokens 无单位数字不算关键参数（否则落点会空转）",
          GATE._evidence_tokens("共 3 项，第 5 条") == [],
          str(GATE._evidence_tokens("共 3 项，第 5 条")))
    check("_evidence_tokens 千分位与单位内空格可比对",
          GATE._norm("1,055 公里") == GATE._norm("1055公里"))

    # 16) 汇总行的计数必须与凭证一致，且不许漏计锚点路径。
    #     旧实现按手写键名清单（exempt / matched）反推，in_body 锚点分支的键是
    #     anchors，于是「整份都用锚点声明」的记录被显示成「0 条在正文有落点」，
    #     与凭证内容相反——不阻塞写入，却让审计文案说谎。
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q", "conclusion": "c", "in_body": ["WLTP 续航 430 公里"], "search_url": "https://example.com"},
        {"term": "某价格", "query": "q", "conclusion": "c", "in_body": ["起售价 30,000 欧元"], "search_url": "https://example.com"},
        {"term": "某无关参数", "query": "q", "conclusion": "c", "not_in_body": "与主题无关", "search_url": "https://example.com"},
    ]}), SUMMARY_BODY, b, n)
    summary = next((x for x in n if x.startswith("验证·")), "")
    landed_true = sum(1 for r in got["landed"] if r.get("landed"))
    check("锚点路径计入汇总行（不再显示 0 条有落点）",
          "其中 3 条在正文有落点或已豁免" in summary, summary)
    check("汇总行条数 == 凭证中 landed 为真的条数",
          f"其中 {landed_true} 条" in summary and landed_true == 3,
          f"{summary} / {got['landed']}")
    check("锚点记录 landing 判定正确（真实锚点 = 已落点）",
          [r["landed"] for r in got["landed"]] == [True, True, True],
          str(got["landed"]))

    # 17) 每一条落点审计记录都必须显式带 landed 布尔（计数唯一依据）
    b, n = [], []
    got = GATE._check_web(_verify_file({"searched": True, "terms": [
        {"term": "某参数", "query": "q", "conclusion": "WLTP 续航 500 公里", "search_url": "https://example.com"},          # 未落
        {"term": "某参数2", "query": "q", "conclusion": "WLTP 续航 430 公里", "search_url": "https://example.com"},          # 已落
        {"term": "某无关参数", "query": "q", "conclusion": "c", "not_in_body": "无关", "search_url": "https://example.com"}, # 豁免
    ]}), WEB_BODY, b, n)
    check("每条审计记录都显式带 landed 布尔",
          all(isinstance(r.get("landed"), bool) for r in got["landed"]),
          str(got["landed"]))
    check("未落点条目 landed=False，已落点/豁免 landed=True",
          [r["landed"] for r in got["landed"]] == [False, True, True],
          str([r["landed"] for r in got["landed"]]))
    n2 = next((x for x in n if x.startswith("验证·")), "")
    check("汇总行点出未落点条目名（审计可定位）", "未落点 1 条" in n2, n2)

    # 18) 结构约束：_check_term_landed 的每个分支都必须经 _landed_record 返回。
    #     裸 dict 返回 = 计数依据可被静默遗漏，正是本次缺口的成因。
    src = (Path(__file__).resolve().parent.parent / "scripts" / "sop_gate.py").read_text(encoding="utf-8")
    m = re.search(r"def _check_term_landed\(.*?\n(?=def )", src, re.S)
    fn = m.group(0) if m else ""
    check("_check_term_landed 无裸 dict 返回（一律经 _landed_record）",
          "return {" not in fn, str(re.findall(r"return \{", fn)))
    check("_check_term_landed 的四个分支都申报了 landed",
          fn.count("return _landed_record(") == 4, str(fn.count("return _landed_record(")))


def run_assemble_gate_cases():
    """assemble_gate：凭证组装（纯函数）+ 机器盲区自声明。"""
    sig = GATE.memo_signature(BODY)
    web = {"searched": True, "term_count": 2, "landed": []}
    g = GATE.assemble_gate(BODY, sig, web, {"keyword_hits": {}}, {"anchor": "x"}, now=1000)

    check("assemble_gate 字段齐备",
          {"signature", "sig_key", "body_hash", "generated_at", "expires_at",
           "web", "dedup", "review"} <= set(g), str(sorted(g)))
    check("assemble_gate 正文指纹与实际正文一致",
          g["body_hash"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest())
    check("assemble_gate 有效期 = 生成时刻 + TTL",
          (g["generated_at"], g["expires_at"]) == (1000, 1000 + GATE.GATE_TTL),
          str((g["generated_at"], g["expires_at"])))
    check("assemble_gate sig_key 与落盘口径同源",
          g["sig_key"] == GATE.signature_key(sig))

    # 机器盲区必须随凭证留痕，且逐项给出「为什么判不了 / 去哪看凭据」。
    # 藏起盲区的凭证会让人误以为「凭证齐 = 全查过了」。
    items = g.get("human_judgment") or []
    check("凭证声明机器盲区清单（非空）", len(items) >= 3, str(items))
    check("盲区项逐项带 item/why/evidence",
          all(i.get("item") and i.get("why") and i.get("evidence") for i in items),
          str(items))
    check("盲区声明覆盖查重判定 / 本卡全文自查 / 抓回读回",
          sum(1 for i in items if "查重" in i["item"]) == 1
          and sum(1 for i in items if "全文自查" in i["item"]) == 1
          and sum(1 for i in items if "抓回" in i["item"]) == 1, str(items))
    check("盲区声明不改放行条件（与 web/dedup/review 并列，非阻塞项）",
          "human_judgment" in g and g["web"] is web, str(sorted(g)))

    # 带盲区声明的凭证在写云侧不得被误判为异常
    tmp = Path(tempfile.mkdtemp())
    VM.GATE_DIR = tmp
    live = GATE.assemble_gate(BODY, sig, web, {"keyword_hits": {}}, {"anchor": "x"})
    (tmp / f"{GATE.signature_key(sig)}.json").write_text(
        json.dumps(live, ensure_ascii=False), encoding="utf-8")
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check_gate(BODY)
    check("validate_memo 对带 human_judgment 的凭证不报错", VM.ERR == [], str(VM.ERR))


def run_concept_keyword_cases():
    """keywords_of_concept 边界（概念名直接派生，不经卡片结构）。"""
    kw = GATE.keywords_of_concept("德塔智能 Delta 0 双足人形机器人基础模型")
    check("keywords_of_concept 派生非空", len(kw) >= 1 and all(kw))
    check("keywords_of_concept 限长 2", len(GATE.keywords_of_concept("某概念名称")) <= 2)


class FakeClient:
    """假 flomo client：按工具名返回预设响应，并可记录调用参数。"""

    def __init__(self, tag_tree=None, search=None, recommended=None, batch_get=None):
        self._tag_tree = tag_tree if tag_tree is not None else {}
        self._search = search or {}
        self._recommended = recommended or {}
        self._batch_get = batch_get or {}
        self.calls = []

    def tool(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        if name == "tag_tree":
            return self._tag_tree
        if name == "memo_search":
            a = args or {}
            # 标签路用 `tag:` 前缀单独开键，避免与关键词路串味——同一份预设里
            # 关键词命中 0 而标签簇有卡，是本组用例要构造的核心场景。
            if a.get("tag"):
                return self._search.get("tag:" + a["tag"],
                                        {"structuredContent": {"memos": []}})
            kw = a.get("keywords", "")
            return self._search.get(kw, {"structuredContent": {"memos": []}})
        if name == "memo_recommended":
            return self._recommended
        if name == "memo_batch_get":
            ids = (args or {}).get("ids", [])
            memos = []
            for mid in ids:
                if mid in self._batch_get:
                    memos.extend(self._batch_get[mid].get("structuredContent", {}).get("memos", []))
            return {"structuredContent": {"memos": memos}}
        return {}


def _tt(total, names):
    """构造 tag_tree 响应（structuredContent 形态）。"""
    return {"structuredContent": {"total": total,
                                  "tags": [{"name": n} for n in names]}}


def run_tag_tree_net_cases():
    """_tag_tree_total_and_count（假 client，不联网）。

    该函数仍被查重两路与复盘三路用于现采云端标签，与本地存储无关，保留。
    """
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

    # 3) 两者皆无 → 抛错，不再返回 (None, [])
    #    旧断言钉的是「无数据返回 None」，而这正是缺陷：total 为 None 时上层继续走，
    #    「标签树读不到」与「标签树是空的」同形，查重标签路会报「近邻 0 个」。
    try:
        GATE._tag_tree_total_and_count(FakeClient(tag_tree={}))
        raised = None
    except Exception as e:  # noqa: BLE001 — 只断言「抛了且说清原因」
        raised = e
    check("tag_tree 两个出口都取不到时抛错（不再静默返回 None）", raised is not None)
    check("tag_tree 抛错带出取不到的原因",
          raised is not None and "取不到结构化数据" in str(raised))


def run_dedup_net_cases():
    """_check_dedup 两路查重（假 client，不联网）。"""
    sig = GATE.memo_signature(BODY)
    # 1) 关键词无法派生 → 判阻塞
    b, n = [], []
    got = GATE._check_dedup(FakeClient(), {"concept": "#", "tagline": "#A/b"}, b, n)
    check("_check_dedup 关键词无法派生判阻塞", any("无法从概念名派生查重关键词" in x for x in b))
    check("_check_dedup 关键词缺失败返回空", got == {})

    # 2) 正常两路：关键词命中 + 标签近邻
    c = FakeClient(
        tag_tree=_tt(3, ["科技/机器人", "科技/机器人/人形", "投资/一级市场"]),
        search={"某测试概念名": {"structuredContent": {"memos": [
            {"id": "M1", "content": "#科技/机器人<br>已有概念名<br><br>已有卡片内容" * 20},
        ]}}},
        batch_get={"M1": {"structuredContent": {"memos": [
            {"id": "M1", "content": "#科技/机器人<br>已有概念名<br><br>已有卡片内容" * 20},
        ]}}},
    )
    b, n = [], []
    got = GATE._check_dedup(c, sig, b, n)
    check("_check_dedup 命中且概念名不同不阻塞", len(b) == 0)
    check("_check_dedup 关键词命中被记录",
          any(v for v in got.get("keyword_hits", {}).values()))
    check("_check_dedup content_head 截断 60", all(
        len(h["content_head"]) <= 60
        for v in got.get("keyword_hits", {}).values() for h in v))
    check("_check_dedup 标签近邻被记录",
          any(x.startswith("科技/机器人") for x in got.get("tag_neighbors", [])))
    check("_check_dedup 两路都跑了",
          any("查重·关键词" in x for x in n) and any("查重·标签路" in x for x in n))

    # 3) 多标签时近邻计数不得跨标签累积（每条 note 只反映自己那个标签）
    c = FakeClient(
        tag_tree=_tt(4, ["科技/机器人", "科技/机器人/人形", "投资/一级市场", "投资/一级市场/私募"]),
        search={},
    )
    b, n = [], []
    GATE._check_dedup(c, GATE.memo_signature("#科技/机器人 #投资/一级市场\n某概念\n\n正文\n"), b, n)
    per_leaf = [x for x in n if "查重·标签路" in x]
    check("_check_dedup 两个标签各出一条计数", len(per_leaf) == 2)
    check("_check_dedup 近邻计数不跨标签累积",
          all("近邻 2 个簇" in x for x in per_leaf))

    # 4) 精确/前缀匹配：无关簇不得被子串误命中
    c = FakeClient(
        tag_tree=_tt(3, ["其他/机器人架构", "科技/机器人学", "科技/机器人"]),
        search={},
    )
    b, n = [], []
    got = GATE._check_dedup(c, GATE.memo_signature("#科技/机器人\n某概念\n\n正文\n"), b, n)
    check("_check_dedup 子串不误命中无关簇",
          "其他/机器人架构" not in got.get("tag_neighbors", []))
    check("_check_dedup 精确与子路径均命中",
          "科技/机器人" in got.get("tag_neighbors", []))
    check("_check_dedup 同级不同名不命中",
          "科技/机器人学" not in got.get("tag_neighbors", []))


def run_semantic_neighbor_cases():
    """新建二级标签时，近邻提示要能指出**该并进哪个既有簇**。

    实测写「塞上银龄行动」时新建了 `政策/老龄`，而库里的 `政策/社会保障`、
    `政策/卫生` 才是语义近邻；旧实现按字符串相似度排，相似度全为 0，
    提示退化成「中小企业、交通、产业」这种任意字典序条目，对判断毫无帮助。
    """
    tree = ["政策/社会保障", "政策/卫生", "政策/公共卫生",
            "政策/中小企业", "政策/交通", "政策/产业"]

    b, n = [], []
    got = GATE._check_tags(tree, "#政策/老龄", b, n)
    rec = got["tags"][0]
    check("语义近邻优先于相似度",
          rec.get("near")[:1] == ["社会保障"], rec.get("near"))
    check("近邻不含无关条目（中小企业/交通/产业）",
          not (set(rec.get("near") or []) & {"中小企业", "交通", "产业"}),
          rec.get("near"))
    check("新建二级仍不阻塞", b == [], b)

    # 表里没有的主题：退回相似度排序，不因此失效
    b, n = [], []
    got = GATE._check_tags(tree, "#政策/云计算", b, n)
    check("表外主题仍给近邻（退回相似度）", bool(got["tags"][0].get("near")), got["tags"][0])

    # 语义表本身必须指向真实可能存在的簇名（防止表里写错字后永远命中不了）
    bad = [k for k, v in GATE.TAG_SEMANTIC_NEIGHBORS.items()
           if not v or not all(isinstance(x, str) and x for x in v)]
    check("语义近邻表无非空条目", not bad, bad)



def run_tag_check_cases():
    """_check_tags 标签存在性核对（假 client，不联网）。

    判据分档：写错字才阻塞，新建簇只提示——新建二级是常态动作，误拦会让
    「合法新建」也要绕路，而绕路的人最终会把检查关掉。
    """
    tree = ["科技/云原生", "科技/后端开发", "商业/获客", "AI/智能体"]

    # 1) 标签已存在 → 不阻塞，逐条留痕
    b, n = [], []
    got = GATE._check_tags(tree, "#科技/云原生", b, n)
    check("_check_tags 已存在不阻塞", b == [], b)
    check("_check_tags 已存在记 exists", got["tags"][0]["exists"] is True, got)
    check("_check_tags 已存在有 note", any("已存在" in x for x in n), n)

    # 2) 新建二级 → 不阻塞，提示「将新建」并给同顶层最近邻
    b, n = [], []
    got = GATE._check_tags(tree, "#科技/容器编排", b, n)
    check("_check_tags 新建二级不阻塞", b == [], b)
    check("_check_tags 新建二级说清是新建", any("将新建此二级" in x for x in n), n)
    check("_check_tags 新建二级给同顶层近邻",
          got["tags"][0].get("near") and "云原生" in got["tags"][0]["near"], got)

    # 3) 只差全角斜杠 / 大小写 → 阻塞（云端会是同一簇的两种写法）
    tree_case = [*tree, "AI/Agent"]
    for bad in ("#科技／云原生", "#ai/agent"):
        b, n = [], []
        GATE._check_tags(tree_case, bad, b, n)
        check(f"_check_tags 写错字阻塞：{bad}",
              b and any("只差空白/全半角/大小写" in x for x in b), b)

    # 4) 树为空 → 阻塞（第 ④ 步无从进行；查重标签路同样空转）
    b, n = [], []
    GATE._check_tags([], "#科技/云原生", b, n)
    check("_check_tags 树未取到判阻塞",
          len(b) == 1 and "标签树未取到" in b[0], b)

    # 5) 首行没有合法两级标签 → 阻塞（H17）
    b, n = [], []
    GATE._check_tags(tree, "#科技/安全/邮件", b, n)
    check("_check_tags 三级标签判阻塞",
          len(b) == 1 and "没有合法的两级标签" in b[0], b)

    # 6) 顶层也不存在 → 不阻塞，但要说清会连顶层一起建
    b, n = [], []
    got = GATE._check_tags(tree, "#新材料/钙钛矿", b, n)
    check("_check_tags 新顶层不阻塞但说明", b == [] and any("顶层一起新建" in x for x in n), (b, n))

    # 6b) 顶层名只差大小写、二级是新的 → 顶层仍算存在。逐字比前缀会报「顶层也不存在，
    #     将连顶层一起新建」，那等于把已有的顶层换个大小写重开一遍。
    b, n = [], []
    got = GATE._check_tags(tree_case, "#ai/新二级", b, n)
    check("_check_tags 顶层只差大小写仍算存在",
          b == [] and any("该顶层存在" in x for x in n), (b, n))
    check("_check_tags 顶层只差大小写不提示连顶层新建",
          not any("顶层一起新建" in x for x in n), n)


    b, n = [], []
    got = GATE._check_tags(["科技/云原生"], "#科技/云原生", b, n)
    check("_check_tags 兼容裸字符串标签", b == [] and got["tags"][0]["exists"] is True, b)

    # 8) 查重标签路：标签不在树里时，「0 个簇」不能与「簇为空」同形
    b, n = [], []
    GATE._check_dedup(FakeClient(), GATE.memo_signature("#科技/不存在的簇\n某概念名\n\n正文\n"),
                      b, n, tree=["科技/云原生"])
    check("_check_dedup 标签不在树中时点明「将新建簇」",
          any("不在树中" in x for x in n), n)


def run_review_net_cases():
    """_check_review 三路复盘补 memo_recommended（假 client，不联网）。"""
    sig = GATE.memo_signature(BODY)

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

    # 3) 关键词无命中，但本卡标签簇里有卡 → 退到同簇首条作锚（不阻塞）
    #    背景：卡 32 就撞上这种情况——概念名全新，关键词 0 命中，闸门直接阻塞，
    #    只能手工 `--anchor-id` 指同簇的卡。人工本来就会这么做，故收进脚本。
    c = FakeClient(
        search={"tag:科技/机器人": {"structuredContent": {"memos": [{"id": "TAG1"}]}}},
        recommended={"structuredContent": {"memos": [{"id": "R9"}]}},
    )
    b, n = [], []
    got = GATE._check_review(c, sig, b, n)
    check("_check_review 关键词无命中退到标签簇取锚", b == [] and got and got["anchor"] == "TAG1")
    check("_check_review 标签簇取锚如实标注来源",
          got and "标签簇" in got["anchor_source"] and "关键词无命中" in got["anchor_source"])
    check("_check_review 标签路查询带完整路径",
          any(k == "memo_search" and a.get("tag") == "科技/机器人" for k, a in c.calls))
    check("_check_review 取锚过程留痕（关键词命中数）",
          any("取锚检索" in x and "0 条" in x for x in n), str(n))

    # 4) 关键词与标签簇两路皆空 → 阻塞，且文案必须给出出路（不靠人去翻 argparse）
    b, n = [], []
    got = GATE._check_review(FakeClient(), sig, b, n)
    check("_check_review 无锚判阻塞", any("无法取锚" in x for x in b) and got is None)
    check("_check_review 阻塞文案给出 --anchor-id 出路",
          any("--anchor-id" in x for x in b), str(b))
    check("_check_review 阻塞文案点名实际查过的标签簇",
          any("科技/机器人" in x for x in b), str(b))

    # 5) 显式 anchor 优先于两种自动取锚（不额外发检索请求）
    c = FakeClient(search={}, recommended={"structuredContent": {"memos": []}})
    b, n = [], []
    GATE._check_review(c, sig, b, n, anchor_id="GIVEN")
    check("_check_review 显式锚优先，不发多余检索",
          [k for k, _ in c.calls] == ["memo_recommended"], str(c.calls))


def run_usage_consistency_cases():
    """docstring「用法」段的参数声明必须与 argparse 实现一致，双向都查。

    背景（真实事故）：usage 里写了 `[--search-kw k1 ...]`，但 argparse 根本没有
    这个参数——照 usage 传会报 `unrecognized arguments`；反过来 `--out`/`--anchor-id`
    /`--allow-no-gate` 三个真实存在的参数又没写进 usage。usage 是手写的、参数由代码
    定义，两处独立维护必然漂移，故用本用例把它焊死。

    解析要点（此处踩过一次坑，别再改回去）：
      - 只取「用法」标题**到下一个空行**为止，段后那几行说明文字不属于声明。
        用 `用法.*?\\n(.*?)\\n\\n` 会因 `.*?` 跨行回溯把说明一并吞进捕获组。
      - 逐行剔除纯注释行，避免说明句里出现的参数名被当成声明。
    """
    doc = GATE.__doc__ or ""
    lines = doc.splitlines()
    declared = set()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("用法"))
    except StopIteration:
        check("usage 段存在且可提取", False, "docstring 里找不到「用法」标题行")
        return

    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if not stripped:
            if declared:
                break  # 声明区结束
            continue
        if stripped.startswith("#"):
            continue
        if not stripped.startswith("python"):
            continue
        # 只认命令行本体，剥掉行尾注释（注释里出现的参数名不算声明）
        body = stripped.split("#", 1)[0]
        declared |= set(re.findall(r"--[a-z][a-z0-9\-]*", body))

    real = {"--" + x for x in re.findall(
        r"add_argument\(\s*[\"']--([a-z0-9\-]+)", Path(GATE.__file__).read_text(encoding="utf-8"))}

    check("usage 段解析出声明", bool(declared), "解析结果为空，用例本身失效")

    ghost = sorted(declared - real)
    check("usage 无幽灵参数（声明了但 argparse 没有）", not ghost, f"幽灵参数：{ghost}")

    undocumented = sorted(real - declared)
    check("usage 无未文档化参数（argparse 有但 usage 没写）", not undocumented,
          f"未文档化：{undocumented}")


if __name__ == "__main__":
    run_signature_cases()
    run_gate_check_cases()
    run_check_web_cases()
    run_symbolic_term_cases()
    run_assemble_gate_cases()
    run_concept_keyword_cases()
    run_no_gate_cases()
    run_tag_tree_net_cases()
    run_dedup_net_cases()
    run_tag_check_cases()
    run_semantic_neighbor_cases()
    run_review_net_cases()
    run_usage_consistency_cases()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
