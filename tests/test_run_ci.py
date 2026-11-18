#!/usr/bin/env python3
"""run_ci.sh 回归测试。

用法：python tests/test_run_ci.py      退出码 0=全过，1=有失败。

覆盖：本地 CI 入口与 `.github/workflows/ci.yml` 是否跑同一批命令、四个阶段是否齐全、
缺工具时是否**非 0 退出**（静默跳过正是「CI 连红、本地全绿」那次事故的形态）、
工具齐备时 `--check` 是否退出 0。

为什么值得一个用例：这条脚本的全部价值是「本地跑的东西和 CI 跑的是同一批」。
两边任何一侧改了命令而另一侧没跟，脚本会安静地少跑一道检查——它自己仍然退出 0，
看起来比不跑更让人放心。命令字符串因此按正面清单逐条比对，不靠人记。
"""

COVERS = ("run_ci",)

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_ci.sh"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"

# ci.yml 各步骤与 run_ci.sh 必须共有的命令（正面清单，缺一条即两侧漂移）
COMMON = (
    "ruff check scripts/ tests/",
    "bash scripts/run_tests.sh",
    "mypy scripts/",
    "coverage run -m pytest",
)

RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if not cond else ""))


def _run(args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=str(ROOT), capture_output=True, text=True, timeout=300
    )


def main():
    script_txt = SCRIPT.read_text(encoding="utf-8")
    ci_txt = CI_YML.read_text(encoding="utf-8")

    for cmd in COMMON:
        check(f"两侧共有命令：{cmd}", cmd in script_txt and cmd in ci_txt)

    # 阶段顺序：按脚本自己打印的阶段序号比对，不用裸命令名——命令名在注释与
    # 说明文字里先出现过一次（「只跑 run_tests.sh」），按首次出现判顺序会判错。
    marks = ("① ruff", "② 离线回归", "③ mypy", "④ pytest")
    pos = [script_txt.find(m) for m in marks]
    check("四阶段标记齐全且顺序与 ci.yml 一致", all(p >= 0 for p in pos) and pos == sorted(pos), str(pos))

    # 缺工具必须非 0 退出：用 CI_TOOL_MODULES 指一个不存在的模块（不改 PATH——
    # 改了 PATH 脚本里的 dirname 会先挂，测到的就不是这条分支了）
    miss = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
        env={**os.environ, "CI_TOOL_MODULES": "definitely_missing_module"},
    )
    check("缺工具时非 0 退出（不静默跳过）", miss.returncode != 0, miss.stdout + miss.stderr)
    check("缺工具时给出安装入口", "pip install" in (miss.stdout + miss.stderr))

    ok = _run(["--check"])
    check("工具齐备时 --check 退出 0", ok.returncode == 0, ok.stdout + ok.stderr)
    check("--check 不跑用例（只查工具）", "离线回归" not in ok.stdout)

    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
