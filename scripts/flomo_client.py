#!/usr/bin/env python3
"""flomo MCP 客户端封装（可复用调用）。

背景：本环境 TRAE 无 MCP 面板，.mcp.json 不会被加载（run_mcp 报 not found）；
经 SKILL 实测，可用 curl 直连 streamable-http 端点调工具。本脚本用纯标准库
实现同一条通道，便于反复调用与脚本化，并把握手、SSE 解析、token 读取收敛到一处。

token 来源优先级：环境变量 FLOMO_TOKEN > 项目根 .mcp.json 的
mcpServers.flomo.headers.Authorization（.mcp.json 已 gitignore，不入库）。

用法：
  python flomo_client.py <tool名> ['{"参数":值}']
  示例：
  python flomo_client.py tag_tree
  python flomo_client.py memo_search '{"keywords":"政策性金融工具","limit":10}'
  python flomo_client.py get_format_guide
  python flomo_client.py memo_create '{"content":"正文..."}'   # 注意：写操作需先征得用户授权

权限纪律：只读工具（tag_tree/memo_search/memo_batch_get/memo_recommended/
get_format_guide/get_tag_guide）可直接调；写工具（memo_create/memo_update/
tag_rename）必须先向用户完整展示内容并征得明确同意，绝不静默写库存。
"""
import itertools
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://flomoapp.com/mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IDS = itertools.count(1)


def load_token():
    """返回 (token, 来源) 元组。

    优先级：项目 .mcp.json（当前配置事实源，含正确 token）优先，
    环境变量 FLOMO_TOKEN 仅在不存 .mcp.json 时作后备。
    注意：本机 FLOMO_TOKEN 曾被写入一个过期 token，故不以 env 为准。
    """
    mcp = PROJECT_ROOT / ".mcp.json"
    if mcp.exists():
        try:
            data = json.loads(mcp.read_text(encoding="utf-8"))
            auth = data["mcpServers"]["flomo"]["headers"]["Authorization"]
            if auth.startswith("Bearer "):
                return auth[7:], "项目 .mcp.json"
        except (KeyError, json.JSONDecodeError):
            pass
    t = os.environ.get("FLOMO_TOKEN")
    if t:
        return t, "env FLOMO_TOKEN"
    raise SystemExit("未找到 flomo token：请补全 .mcp.json，或设置 FLOMO_TOKEN 并 Export")


class FlomoClient:
    def __init__(self, token):
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
        }
        self._session = None

    def _post(self, payload, with_session=True):
        headers = dict(self._headers)
        if with_session and self._session:
            headers["Mcp-Session-Id"] = self._session
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(ENDPOINT, data=data, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        body = resp.read().decode("utf-8", "replace")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid and not self._session:
            self._session = sid
        return self._parse_sse(body)

    @staticmethod
    def _parse_sse(body):
        """解析 SSE，返回匹配本请求 id 的 result；遇 error 直接抛错。"""
        want = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            try:
                obj = json.loads(d)
            except json.JSONDecodeError:
                continue
            if obj.get("id") is None:
                continue
            if want is None:
                want = obj.get("id")
            if obj.get("id") != want:
                continue
            if "error" in obj:
                raise SystemExit("MCP error: " + json.dumps(obj["error"], ensure_ascii=False))
            if "result" in obj:
                return obj["result"]
        # JSON-RPC 通知（如 notifications/initialized）无 result/error，属正常空响应
        return None

    def init(self):
        self._post(
            {
                "jsonrpc": "2.0",
                "id": next(_IDS),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "flake-client", "version": "1.0"},
                },
            },
            with_session=False,
        )
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            with_session=True,
        )

    def tool(self, name, arguments=None):
        payload = {
            "jsonrpc": "2.0",
            "id": next(_IDS),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        return self._post(payload)


def usage():
    print(__doc__)
    return 2


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    token, src = load_token()
    client = FlomoClient(token)
    client.init()
    name = argv[0]
    if len(argv) > 1 and argv[1] == "--file":
        # 参数从 JSON 文件读（规避 PowerShell 中文环境把中文引号问题的双引号转成全角）
        arguments = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    else:
        arguments = json.loads(argv[1]) if len(argv) > 1 else {}
    if name in ("memo_create", "memo_update", "tag_rename"):
        sys.stderr.write(f"[注意] {name} 是写操作，调用前应已征得用户明确授权。\n")
    result = client.tool(name, arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())