#!/usr/bin/env python3
"""console_data.py 回归用例（离线，临时目录内进行，不联网）。

覆盖控制台数据抽取的解析口径：
  - 硬限：条目 id 后带括注（如 `Hn（说明）· 标题`）须识别；分隔线与指向治理条目的
    横向引用注记不得并入上一条正文；分组名归并与治理标记；
  - 管线：`### N. 标题` 小节抽取，标题或正文任一处出现「阻塞」即标记；
  - 标签树：计数**委托 memo_util**（不得本模块另算），并覆盖缺文件与不一致两种情形；
  - 脚本：工具 / 用例分类、配套用例存在性、一句话说明的抽取；
  - 概览：文档规模与统计字段。

用法：python scripts/test_console_data.py     退出码 0=全过，1=有失败。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def load_module():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("console_data", here / "console_data.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["console_data"] = mod
    spec.loader.exec_module(mod)
    return mod


C = load_module()

SKILL_FIXTURE = """# 标题

## 二、硬限清单

### A. 分组甲

**H1 · 甲条。**

甲条正文第一行。

甲条正文第二行。

> **H9 / H10 属治理，见附录。**

---

**H2（括注说明）· 乙条。**

乙条正文。

### B. 分组乙

**H3 · 丙条。**

丙条正文。

## 三、流程

按序执行，标「阻塞」的步骤未完成不得进入下一步。

### 1. 第一步

- 第一步说明，含阻塞字样。

### 2. 第二步

- 第二步说明。

### 9. 最后一步

- 说明。

## 四、收尾输出

正文。

### 技能文档治理（与写卡执行无关）

**H9 · 治理条。**

治理正文。
"""

TAG_FIXTURE = """# total=4
# 甲
  甲/一
  甲/二
# 乙
乙/
  乙/三
"""


def make_repo(tmp: Path, with_tag=True, skill=SKILL_FIXTURE, tag=TAG_FIXTURE):
    (tmp / "SKILL.md").write_text(skill, encoding="utf-8")
    (tmp / "AGENTS.md").write_text("# 总则\n\n一句话。\n", encoding="utf-8")
    sdir = tmp / "scripts"
    sdir.mkdir(exist_ok=True)
    (sdir / "tool_one.py").write_text(
        '#!/usr/bin/env python3\n"""tool_one — 演示工具。\n\n更多说明。\n"""\n', encoding="utf-8"
    )
    (sdir / "tool_two.sh").write_text(
        "#!/usr/bin/env bash\n# tool_two — 演示脚本。\n#\n# 用法：bash tool_two.sh\nset -e\n", encoding="utf-8"
    )
    (sdir / "test_tool_one.py").write_text('"""用例。"""\n', encoding="utf-8")
    if with_tag:
        (tmp / "tag_tree.txt").write_text(tag, encoding="utf-8")
    return tmp


def run_limits(tmp: Path):
    limits = C.load_limits(tmp)
    ids = [l["id"] for l in limits]
    check("硬限 id 全数抽取", ids == ["H1", "H2", "H3", "H9"], f"实际 {ids}")
    h2 = next(l for l in limits if l["id"] == "H2")
    check("带括注的条目名正确剥离括注", h2["title"] == "乙条", h2["title"])
    h1 = next(l for l in limits if l["id"] == "H1")
    check("横向引用注记不并入上一条正文", "属治理" not in h1["body"], h1["body"])
    check("分隔线终止正文（不吞并下一条）", "H2" not in h1["body"] and "乙条正文" not in h1["body"], h1["body"])
    check("正文保留多行", "甲条正文第二行" in h1["body"])
    check("分组标记正确", h1["group"] == "A" and h1["group_name"] == "A. 分组甲",
          (h1["group"], h1["group_name"]))
    check("分组标题保留字母前缀供展示", h2["group_name"] == "A. 分组甲")
    h9 = next(l for l in limits if l["id"] == "H9")
    check("治理节归为治理", h9["governance"] is True and h9["group"] == "GOV")


def run_pipeline(tmp: Path):
    steps = C.load_pipeline(tmp)
    check("管线步骤数与编号", [s["n"] for s in steps] == [1, 2, 9], [s["n"] for s in steps])
    check("正文含阻塞亦标记", steps[0]["blocking"] is True)
    check("无阻塞不误标", steps[1]["blocking"] is False)
    check("末步抽取完整", steps[2]["title"] == "最后一步")
    check("节内引言不入步", all("按序执行" not in s["body"] for s in steps))


def run_tagtree(tmp: Path):
    t = C.load_tagtree(tmp)
    check("标签树 total 抽取", t["total"] == 4, t["total"])
    check("计数委托 memo_util（二级 3 + 裸顶层 1）", (t["leaves"], t["bare_count"]) == (3, 1),
          (t["leaves"], t["bare_count"]))
    check("列出数自洽", t["listed"] == 4 and t["consistent"] is True)
    check("裸顶层识别", t["bare"] == ["乙"], t["bare"])
    check("分组结构供展示", len(t["groups"]) == 2 and t["groups"][0]["children"] == ["甲/一", "甲/二"])
    check("裸顶层分组带标记", t["groups"][1]["bare"] is True)

    # 计数口径必须与 memo_util 一致（同源，不得本模块另算）
    from memo_util import count_snapshot, snapshot_total
    lines = (tmp / "tag_tree.txt").read_text(encoding="utf-8").splitlines()
    check("与 memo_util 口径同源", (t["total"], t["leaves"], t["bare_count"])
          == (snapshot_total(lines), *count_snapshot(lines)))

    # 不一致时如实上报
    (tmp / "tag_tree.txt").write_text("# total=99\n# 甲\n  甲/一\n", encoding="utf-8")
    t2 = C.load_tagtree(tmp)
    check("不一致时 consistent=False", t2["consistent"] is False and t2["listed"] == 1)

    # 缺文件
    empty = Path(tempfile.mkdtemp())
    t3 = C.load_tagtree(empty)
    check("缺快照时 present=False", t3["present"] is False and t3["total"] is None)


def run_scripts(tmp: Path):
    scripts = C.load_scripts(tmp)
    by_name = {s["name"]: s for s in scripts}
    check("工具/用例分类", by_name["tool_one.py"]["kind"] == "tool"
          and by_name["test_tool_one.py"]["kind"] == "test")
    check("配套用例存在性判定", by_name["tool_one.py"]["has_test"] is True
          and by_name["tool_two.sh"]["has_test"] is False)
    check("py 说明取 docstring 首行", "演示工具" in by_name["tool_one.py"]["summary"])
    check("sh 说明取首个注释行", "演示脚本" in by_name["tool_two.sh"]["summary"])
    check("非脚本文件不入列", all(s["name"].endswith((".py", ".sh")) for s in scripts))


def run_overview(tmp: Path):
    data = C.collect_all(tmp)
    st = data["stats"]
    check("统计字段齐全", set(st) >= {
        "tags", "tag_groups", "tag_leaves", "tags_consistent", "steps",
        "steps_blocking", "limits", "limits_core", "tools", "tests"})
    check("统计值与解析一致", st["steps"] == 3 and st["limits"] == 4
          and st["steps_blocking"] == 1 and st["tools"] == 2 and st["tests"] == 1)
    check("文档规模收集", {d["name"] for d in data["docs"]} == {"SKILL.md", "AGENTS.md"})

    # 缺 SKILL.md 时不得抛异常
    blank = Path(tempfile.mkdtemp())
    d2 = C.collect_all(blank)
    check("缺 SKILL.md 不抛异常", d2["stats"]["steps"] == 0 and d2["stats"]["limits"] == 0)


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        make_repo(tmp)
        run_limits(tmp)
        run_pipeline(tmp)
        run_scripts(tmp)
        run_overview(tmp)
        run_tagtree(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("---")
    ok = all(_RESULTS)
    print("全部通过" if ok else "存在失败用例")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
