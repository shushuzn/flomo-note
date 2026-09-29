#!/usr/bin/env python3
"""git_tunnel.py 回归用例（离线桩，不联网）。

只验证 connect_target 的选路与回落逻辑：
  - 命中候选且可用 → 直接用它，不再尝试其余候选；
  - 先失败的候选 → 按序推进到下一个；
  - 候选全失败 → 回落系统 DNS（以 host 名建连），不得直接放弃；
  - 未配置候选的域名 → 直接以 host 名建连；
  - 候选与 DNS 均失败 → 异常抛出（调用方据此回 502）。

用桩替换 socket.create_connection，全程在内存中判定，无真实网络 IO。
退出码：0 = 全过。
"""
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, _HERE / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load("git_tunnel_mod", "git_tunnel.py")

RESULTS = []


def check(label, cond):
    RESULTS.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {label}")


class FakeSock:
    def close(self):
        pass


def _run(host, port, routes, reachable):
    """在桩环境里跑一次 connect_target，返回 (命中的目标, 尝试序列, 异常)。

    reachable：可达的目标集合，集合外的一律建连失败。
    """
    calls = []

    def fake_create_connection(addr, timeout=None):
        target, _p = addr
        calls.append(target)
        if target in reachable:
            return FakeSock()
        raise OSError(f"unreachable {target}")

    orig = T.socket.create_connection
    T.ROUTES = routes
    T.socket.create_connection = fake_create_connection
    try:
        try:
            _sock, used = T.connect_target(host, port)
            return used, calls, None
        except Exception as e:  # noqa: BLE001
            return None, calls, e
    finally:
        T.socket.create_connection = orig


def run_cases():
    # 1. 首位候选可用：命中即用，不试其余
    used, calls, _err = _run("github.com", 443,
                             {"github.com": ["10.0.0.1", "10.0.0.2"]},
                             {"10.0.0.1"})
    check("首位候选可用时命中它", used == "10.0.0.1")
    check("命中后不再尝试其余候选", calls == ["10.0.0.1"])

    # 2. 首位失败、次位可用：按序推进
    used, calls, _err = _run("github.com", 443,
                             {"github.com": ["10.0.0.1", "10.0.0.2"]},
                             {"10.0.0.2"})
    check("首位失败后按序试次位", used == "10.0.0.2")
    check("记录到两次按序尝试", calls == ["10.0.0.1", "10.0.0.2"])

    # 3. 候选全失败 → 回落系统 DNS（关键修复点）
    used, calls, err = _run("github.com", 443,
                            {"github.com": ["10.0.0.1", "10.0.0.2"]},
                            {"github.com"})
    check("候选全失败时回落系统 DNS", used == "github.com")
    check("回落前确实逐个试过候选", calls == ["10.0.0.1", "10.0.0.2", "github.com"])
    check("候选全失败不抛异常", err is None)

    # 4. 未配置候选的域名 → 直接按 host 名建连
    used, calls, _err = _run("example.test", 443, {}, {"example.test"})
    check("未配置候选直接走 DNS", used == "example.test")
    check("未配置候选时不做多余尝试", calls == ["example.test"])

    # 5. 候选与 DNS 都失败 → 抛异常（调用方据此回 502）
    used, calls, err = _run("github.com", 443, {"github.com": ["10.0.0.1"]}, set())
    check("候选与 DNS 均失败时抛异常", used is None and isinstance(err, OSError))
    check("失败路径同样走完 DNS 回落", calls == ["10.0.0.1", "github.com"])

    # 6. 多候选全失败后回落成功：确认不是只试第一个就放弃
    used, calls, _err = _run("github.com", 443,
                             {"github.com": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]},
                             {"github.com"})
    check("三候选全失败后仍回落成功", used == "github.com")
    check("三个候选都被试过", calls == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "github.com"])


if __name__ == "__main__":
    run_cases()
    print("---")
    ok = all(RESULTS)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)
