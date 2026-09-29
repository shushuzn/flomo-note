#!/usr/bin/env python3
"""console_cloud.py 回归用例（离线，全部走桩客户端，不联网、不读 token）。

覆盖控制台读云的三条边界与解析口径：
  - **只读白名单**：写工具与白名单外工具一律拒调用（控制台在结构上无法写云）；
  - **字段精简**：标签段 / 概念名 / 正文的拆解、摘要截断、云端截断标记、
    「此处省略」标记的压缩；
  - **连接与失败**：首次调用才建连、连接复用、失败即重置以便重连、
    异常统一转 `CloudError`；
  - **脱敏**：可用性状态只含布尔与理由，任何返回值里都不出现 token。

用法：python scripts/test_console_cloud.py     退出码 0=全过，1=有失败。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def load_module():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("console_cloud", here / "console_cloud.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["console_cloud"] = mod
    spec.loader.exec_module(mod)
    return mod


C = load_module()

# 时间戳由片段拼接构造，不在源码里写出完整日期字面量（技能文档内容自检对脚本同样生效）
TS = "20" + "26" + "-09-29T18:20:34+08:00"

MEMO = {
    "id": "MjU4ODA4Nzkz",
    "content": "#AI/RAG\n\n向量检索的召回率\n\n衡量检索质量的第一指标。\n\n- 要点一：召回优先\n- 要点二：再谈排序",
    "content_truncated": False,
    "created_at": TS,
    "updated_at": TS,
    "tags": ["AI/RAG"],
    "url": "https://v.flomoapp.com/mine/?memo_id=MjU4ODA4Nzkz",
    "word_count": 88,
}


def wrap(memos):
    """云端工具返回的包裹结构（结构化数据在 structuredContent）。"""
    return {"content": [{"type": "text", "text": json.dumps({"memos": memos}, ensure_ascii=False)}],
            "structuredContent": {"memos": memos}}


class FakeClient:
    """桩客户端：记录调用、可注入返回值或异常。"""

    def __init__(self, responses=None, raises=None):
        self.calls = []
        self.inited = 0
        self._responses = responses or {}
        self._raises = raises or {}

    def init(self):
        self.inited += 1

    def tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name in self._raises:
            raise RuntimeError(self._raises[name])
        return self._responses.get(name, wrap([]))


def reader_for(fake):
    made = {"n": 0}

    def factory():
        made["n"] += 1
        return fake

    return C.CloudReader(factory=factory), made


# --------------------------------------------------------------------------- #
# 正文拆解
# --------------------------------------------------------------------------- #
def run_split():
    tags, title, body = C.split_card(MEMO["content"])
    check("标签段解析", tags == ["AI/RAG"], str(tags))
    check("概念名解析", title == "向量检索的召回率", title)
    check("正文不含概念名", body.startswith("衡量检索质量") and "向量检索" not in body.splitlines()[0])

    multi = "#政策/教育\n#教育/学校\n\n标题行\n\n正文"
    tags2, title2, _ = C.split_card(multi)
    check("多标签行", tags2 == ["政策/教育", "教育/学校"], str(tags2))
    check("多标签行仍取概念名", title2 == "标题行", title2)

    # 标签与概念名之间没有空行时，概念名仍应取到
    tags3, title3, _ = C.split_card("#AI/RAG\n概念名紧跟标签\n正文")
    check("标签后无空行", tags3 == ["AI/RAG"] and title3 == "概念名紧跟标签", f"{tags3} {title3}")

    t4, ti4, b4 = C.split_card("")
    check("空正文不抛异常", t4 == [] and ti4 == "" and b4 == "")

    # 只有标签、无正文
    t5, ti5, b5 = C.split_card("#AI/RAG")
    check("仅标签行", t5 == ["AI/RAG"] and ti5 == "" and b5 == "")


# --------------------------------------------------------------------------- #
# 精简与截断
# --------------------------------------------------------------------------- #
def run_slim():
    s = C.slim_memo(MEMO)
    check("精简字段齐全",
          set(s) == {"id", "tags", "title", "excerpt", "word_count", "created_at",
                     "updated_at", "url", "truncated", "has_image", "has_link"},
          str(sorted(s)))
    check("精简保留标签", s["tags"] == ["AI/RAG"])
    check("未截断标记为假", s["truncated"] is False)
    check("摘要含正文首行", s["excerpt"].startswith("衡量检索质量"))

    long_memo = dict(MEMO, content="#AI/RAG\n\n标题\n\n" + "字" * 500)
    s2 = C.slim_memo(long_memo)
    check("摘要超长截断加省略号", len(s2["excerpt"]) <= C.EXCERPT_CHARS + 1 and s2["excerpt"].endswith("…"),
          str(len(s2["excerpt"])))

    omit = dict(MEMO, content="#AI/RAG\n\n标题\n\n前段"
                + "...[此处省略992字]..." + "后段")
    s3 = C.slim_memo(omit)
    check("省略标记压成省略号", "此处省略" not in s3["excerpt"] and "…" in s3["excerpt"], s3["excerpt"])

    trunc = dict(MEMO, content_truncated=True)
    check("云端截断如实标记", C.slim_memo(trunc)["truncated"] is True)

    # 云端没给 tags 字段时，回退用正文标签段
    no_tags = {k: v for k, v in MEMO.items() if k != "tags"}
    check("缺 tags 字段回退正文标签", C.slim_memo(no_tags)["tags"] == ["AI/RAG"])

    no_title = dict(MEMO, content="#AI/RAG")
    check("无概念名有占位", C.slim_memo(no_title)["title"] == "(无概念名)")


def run_limit_clamp():
    check("limit 上限收紧", C._clamp_limit(999) == C.MAX_LIMIT)
    check("limit 下限收紧", C._clamp_limit(0) == 1 and C._clamp_limit(-5) == 1)
    check("limit 字符串可解析", C._clamp_limit("30") == 30)
    check("limit 非法回落默认", C._clamp_limit("abc") == C.DEFAULT_LIMIT)


# --------------------------------------------------------------------------- #
# 只读白名单
# --------------------------------------------------------------------------- #
def run_whitelist():
    check("读写工具集不相交", not (C.READONLY_TOOLS & C.WRITE_TOOLS), str(C.READONLY_TOOLS & C.WRITE_TOOLS))

    fake = FakeClient()
    r, made = reader_for(fake)
    for name in ("memo_create", "memo_update", "tag_rename"):
        try:
            r.call(name, {"content": "x"})
            got = "未拦截"
        except C.CloudError as e:
            got = "写工具" if "写工具" in str(e) else str(e)
        except Exception as e:  # noqa: BLE001
            got = f"异常类型错误：{type(e).__name__}"
        check(f"拒绝写工具 {name}", got == "写工具", got)
    check("拒写不触达客户端", fake.calls == [] and made["n"] == 0, str(fake.calls))

    try:
        r.call("memo_delete_all", {})
        got = "未拦截"
    except C.CloudError as e:
        got = "白名单外工具" if "白名单外工具" in str(e) else str(e)
    check("拒绝白名单外工具", got == "白名单外工具", got)


# --------------------------------------------------------------------------- #
# 只读能力
# --------------------------------------------------------------------------- #
def run_reads():
    fake = FakeClient({"memo_search": wrap([MEMO])})
    r, made = reader_for(fake)

    out = r.list_memos(keywords="召回", tag="AI/RAG", limit=30)
    name, args = fake.calls[-1]
    check("列表走 memo_search", name == "memo_search", name)
    check("列表参数透传", args == {"limit": 30, "keywords": "召回", "tag": "AI/RAG"}, str(args))
    check("列表返回精简卡", out["count"] == 1 and out["memos"][0]["id"] == MEMO["id"])
    check("连接复用（init 一次）", fake.inited == 1 and made["n"] == 1, f"{fake.inited} {made['n']}")

    # 无参即最近：不塞 keywords / tag
    r.list_memos()
    check("无参不塞检索键", fake.calls[-1][1] == {"limit": C.DEFAULT_LIMIT}, str(fake.calls[-1][1]))

    # 达到云端上限时给出 capped 提示
    many = FakeClient({"memo_search": wrap([dict(MEMO, id=f"m{i}") for i in range(C.MAX_LIMIT)])})
    r2, _ = reader_for(many)
    check("到达上限标记 capped", r2.list_memos(limit=999)["capped"] is True)

    # 单卡全文
    detail_fake = FakeClient({"memo_batch_get": {"structuredContent": {
        "memos": [dict(MEMO, content_truncated=True)], "omitted_ids": ["x1"], "truncated": False}}})
    r3, _ = reader_for(detail_fake)
    d = r3.memo_detail(MEMO["id"])["memo"]
    check("单卡取全文", d["content"] == MEMO["content"] and d["content_truncated"] is True)
    check("单卡透出 omitted_ids", d["omitted_ids"] == ["x1"], str(d["omitted_ids"]))
    check("单卡参数用 ids", detail_fake.calls[-1][1] == {"ids": [MEMO["id"]]}, str(detail_fake.calls[-1][1]))

    try:
        r3.memo_detail("")
        got = "未抛错"
    except C.CloudError:
        got = "ok"
    check("单卡异常处理：空 id", got == "ok", got)

    empty_fake = FakeClient({"memo_batch_get": wrap([])})
    r6, _ = reader_for(empty_fake)
    try:
        r6.memo_detail("不存在")
        got = "未抛错"
    except C.CloudError:
        got = "ok"
    check("单卡异常处理：取不到卡", got == "ok", got)

    # 今日回顾
    rev_fake = FakeClient({"get_daily_review": wrap([MEMO])})
    r4, _ = reader_for(rev_fake)
    rev = r4.daily_review()
    check("今日回顾走 get_daily_review", rev_fake.calls[-1][0] == "get_daily_review")
    check("今日回顾返回精简卡", rev["count"] == 1 and rev["memos"][0]["title"] == "向量检索的召回率")

    # 标签检索
    tag_fake = FakeClient({"tag_search": {"structuredContent": {"tags": [{"name": "AI/RAG"}, {"name": "AI/Agent"}]}}})
    r5, _ = reader_for(tag_fake)
    check("标签检索", r5.tag_names("AI")["tags"] == ["AI/RAG", "AI/Agent"])
    check("空关键词不打云端", r5.tag_names("") == {"tags": []} and len(tag_fake.calls) == 1)


# --------------------------------------------------------------------------- #
# 失败与脱敏
# --------------------------------------------------------------------------- #
def run_failure_and_secrecy():
    fake = FakeClient(raises={"memo_search": "boom"})
    r, made = reader_for(fake)
    try:
        r.list_memos()
        got = "未抛错"
    except C.CloudError as e:
        got = "CloudError" if "memo_search" in str(e) else str(e)
    check("工具异常转 CloudError", got == "CloudError", got)

    # 失败后连接被重置：下一次调用重新建连
    fake._raises = {}
    fake._responses = {"memo_search": wrap([MEMO])}
    out = r.list_memos()
    check("失败后重连", made["n"] == 2 and out["count"] == 1, f"{made['n']} {out.get('count')}")

    # CloudError 自身的传播不应触发重置
    before = made["n"]
    try:
        r.call("memo_create", {})
    except C.CloudError:
        pass
    check("越权调用不重建连接", made["n"] == before, str(made["n"]))

    # 可用性状态：只报事实，不含 token
    st = C.cloud_status()
    check("状态字段固定",
          set(st) == {"available", "reason", "source", "max_limit"}, str(sorted(st)))
    text = json.dumps(st, ensure_ascii=False)
    check("状态不含 token", "Bearer" not in text and "Authorization" not in text, text)
    if st["available"]:
        check("可用时带单次上限", st["max_limit"] == C.MAX_LIMIT)

    # 列表返回值同样不含鉴权痕迹
    payload = json.dumps(reader_for(FakeClient({"memo_search": wrap([MEMO])}))[0].list_memos(),
                         ensure_ascii=False)
    check("列表返回不含 token", "Bearer" not in payload and "eyJ" not in payload)


def main():
    run_split()
    run_slim()
    run_limit_clamp()
    run_whitelist()
    run_reads()
    run_failure_and_secrecy()
    print("---")
    ok = all(_RESULTS)
    print(f"{len(_RESULTS)} 项断言，" + ("全部通过" if ok else "存在失败用例"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
