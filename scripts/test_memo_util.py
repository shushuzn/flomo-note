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


def run_tag_leaves():
    check("拆出两级标签",
          MU.tag_leaves("#科技/机器人 #AI/物理AI")
          == [("科技", "机器人"), ("AI", "物理AI")])
    check("空值返回空表", MU.tag_leaves("") == [] and MU.tag_leaves(None) == [])
    check("三段标签不匹配两级",
          MU.tag_leaves("#科技/安全/邮件") == [])


if __name__ == "__main__":
    run_signature()
    run_key()
    run_body_hash()
    run_keywords()
    run_tag_leaves()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
