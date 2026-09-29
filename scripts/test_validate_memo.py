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
    # 字数上限（与 SKILL「卡片格式」承诺的硬限一致，含标签段）
    ("正文超 20000 字判错",
     "#测试/标签\n概念名\n\n" + "字" * 20010, 1, 0),
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
    # 注意：以下两条在旧版契约里曾是"反例"（旧脚本显式豁免报道/消息/通报，视为事实来源）。
    # 该豁免正是历次「私自添加来源」违规的根源——H15/H16 明确「事件自身的报道方 / 通报方
    # 与材料载体同等对待、一律不得写入正文」，故此处反转为正例：必须 ERR 阻断。
    ("据纪委消息属事实来源必须判错",
     "#时政/反腐\n李耀楠被查\n\n据沈阳市纪委监委消息：李耀楠涉嫌严重违纪违法。\n\n要点：\n- 通报渠道：沈阳市纪委监委\n", 2, 0),
    ("新华社报道必须判错",
     "#时政/反腐\n某案被查\n\n要点：\n- 新华社报道称该案由省级监委指定管辖\n", 1, 0),
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
     "#数学/集合论\n某定理\n\n要点：\n- 该定义源自课堂讲义（某公众号）\n", 1, 0),
    ("括号内歧义载体词只提示",
     "#科技/AI\n世界模型\n\n要点：\n- 该框架在机器人仿真（视频预测）上验证\n", 0, 1),

    # 事件来源信息（H15/H16：报道方/通报方/发布方与材料载体同等对待，一律 ERR 阻断）
    # 正例：原始违规形态，必须判错。旧版仅 WARN 或完全不检，是本次加固的动因。
    ("正文首句据某纪委消息判错",
     "#时政/反腐\n某甲被查\n\n据沈阳市纪委监委消息，某甲涉嫌严重违纪违法，正接受纪律审查和监察调查。\n", 1, 0),
    ("据某媒体报道判错",
     "#AI/大模型\n某模型发布\n\n据路透社报道，该模型将于下月发布。\n", 1, 0),
    ("据某纪委消息带日期判错",
     "#时政/反腐\n某乙被查\n\nＸ 月 Ｘ 日，据某省纪委监委消息，某乙正接受审查调查。\n", 1, 0),
    ("据外媒报道判错",
     "#产业/汽车\n某车企重组\n\n据外媒报道，该公司计划裁员五万人。\n", 1, 0),
    ("要点内通报来源整条判错",
     "#时政/反腐\n某丙被查\n\n要点：\n- 通报来源：中央纪委国家监委网站 9 月 18 日发布\n", 1, 0),
    ("要点内发布渠道判错",
     "#时政/反腐\n某丁被查\n\n要点：\n- 通报渠道：惠州市纪委监委微信公众号\n", 1, 0),
    ("概念名括号带通报方判错",
     "#时政/反腐\n某戊被开除党籍和公职（江苏“风腐一体”通报）\n\n正文陈述。\n", 1, 0),
    ("概念名括号带地名通报判错",
     "#时政/反腐\n某己履职不力受政务警告（济南通报）\n\n正文陈述。\n", 1, 0),
    ("概念名括号带纪委全称判错",
     "#时政/反腐\n某庚被开除党籍（中央纪委国家监委通报）\n\n正文陈述。\n", 1, 0),
    ("行首来源冒号判错",
     "#时政/反腐\n某辛被查\n\n来源：某省纪委监委\n", 1, 0),
    ("复合标签通报主体判错",
     "#时政/反腐\n某癸被查\n\n要点：\n- 通报主体：由某纪检监察组、某省纪委监委发布\n", 1, 0),
    ("复合标签发布渠道判错",
     "#时政/反腐\n某子被查\n\n要点：\n- 发布主体：某市纪委监委官网\n", 1, 0),
    ("由某纪委发布判错",
     "#时政/反腐\n某丑被查\n\n要点：\n- 该案由某省纪委监委发布，属跨省指定管辖\n", 1, 0),
    ("转自某公众号判错",
     "#时政/反腐\n某壬被查\n\n要点：\n- 通报全文（转载自某纪检监察公众号）\n", 2, 0),
    ("通过某网站发布判错",
     "#时政/反腐\n某癸被查\n\n要点：\n- 本次通报未通过中央纪委国家监委网站发布\n", 1, 0),
    # 反例：以下均是事件内容自身或合法写法，不得判错（防误伤）
    ("法条依据不判错",
     "#法律/立法法源\n溯及力规则\n\n依据《中华人民共和国立法法》确立的不溯及既往原则，新法原则上不适用于施行前的行为。\n", 0, 0),
    ("引用条例作为处分依据不判错",
     "#时政/反腐\n某甲被双开\n\n**定性与处理**：依据《中国共产党纪律处分条例》《中华人民共和国监察法》等有关规定，决定给予开除党籍处分。\n", 0, 0),
    ("来源术语不判错",
     "#媒体/记者权益\n新闻来源保护\n\n新闻报道中记者对消息来源负有保密义务，这是各国新闻法的通行规则。\n", 0, 0),
    ("合法括号补充说明不判错",
     "#金融/保险\n医疗险示范条款（草案征求意见稿）\n\n正文陈述。\n", 0, 0),
    ("合法括号进展说明不判错",
     "#科技/航天\n某型火箭（海上发射进展）\n\n正文陈述。\n", 0, 0),
    ("含链接行不判错",
     "#AI/大模型\nTransformer\n\n论文正文见 https://arxiv.org/abs/1706.03762 的论述。\n", 0, 0),
    ("据研究表述不判错",
     "#物理/凝聚态\n某效应\n\n据实验观测，该效应在低温下显著增强。\n", 0, 0),
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
    # 卡片签名失效路径：第二行写成标签 → 签名取不到
    # → 连带使写前幂等查重与 SOP 闸门校验同时静默失效，必须 ERR 阻断
    ("第二行是标签判错（签名失效）",
     "#数学/泛函\n#这不是概念名\n\n正文结论句。\n", 1, 0),
    ("第三行是标签判错（概念名被挤走）",
     "#数学/泛函\n\n#另一个标签\n\n正文结论句。\n", 1, 1),
    # 反例：签名两行之外的正文不应被本条误伤（正文含 # 另由脏标签规则判，此处不重复判错）
    ("正文普通行不因本条判错",
     "#数学/泛函方程\n某概念\n\n结论句正常书写，无异常字符。\n", 0, 0),
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

        # 带 BOM 的纯文本：utf-8-sig 应剥掉 BOM，首行仍是标签段
        bom = d / "bom.txt"
        bom.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        got = VM.load_content(["validate_memo.py", "--file", str(bom)])
        ok &= got == body and not got.startswith("\ufeff")
        print(f"{'PASS' if got == body and not got.startswith(chr(0xFEFF)) else 'FAIL'}  "
              f"带 BOM 文件剥离 BOM（首行仍为标签段）")

        # 带 BOM 的 JSON：同样应剥 BOM 后正常抽取 content
        bomj = d / "bom.json"
        bomj.write_bytes(b"\xef\xbb\xbf" + json.dumps({"content": body}, ensure_ascii=False).encode("utf-8"))
        got = VM.load_content(["validate_memo.py", "--create", str(bomj)])
        ok &= got == body
        print(f"{'PASS' if got == body else 'FAIL'}  带 BOM 的 JSON 正常抽取 content")
    return ok


def run_table_row_cases():
    """_is_table_row 边界：真表格行 / 非表格行 / 空串。"""
    cases = [
        ("| 项 | 值 |", True, "标准管道分隔行"),
        ("| a | b | c |", True, "三列表格行"),
        ("|---|---|", True, "表格分隔行"),
        ("- 普通列表项", False, "列表项非表格"),
        ("普通句子含 | 竖线", False, "句中竖线非表格"),
        ("|", False, "仅一个竖线不足以判表格"),
        ("", False, "空串非表格"),
        ("｜ 项目 ｜ 值 ｜", True, "全角竖线包裹行"),
        ("｜ 项 ｜ 值 ｜ 备注 ｜", True, "全角三列表格行"),
        ("a ｜ b ｜ c", True, "全角竖线空格成组"),
        ("｜p｜≤r", False, "全角竖线紧贴文字属数学书写"),
        ("|p|≤r", False, "半角竖线紧贴文字属数学书写"),
        ("P(A|B) 表示条件概率", False, "条件概率竖线"),
    ]
    ok = True
    for s, want, label in cases:
        got = VM._is_table_row(s)
        same = got == want
        ok &= same
        print(f"{'PASS' if same else 'FAIL'}  _is_table_row({label}) -> {got}（期望 {want}）")
    return ok


def run_content_from_json_cases():
    """_content_from_json 各分支：顶层/params.arguments/顶层 arguments/非法。"""
    body = "正文内容"
    cases = [
        ("顶层 content", {"content": body}, body),
        ("params.arguments.content",
         {"params": {"arguments": {"content": body}}}, body),
        ("顶层 arguments.content", {"arguments": {"content": body}}, body),
        ("非 dict 输入", "不是字典", ""),
        ("content 非字符串", {"content": 123}, ""),
        ("arguments 非 dict", {"arguments": "x"}, ""),
        ("无 content 键", {"foo": 1}, ""),
    ]
    ok = True
    for label, obj, want in cases:
        got = VM._content_from_json(obj)
        same = got == want
        ok &= same
        print(f"{'PASS' if same else 'FAIL'}  _content_from_json({label}) -> {got!r}")
    return ok


def run_gate_cases():
    """check_gate 的签名失效路径：不得因「取不到签名」而静默放行整套闸门。"""
    ok = True
    cases = [
        ("第二行是标签 → 签名失效须判错",
         "#数学/泛函\n#这不是概念名\n\n正文结论句。\n", True),
        ("仅一行标签 → 签名失效须判错",
         "#数学/泛函\n", True),
        ("结构合规但无凭证 → 判错（缺凭证）",
         "#数学/泛函方程\n某概念\n\n正文结论句。\n", True),
    ]
    for label, content, want_err in cases:
        VM.ERR.clear()
        VM.WARN.clear()
        VM.check_gate(content)
        got_err = len(VM.ERR) > 0
        same = got_err == want_err
        ok &= same
        print(f"{'PASS' if same else 'FAIL'}  check_gate {label}（得到 ERR={got_err}）")
        if not same:
            for m in VM.ERR:
                print("      ERR :", m)
    # 签名失效时错误信息必须点明「签名取不到」，不能只说缺凭证
    VM.ERR.clear()
    VM.check_gate("#数学/泛函\n#这不是概念名\n\n正文。\n")
    has_msg = any("签名取不到" in m for m in VM.ERR)
    ok &= has_msg
    print(f"{'PASS' if has_msg else 'FAIL'}  check_gate 签名失效错误信息可辨识")
    if not has_msg:
        for m in VM.ERR:
            print("      ERR :", m)
    return ok


if __name__ == "__main__":
    results = [run_case(*c) for c in CASES]  # 不用 all() 短路，需跑完全部用例
    results.append(run_loader_cases())
    results.append(run_table_row_cases())
    results.append(run_content_from_json_cases())
    results.append(run_gate_cases())
    print("---")
    print("全部通过" if all(results) else "存在失败用例")
    sys.exit(0 if all(results) else 1)
