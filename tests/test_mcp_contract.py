#!/usr/bin/env python3
"""mcp_contract.py 的离线回归用例（不联网）。"""

COVERS = ("mcp_contract",)

import contextlib
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


MC = _load("mcp_contract", "mcp_contract.py")
from cli_util import cli_entry as CLI_ENTRY

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


class FakeClient:
    def __init__(self, tools=None, boom=None):
        self.tools = tools
        self.boom = boom
        self.calls = []

    def init(self):
        pass

    def tools_list(self):
        self.calls.append("tools_list")
        if self.boom:
            raise self.boom
        return {"tools": self.tools}


def run(argv, client):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = MC.main(argv, client=client)
    return code, out.getvalue(), err.getvalue()


def run_cli(argv, client):
    """走真实入口：参数写错由 `cli_entry` 渲染成一行提示 + 退出码，不冒 traceback。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = CLI_ENTRY(lambda: MC.main(argv, client=client), errors=(MC.FlomoError,))
    return code, out.getvalue(), err.getvalue()


def baseline():
    """以抄本为基线复刻一份服务端回包；只在需要的工具上做变异。

    这样每条变异只产生**一条**漂移行，断言才咬得住——另造一个残缺清单会顺带报出
    一堆「抄本已无」，关键词断言即使写错也能蒙过去。
    """
    return copy.deepcopy(MC.FLOMO_MCP_TOOLS)


def at(tools, name):
    return next(t for t in tools if t["name"] == name)


def test_identical():
    code, out, _err = run([], FakeClient(baseline()))
    check("一致时退出码 0", code == 0, (code, out))
    check("一致时明说逐项一致", "逐项一致" in out, out)
    check("一致时打印两边的工具数", f"服务端 {len(MC.FLOMO_MCP_TOOLS)} 个工具" in out, out)


def test_identical_is_not_vacuous():
    # 一致不能靠「两边都空」蒙出来：空清单走的是「取不到」那条出口（退出码 3）
    code, _out, _err = run([], FakeClient([]))
    check("空回包不算一致", code == 3, code)


def test_server_extra_tool():
    server = [*baseline(), {"name": "tag_merge", "inputSchema": {"type": "object", "properties": {}}}]
    code, out, _err = run([], FakeClient(server))
    check("服务端多工具：退出码 1", code == 1, code)
    check("服务端多工具：点名未登记", "tag_merge" in out and "未登记" in out, out)


def test_contract_extra_tool():
    # 抄本比服务端宽同样算漂移：抄本会让人以为能调一个已经不存在的工具
    server = [t for t in baseline() if t["name"] != "tag_search"]
    code, out, _err = run([], FakeClient(server))
    check("抄本多工具：退出码 1", code == 1, code)
    check("抄本多工具：报告服务端已无", "tag_search" in out and "已无" in out, out)


def test_param_missing_in_contract():
    server = baseline()
    at(server, "memo_create")["inputSchema"]["properties"]["extra_field"] = {"type": "string"}
    code, out, _err = run([], FakeClient(server))
    check("抄本漏写参数：退出码 1", code == 1, code)
    check("抄本漏写参数：点名 extra_field", "extra_field" in out and "漏写" in out, out)


def test_param_extra_in_contract():
    server = baseline()
    at(server, "memo_create")["inputSchema"]["properties"].pop("format")
    _code, out, _err = run([], FakeClient(server))
    check("抄本多写参数：点名 format", "format" in out and "多写" in out, out)


def test_param_drift_notes_rejection():
    # 服务端全表 additionalProperties:false，多写的参数会被拒而不是忽略——提示要说清
    server = baseline()
    at(server, "memo_create")["inputSchema"]["properties"].pop("format")
    _code, out, _err = run([], FakeClient(server))
    check("多写参数：提示会被服务端拒", "会拒" in out, out)


def test_required_drift():
    server = baseline()
    at(server, "memo_create")["inputSchema"]["required"] = []
    code, out, _err = run([], FakeClient(server))
    check("必填漂移：退出码 1", code == 1, code)
    check("必填漂移：指出抄本多记必填", "记成必填" in out, out)


def test_required_missing_in_contract():
    server = baseline()
    sch = at(server, "memo_create")["inputSchema"]
    sch["required"] = [*list(sch.get("required") or []), "format"]
    _code, out, _err = run([], FakeClient(server))
    check("抄本漏标必填：点名 format", "漏标必填" in out, out)


def test_fetch_failure():
    code, out, err = run([], FakeClient(boom=MC.FlomoError("连接失败")))
    check("取不到清单：退出码 3", code == 3, code)
    check("取不到清单：明说结论不可用", "结论不可用" in err, err)
    check("取不到清单：不冒充一致", "逐项一致" not in out, out)


def test_json_output():
    server = [*baseline(), {"name": "tag_merge", "inputSchema": {"type": "object"}}]
    code, out, _err = run(["--json"], FakeClient(server))
    data = json.loads(out)
    check("--json：退出码仍按漂移给", code == 1, code)
    check("--json：带两边数量",
          data["server_count"] == len(MC.FLOMO_MCP_TOOLS) + 1
          and data["contract_count"] == len(MC.FLOMO_MCP_TOOLS), data)
    check("--json：漂移可机读", any("tag_merge" in d for d in data["drift"]), data)


def test_compare_is_pure():
    same = [{"name": "x", "inputSchema": {"type": "object", "properties": {"a": {}}}}]
    check("compare 同输入无差异", MC.compare(same, same) == [], MC.compare(same, same))
    drifted = MC.compare(same, [{"name": "x", "inputSchema": {"type": "object"}}])
    check("compare 漏写参数被指出", any("漏写" in d for d in drifted), drifted)


def test_contract_self_consistent():
    # 抄本自身不能有重名：compare 按名建字典，重名会静默丢掉一个，等于少核
    names = [t["name"] for t in MC.FLOMO_MCP_TOOLS]
    check("抄本无重名工具", len(names) == len(set(names)), names)
    check("抄本自身比对无差异", MC.compare(MC.FLOMO_MCP_TOOLS, MC.FLOMO_MCP_TOOLS) == [])


def test_usage_errors():
    for argv, want in ([["--nope"], "未知"], [["--json", "--json"], "重复"]):
        code, _out, err = run_cli(argv, FakeClient([]))
        check(f"用法错（{' '.join(argv)}）：退出码 2", code == 2, code)
        check(f"用法错（{' '.join(argv)}）：提示含「{want}」", want in err, err)


if __name__ == "__main__":
    for fn in (test_identical, test_identical_is_not_vacuous, test_server_extra_tool,
               test_contract_extra_tool, test_param_missing_in_contract,
               test_param_extra_in_contract, test_param_drift_notes_rejection,
               test_required_drift, test_required_missing_in_contract,
               test_fetch_failure, test_json_output, test_compare_is_pure,
               test_contract_self_consistent, test_usage_errors):
        fn()
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
