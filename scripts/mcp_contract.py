#!/usr/bin/env python3
"""mcp_contract.py — 核对 `FLOMO_MCP_TOOLS` 与云端实际暴露的工具是否一致。

为什么要有它：`flomo_client.FLOMO_MCP_TOOLS` 是**抄本**，不是事实源。服务端加参数、
去工具时它不会自己变，而它的用途恰恰是给外部审计看「客户端以为的契约」——抄本一旧，
审计的结论就是对着旧地图找路。本项目就撞过一次：`memo_batch_get` 的 `slugs` 早被
服务端下线、`memo_search` 多出五个筛选参数，抄本一处都没记；而服务端所有工具的
inputSchema 都是 `additionalProperties: false`，照着旧抄本传参数是**被拒**而不是被忽略。

比对三项（缺一都算漂移）：
  1. 工具集合——服务端有而抄本无、抄本有而服务端无；
  2. 每个工具的 `properties` 名字集合；
  3. 每个工具的 `required` 集合。

用法：
  python scripts/mcp_contract.py            # 拉一次 tools/list 逐项比对
  python scripts/mcp_contract.py --json     # 机器可读，便于接巡检

退出码：
  0  一致
  1  存在漂移——按输出补齐抄本（服务端是事实源，改的是抄本）
  2  命令行参数写错
  3  取不到服务端工具清单（网络/凭据/协议问题，结论不可用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cli_util import Opt, cli_entry, force_utf8, parse_options  # noqa: E402
from flomo_client import (  # noqa: E402
    FLOMO_MCP_TOOLS,
    FlomoClient,
    FlomoError,
    load_token,
)

_OPTS = [
    Opt("--json", "as_json", "flag", hint="；输出机器可读结果"),
]


def _spec(tool: dict) -> tuple[set[str], set[str]]:
    schema = tool.get("inputSchema") or {}
    props = set((schema.get("properties") or {}).keys())
    req = set(schema.get("required") or [])
    return props, req


def compare(server_tools: list[dict], contract_tools: list[dict]) -> list[str]:
    """逐工具比对，返回人话差异行（空列表＝一致）。纯函数，供用例直接调。"""
    server = {t["name"]: t for t in server_tools if isinstance(t, dict) and t.get("name")}
    contract = {t["name"]: t for t in contract_tools if isinstance(t, dict) and t.get("name")}
    out: list[str] = []

    for name in sorted(set(server) - set(contract)):
        out.append(f"工具 {name}：服务端有、抄本未登记（补一条，否则调用方不知道它存在）")
    for name in sorted(set(contract) - set(server)):
        out.append(f"工具 {name}：抄本有、服务端已无（删掉，抄本不能比服务端宽）")

    for name in sorted(set(server) & set(contract)):
        sp, sr = _spec(server[name])
        cp, cr = _spec(contract[name])
        if sp != cp:
            bits = []
            if cp - sp:
                bits.append(f"抄本多写 {sorted(cp - sp)}（服务端会拒，不是忽略）")
            if sp - cp:
                bits.append(f"抄本漏写 {sorted(sp - cp)}")
            out.append(f"工具 {name} 参数：" + "；".join(bits))
        if sr != cr:
            bits = []
            if cr - sr:
                bits.append(f"抄本把 {sorted(cr - sr)} 记成必填，服务端不是")
            if sr - cr:
                bits.append(f"抄本漏标必填 {sorted(sr - cr)}")
            out.append(f"工具 {name} 必填项：" + "；".join(bits))
    return out


def main(argv: list[str] | None = None, client=None) -> int:
    force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    # 本脚本没有必需参数：默认动作就是比对，不能因为「没给参数」去打印帮助——
    # 那会让 `python scripts/mcp_contract.py` 什么都不查却以 0 退出，看起来像「已对齐」。
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    ns, _pos = parse_options(args, _OPTS)
    as_json = ns["as_json"]

    try:
        if client is None:
            token, _src = load_token()
            client = FlomoClient(token)
            client.init()
        raw = client.tools_list()
    except (FlomoError, RuntimeError) as exc:
        print(f"[失败] 取不到服务端工具清单：{exc}", file=sys.stderr)
        print("       结论不可用——不要据此判「契约已对齐」", file=sys.stderr)
        return 3

    server_tools = (raw or {}).get("tools") or []
    if not server_tools:
        print("[失败] 服务端回包没有 tools 字段（协议形态变了？），结论不可用", file=sys.stderr)
        return 3

    drift = compare(server_tools, FLOMO_MCP_TOOLS)

    if as_json:
        print(json.dumps({
            "server_count": len(server_tools),
            "contract_count": len(FLOMO_MCP_TOOLS),
            "drift": drift,
        }, ensure_ascii=False, indent=1))
    else:
        print(f"[比对] 服务端 {len(server_tools)} 个工具 / 抄本 {len(FLOMO_MCP_TOOLS)} 个")
        for line in drift:
            print("  ✗ " + line)
        if not drift:
            print("[结论] 工具集合、每个工具的参数与必填项，逐项一致")

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(FlomoError,)))
