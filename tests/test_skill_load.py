#!/usr/bin/env python3
"""skill_load.py 回归用例（离线，临时目录内造假技能目录）。

为什么单独钉：这道闸门管「这一轮到底读没读细则」。判据松了等于放行凭记忆跑的轮次，
判据紧了会把正常轮次挡在抓取之前——两个方向都直接毁掉流程。故这里把四种状态分别
钉住：引对原文才发凭证；引错、引太短都不发；凭证过期与文档改动都判失效；无凭证时
入口闸门退出 9。另有一条查真文档能否挑出可引用的行——挑不出等于闸门永远过不去。

用法：python tests/test_skill_load.py   退出码 0=全过，1=有失败。
"""

COVERS = ("skill_load",)

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location("skill_load", SCRIPT_DIR / "skill_load.py")
SL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SL)

ROOT = Path(__file__).resolve().parent.parent
RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def _fixture(tmp: Path, lines: int = 60) -> Path:
    """造一份假技能目录：SKILL.md 够长，`.workbuddy` 不存在（等脚本自己建）。"""
    root = tmp / "skill"
    root.mkdir(parents=True, exist_ok=True)
    body = ["---", "name: fake", "---", ""]
    for i in range(4, lines + 1):
        body.append(f"第 {i} 行：这是一条足够长的细则原文，用来做引用核对。")
    (root / "SKILL.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    return root


def _challenge_quote(root: Path, span: slice = slice(0, 12)) -> str:
    """按挑战行号取该行开头，模拟「真去读了那一行」。"""
    challenge = json.loads((root / ".workbuddy" / SL.CHALLENGE_NAME).read_text(encoding="utf-8"))
    line = (root / "SKILL.md").read_text(encoding="utf-8").splitlines()[challenge["line"] - 1]
    return line.strip()[span]


def run_cases():
    tmp = Path(tempfile.mkdtemp(prefix="skill-load-"))
    old_env = {k: os.environ.get(k) for k in (SL.ROOT_ENV, SL.TTL_ENV)}
    try:
        # ① 引错原文：不发凭证
        root = _fixture(tmp)
        os.environ[SL.ROOT_ENV] = str(root)
        check("发挑战成功", SL.issue(root) == 0)
        check("挑战文件已落盘", (root / ".workbuddy" / SL.CHALLENGE_NAME).exists())
        check("引错原文被拒（退出 1）", SL.confirm(root, "完全不相干的字串内容") == 1)
        check("引错后无凭证", not (root / ".workbuddy" / SL.TOKEN_NAME).exists())

        # ② 引用太短：同样拒（太短靠猜就能蒙过去）
        check("引太短被拒", SL.confirm(root, "第 3") == 1)

        # ③ 引对原文：写凭证、闸门放行、挑战作废
        quote = _challenge_quote(root)
        check("引对原文发凭证", SL.confirm(root, quote) == 0)
        check("凭证已落盘", (root / ".workbuddy" / SL.TOKEN_NAME).exists())
        check("闸门放行", SL.guard(root) == 0)
        check("挑战用完即删", not (root / ".workbuddy" / SL.CHALLENGE_NAME).exists())

        # ③b 行中片段也算读过（不要求正好是开头，行内任何连续片段都可举证）
        one = _fixture(tmp / "e")
        os.environ[SL.ROOT_ENV] = str(one)
        SL.issue(one)
        mid = _challenge_quote(one, slice(6, 20))
        check("行中片段可作引用", SL.confirm(one, mid) == 0, mid)

        # ④ 无凭证：闸门退出 9
        clean = _fixture(tmp / "b")
        os.environ[SL.ROOT_ENV] = str(clean)
        check("无凭证 → 退出码 9", SL.guard(clean) == int(SL.LOAD_BLOCKED))
        check("LOAD_BLOCKED 是 9", SL.LOAD_BLOCKED == "9")

        # ⑤ 文档改动：凭证失效（改完文档正是最该重读的时候）
        with (root / "SKILL.md").open("a", encoding="utf-8") as fh:
            fh.write("补一行，文档从此不是取证时那一版。\n")
        check("追加一行后判失效", SL.guard(root) == int(SL.LOAD_BLOCKED))
        check("体检同时报失效", SL.status(root) == 1)

        # ⑥ 过期：时间戳往前拨，超 TTL 即拦
        os.environ[SL.ROOT_ENV] = str(_fixture(tmp / "c"))
        root3 = Path(os.environ[SL.ROOT_ENV])
        SL.issue(root3)
        SL.confirm(root3, _challenge_quote(root3))
        os.environ[SL.TTL_ENV] = "1"
        token = json.loads((root3 / ".workbuddy" / SL.TOKEN_NAME).read_text(encoding="utf-8"))
        token["ts"] = int(time.time()) - 3600
        (root3 / ".workbuddy" / SL.TOKEN_NAME).write_text(json.dumps(token), encoding="utf-8")
        check("过期凭证被拦", SL.guard(root3) == int(SL.LOAD_BLOCKED))

        # ⑦ 体检只读：没有凭证时不建任何文件
        bare = _fixture(tmp / "d")
        before = sorted(p.name for p in (bare / ".workbuddy").glob("*")) if (bare / ".workbuddy").exists() else []
        check("体检无凭证退出 1", SL.status(bare) == 1)
        after = sorted(p.name for p in (bare / ".workbuddy").glob("*")) if (bare / ".workbuddy").exists() else []
        check("体检不改状态", before == after, after)

        # ⑧ 真文档：挑得出可引用的行，否则闸门永远过不去
        lines, sha = SL._read_doc(ROOT)
        check("真 SKILL.md 有 sha256", len(sha) == 64)
        check("真 SKILL.md 能挑出引用行", SL._pick_line(lines) > SL.MIN_LINE_INDEX)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    run_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
