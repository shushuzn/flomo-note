#!/usr/bin/env python3
"""validate_memo.py 回归测试。

用法：python scripts/test_validate_memo.py      退出码 0=全过，1=有失败。

覆盖三个真 bug（表格误报 / --create 读空 / BOM 误判）
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
    # 真 bug：正文 f#(z) 会让 flomo 建出 '(z)'、'(k)(z)' 等脏标签，
    # 旧正则只认 # 后跟 [A-Za-z0-9_中文]，放过 '#(' —— 必须判错
    ("正文 #(z) 判错", "#数学/复分析\n某概念\n\n要点：\n- 球面导数 f#(z) 在盘内局部有界\n", 1, 0),
    ("球面导数用 ♯ 不判错", "#数学/复分析\n某概念\n\n要点：\n- 球面导数 f♯(z) 在盘内局部有界\n", 0, 0),
    ("C# 后接空格不判错", "#科技/命令行\n某概念\n\n要点：\n- C# 与 F# 各有生态\n", 0, 0),
    ("正文 /词 不判脏标签", "#科技/云原生\n某概念\n\n要点：\n- 见 科技/方法论 一节\n", 0, 0),
    ("数字斜杠不提示", "#数学/泛函方程\n某概念\n\n要点：\n- 占比 1/2，编号 12/09，分类 MSC 39B12/26E60\n", 0, 0),
    ("含 URL 的行整行豁免",
     "#数学/泛函方程\n某概念\n\n要点：\n- 详见 https://arxiv.org/abs/2609.11102#frag\n", 0, 0),
    ("BOM 不得误判首行", "\ufeff#数学/泛函方程\n某概念\n\n要点：\n- 内容\n", 0, 0),
    ("CRLF 不得误判", "#数学/泛函方程\r\n某概念\r\n\r\n要点：\r\n- 内容\r\n", 0, 0),
    ("标题后缺空行判错", "#数学/泛函方程\n某概念\n正文\n", 1, 0),
    # 预印本摘要依赖（SKILL 流程第 1 步「预印本正文优先」）：只是 WARN 提示回查正文
    ("据摘要写卡提示", "#数学/泛函方程\n某概念\n\n要点：\n- 据摘要，该方法在三个基准上领先\n", 0, 1),
    ("摘要显示提示", "#产业/化工\n某概念\n\n要点：\n- 摘要显示该催化剂转化率提升 20%\n", 0, 1),
    ("正文级陈述不提示",
     "#数学/泛函方程\n某概念\n\n要点：\n- 正文定理 1.2 给出全纯情形的解；第 3 节附曲率判据的证明\n", 0, 0),
    ("摘要作为叙述对象不提示",
     "#产业/化工\n某概念\n\n要点：\n- 论文含中英文摘要与 12 页附录\n", 0, 0),
    ("空正文判错", "#数学/泛函方程\n", 1, 1),  # 同时提示缺概念名称行
    # 卡片载体元信息（截图 / 讲义 / 页码 / UP主）混入概念卡正文的加固用例
    # 正例（原始违规形态，必须 ERR）
    ("首段括号写明直播截图与UP主判错",
     "#科技/数学基础\nFGH 分级\n\n（B 站数学直播截图，UP 主\"某人1234509876二代\"）\n\n要点：\n- FGH 是有限维 Hilbert 空间的分级\n", 1, 0),
    ("要点句内括号写课程讲义判错",
     "#数学/集合论\n强不可达基数\n\n要点：\n- 若 κ 正则且对任意 α<κ 有 V_α∈V_κ（范畴论课程讲义，定义 3）\n", 1, 0),
    ("截图未展开之类保留说法判错",
     "#数学/集合论\nTG 公理\n\n要点：\n- 公理陈述见板书，此处从略\n", 1, 0),
    ("页码类载体信息判错",
     "#数学/集合论\nGrothendieck 宇宙\n\n要点：\n- 定义见第 9 页\n", 1, 0),
    ("字幕类载体信息判错",
     "#数学/集合论\n定理 5\n\n要点：\n- 课程字幕重复两遍，取其一\n", 1, 0),
    ("图序页码 10–11/36 页判错",
     "#数学/集合论\n定理 5\n\n要点：\n- 方向 1 的证明（10–11/36 页）\n", 1, 0),
    # 反例（合法内容，不得误伤）
    ("纪委监委消息属事实来源不判错",
     "#时政/反腐\n李耀楠被查\n\n据沈阳市纪委监委消息：李耀楠涉嫌严重违纪违法。\n\n要点：\n- 通报渠道：沈阳市纪委监委\n", 0, 0),
    ("新华社报道不判错",
     "#时政/反腐\n某案被查\n\n要点：\n- 新华社报道称该案由省级监委指定管辖\n", 0, 0),
    ("公众号作为运营主体不判错",
     "#科技/新媒体\n微信公众号\n\n要点：\n- 微信公众号是腾讯提供的公开创作平台\n", 0, 0),
    ("演讲内容本身不判错",
     "#科技/云计算\nAgentic 编程\n\n要点：\n- 在云栖大会演讲中提出 Qoder 工作台\n", 0, 0),
    ("视频作为概念本体不判错",
     "#科技/AI\n视频生成模型\n\n要点：\n- 视频生成模型以扩散架构合成时序帧\n", 0, 0),
    ("概念名为平台企业不判错",
     "#科技/平台\n哔哩哔哩\n\n要点：\n- 该平台以弹幕社区起家\n", 0, 0),
    ("概念名用取材方式命名只提示",
     "#数学/集合论\n课程讲义里的宇宙定义\n\n要点：\n- 宇宙 U 对幂集封闭\n", 0, 1),
    ("括号内注明取材于公众号判错",
     "#时政/反腐\n张三被查\n\n要点：\n- 通报全文（转载自某纪检监察公众号）\n", 1, 0),
    ("括号内歧义载体词只提示",
     "#科技/AI\n世界模型\n\n要点：\n- 该框架在机器人仿真（视频预测）上验证\n", 0, 1),
    # 模板指令回显（SKILL 条目名/格式词被原样写进卡片正文）的加固用例
    # 正例（原始违规形态，必须 ERR 阻断）
    ("结论先行回显判错",
     "#数学/统计推断\n某概念\n\n结论先行：该方法是成本感知的最优策略\n", 1, 0),
    ("结论先行出现在中段也判错",
     "#数学/统计推断\n某概念\n\n要点：\n- 结论先行：该方法是成本感知的最优策略\n", 1, 0),
    ("一句话核心结论前缀判错",
     "#数学/统计推断\n某概念\n\n一句话核心结论：该方法是成本感知的最优策略\n", 1, 0),
    ("一句话结论前缀判错",
     "#数学/统计推断\n某概念\n\n一句话结论：该方法省 36% 人工\n", 1, 0),
    ("列表项内一句话核心结论前缀也判错",
     "#数学/统计推断\n某概念\n\n要点：\n- 一句话核心结论：该方法省 36% 人工\n", 1, 0),
    # 反例（合法内容，不得误伤）
    ("核心结论作要点小标题不判错",
     "#AI/训练规划\n某概念\n\n核心结论：Paul Graham 称…\n\n要点：\n- 其一\n", 0, 0),
    ("结论先于正写不判错",
     "#数学/统计推断\n某概念\n\n结论先于论证：先给结论再补细节\n", 0, 0),
    ("正常结论句不判错",
     "#数学/统计推断\n某概念\n\n结论是 AI 判官可降检验成本\n", 0, 0),
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
