#!/usr/bin/env python3
"""retire.py 回归用例（离线，纯函数）。

退场骨架是「用户按标签批量删卡」的唯一入口，它的形态判据错一次，就会把正文
写成骨架（内容被永久压掉）或把骨架写成正文（被当成真卡留在库里）。故这里钉住：

  - 骨架固定三行：标签 + 概念名 + 「原标签/原字数」，且只有首行允许出现 `#`；
  - `parse_card` 从原文取概念名与标签：首行标签段可带多个标签，概念名取第一个
    非空且非标签的行（空行不占位）；
  - 概念名里的 `#` 被剥掉——否则骨架会被渲染成第二个标签；
  - 平台把标签行回成 `#政策/产业<br>`：`<br>` 是渲染记号，不许跟着进骨架；
  - `--dry-run` 只看计划：不写云端、也不落备份（备份属写路径的保险）；
  - 单级标签 `#待清理` 与 flomo_client 的放行判据同源（`is_retire_skeleton`），
    这里断言两者对同一段文本给出一致的结论，防两处各改各的。

用法：python tests/test_retire.py   退出码 0=全过，1=有失败。
"""

COVERS = ("retire",)

import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, HERE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load("retire_mod", "retire.py")
FC = _load("fc_mod", "flomo_client.py")

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


class FakeClient:
    """只实现 retire_one 用到的两件事：批量取全文、覆盖写入（含写后回读）。"""

    def __init__(self, content):
        self.store = {"MjTEST": content}
        self.calls = []

    def tool(self, name, args):
        self.calls.append(name)
        if name == "memo_batch_get":
            mid = args["ids"][0]
            body = self.store.get(mid)
            memos = [{"id": mid, "content": body, "word_count": len(body or "")}] if body is not None else []
            return {"structuredContent": {"memos": memos}}
        if name == "memo_update":
            self.store[args["id"]] = args["content"]
            return {"structuredContent": {"id": args["id"]}}
        return {"structuredContent": {"ok": True}}


def dry_run_checks():
    """`--dry-run` 只看计划：不写云端，也不落备份。

    备份写在**写路径**上，理由是「卡面换成骨架后，正文只此一份」；预览阶段写它属于
    副作用——用户只想看一眼计划，正文却落到了本地磁盘。故这里把两条路径分别钉住。
    """
    body = "#政策/产业<br>\n概念甲\n\n正文若干，用于核对备份与骨架。\n"
    with tempfile.TemporaryDirectory() as td:
        original = R.BACKUP_DIR
        R.BACKUP_DIR = Path(td) / "retire_backup"
        try:
            fake = FakeClient(body)
            state, why = R.retire_one(fake, "MjTEST", dry_run=True)
            check("dry-run 给出计划", state == "ok" and "dry-run" in why, why)
            check("dry-run 不写云端", "memo_update" not in fake.calls, fake.calls)
            check("dry-run 不落备份", not R.BACKUP_DIR.exists(),
                  sorted(p.name for p in Path(td).rglob("*")))
            check("dry-run 不动卡面", fake.store["MjTEST"] == body)

            state2, why2 = R.retire_one(fake, "MjTEST")
            check("真退场成功", state2 == "ok", why2)
            check("真退场落备份",
                  (R.BACKUP_DIR / "MjTEST.txt").read_text(encoding="utf-8") == body)
            check("真退场换成骨架", fake.store["MjTEST"].startswith("#待清理"), fake.store["MjTEST"])
            check("真退场骨架无 <br>", "<br" not in fake.store["MjTEST"], fake.store["MjTEST"])

            state3, _ = R.retire_one(FakeClient(body), "MjNONE")
            check("取不到全文就跳过", state3 == "skip", state3)
        finally:
            R.BACKUP_DIR = original


def main():
    skeleton = R.build_skeleton("周永富案", ["时政/反腐", "政策/生态"], 500)
    check("骨架首行是待清理标签", skeleton.splitlines()[0] == "#待清理")
    check("骨架保留概念名", skeleton.splitlines()[1] == "周永富案")
    check("骨架记原标签与原字数", "原标签：时政/反腐、政策/生态" in skeleton and "原字数：500" in skeleton)
    check("骨架里除首行外不含 #", not any(l.startswith("#") for l in skeleton.splitlines()[1:]))

    dirty = R.build_skeleton("#伪标签 概念", [], 0)
    check("概念名里的 # 被剥掉", "#伪标签" not in dirty, dirty)
    check("无标签时写占位", "原标签：（无）" in dirty, dirty)

    name, tags = R.parse_card("#时政/反腐 #政策/生态\n周永富案\n\n正文第一段\n")
    check("parse_card 取概念名", name == "周永富案", name)
    check("parse_card 取标签（去掉 #）", tags == ["时政/反腐", "政策/生态"], tags)

    name2, _ = R.parse_card("#标签/二级\n\n#待清理\n真名\n")
    check("parse_card 跳过空行与标签行", name2 == "真名", name2)

    # 平台把读回的标签行渲染成 `#政策/产业<br>`（全库一致）。这是外层记号，
    # 剥不干净就会写进骨架末行，卡面上多出一条假标签文本。
    name3, tags3 = R.parse_card("#政策/产业<br>\n重要工业品通知\n\n正文\n")
    check("标签行尾的 <br> 被剥掉", tags3 == ["政策/产业"], tags3)
    check("概念名与 <br> 无关", name3 == "重要工业品通知", name3)
    br_skeleton = R.build_skeleton("重要工业品通知", ["政策/产业<br>"], 747)
    check("骨架里不出现 <br>", "<br" not in br_skeleton, br_skeleton)
    check("骨架仍记对原标签", "原标签：政策/产业；原字数：747" in br_skeleton, br_skeleton)
    check("剥净后仍认作退场骨架", FC.is_retire_skeleton(br_skeleton) is True)
    check("manifest 给的标签带 # 也剥掉",
          "原标签：政策/产业" in R.build_skeleton("甲", ["#政策/产业"], 10))

    check("空正文 parse_card 不炸", R.parse_card("") == ("", []))
    check("retire 与 flomo_client 判据同源", FC.is_retire_skeleton(skeleton) is True)
    check("正文撑大的不认作骨架", FC.is_retire_skeleton(skeleton + "\n- 正文夹带") is False)
    check("标签不对的不认作骨架", FC.is_retire_skeleton("#时政/反腐\n周永富案") is False)

    dry_run_checks()

    passed = sum(1 for r in RESULTS if r)
    print(f"[结果] {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
