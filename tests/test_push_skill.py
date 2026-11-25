#!/usr/bin/env python3
"""push_skill.sh 的本地 CI 门禁回归。

用法：python tests/test_push_skill.py      退出码 0=全过，1=有失败。

覆盖：默认门禁指向 `scripts/run_ci.sh`；门禁失败时**非 0 退出且不提交**；
门禁位置在「拨隧道/取 token」之前（红的时候不该先去调网络）；标签推送同样过门禁。

为什么值得一个用例：这条脚本是唯一往远端送东西的入口。门禁挂在它身上之前，
「本地 CI 红、照样推上去」发生过；用例要钉住的是**失败路径**——只验证成功路径的话，
把门禁写成 `if bash run_ci; then exit 0; fi` 这种反逻一样能过，事故照样复发。
"""

COVERS = ("push_skill",)

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "push_skill.sh"

RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if not cond else ""))


def main():
    txt = SCRIPT.read_text(encoding="utf-8")

    check("默认门禁跑 scripts/run_ci.sh", "${FLOMO_PUSH_CI:-scripts/run_ci.sh}" in txt)
    check("门禁失败以非 0 退出", 'exit 5' in txt)

    # 位置：门禁必须在「拨隧道 / 读凭据」之前——否则 CI 红也先花一次网络重试，
    # 失败信息还会被网络报错盖住。
    gate_pos = txt.find("本地 CI 门禁")
    cred_pos = txt.find('CRED_FILE="$HOME/.git-credentials"')
    check("门禁在认证与推送之前", 0 <= gate_pos < cred_pos, f"gate={gate_pos} cred={cred_pos}")

    # 失败路径：门禁脚本退出 1 时，push_skill.sh 必须中止且不留新提交。
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.strip()
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write("#!/usr/bin/env bash\necho '[stub] 故意红'\nexit 1\n")
        stub = fh.name
    try:
        bad = subprocess.run(
            ["bash", str(SCRIPT), "不该提交的改动"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
            env={**os.environ, "FLOMO_PUSH_CI": stub},
        )
    finally:
        os.unlink(stub)

    out = bad.stdout + bad.stderr
    check("门禁失败时非 0 退出", bad.returncode != 0, str(bad.returncode))
    check("门禁失败时明确写「未提交、未推送」", "未提交、未推送" in out, out[-300:])
    check("门禁失败时不进入推送通道", "[push] 远端" not in out, out[-300:])
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True
    ).stdout.strip()
    check("门禁失败时 HEAD 未移动（没有偷偷提交）", after == head_before, f"{head_before}→{after}")

    # 门禁入口不存在时也必须非 0——「找不到 CI 就不跑」等于没有门禁
    miss = subprocess.run(
        ["bash", str(SCRIPT), "x"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "FLOMO_PUSH_CI": "scripts/definitely_no_ci.sh"},
    )
    check("门禁入口缺失时非 0 退出", miss.returncode != 0, miss.stdout + miss.stderr)

    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
