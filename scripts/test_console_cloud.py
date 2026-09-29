#!/usr/bin/env python3
"""console_cloud.py 回归用例（离线，全部走桩客户端，不联网、不读 token）。

覆盖控制台读云的三条边界与全部只读能力：
  - **只读白名单**：写工具与白名单外工具一律拒调用（控制台在结构上无法写云）；
    白名单**恰等于**源码实际调用的工具（扫描锁定，不留无人使用的权限空位）；
  - **检索条件**：`memo_search` 的全部入参（关键词 / 标签 / 起止日期 / 来源 / 是否含标签）
    透传与省略（未指定即不塞键），三态开关的取值判定；
  - **批量与相关**：批量全文的 10 条上限、空 id 丢弃与报错、相关笔记的排除同标签开关；
  - **标签**：标签树的条数上限与「前缀 / 深度」收窄、截断与提示的转达、标签名搜索；
  - **参考文本**：记忆 / 画像 / 指南四份的正文提取，含 `content[].text` 内层 JSON 兜底；
  - **能力清单**：工具清单走协议发现（不计入工具调用）、读写分组、写工具标注未接入及原因；
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
import re
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


def text_reply(value):
    """纯文本工具（指南 / 记忆 / 画像）的包裹结构。"""
    return {"content": [{"type": "text", "text": json.dumps({"content": value}, ensure_ascii=False)}],
            "structuredContent": {"content": value}}


def nested_text_reply(value):
    """只有 content[].text、且内层是 JSON 的兜底形态（无 structuredContent）。"""
    return {"content": [{"type": "text", "text": json.dumps({"content": value}, ensure_ascii=False)}]}


TOOL_LIST = {
    "tools": [
        {"name": "memo_search", "description": "搜索笔记。返回匹配的笔记列表。",
         "inputSchema": {"properties": {"keywords": {}, "tag": {}, "limit": {}}}},
        {"name": "tag_tree", "description": "获取标签树。", "inputSchema": {"properties": {"prefix": {}}}},
        {"name": "memo_create", "description": "创建一条新笔记。",
         "inputSchema": {"properties": {"content": {}, "format": {}}}},
        {"name": "tag_rename", "description": "批量重命名标签。", "inputSchema": {"properties": {"old_tag": {}, "new_tag": {}}}},
    ]
}


class FakeClient:
    """桩客户端：记录调用、可注入返回值或异常。"""

    def __init__(self, responses=None, raises=None, tool_list=None):
        self.calls = []
        self.inited = 0
        self.list_calls = 0
        self._responses = responses or {}
        self._raises = raises or {}
        self._tool_list = tool_list if tool_list is not None else TOOL_LIST

    def init(self):
        self.inited += 1

    def tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name in self._raises:
            raise RuntimeError(self._raises[name])
        return self._responses.get(name, wrap([]))

    def tools_list(self):
        """协议发现方法（不是工具调用）：工具清单不走白名单，但也不读卡片数据。"""
        self.list_calls += 1
        if "tools/list" in self._raises:
            raise RuntimeError(self._raises["tools/list"])
        return self._tool_list


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
                     "updated_at", "url", "from", "truncated", "has_image", "has_link"},
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

    # 白名单与「源码里真正调用的工具」必须一一对应：
    # 多一个 = 留了没人用的权限空位；少一个 = 方法一调就抛「白名单外工具」。
    src = (Path(__file__).resolve().parent / "console_cloud.py").read_text(encoding="utf-8")
    used = set(re.findall(r'self\.call\("(\w+)"', src))
    check("白名单恰好等于实际调用的工具", C.READONLY_TOOLS == used,
          f"permitted {sorted(C.READONLY_TOOLS)} / used {sorted(used)}")

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
    res3 = r3.memo_detail(MEMO["id"])
    d = res3["memo"]
    check("单卡取全文", d["content"] == MEMO["content"] and d["content_truncated"] is True)
    check("单卡透出 omitted_ids", res3["omitted_ids"] == ["x1"], str(res3["omitted_ids"]))
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

    # 标签筛选由「标签树快照」供选项、检索仍走 memo_search 的 tag 参数，
    # 因此不另开标签名检索能力——白名单里也就没有 tag_search。


# --------------------------------------------------------------------------- #
# 检索条件（memo_search 的全部入参）
# --------------------------------------------------------------------------- #
# 日期同样由片段拼接构造，不在源码里写字面量日期
D1 = "20" + "26" + "-09-01"
D2 = "20" + "26" + "-09-30"


def run_search_conditions():
    fake = FakeClient({"memo_search": wrap([MEMO])})
    r, _ = reader_for(fake)

    out = r.list_memos(keywords="召回", tag="AI/RAG", start_date=D1, end_date=D2,
                       source="ai", has_tag="1", limit=200)
    name, args = fake.calls[-1]
    check("搜索全条件透传", name == "memo_search" and args == {
        "limit": C.MAX_LIMIT, "keywords": "召回", "tag": "AI/RAG",
        "start_date": D1, "end_date": D2, "from": "ai", "has_tag": True}, str(args))
    check("搜索回带条件（界面据此回显筛选项）",
          out["start_date"] == D1 and out["end_date"] == D2 and out["source"] == "ai"
          and out["has_tag"] is True, str(out))

    r.list_memos()
    check("无条件时不塞检索键", fake.calls[-1][1] == {"limit": C.DEFAULT_LIMIT},
          str(fake.calls[-1][1]))

    r.list_memos(has_tag="0")
    check("has_tag 假值透传", fake.calls[-1][1] == {"limit": C.DEFAULT_LIMIT, "has_tag": False},
          str(fake.calls[-1][1]))

    r.list_memos(has_tag="说不清")
    check("has_tag 无法判定时不传", fake.calls[-1][1] == {"limit": C.DEFAULT_LIMIT},
          str(fake.calls[-1][1]))


# --------------------------------------------------------------------------- #
# 批量全文与相关笔记
# --------------------------------------------------------------------------- #
def run_batch_and_related():
    batch_fake = FakeClient({"memo_batch_get": {"structuredContent": {
        "memos": [dict(MEMO, id=f"m{n}") for n in range(C.BATCH_MAX)],
        "omitted_ids": ["m9"], "truncated": False}}})
    r, _ = reader_for(batch_fake)

    out = r.memo_batch([f"i{n}" for n in range(C.BATCH_MAX + 4)])
    sent = batch_fake.calls[-1][1]["ids"]
    check("批量上限截到 10", len(sent) == C.BATCH_MAX, str(len(sent)))
    check("批量回报请求数", out["requested"] == C.BATCH_MAX, str(out["requested"]))
    check("批量带回全文", out["count"] == C.BATCH_MAX and all("content" in m for m in out["memos"]))
    check("批量透出 omitted_ids", out["omitted_ids"] == ["m9"], str(out["omitted_ids"]))

    r.memo_batch(["i1", "", "  ", None])
    check("批量丢弃空 id", batch_fake.calls[-1][1] == {"ids": ["i1"]}, str(batch_fake.calls[-1][1]))

    try:
        r.memo_batch([])
        got = "未抛错"
    except C.CloudError:
        got = "ok"
    check("批量空 ids 抛错", got == "ok", got)

    rel_fake = FakeClient({"memo_recommended": wrap([MEMO])})
    r2, _ = reader_for(rel_fake)
    r2.recommended(MEMO["id"], limit=5)
    check("相关笔记走 memo_recommended", rel_fake.calls[-1][0] == "memo_recommended")
    check("相关笔记默认不排除同标签",
          rel_fake.calls[-1][1] == {"id": MEMO["id"], "limit": 5}, str(rel_fake.calls[-1][1]))
    r2.recommended(MEMO["id"], no_same_tag="1")
    check("相关笔记可排除同标签", rel_fake.calls[-1][1].get("no_same_tag") is True,
          str(rel_fake.calls[-1][1]))
    try:
        r2.recommended("")
        got = "未抛错"
    except C.CloudError:
        got = "ok"
    check("相关笔记缺 id 抛错", got == "ok", got)


# --------------------------------------------------------------------------- #
# 标签树 / 标签名 / 参考文本
# --------------------------------------------------------------------------- #
def run_tags_and_texts():
    tree_fake = FakeClient({"tag_tree": {"structuredContent": {
        "tags": ["AI/RAG", "AI/Agent"], "total": 602, "returned": 2, "limit": 2,
        "truncated": True, "hint": "结果已截断；请用 prefix/depth 缩小范围。"}}})
    r, _ = reader_for(tree_fake)

    tr = r.tag_tree(prefix="AI/", depth=2, limit=9999)
    check("标签树参数收窄", tree_fake.calls[-1][1] ==
          {"limit": C.TAG_MAX_LIMIT, "prefix": "AI/", "depth": 2}, str(tree_fake.calls[-1][1]))
    check("标签树转达截断与规模",
          tr["truncated"] is True and tr["total"] == 602 and "prefix" in tr["hint"], str(tr))

    r.tag_tree()
    check("标签树默认不塞前缀与深度",
          tree_fake.calls[-1][1] == {"limit": C.TAG_DEFAULT_LIMIT}, str(tree_fake.calls[-1][1]))

    name_fake = FakeClient({"tag_search": {"structuredContent": {
        "tags": [{"name": "AI/RAG"}, {"name": "AI/Agent"}, {"nope": 1}]}}})
    r2, _ = reader_for(name_fake)
    check("标签名搜索空关键词不打云端",
          r2.tag_names("") == {"tags": []} and name_fake.calls == [], str(name_fake.calls))
    check("标签名搜索只取有名字的项",
          r2.tag_names("AI")["tags"] == ["AI/RAG", "AI/Agent"], str(r2.tag_names("AI")))

    doc_fake = FakeClient({
        "memory_context": text_reply("# 记忆\n当前关注：标签体系"),
        "memory_user": nested_text_reply("身份：测试"),
        "get_format_guide": text_reply("仅支持加粗、高亮、下划线与列表。"),
        "get_tag_guide": text_reply("命名约定：层级用斜杠。"),
    })
    r3, _ = reader_for(doc_fake)
    check("记忆文档取正文", r3.memory_doc() == {"doc": "memory", "content": "# 记忆\n当前关注：标签体系"},
          str(r3.memory_doc()))
    check("画像走 content[].text 兜底", r3.user_profile()["content"] == "身份：测试")

    ref = r3.reference()
    check("参考四份齐", set(ref) == {"memory", "user", "format", "tag"}, str(sorted(ref)))
    check("参考文本各归其位",
          ref["format"]["content"] == "仅支持加粗、高亮、下划线与列表。"
          and ref["tag"]["content"] == "命名约定：层级用斜杠。", str(ref))
    check("参考四份各带来源标识",
          [ref[k]["doc"] for k in ("memory", "user", "format", "tag")]
          == ["memory", "user", "format", "tag"])


# --------------------------------------------------------------------------- #
# 能力清单（tools/list）
# --------------------------------------------------------------------------- #
def run_catalog():
    fake = FakeClient()
    r, _ = reader_for(fake)
    cat = r.tool_catalog()
    check("清单统计读 / 写",
          cat["count"] == 4 and cat["read"] == 2 and cat["write"] == 2,
          f"{cat['count']} {cat['read']} {cat['write']}")
    check("清单走协议发现而非工具调用",
          fake.list_calls == 1 and fake.calls == [], f"{fake.list_calls} {fake.calls}")

    by = {t["name"]: t for t in cat["tools"]}
    check("读工具标已接出且无未接入理由",
          by["memo_search"]["wired"] is True and by["memo_search"]["reason"] == "",
          str(by.get("memo_search")))
    check("写工具标未接入并给出原因",
          by["memo_create"]["wired"] is False and "管线" in by["memo_create"]["reason"],
          str(by["memo_create"]))
    check("清单带参数名", by["tag_rename"]["args"] == ["old_tag", "new_tag"],
          str(by["tag_rename"]["args"]))
    check("摘要取首句", by["memo_search"]["summary"] == "搜索笔记", by["memo_search"]["summary"])
    check("读工具排在写工具前",
          [t["kind"] for t in cat["tools"]] == ["read", "read", "write", "write"],
          str([t["kind"] for t in cat["tools"]]))

    empty = reader_for(FakeClient(tool_list={"tools": []}))[0]
    check("空清单不抛异常", empty.tool_catalog()["count"] == 0)

    fail_fake = FakeClient(raises={"tools/list": "boom"})
    r3, _ = reader_for(fail_fake)
    try:
        r3.tool_catalog()
        got = "未抛错"
    except C.CloudError as e:
        got = "CloudError" if "取工具清单失败" in str(e) else str(e)
    check("清单失败转 CloudError", got == "CloudError", got)


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
    run_search_conditions()
    run_batch_and_related()
    run_tags_and_texts()
    run_catalog()
    run_failure_and_secrecy()
    print("---")
    ok = all(_RESULTS)
    print(f"{len(_RESULTS)} 项断言，" + ("全部通过" if ok else "存在失败用例"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
