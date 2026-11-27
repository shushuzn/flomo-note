#!/usr/bin/env python3
"""cli_util.py — 命令行契约：选项声明与解析、参数类型转换、统一出口。

为什么单独一层：这些代码原本长在 `flomo_client.py` 里，而那个模块是**云端通道**
（握手、token、MCP 协议、写后回读）。于是「只想解析一下命令行」的脚本
（如 `fetch_article.py`，纯网页抓取，与 flomo 无关）被迫依赖整个云端客户端——
云端的任何契约变化都要连带想起它，它自己的 import 图里也平白多出 token 与网络语义。

本模块**不 import 任何项目内模块、不碰网络**，是依赖图里的叶子。判断标准很简单：
只靠标准库就能回答的问题（这个参数是什么类型、缺值/重复/未知怎么报、入口怎么给
退出码），代码就属于这里。

一处定义：`Opt` / `parse_options` 由各脚本自写时，会退化成四份 while 循环，各带两个
一样的漏（末尾选项被报成「未知参数」、同一选项给两次静默取后者）；`UsageError` 的
分流也会散落进每个 `main`。故收成一份，各脚本只写自己的 `Opt(...)` 声明表。
"""
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

# 本文件所在目录进 sys.path：`_ensure_fresh` 要惰性导入 `skill_sync`（同目录兄弟）。
# 各脚本自己也会插一遍，这里再插是为了让「直接 import cli_util」的调用方同样能用。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

_NumT = TypeVar("_NumT", int, float)


def force_utf8() -> None:
    """把标准输出/错误切到 utf-8（已切过的不重复切）。

    用例名与断言详情里有 GBK 编不出来的字符（如 "♯" U+266F、从服务回包解码出来的
    替换符），打到 GBK 控制台会 UnicodeEncodeError——于是「用例跑挂了」其实只是
    「打印挂了」。七个脚本各写一遍 `if hasattr(...)` 样板，收成一处；判据依旧只有
    一份：先探 `reconfigure` 在不在（Python 3.7 以下没有），再切。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


class UsageError(Exception):
    """命令行参数本身写错了（如 `--limit abc`）：不是云端或凭据问题。

    单列一类，是因为两者的正确出口不同——参数错该指回那个参数、按各脚本自己的
    「用法错」退出码结束；混进云端错误会被读成「调用失败」，用户去查网络和 token，
    实际只是少打了个数字。

    **故它不继承云端错误类型**：继承会让 `except FlomoError` 顺手把「打错参数」读成
    云端失败，与上一条理由正好相反。用继承换「`except FlomoError` 不漏接」，代价是
    分类失效；不漏接改由入口的 `errors=` 显式声明承担——这样还有一个好处：漏写会在
    调用处立刻 TypeError。
    """


def _num_arg(raw: str | None, flag: str, cast: Callable[[str], _NumT],
             low: _NumT | None = None, high: _NumT | None = None) -> _NumT:
    """把命令行里的数字参数转成 `cast`，并在越界/非数字时抛 `UsageError`。

    不做「宽松解析」——`int("2000条")` 会抛、`int("2_000")` 会过，都是意外的。
    此前三个脚本直接写 `int(args[i+1])`，用户打错一个字得到的是 traceback。
    """
    text = (raw or "").strip()
    try:
        value = cast(text)
    except (TypeError, ValueError):
        kind = "整数" if cast is int else "数字"
        raise UsageError(f"{flag} 需要{kind}，收到 {raw!r}") from None
    if low is not None and value < low:
        raise UsageError(f"{flag} 不能小于 {low}，收到 {value}")
    if high is not None and value > high:
        raise UsageError(f"{flag} 不能大于 {high}，收到 {value}")
    return value


def int_arg(raw: str | None, flag: str, *, low: int | None = None,
            high: int | None = None) -> int:
    """`--limit 2000` 这类整数参数：见 `_num_arg`。"""
    return _num_arg(raw, flag, int, low, high)


def float_arg(raw: str | None, flag: str, *, low: float | None = None,
              high: float | None = None) -> float:
    """`--timeout 30` 这类浮点参数：见 `_num_arg`。"""
    return _num_arg(raw, flag, float, low, high)


class Opt:
    """一条命令行选项的声明：选项名、落到哪个字段、类型、取值范围、缺省值。

    声明式而不是各脚本自己写 `if a == "--x" and i + 1 < len(args)`，是为了让
    「缺值」「重复」「未知」这三种错误只在一处判定（见 `parse_options`）。
    """

    __slots__ = ("default", "dest", "high", "hint", "kind", "low", "name")

    default: Any
    dest: str
    high: float | None
    hint: str
    kind: str
    low: float | None
    name: str

    def __init__(self, name: str, dest: str, kind: str = "str", *,
                 low: float | None = None, high: float | None = None,
                 default: Any = None, hint: str = "") -> None:
        if kind not in ("str", "int", "float", "flag"):
            raise ValueError(f"未知的选项类型：{kind}")
        if kind == "flag" and (low is not None or high is not None):
            raise ValueError(f"{name} 是开关，不该有取值范围")
        self.name = name
        self.dest = dest
        self.kind = kind
        self.low = low
        self.high = high
        self.default = False if kind == "flag" else default
        self.hint = hint


def parse_options(argv: Sequence[str], spec: Sequence[Opt], *,
                  positional_ok: bool = False) -> tuple[dict[str, Any], list[str]]:
    """按声明表解析命令行选项，返回 `(字段字典, 位置参数列表)`；用法错抛 `UsageError`。

    收在一处，是因为此前四个脚本各写一遍 while 循环，带着两个一模一样的漏：

    ① **选项落在末尾时被报成「未知参数」**。写成
       `if a == "--draft" and i + 1 < len(args)`，当 `--draft` 是最后一个词时该条件为假，
       直接掉进 else 分支——用户看到「未知参数：--draft」，会以为选项名写错、去翻文档，
       而事实只是漏了值。两种错误的修法完全不同，不能共用一句提示。
    ② **同一选项给两次时后者静默覆盖前者**。`--ids A --ids B` 只读了 B，用户以为两张
       都核过；而「以为核过、其实漏了」正是查重与标签核对要防的事。

    `positional_ok=False` 时出现裸词也按用法错报出（多半是把值写到了选项名前面）。
    """
    by_name = {o.name: o for o in spec}
    values = {o.dest: o.default for o in spec}
    positionals: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(argv):
        token = argv[i]
        opt = by_name.get(token)
        if opt is None:
            if token.startswith("-") and token != "-":
                raise UsageError(f"未知参数：{token}")
            if not positional_ok:
                raise UsageError(f"多余的参数：{token}（选项名要以 -- 开头，值紧跟在选项后面）")
            positionals.append(token)
            i += 1
            continue
        if token in seen:
            # 「怎么办」由选项自己声明：`--tags` 可以合并成一次给全，`--draft` 不行，
            # 通用地建议「用逗号合并」会把路径选项说成能塞多个草稿。
            raise UsageError(f"{token} 只能给一次{opt.hint}（重复时后者会静默覆盖前者）")
        seen.add(token)
        if opt.kind == "flag":
            values[opt.dest] = True
            i += 1
            continue
        # 缺值的判据是「后面没有词」或「后面那个词本身是个选项名」：`--limit -1`
        # 要把 `-1` 当值交给 int_arg 报「不能小于 1」，不能在这里报「缺少参数值」。
        if i + 1 >= len(argv) or argv[i + 1] in by_name:
            raise UsageError(f"{token} 缺少参数值")
        raw = argv[i + 1]
        if opt.kind == "int":
            values[opt.dest] = _num_arg(raw, token, int, opt.low, opt.high)
        elif opt.kind == "float":
            values[opt.dest] = _num_arg(raw, token, float, opt.low, opt.high)
        else:
            values[opt.dest] = raw
        i += 2
    return values, positionals


def _ensure_fresh() -> None:
    """入口自愈：跑任何脚本前，先确认本地代码是当前版本（`skill_sync`）。

    为什么放在入口而不是各脚本里：技能目录装在容器的可写层，容器重启就退回镜像里
    那份旧提交（本地 `pull` 的成果随之丢失），而旧脚本**不会报错**——只是按旧细则
    跑。二十几个脚本各写一遍必然漏，入口是唯一都会被经过的地方。

    三条自我约束，缺一条就该被这条规则挡下：
      · **惰性导入**：`skill_sync` 只在真的要用时才 import，`cli_util` 的模块级依赖
        仍然只有标准库（分层用例核这一条）；
      · **失败即放过**：同步是辅助动作，取不到远端、目录不是仓库、git 不存在，都
        只返回、不抛——只读命令该跑完；写云的拦截由 `cli_entry(write=True)` 另做，
        因为「旧版跑只读」与「旧版写云」的代价不是一回事；
      · **不在用例里跑**：`test_*.py` 与 pytest 进程一律不碰网络（离线用例的前提）。
    """
    if os.environ.get("FLOMO_SKIP_SYNC") == "1" or os.environ.get("CI"):
        return
    argv0 = os.path.basename(sys.argv[0] or "")
    if argv0.startswith("test_") or "pytest" in sys.modules:
        return
    try:
        from skill_sync import ensure_current
    except Exception:  # noqa: BLE001 —— 自愈是辅助动作，导入失败只能放过，不能拦住本轮
        return
    try:
        ensure_current()
    except Exception:  # noqa: BLE001 —— 同上：取不到远端或 git 缺失都只放过
        return


def cli_entry(main: Callable[[], Any], *,
              errors: tuple[type[BaseException], ...], code: int = 1,
              prefix: str = "[失败]", usage_code: int = 2,
              sync: bool = True, write: bool = False, load: bool = False) -> Any:
    """CLI 入口的统一出口：跑 `main`，把**预期内**的失败转成退出码。

    所有命令行脚本的 `__main__` 都走这里。凭据缺失、握手失败、协议错误都是可预期
    的失败，该以「一行说明 + 非 0 退出码」结束；traceback 只该留给真缺陷。此前各脚本
    各写各的（有的转了、有的没转），于是同一个「没配 token」在不同脚本里分别表现为
    一行提示和一段 traceback——用户看不出这是同一件事。

    `errors` **必填、无默认值**：它声明「本脚本还会抛出哪些非用法类失败」——云端脚本
    传 `(FlomoError,)`，纯本地脚本传 `()`。不给默认值、也不让本模块 import 云端模块，
    是因为默认值会让这层重新耦合回云端，而耦合一旦免费就没人再想它该不该在；必填的
    另一个好处是漏写会在调用处当场 TypeError，不会退化成运行时的 traceback——那正是
    本函数要消灭的东西。

    `sync=True` 时先做一次版本自愈（见 `_ensure_fresh`）；用例与 CI 里自动跳过。

    `write=True` 是给**会落云端账**的命令用的（`publish` / `exit_confirm` / `retire`）：
    版本没确认（取不到远端、快进失败、本地脏、本地领先）就拒绝执行并退出 8，不再往下跑。
    只读命令不传它——旧版跑只读还能靠人复核，旧版写云只在事后人工回滚。

    `load=True` 是给**流程必经点**用的（`fetch_article` / `publish` / `exit_confirm`）：
    没有技能加载凭证、凭证过期、或 `SKILL.md` 改过之后没重读，就退出 9。加载口径见
    `skill_load.py`——它要的是引用一行随机原文，不是打勾，理由是「读没读」只能靠原文举证。
    只读的辅助脚本不传它：拦太宽会把人逼到「为过检而编凭证」，比不拦更坏。
    """
    # 查手册不算开工：`--help` 放行，否则连「这条命令怎么用」都得先取证。
    if load and not any(a in ("-h", "--help", "help") for a in sys.argv[1:]):
        try:
            from skill_load import guard
        except Exception:  # noqa: BLE001 —— 取不到实现时不额外拦，主流程照旧
            guard = None  # type: ignore[assignment]
        if guard is not None:
            blocked = guard()
            if blocked:
                return blocked
    if sync:
        _ensure_fresh()
    if write:
        try:
            from skill_sync import write_guard
        except Exception:  # noqa: BLE001 —— 取不到实现时不额外拦，主流程照旧
            write_guard = None  # type: ignore[assignment]
        if write_guard is not None:
            blocked = write_guard()
            if blocked:
                return blocked
    try:
        return main()
    except UsageError as e:
        # 用法错一律以「[失败] + 参数名」开头。`prefix` 是用来补调用上下文的
        # （如「查重未能进行：」），而用法错根本没进到那一步，套上去会读成云端失败。
        print(f"[失败] {e}", file=sys.stderr)
        return usage_code
    except errors as e:
        # 中文冒号后面不再补空格（`[失败] 查重未能进行：` 直接接原因）
        sep = "" if prefix.endswith(("：", ":")) else " "
        print(f"{prefix}{sep}{e}", file=sys.stderr)
        return code
