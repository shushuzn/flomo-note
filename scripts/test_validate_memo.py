#!/usr/bin/env python3
"""validate_memo.py 回归测试。

用法：python scripts/test_validate_memo.py      退出码 0=全过，1=有失败。

覆盖 2026-09-11 修复的三个真 bug（表格误报 / --create 读空 / BOM 误判）
及既有硬限（两级标签），并锁住告警数量防止规则相互干扰。
脏标签认定聚焦首行标签段本身（层级/字符/前缀）；正文 # 形态仍检查（flomo 会把 #xxx 当标签），正文斜杠（/词）属正常书写、不再扫描。
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("validate_memo", HERE / "validate_memo.py")
VM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VM)

CASES = [
    # (用例名, 正文, 期望错数, 期望警数)
    ("正常卡片", "#数学/泛函方程\n某概念\n\n要点：\n- 内容一\n", 0, 0),
    # 本轮实际误报：数学绝对值竖线不是表格
    ("绝对值竖线不算表格",
     "#数学/泛函方程\n某概念\n\n要点：\n- 圆盘内 p 的模不超过 r 分之一：|p|≤r，条带内 |Im z|<d/2\n", 0, 0),
    ("集合势竖线不算表格",
     "#数学/泛函方程\n某概念\n\n要点：\n- 当 |F(s)−F(t)|<2|s−t| 时两映射互逆\n", 0, 0),
    ("条件概率竖线不算表格",
     "#数学/泛函方程\n某概念\n\n要点：\n- 条件概率 P(A|B) 与 P(B|A) 不等\n", 0, 0),
    ("真表格-竖线包裹", "#数学/泛函方程\n某概念\n\n要点：\n- | 项目 | 值 |\n", 0, 1),
    ("真表格-无外竖线", "#数学/泛函方程\n某概念\n\n要点：\n- 名称 | 数量 | 单价\n", 0, 1),
    ("真表格-分隔行", "#数学/泛函方程\n某概念\n\n要点：\n- |---|:---:|\n", 0, 1),
    ("三级标签判错", "#科技/安全/邮件认证\n某概念\n\n要点：\n- 内容\n", 1, 0),
    ("裸顶层标签判错", "#数学\n某概念\n\n要点：\n- 内容\n", 1, 0),
    ("非法字符标签只报一次", "#科技*安全\n某概念\n\n要点：\n- 内容\n", 1, 0),
    ("正文 #数字 判错", "#数学/泛函方程\n某概念\n\n要点：\n- issue #8435 已修复\n", 1, 0),
    ("正文 /词 不判脏标签", "#科技/云原生\n某概念\n\n要点：\n- 见 科技/方法论 一节\n", 0, 0),
    ("数字斜杠不提示", "#数学/泛函方程\n某概念\n\n要点：\n- 占比 1/2，日期 2026/09，分类 MSC 39B12/26E60\n", 0, 0),
    ("含 URL 的行整行豁免",
     "#数学/泛函方程\n某概念\n\n要点：\n- 详见 https://arxiv.org/abs/2609.11102#frag\n", 0, 0),
    ("BOM 不得误判首行", "\ufeff#数学/泛函方程\n某概念\n\n要点：\n- 内容\n", 0, 0),
    ("CRLF 不得误判", "#数学/泛函方程\r\n某概念\r\n\r\n要点：\r\n- 内容\r\n", 0, 0),
    ("标题后缺空行提示", "#数学/泛函方程\n某概念\n正文\n", 0, 1),
    # 预印本摘要依赖（SKILL 流程第 1 步「预印本正文优先」）：只是 WARN 提示回查正文
    ("据摘要写卡提示", "#数学/泛函方程\n某概念\n\n要点：\n- 据摘要，该方法在三个基准上领先\n", 0, 1),
    ("摘要显示提示", "#产业/化工\n某概念\n\n要点：\n- 摘要显示该催化剂转化率提升 20%\n", 0, 1),
    ("正文级陈述不提示",
     "#数学/泛函方程\n某概念\n\n要点：\n- 正文定理 1.2 给出全纯情形的解；第 3 节附曲率判据的证明\n", 0, 0),
    ("摘要作为叙述对象不提示",
     "#产业/化工\n某概念\n\n要点：\n- 论文含中英文摘要与 12 页附录\n", 0, 0),
    ("空正文判错", "#数学/泛函方程\n", 1, 1),  # 同时提示缺概念名称行
]


def run_case(name, content, want_err, want_warn):
    VM.ERR.clear()
    VM.WARN.clear()
    VM.check(content)
    got = (len(VM.ERR), len(VM.WARN))
    ok = got == (want_err, want_warn)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 得到 {got[0]} 错/{got[1]} 警，"
          f"期望 {want_err} 错/{want_warn} 警")
    if not ok:
        for m in VM.ERR:
            print("      ERR :", m)
        for m in VM.WARN:
            print("      WARN:", m)
    return ok


def run_loader_cases():
    """--create / --file 必须能读 flomo_client.py 实际发送的顶层 content 形态。"""
    body = "#数学/泛函方程\n某概念\n\n要点：\n- 内容\n"
    ok = True
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        forms = {
            "顶层 content（flomo_client 实际请求体）": {"content": body},
            "JSON-RPC 信封 params.arguments.content": {
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": "memo_create", "arguments": {"content": body}},
            },
            "顶层 arguments.content": {"arguments": {"content": body}},
        }
        for label, obj in forms.items():
            p = d / "req.json"
            p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            got = VM.load_content(["validate_memo.py", "--create", str(p)])
            same = got == body
            ok &= same
            print(f"{'PASS' if same else 'FAIL'}  读取 {label}: {'一致' if same else repr(got[:40])}")

        # 纯文本文件仍按原文读
        t = d / "memo.txt"
        t.write_text(body, encoding="utf-8")
        got = VM.load_content(["validate_memo.py", "--file", str(t)])
        ok &= got == body
        print(f"{'PASS' if got == body else 'FAIL'}  读取纯文本文件")

        # JSON 但无 content 时不得静默返回空串
        bad = d / "bad.json"
        bad.write_text('{"foo": 1}', encoding="utf-8")
        got = VM.load_content(["validate_memo.py", "--create", str(bad)])
        ok &= got.strip() != ""
        print(f"{'PASS' if got.strip() else 'FAIL'}  JSON 无 content 时回落为原文（不静默读空）")
    return ok


if __name__ == "__main__":
    results = [run_case(*c) for c in CASES]  # 不用 all() 短路，需跑完全部用例
    results.append(run_loader_cases())
    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    sys.exit(0 if all(results) else 1)
