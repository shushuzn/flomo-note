#!/usr/bin/env python3
"""memo_util.py 回归用例（离线，不联网）。

memo_util 是签名/指纹/关键词/标签拆分的**唯一实现**，被 flomo_client、sop_gate、
validate_memo 三处共用；本文件确保这条公共口径被独立锁住。
退出码 0 = 全部通过。
"""

COVERS = ("memo_util",)

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("memo_util", HERE / "memo_util.py")
MU = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MU)

RESULTS = []


def check(label, ok):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


CARD = "#科技/机器人 #AI/物理AI\n德塔智能 Delta 0 双足人形机器人基础模型\n\n正文。\n"


def run_signature():
    check("标准卡签名",
          MU.memo_signature(CARD) == {"tagline": "#科技/机器人 #AI/物理AI",
                                      "concept": "德塔智能 Delta 0 双足人形机器人基础模型"})
    # H14 变体：标签段与概念名之间夹空行
    check("标签段后夹空行仍取到概念名",
          MU.memo_signature("#科技/机器人\n\n概念名\n正文\n")
          == {"tagline": "#科技/机器人", "concept": "概念名"})
    # 读接口回成 `#标签<br>`（全库一致）：签名必须与本地草稿一致，否则查重找不到
    # 已有卡 → 建重复卡，闸门凭证也对不上。
    check("行尾 <br> 不影响签名（否则查重会建重复卡）",
          MU.memo_signature("#科技/机器人<br>\n概念名<br>\n正文<br>")
          == MU.memo_signature("#科技/机器人\n概念名\n正文"))
    check("行中 <br> 仍属正文，不剥",
          MU.strip_trailing_br("甲<br>乙") == "甲<br>乙")
    check("空串返回 None", MU.memo_signature("") is None)
    check("None 返回 None", MU.memo_signature(None) is None)
    check("非字符串返回 None", MU.memo_signature(123) is None)
    check("仅一行返回 None", MU.memo_signature("#标签\n") is None)
    check("首行非标签返回 None", MU.memo_signature("普通行\n概念名\n") is None)
    check("第二行也是标签返回 None", MU.memo_signature("#a/b\n#c/d\n正文\n") is None)


def run_key():
    k1 = MU.signature_key(MU.memo_signature(CARD))
    k2 = MU.signature_key(MU.memo_signature(CARD))
    check("同签名同键", k1 == k2 and len(k1) == 16)
    check("签名 None 时键为 None", MU.signature_key(None) is None)
    other = MU.signature_key(MU.memo_signature("#别的/标签\n另一概念\n\n正文\n"))
    check("不同签名不同键", k1 != other)


def run_body_hash():
    check("同正文同指纹", MU.body_hash(CARD) == MU.body_hash(CARD))
    check("正文微改指纹即变", MU.body_hash(CARD) != MU.body_hash(CARD + "x"))
    check("None 不崩", isinstance(MU.body_hash(None), str))
    # None 与空串必须可区分：否则"取不到正文"会被误当成"正文为空"
    check("None 与空串指纹不同", MU.body_hash(None) != MU.body_hash(""))
    check("None 指纹稳定", MU.body_hash(None) == MU.body_hash(None))
    check("数字与非字符串同类型同指纹", MU.body_hash(1) == MU.body_hash(2))
    check("字符串与非字符串指纹不同", MU.body_hash("1") != MU.body_hash(1))


def run_keywords():
    kw = MU.concept_keywords(CARD)
    check("派生出关键词", len(kw) >= 1 and all(kw))
    check("含中文核心段", any("双足人形机器人" in k for k in kw))
    check("limit 生效", len(MU.concept_keywords(CARD, limit=1)) == 1)
    check("无效输入返回空表", MU.concept_keywords(None) == []
          and MU.concept_keywords("#标签\n") == [])
    # 全角标点不应污染关键词
    kw2 = MU.concept_keywords("#数学/泛函\n算子（含括号）的定义\n\n正文\n")
    check("括号不进关键词", all("（" not in k and "）" not in k for k in kw2))


def run_keywords_of_concept():
    """概念名直接派生：闸门/客户端只握有概念名时的入口（不经卡片结构）。"""
    c = "德塔智能 Delta 0 双足人形机器人基础模型"
    check("概念名直接派生", MU.keywords_of_concept(c) == MU.concept_keywords(f"#_/_\n{c}\n"))
    check("概念名派生非空", len(MU.keywords_of_concept(c)) >= 1)
    check("概念名 limit 生效", len(MU.keywords_of_concept(c, limit=1)) == 1)
    check("概念名空值返回空表",
          MU.keywords_of_concept(None) == [] and MU.keywords_of_concept("") == []
          and MU.keywords_of_concept("   ") == [])
    check("概念名非字符串返回空表", MU.keywords_of_concept(123) == [])
    # 去重保序：同一关键词不重复出现
    kw = MU.keywords_of_concept("某概念名称")
    check("概念名派生结果无重复", len(kw) == len(set(kw)))


def run_tag_leaves():
    check("拆出两级标签",
          MU.tag_leaves("#科技/机器人 #AI/物理AI")
          == [("科技", "机器人"), ("AI", "物理AI")])
    check("空值返回空表", MU.tag_leaves("") == [] and MU.tag_leaves(None) == [])
    check("三段标签不匹配两级",
          MU.tag_leaves("#科技/安全/邮件") == [])
    # 二级名收尾标点须剔除，否则近邻比对会指向不存在的簇
    check("二级名收尾句号被剔除", MU.tag_leaves("#科技/机器人。") == [("科技", "机器人")])
    check("二级名收尾逗号/分号被剔除",
          MU.tag_leaves("#科技/机器人， #投资/一级市场；")
          == [("科技", "机器人"), ("投资", "一级市场")])
    check("混合标点与多标签",
          MU.tag_leaves("#科技/安全  #投资/一级市场。")
          == [("科技", "安全"), ("投资", "一级市场")])
    # 全角括号属二级名的一部分还是收尾标点：右括号在尾部应剔除
    check("二级名尾右括号被剔除",
          MU.tag_leaves("#科技/机器人）") == [("科技", "机器人")])
    # 成对括号必须成对保留——一刀切 rstrip 会剥成残缺的「机器人（人形」
    check("成对括号原样保留",
          MU.tag_leaves("#科技/机器人（人形）") == [("科技", "机器人（人形）")])


def run_tag_key():
    """`tag_key` 只认「同一簇被写成两种」的三档：空白、全半角斜杠、大小写。"""
    check("同写法同键", MU.tag_key("AI/Agent") == MU.tag_key("AI/Agent"))
    check("折叠半角空白", MU.tag_key("AI / Agent") == MU.tag_key("AI/Agent"))
    check("折叠全角空格", MU.tag_key("AI\u3000/Agent") == MU.tag_key("AI/Agent"))
    check("全角斜杠归一", MU.tag_key("AI／Agent") == MU.tag_key("AI/Agent"))
    check("统一小写", MU.tag_key("ai/agent") == MU.tag_key("AI/Agent"))
    # 不是「近似」：换字/换层就是不同的簇，这个键不能把它们并成一个
    check("换字不并簇", MU.tag_key("AI/Agents") != MU.tag_key("AI/Agent"))
    check("换层不并簇", MU.tag_key("AI/Agent") != MU.tag_key("科技/Agent"))
    check("空值安全", MU.tag_key("") == "" and MU.tag_key(None) == "")
    check("未配对左括号原样保留",
          MU.tag_leaves("#科技/机器人（人形") == [("科技", "机器人（人形")])
    check("纯中文二级名不被误剔",
          MU.tag_leaves("#科技/机器人") == [("科技", "机器人")])
    # 嵌套/多层收尾标点应被清干净
    check("句号加尾括号一并剔除",
          MU.tag_leaves("#科技/机器人）。") == [("科技", "机器人")])


def run_slice_qualifiers():
    """切片限定识别（H6b）：拦截面与放行面都要钉住。

    这批用例是**两侧同源契约**的锁：质检拦截（validate_memo 5.8）与存量取证
    （scan_qualified_concepts）都调用本实现。若有人图省事在调用方另写一套正则，
    两侧会静默失配——失配只表现为「统计数字对不上」，不会被任何单侧用例发现。
    故除功能用例外，另加一条源码扫描断言两侧确实共用本实现。
    """
    # 应命中：各类切片
    must_hit = [
        "某模型 2026 年度财报", "某市场 Q3 格局", "最新的大模型进展",
        "某工具 v0.21.4", "某工具 2.0 版本", "某模型 RC2 预览",
        "谷歌第七代TPU", "某赛事第 3 轮", "某项目首期上线",
        "某产品中文版", "某基准赛夺冠", "某大会 举办",
        # 事件动词回归（实测漏检过五张真卡）：动作词表之外的
        # 事件动词曾整批漏过质检，卡写成「对象+谓语」形态却一路绿灯。
        "iPhone Duo 首拆", "丹麦弃用微软 Office 计划中止",
        "《灵异女仆》抄袭诉讼终审胜诉", "卫星互联网低轨 28 组卫星发射",
        "OpenAI 解雇三名安全研究人员",
        # 动词 + 数量宾语：谓语标志不是「后接具体事物」，而是数量短语
        "某平台清退 12 名商家", "某医院解雇 4 名医生",
        # 事件名词切片：名字说的是「这件事」，不是那个对象（H6b ②③）。
        # 事件名词不是动词，谓语词表整批漏过——真卡因此写成「对象+事件」却 0 ERR。
        "小鹏 X9 气囊未弹开争议", "某车型高速碰撞事故", "某小区电梯事故",
        "某品牌动力电池召回", "某平台佣金纠纷", "某电池厂专利诉讼",
        # 个例指称（某 / 数字）＋动词：这里不再靠「术语级构词豁免表」区分，
        # 而是「术语前面不会出现个例指称」——「某项目」是某一次，「睡眠」是类别名。
        "某项目暂停", "某服务全量暂停",
        # 状语只出现在主谓句的谓语前：复合术语内部不插状语
        "康佳集团主动退市",
    ]
    for n in must_hit:
        check(f"切片命中：{n}", len(MU.slice_qualifiers(n)) > 0)

    # 不应命中：对象名自身含数字/型号/地名/阶段词
    must_miss = [
        "Grok Voice Transcribe 2.0 语音转文本模型", "阶跃星辰 Step 5 Preview 大模型",
        "LPDDR6 低功耗内存", "iPhone 17 Pro", "华为 Mate 40 系列",
        "Qwen3 与 Qwen3.5 的对比", "Claude Opus 4.6 与 Opus 4.8 的对比",
        "京东物流履约时效", "北交所改革试点", "混合专家模型 MoE 架构",
        "Python 3.14 新特性", "比特币与以太坊", "开源模型的安全对齐",
        # 构词防线：动作词表扩到事件动词后，「动+名」构成的合法名词短语
        # 最容易被规则③整片误拦，而质检 ERR 会直接阻断写云。
        "手机拆解指南", "电池拆解成本", "信创认证体系", "发布会", "获奖情况",
        "胜诉率", "交付周期", "量产良率", "某公司发布口径",
        # 事件名词的误拦防线：法律/行业分类名去掉事件名词就不成词，属对象名本身
        "医疗事故", "劳动争议", "生产安全事故", "数据安全事件",
        "汽车召回制度", "事件驱动架构", "争议解决机制",
    ]
    for n in must_miss:
        check(f"不误伤：{n}", MU.slice_qualifiers(n) == [])

    # 术语与事件**同形**的形态：一律落到「待判」档，不阻断、也不假装合规。
    #
    # 旧设计挂 `_TERM_COMPOUND = {("呼吸", "暂停")}` 一张豁免表，靠具体词放行：
    # 表不可能收敛（每冒出一个新术语都要先被拦一次、再补一行），而且补表的触发
    # 条件是「有人正好在写这类卡」——没人写就永远停在一条。现在改判证据强度，
    # 这一档统一只提示，术语按 H13 不必为过检改写，事件名由人读一眼定。
    soft_only = [
        "睡眠呼吸暂停", "阻塞性睡眠呼吸暂停低通气综合征", "呼吸暂停综合征",
        "场发射透射电镜", "康佳集团退市",
    ]
    for n in soft_only:
        hits = MU.slice_qualifiers(n)
        check(f"术语/事件同形只提示（{n}）：得到 {hits}",
              bool(hits) and all(lbl == "谓语动作（待判）" for lbl, _ in hits))

    # 省略主语的事件名：同样以动作开头，但后接宾语形态即不是构词。
    # 构词要求动词后是名词性成分（`发布会`、`付费墙`），后接数量宾语 / 型号 /
    # 另一动作是反面证据——`解雇三名研究员` 说的是「谁做了什么」，不是「解雇」本身。
    # 旧实现里「动词前无成分」直接判构词放行，这类事件名整批穿过质检。
    for n in ["解雇三名研究员", "清退 12 名商家", "发布 K2.8", "裁员两千余人"]:
        hits = MU.slice_qualifiers(n)
        check(f"省略主语的事件名须判谓语（{n}）：得到 {hits}",
              bool(hits) and all(lbl == "谓语动作" for lbl, _ in hits))


    # 返回结构：[(标签, 命中文本)]，供错误文案指名具体切片
    h = MU.slice_qualifiers("某工具 v0.21.4")
    check("返回 (标签, 命中文本) 二元组", len(h) == 1 and h[0][1] == "v0.21.4")
    check("空串返回 []", MU.slice_qualifiers("") == [])
    check("None 返回 []（不抛异常）", MU.slice_qualifiers(None) == [])

    # 年份区间必须整段命中。旧写法只匹到 `2023-20`，残余的 `24` 留在名字里：
    # 对质检只是文案难读，对取证脚本却是**判据错**——归一化后「2023-2024 的 X」
    # 与「2024 的 X」成了两个键（同一对象漏报），而不同对象的残串又可能撞键
    # （报出不存在的重复）。两处都读同一份实现，所以这里钉住「整段」这一条。
    for span in ("某峰会 2023-2024 议程", "某峰会 2023/2024 议程", "某峰会 2023 至 2024 议程"):
        hits = MU.slice_qualifiers(span)
        got = hits[0][1] if hits else ""
        check(f"区间年份整段命中（{span}）：得到 {got!r}",
              got.startswith("2023") and got.endswith("2024"))

    # 作用域：只扫概念名的**标题部分**。「标题：摘要」形态（H14 另判概念名写成整句）
    # 里的摘要用词不得被误判成概念名的切片——判据作用域错了会让合规卡因摘要用词被拦。
    check("摘要里的当前不误判",
          MU.slice_qualifiers("某模型：当前能力边界与今日实践") == [])
    check("摘要里的近日不误判",
          MU.slice_qualifiers("某论文的核心论点：给长程 agent 以近日表现") == [])
    check("标题里的切片照常命中（冒号前）",
          MU.slice_qualifiers("某工具 v0.21.4：一次更新") != [])
    check("concept_head 基本形态",
          MU.concept_head("某模型") == "某模型")
    check("concept_head 遇冒号截断",
          MU.concept_head("某模型：摘要") == "某模型")
    check("concept_head 空/None 安全",
          MU.concept_head("") == "" and MU.concept_head(None) == "")

    # 两侧同源契约：调用方不得自带切片正则
    root = Path(__file__).resolve().parent.parent / "scripts"
    for fn in ("validate_memo.py", "scan_qualified_concepts.py",
               "scan_concept_duplication.py"):
        src = (root / fn).read_text(encoding="utf-8")
        has_import = "slice_qualifiers" in src
        # 本地正则特征：20\d{2} 年份类、相对时间词枚举、Qq 季度类
        local = re.search(r'最新\|近期\|当前|(?:19\|20)\\d\{2\}|\[Qq\]\\s\?\[1-4\]', src)
        check(f"{fn} 使用共享实现", has_import)
        check(f"{fn} 不自带切片正则", local is None)


if __name__ == "__main__":
    run_signature()
    run_key()
    run_body_hash()
    run_keywords()
    run_keywords_of_concept()
    run_tag_leaves()
    run_tag_key()
    run_slice_qualifiers()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
