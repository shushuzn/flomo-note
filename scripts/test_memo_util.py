#!/usr/bin/env python3
"""memo_util.py 回归用例（离线，不联网）。

memo_util 是签名/指纹/关键词/标签拆分的**唯一实现**，被 flomo_client、sop_gate、
validate_memo 三处共用；本文件确保这条公共口径被独立锁住。
退出码 0 = 全部通过。
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
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
    check("未配对左括号原样保留",
          MU.tag_leaves("#科技/机器人（人形") == [("科技", "机器人（人形")])
    check("纯中文二级名不被误剔",
          MU.tag_leaves("#科技/机器人") == [("科技", "机器人")])
    # 嵌套/多层收尾标点应被清干净
    check("句号加尾括号一并剔除",
          MU.tag_leaves("#科技/机器人）。") == [("科技", "机器人")])


def run_tag_tree_snapshot():
    """快照的渲染与计数必须同源：渲染端产出多少条，计数端就该读出多少条。

    这两端分别被同步脚本与闸门使用，任一侧口径漂移都会让快照永远"判不自洽"。
    """
    tags = ["科技/机器人", "科技/安全", "投资/一级市场", "投资/"]
    text = MU.render_tag_tree(tags, 4)
    lines = text.splitlines()

    check("首行为 total", lines[0] == "# total=4")
    leaves, bare = MU.count_snapshot(lines)
    check("渲染与计数同源（二级 3 + 裸顶层 1 == total）", leaves == 3 and bare == 1)
    check("顶层分组标题行不计数", "# 科技" in lines and "# 投资" in lines)
    check("裸顶层行不缩进且带斜杠", "投资/" in lines)
    check("二级行缩进", any(ln.startswith("  ") for ln in lines))
    check("round-trip 恒等（渲染后再计数 == total）",
          sum(MU.count_snapshot(MU.render_tag_tree(tags, 4).splitlines())) == 4)

    check("字典形态 tags 与字符串形态等价",
          MU.render_tag_tree([{"name": "科技/机器人"}], 1)
          == MU.render_tag_tree(["科技/机器人"], 1))

    t2 = MU.render_tag_tree(["", None, {"name": None}, "科技/机器人"], 1)
    check("空名与非字符串被忽略", sum(MU.count_snapshot(t2.splitlines())) == 1)
    check("重复标签不重复成行",
          sum(MU.count_snapshot(
              MU.render_tag_tree(["科技/机器人", "科技/机器人"], 1).splitlines())) == 1)

    t3 = MU.render_tag_tree(["科技/安全/邮件"], 1)
    check("三级名原样保留（不擅自降级）", "  科技/安全/邮件" in t3.splitlines())
    check("三级名仍计 1 条", sum(MU.count_snapshot(t3.splitlines())) == 1)

    check("snapshot_total 取首行数字", MU.snapshot_total(["# total=596"]) == 596)
    check("snapshot_total 空输入为 None", MU.snapshot_total([]) is None)
    check("snapshot_total 无数字为 None", MU.snapshot_total(["# total=x"]) is None)
    check("count_snapshot 空输入为 (0, 0)", MU.count_snapshot([]) == (0, 0))


if __name__ == "__main__":
    run_signature()
    run_key()
    run_body_hash()
    run_keywords()
    run_keywords_of_concept()
    run_tag_leaves()
    run_tag_tree_snapshot()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
