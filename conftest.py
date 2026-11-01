"""conftest.py — 让 pytest 能收 tests/ 下的自研 runner 脚本。

为什么放在仓库根而不是 tests/ 下：check_skill_docs.py 有一条硬约束——`tests/` 下的
.py 必须以 `test_` 开头（否则 run_tests.sh 的 glob 收不到它，用例静默不跑）。
conftest.py 是 pytest 的配置文件名，天生不带 test_ 前缀，放 tests/ 会触发那条
ERR。放仓库根是 pytest 的标准位置，也绕开了命名冲突。

收集方式：每个 `tests/test_*.py` 由 `runpy` 按「python tests/test_x.py」的**原语义**
整体执行——文件里 `if __name__ == "__main__"` 那段照跑，退出码非 0 即用例失败。
不把测试改写成 pytest 函数，是因为判据必须只有一份：run_tests.sh 跑的是同一批
文件，pytest 只是第二个入口（给 IDE、给 `pytest -x`、给 coverage 用），不是第二套
判据。真按 pytest 函数重写十三套用例，两套入口迟早对不上，而对不上时没人知道
该信哪边。

`python_files = []`（pyproject 里）关掉 pytest 默认的模块收集：本仓库的用例文件
有的把测试逻辑写在模块级（test_exit_confirm.py 整文件 import 即跑、末尾直接
sys.exit），默认收集会 import 它们，SystemExit 在收集阶段就炸成 INTERNALERROR。
关掉之后，收集只走本文件的 `pytest_collect_file` 一条路。
"""

import runpy
import types
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent / "tests"


class SafeModule(pytest.Module):
    """占位 Module：只为让 pytest 有节点可挂，**不 import 用例文件**。

    为什么需要它：`pytest_collect_file` 在 pytest 8+ 不是 firstresult——所有实现的
    返回值都会进收集树。内置 python 插件对命令行直接指定的文件（initpath）会跳过
    `python_files` 匹配、无条件建一个真 Module 并 import；而本仓库有的用例文件把
    逻辑写在模块级（import 即跑完、末尾 sys.exit），那一 import 就在收集阶段炸成
    INTERNALERROR。`pytest_pycollect_makemodule` 是 firstresult，用 tryfirst 抢在
    内置实现前面返回这个占位模块，真 import 就不会发生。它按 0 个用例函数收集，
    真正的执行在 ScriptItem（runpy，按脚本语义跑）。
    """

    def _getobj(self):
        return types.SimpleNamespace()


class ScriptFile(pytest.File):
    """一个测试文件 = 一个用例项（整文件按脚本语义跑）。"""

    def collect(self):
        yield ScriptItem.from_parent(self, name=self.path.name)


class ScriptItem(pytest.Item):
    def runtest(self):
        # run_name="__main__" 让文件里的 `if __name__ == "__main__"` 照常执行；
        # SystemExit 是脚本的正常出口，码非 0 才算失败。
        try:
            runpy.run_path(str(self.path), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise AssertionError(f"退出码 {exc.code}（0 才算过）") from None

    def repr_failure(self, excinfo):  # 失败时把文件自己的输出摆出来，不套 pytest 的话术
        if isinstance(excinfo.value, AssertionError):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, None, ""


@pytest.hookimpl(tryfirst=True)
def pytest_collect_file(file_path, parent):
    """只接 tests/ 下的 test_*.py；其余文件交给 pytest 默认逻辑。

    `tryfirst=True` 不能省：pytest 内置的 python 插件对**命令行直接指定的文件**
    （initpath）会跳过 python_files 匹配、无条件建成 Module 并 import——而本仓库
    有的用例文件把逻辑写在模块级（import 即跑、末尾 sys.exit），那一 import 就在
    收集阶段炸成 INTERNALERROR。不抢在内置钩子之前，`pytest tests/test_x.py`
     （单文件调试最自然的打法）就正好落在唯一会炸的那条路上。
    """
    if (file_path.suffix == ".py" and file_path.name.startswith("test_")
            and file_path.parent == TESTS_DIR):
        return ScriptFile.from_parent(parent, path=file_path)
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path: Path, parent):
    """tests/ 下的用例文件用占位模块顶掉内置的真 import（见 SafeModule 文档）。"""
    if (module_path.suffix == ".py" and module_path.name.startswith("test_")
            and module_path.parent == TESTS_DIR):
        return SafeModule.from_parent(parent, path=module_path)
    return None
