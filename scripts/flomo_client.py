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
  python flomo_client.py memo_create '{"content":"正文..."}'   # 写操作一律免确认

权限纪律：所有工具（只读与写操作）均可直接调用，无需额外授权。
写操作（memo_create / memo_update / tag_rename / tag_add）执行后直接报告结果。
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
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
    # 写操作后强制校验结构化 id，杜绝"看不到 id 就当成失败重发"导致的重复写。
    # flomo 成功响应中 id 位于 result.structuredContent.id（嵌套结构），
    # 取不到即视为失败，退出码非 0，下游自动化不会据此重试。
    if name in ("memo_create", "memo_update"):
        try:
            created_id = result["structuredContent"]["id"]
        except (TypeError, KeyError):
            sys.stderr.write(
                "[失败] 写操作响应中未取到 structuredContent.id，"
                "按失败处理，禁止基于猜测重发写请求。\n"
            )
            return 1
        sys.stderr.write(f"[成功] 已写入 memo id={created_id}\n")
    return 0


# 标准 MCP 工具契约（客户端侧镜像，供 sounding 等治理工具审计）。
# flomo_client.py 透传任意工具名，此处只声明实际由 flomo MCP server 暴露、
#本项目常用且有明确参数的工具；webfetch/websearch/memory_* 属环境级通用工具，
#不在此 flomo 契约内。字段对齐 MCP spec：name / description / inputSchema / annotations。
FLOMO_MCP_TOOLS = [
    {
        "name": "memo_create",
        "description": "新建一张 flomo 云端 memo。content 为卡片正文（首行即标签段，空一行接正文）；format 可选 markdown/html，省略即纯文本。当要把网页、文章或想法沉淀成云端卡片、且已征得用户授权时使用。只读查询或更新已有 memo 不要用它，应改用 memo_search 或 memo_update。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "卡片正文，首行标签段加空行接正文"},
                "format": {"type": "string", "description": "可选，markdown 或 html；省略即纯文本"},
                "linked_memos": {"type": "array", "items": {"type": "string"}, "description": "可选，关联的其他 memo id 列表"}
            },
            "required": ["content"]
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
    },
    {
        "name": "memo_update",
        "description": "更新一张已有 flomo memo 的内容或标签。id 指定目标；content 为新正文（覆盖式），format 同 memo_create，local_updated_at 用于并发防覆盖。当要修改已落云卡片、且已授权时使用。新建卡片请用 memo_create，不要误用本工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "目标 memo 的 id（slug）"},
                "content": {"type": "string", "description": "新正文，覆盖原内容"},
                "format": {"type": "string", "description": "可选，markdown 或 html"},
                "local_updated_at": {"type": "string", "description": "可选，本地更新时间戳用于并发控制"}
            },
            "required": ["id"]
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
    },
    {
        "name": "memo_search",
        "description": "按关键词或标签检索云端 memo。keywords 与 tag 二选一或并用；limit 限制返回条数。当要查重、找历史卡片、核对某主题是否已记录时使用。新建卡片前必须先调用本工具做查重。不需要全文抓取内容时不要用 tag_tree。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "关键词，匹配正文与标签"},
                "tag": {"type": "string", "description": "按顶层或子标签精确检索"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 10"}
            },
            "required": []
        },
        "annotations": {"readOnlyHint": True}
    },
    {
        "name": "memo_batch_get",
        "description": "按 id 或 slug 批量取 memo 完整内容，含正文、标签与时间戳。当要读某张卡全文、确认 linked_memos 或取 local_updated_at 以更新卡时使用。单卡概览可用 tag_tree，不必本工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "description": "memo id 列表"},
                "slugs": {"type": "array", "items": {"type": "string"}, "description": "memo slug 列表"}
            },
            "required": []
        },
        "annotations": {"readOnlyHint": True}
    },
    {
        "name": "memo_recommended",
        "description": "获取 flomo 推荐的关联 memo，按时间或主题排序。当要发现可合并或相关的邻近卡片、做复盘查重时使用。不需要推荐、只想精确检索时用 memo_search。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回条数上限"}
            },
            "required": []
        },
        "annotations": {"readOnlyHint": True}
    },
    {
        "name": "tag_tree",
        "description": "返回云端全部标签的层级树，含顶层、子标签与计数。当要现采标签树、查重前核近义顶层、或维护顶层词表时使用。只想要某主题卡片请用 memo_search，不要本工具。",
        "inputSchema": {
            "type": "object"
        },
        "annotations": {"readOnlyHint": True}
    },
    {
        "name": "tag_rename",
        "description": "全库重命名一个标签，old_tag 改为 new_tag，会同步改所有引用该标签的 memo。写操作、不可逆，调用前必须授权。当要合并、规范化标签或修正拼写时使用。单卡改标签请用 memo_update，不要本工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_tag": {"type": "string", "description": "原标签名，含顶层斜杠"},
                "new_tag": {"type": "string", "description": "新标签名"},
                "max_memos": {"type": "integer", "description": "受影响 memo 上限，防误改范围过大"}
            },
            "required": ["old_tag", "new_tag"]
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
    },
    {
        "name": "get_format_guide",
        "description": "返回 flomo 支持的卡片格式与富文本写法指南，含加粗、高亮、列表、下划线。当要确认某富文本语法是否被支持、写作前对齐格式时使用。不需要格式细节时不要用。",
        "inputSchema": {
            "type": "object"
        },
        "annotations": {"readOnlyHint": True}
    },
    {
        "name": "get_tag_guide",
        "description": "返回 flomo 标签规则与命名约定指南。当要定新标签、判断顶层与子标签边界、或核顶层词表时使用。格式细节请用 get_format_guide。",
        "inputSchema": {
            "type": "object"
        },
        "annotations": {"readOnlyHint": True}
    },
]


if __name__ == "__main__":
    sys.exit(main())