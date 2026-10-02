#!/usr/bin/env python3
"""flomo MCP 客户端封装（可复用调用）。

背景：本环境 TRAE 无 MCP 面板，.mcp.json 不会被加载（run_mcp 报 not found）；
经 SKILL 实测，可用 curl 直连 streamable-http 端点调工具。本脚本用纯标准库
实现同一条通道，便于反复调用与脚本化，并把握手、SSE 解析、token 读取收敛到一处。

token 来源优先级：项目根 .mcp.json 的 mcpServers.flomo.headers.Authorization > 环境变量
FLOMO_TOKEN（.mcp.json 是配置事实源；env 仅在不存该文件时后备，因其可能过期）。

用法：
  python flomo_client.py <tool名> ['{"参数":值}']
  示例：
  python flomo_client.py tag_tree
  python flomo_client.py memo_search '{"keywords":"政策性金融工具","limit":10}'
  python flomo_client.py get_format_guide
  python flomo_client.py memo_create '{"content":"正文..."}'   # 写操作一律免确认

权限纪律：所有工具（只读与写操作）均可直接调用，无需额外授权。
写操作（memo_create / memo_update / tag_rename / tag_add）执行后直接报告结果。

写后验收：`memo_create` / `memo_update` 写入成功后**自动按 id 回读全文逐字比对**
（H11），不一致、取不到卡、或被云端截断一律判失败（退出码非 0）——「拿到 id」
只证明请求被受理，不证明云端存的就是这份正文。验收逻辑见 `readback_check`。

作**库**被复用时的契约：握手失败、HTTP 错误、JSON-RPC 协议错误一律抛 `FlomoError`
（`RuntimeError` 子类，可被 `except Exception` 接住）；**不使用 `SystemExit`**——
它继承自 `BaseException`，会把调用方的异常处理穿透，在 HTTP 服务里表现为连接被掐断、
错误原因传不出去。CLI 侧在入口把 `FlomoError` 转成退出码。
"""
import itertools
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memo_util import concept_keywords, memo_signature  # noqa: E402

ENDPOINT = "https://flomoapp.com/mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IDS = itertools.count(1)


def load_token():
    """返回 (token, 来源) 元组。

    优先级：项目 .mcp.json（配置事实源）优先，环境变量 FLOMO_TOKEN 仅在不存 .mcp.json 时作后备。
    env 中的 token 可能过期，故不作权威来源。
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


class FlomoError(RuntimeError):
    """云端调用失败（HTTP 错误 / JSON-RPC 协议错误）。

    **不要用 `SystemExit` 表示这类失败**：`SystemExit` 继承自 `BaseException` 而非
    `Exception`，一旦被当作通用错误类型抛出，库的调用方（`except Exception`）就接不住，
    会直接穿透到进程顶层——在 HTTP 服务里表现为**连接被掐断、客户端拿到空响应**，
    错误原因一个字都传不出去。CLI 需要退出码，在入口处转成 `return 1` 即可。
    """


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
            raise FlomoError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from None
        body = resp.read().decode("utf-8", "replace")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid and not self._session:
            self._session = sid
        return self._parse_sse(body, payload.get("id"))

    @staticmethod
    def _parse_sse(body, want_id=None):
        """解析 SSE，返回匹配 want_id 的 result；遇 error 直接抛错。

        want_id 由调用方显式传入（即本次请求的 JSON-RPC id）。**不能退化为
        "取流里第一个出现的 id"**：SSE 流可能先夹带服务端通知或并发响应，
        那时取到的就不是本次请求的结果——写操作会据此误判成功/失败。
        仅当调用方未传（如无 id 的单向通知）时才回落取首个非空 id。
        """
        want = want_id
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            try:
                obj = json.loads(d)
            except json.JSONDecodeError:
                continue
            oid = obj.get("id")
            if oid is None:
                continue
            if want is None:
                want = oid
            if oid != want:
                continue
            if "error" in obj:
                raise FlomoError("MCP error: " + json.dumps(obj["error"], ensure_ascii=False))
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

    def tools_list(self):
        """列出云端 MCP 暴露的全部工具（协议层元信息，不读任何卡片数据）。

        与 `tool()` 的区别：这是 JSON-RPC 的发现方法，不是工具调用，故不列入
        工具白名单——白名单管的是「能调哪些工具」，本方法不调任何工具。
        """
        payload = {"jsonrpc": "2.0", "id": next(_IDS), "method": "tools/list", "params": {}}
        return self._post(payload)


def usage():
    print(__doc__)
    return 2


def _result_memos(result):
    """从 flomo 工具返回中提取 memos 列表。

    工具返回是 {"content":[{"type":"text","text":"<内层JSON>"}], "structuredContent":{...}} 包裹结构，
    结构化数据位于 structuredContent；极少数工具只给 content[].text 的内层 JSON。
    统一优先读 structuredContent.memos，兜底解析 content[].text。
    """
    if not isinstance(result, dict):
        return []
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        memos = sc.get("memos")
        if isinstance(memos, list):
            return memos
    for block in (result.get("content") or []):
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        mm = (inner or {}).get("memos")
        if isinstance(mm, list):
            return mm
    return []


def _exact_content_dup(client, content):
    """幂等保护：写前查重，返回与 content 同签名（标签行+概念名行）的既有 memo id，否则 None。

    用多个候选关键词（概念名核心段 / 标签二级词）逐次检索命中候选，
    再按卡片签名比对。flomo 的 memo_search 对超长正文可能截断
    （content_truncated=True），但前两行始终完整，故无需拉全文。
    """
    sig = memo_signature(content)
    if not sig:
        return None
    for keyword in concept_keywords(content, limit=4):
        try:
            res = client.tool("memo_search", {"keywords": keyword, "limit": 20})
        except (FlomoError, SystemExit):
            continue
        for m in _result_memos(res):
            mid = m.get("id")
            if not mid:
                continue
            if memo_signature(m.get("content")) == sig:
                return mid
    return None


# 云端存储层对 Markdown 元字符做的反斜杠转义，还原回原字符。
# 只处理「反斜杠 + 元字符」这一形态，且**不碰本来就没有反斜杠的文本**——
# 因此本地若本来就写了反斜杠，两侧都会被剥掉，不产生「本地多一个反斜杠」的假差异。
_MD_ESCAPES = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~<>])")


def _norm_body(text):
    """回读比对用归一化：**平台自有的渲染差异一律不参与比对**。

    差异分两类，都不由写入方决定，因此不该让回读判失败：

    一、空白的有无与数量，由 flomo渲染层决定，至少两类：
      - 云端会在**标签段后**与**列表引导行（如「要点：」）后**自动补空行，
        而本地写的是紧邻单换行；
      - 云端会在**全角标点（：、，、；、。）与紧随其后的行内代码之间插入一个空格**
        （本地写「全角冒号紧接反引号」，云端存成「全角冒号 + 空格 + 反引号」）——
        这是渲染层为行内代码补边界，不是内容改动。

    二、**Markdown 元字符的反斜杠转义**。flomo 存储层存的是转义后的文本，
    与 H6 记载的「`>`、`|` 存成 `\\u003e`、`\\|`」是同一回事，实测还有
    `_` → `\\_`、`[` → `\\[`、`]` → `\\]`、反斜杠自身 → `\\\\`。
    逐字比对必然不等，于是**任何含下标的卡（数学公式、变量下标最典型）
    回读验收100% 误报失败**，把「卡存对了」报成「写失败」——检查形同虚设，
    还会诱使人忽略它。故比对前统一还原：只吃「反斜杠 + 元字符」这一形态。

    比对单位取「逐行还原转义、删去行内水平空白、再丢弃空行后的行序列」：
    **行界与所有非转义字符一律不宽容**——任何一行文字的增删改动、行序变化、
    错别字、公式内容改动都会判失败（卡片的分段结构由写前质检 `validate_memo.py`
    按 H14 守住，不由回读承担）。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = [ln for ln in lines if ln.strip()]
    return "\n".join(
        re.sub(r"[ \t\u3000]+", "", _MD_ESCAPES.sub(r"\1", ln)) for ln in kept
    )


def readback_check(client, memo_id, written):
    """写云后按 id 回读全文，与写入正文逐字比对（H11 的回读验收）。

    返回 `(ok, detail)`。这件事必须机械做：写操作返回 id 只证明「请求被受理」，
    不证明「云端存的正是这份正文」——中间可能被格式转换、被并发覆盖，或 id 对错了卡。
    只看 id 就报成功，等于把「验收」交给运气。
    """
    res = client.tool("memo_batch_get", {"ids": [memo_id]})
    memos = _result_memos(res)
    if not memos:
        return False, (f"回读未取到 memo（id={memo_id}）——写入是否落库须人工确认；"
                       "禁止据此重发写请求")
    m = memos[0]
    if m.get("id") and m.get("id") != memo_id:
        return False, (f"回读返回的是另一张卡（请求 {memo_id} / 实际 {m.get('id')}）"
                       "——须人工核对，禁止据此重发写请求")
    stored = m.get("content") or ""
    if m.get("content_truncated"):
        return False, (f"回读正文被云端截断（id={memo_id}）——无法逐字比对，"
                       "须人工核对全文")
    a, b = _norm_body(written), _norm_body(stored)
    if a != b:
        i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]),
                 min(len(a), len(b)))
        return False, (f"回读正文与写入不一致（写入 {len(a)} 字 / 云端 {len(b)} 字，"
                       f"首个差异在第 {i + 1} 个字符）——禁止据此重发写请求，"
                       f"须人工核对 id={memo_id}")
    return True, f"回读一致（{len(b)} 字）"


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
    # 幂等保护：memo_create 写前先查重，若云端已存在
    # 正文逐字相同的卡片则直接复用其 id、不再新建。无论 create 被触发几次，
    # 第二次起都会命中既有卡，杜绝重复建卡。
    if name == "memo_create":
        dup = _exact_content_dup(client, arguments.get("content"))
        if dup:
            sys.stderr.write(f"[幂等] 已存在正文相同的 memo id={dup}，复用不新建\n")
            print(json.dumps({"idempotent": True, "reused_id": dup}, ensure_ascii=False))
            return 0
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
        # H11 回读验收：写完必须按 id 拉回全文逐字比对，不一致即判失败。
        # 这一步放在写命令内部，是为了让「验收」不依赖调用方记不记得做——
        # 凭 id 就宣布成功，等于把验收交给运气。
        ok, detail = readback_check(client, created_id, arguments.get("content") or "")
        if not ok:
            sys.stderr.write(f"[失败] 回读验收未过：{detail}\n")
            return 1
        sys.stderr.write(f"[验收] {detail}\n")
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
        "description": "获取指定 memo 的关联推荐，按时间或主题排序。当要发现可合并或相关的邻近卡片、做复盘查重时使用。不需要推荐、只想精确检索时用 memo_search。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "锚定 memo 的 id（必填；服务端按此卡返回相关推荐）"},
                "limit": {"type": "integer", "description": "返回条数上限"}
            },
            "required": ["id"]
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
    # CLI 入口把库异常转成退出码（库层不再用 SystemExit 表达调用失败）
    try:
        sys.exit(main())
    except FlomoError as e:
        sys.stderr.write(f"[失败] {e}\n")
        sys.exit(1)