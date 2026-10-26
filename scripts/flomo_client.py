#!/usr/bin/env python3
"""flomo MCP 客户端封装（可复用调用）。

背景：本环境 TRAE 无 MCP 面板，.mcp.json 不会被加载（run_mcp 报 not found）；
经 SKILL 实测，可用 curl 直连 streamable-http 端点调工具。本脚本用纯标准库
实现同一条通道，便于反复调用与脚本化，并把握手、SSE 解析、token 读取收敛到一处。

token 来源优先级：项目根 .mcp.json 的 mcpServers.flomo.headers.Authorization > 环境变量
FLOMO_TOKEN（.mcp.json 是配置事实源；env 仅在不存该文件时后备，因其可能过期）。

用法：
  python flomo_client.py <tool名> ['{"参数":值}'] [--brief]
  示例：
  python flomo_client.py tag_tree
  python flomo_client.py memo_search '{"keywords":"政策性金融工具","limit":10}'
  python flomo_client.py get_format_guide
  python flomo_client.py memo_create '{"content":"正文..."}'   # 写操作一律免确认

`--brief` 只压缩**列表型**回包（搜索/推荐/标签树）：给 id、字数、标签与前两行签名，
正文不入表，省掉每次 27–55 KB 的原始 JSON。`memo_batch_get` 永远走全文分支、
不截断——H1 禁的正是「用截断内容判断正文」。写操作的 id 校验与写后回读验收不受影响。

权限纪律：所有工具（只读与写操作）均可直接调用，无需额外授权。
写操作（memo_create / memo_update / tag_rename / tag_add）执行后直接报告结果。

写后验收：`memo_create` / `memo_update` 写入成功后**自动按 id 回读全文逐字比对**
（H11），不一致、取不到卡、或被云端截断一律判失败（退出码非 0）——「拿到 id」
只证明请求被受理，不证明云端存的就是这份正文。验收逻辑见 `readback_check`。

作**库**被复用时的契约：凭据缺失、握手失败、HTTP 错误、JSON-RPC 协议错误一律抛 `FlomoError`
（`RuntimeError` 子类，可被 `except Exception` 接住）；**不使用 `SystemExit`**——
它继承自 `BaseException`，会把调用方的异常处理穿透，在 HTTP 服务里表现为连接被掐断、
错误原因传不出去。CLI 侧在入口把 `FlomoError` 转成退出码。
命令行参数写错（`--limit abc`）另有 `UsageError`（`FlomoError` 子类），由入口按用法码
退出——不要让它和「调用失败」共用一个退出码，用户会去查网络。
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
from memo_util import concept_keywords, find_truncation, memo_signature  # noqa: E402
from cli_util import UsageError, cli_entry  # noqa: E402  命令行契约在叶子层，本模块只负责云端通道
import net_util  # noqa: E402  出网通道与「请求是否已发出」的唯一实现

ENDPOINT = "https://flomoapp.com/mcp"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IDS = itertools.count(1)

# 不幂等的工具：重复执行会产生第二张卡，或让读数变化。
# **回包没回来时绝不许换出口重发**——`memo_create` 的签名幂等有索引滞后窗口
# （实测 13 秒内重发即重复建卡），所以「重试」在这里不是省事，是造脏数据。
# 未登记的工具按不幂等处理（见 `FlomoClient.tool`）：新加工具忘了登记时，
# 落在「不重发」这侧只多报一次错，落在「重发」那侧就多一张卡。
WRITE_TOOLS = frozenset({"memo_create", "memo_update", "tag_rename"})


class FlomoError(RuntimeError):
    """调用无法进行：本地凭据缺失，或云端调用失败（HTTP 错误 / JSON-RPC 协议错误）。

    两类都归这里，是因为调用方对它们的处置相同——都只能终止本次调用并报出原因。
    **不要用 `SystemExit` 表示这类失败**：`SystemExit` 继承自 `BaseException` 而非
    `Exception`，一旦被当作通用错误类型抛出，库的调用方（`except Exception`）就接不住，
    会直接穿透到进程顶层——在 HTTP 服务里表现为**连接被掐断、客户端拿到空响应**，
    错误原因一个字都传不出去。CLI 需要退出码，在入口处转成 `return 1` 即可。
    """


def load_token():
    """返回 (token, 来源) 元组。

    优先级：项目 .mcp.json（配置事实源）优先，环境变量 FLOMO_TOKEN 仅在不存 .mcp.json 时作后备。
    env 中的 token 可能过期，故不作权威来源。

    取不到就抛 `FlomoError`——这是本模块唯一的失败类型，调用方 `except FlomoError`
    一处即可覆盖（**不能抛 `SystemExit`**，见类文档）。
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
    raise FlomoError("未找到 flomo token：请补全 .mcp.json，或设置 FLOMO_TOKEN 并 Export")


class CloudUnreachable(FlomoError):
    """出网通道不可用（连接被拒 / 超时 / DNS / 中断），不是服务端拒绝。

    **必须与「服务端返回错误」分开**：两者处置相反——服务端报错要改内容或参数，
    通道报错只要换个出口再试。混成一个错误，人会照着「云端失败」去改卡片，
    而真正的病根在网络出口。

    `request_sent` 是给写操作的护栏：
      False（连接被拒 / 域名解析失败）→ 请求确定没发出，换通道重发安全；
      True（超时 / 连接中断）→ 可能已到达服务端，**不许重发**，
              `memo_create` 重发就是第二张卡。此时只能报「需人工核对」。
    """

    def __init__(self, message, *, request_sent):
        super().__init__(message)
        self.request_sent = bool(request_sent)


class FlomoClient:
    def __init__(self, token, timeout=30.0):
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
        }
        self._session = None
        self._timeout = timeout

    def _post(self, payload, with_session=True, idempotent=True):
        headers = dict(self._headers)
        if with_session and self._session:
            headers["Mcp-Session-Id"] = self._session
        data = json.dumps(payload).encode("utf-8")
        # 出网走 net_util：代理端口每次会话重新分配，裸调 urlopen 会让「端口没对上」
        # 表现为整轮写卡失败，而且抛的是 URLError——`except FlomoError` 接不住，
        # 上层只能看到 traceback，误以为是云端出了事。
        try:
            reply = net_util.request(
                ENDPOINT,
                data=data,
                headers=headers,
                timeout=self._timeout,
                idempotent=idempotent,
            )
        except urllib.error.HTTPError as e:
            raise FlomoError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from None
        except net_util.TransportError as exc:
            raise CloudUnreachable(
                f"云端不可达：{exc}（环境代理：{net_util.proxy_label()}）",
                request_sent=exc.request_sent,
            ) from None
        body = reply.body.decode("utf-8", "replace")
        sid = reply.headers.get("Mcp-Session-Id")
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
        # 只有**读**工具允许在通道失败时换出口重发；写工具一律不重发
        # （判定与「不幂等」同源，见 WRITE_TOOLS 的注释）。
        return self._post(payload, idempotent=name not in WRITE_TOOLS)

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


def _extract_data(result):
    """从一次 tools/call 回包取出结构化数据，返回 `(数据字典, 取不到的原因)`。

    MCP 的回包形态是 `{"content":[{"type":"text","text":"<内层JSON>"}], "structuredContent":{...}}`：
    正常情况数据在 `structuredContent`，极少数工具只给 `content[].text` 的内层 JSON。

    **「取不到」与「取到空」是两件事**：这里一律返回 `None` 并附原因，出口交给调用方分叉——
    读路径用 `tool_data`（读不到就抛），写后回读用 `tool_data_or_none`（只能标未知，
    抛错会被当成失败而重发）。此前每个脚本各写一份解析、各定一套兜底，同一个回包在
    `tagcheck` 判「缺 total」、在 `sop_gate` 判通过，结论分叉而没人报错。

    工具级错误（`isError` 为真）也走这里并带出错误正文：这类回包此前被解析失败后读成
    「0 条」，查重与幂等检查拿着「没撞车」的结论放行。
    """
    if not isinstance(result, dict):
        return None, f"回包不是对象（{type(result).__name__}）"
    if result.get("isError"):
        msg = " ".join(
            b.get("text") or "" for b in (result.get("content") or []) if isinstance(b, dict)
        ).strip()
        return None, "工具返回错误" + ("：" + msg[:120] if msg else "（无正文）")
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and sc:
        return sc, ""
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(inner, dict) and inner:
            return inner, ""
    return None, "既没有 structuredContent，也没有可解析的 content[].text 内层 JSON"


def tool_data(result):
    """结构化数据；取不到即抛 `FlomoError`（读路径出口：读不到必须当场知道）。"""
    data, why = _extract_data(result)
    if data is None:
        raise FlomoError(f"工具回包取不到结构化数据：{why}；读不到不等于「0 条」")
    return data


def tool_data_or_none(result):
    """结构化数据；取不到返回 None（写后回读出口：只许标未知，不许抛）。"""
    return _extract_data(result)[0]


def brief_text(name, payload):
    """把一次工具返回压成可核对的短表（供 `--brief`）。

    用途只有一个：查重/复盘时「发现有哪些卡」。故只给 id、字数、标签与前两行签名，
    正文不入表。**`memo_batch_get` 例外，走全文分支、绝不截断**——把截断内容包装成
    便利功能，等于给 H1 开了一个后门（H1 禁的正是「用截断内容判断正文」）。

    返回 None 表示这个形态不适合压缩，调用方应退回完整 JSON。
    """
    sc = tool_data_or_none(payload)
    if not sc:
        return None

    memos = sc.get("memos")
    if isinstance(memos, list) and name != "memo_batch_get":
        lines = [f"memos={len(memos)}"]
        for m in memos:
            content = m.get("content") or ""
            sig = [ln.strip() for ln in content.splitlines() if ln.strip()][:2]
            concept = sig[1] if len(sig) > 1 else (sig[0] if sig else "")
            lines.append(
                f"{m.get('id')} | {m.get('word_count')} 字 | "
                f"{','.join(m.get('tags') or [])} | {concept[:40]}"
            )
        return "\n".join(lines)

    if name == "memo_batch_get" and isinstance(memos, list):
        lines = [f"memos={len(memos)}（全文，未截断）"]
        for m in memos:
            lines.append(
                f"===== {m.get('id')} | {m.get('word_count')} 字 | "
                f"{','.join(m.get('tags') or [])}"
            )
            lines.append(m.get("content") or "")
        return "\n".join(lines)

    tags = sc.get("tags")
    # 顺序要紧：写操作/单卡回包同样带 `tags`，若先判 tags 会把一张卡渲染成标签树
    # （实测 memo_create 回包即 {id, tags, word_count}，一度被误判）。
    if sc.get("id"):
        return (
            f"id={sc.get('id')} | {sc.get('word_count')} 字 | "
            f"{','.join(tags if isinstance(tags, list) else [])} | "
            f"link={sc.get('has_link')} image={sc.get('has_image')}"
        )

    if isinstance(tags, list):
        shown = tags if len(tags) <= 30 else tags[:20]
        out = [f"total={sc.get('total')} returned={len(tags)}"] + [str(t) for t in shown]
        if len(tags) > len(shown):
            out.append(f"……另有 {len(tags) - len(shown)} 个（按前缀收窄或调大 limit）")
        return "\n".join(out)

    return None


def _result_memos(result):
    """从 flomo 工具返回中提取 memos 列表。

    回包解析走 `tool_data`（唯一实现）；取不到结构化数据即抛错——不返回空列表。
    **「0 条」与「没读到」此前同形**：云端的工具级错误、或协议形态一变，解析失败
    都退化成 `[]`，查重据此报「没有相近的卡」、写前幂等检查据此判「不是重复」，
    两处安全阀同时无声失效。工具正常返回但确无 memos 字段时仍返回 `[]`。
    """
    memos = tool_data(result).get("memos")
    return memos if isinstance(memos, list) else []


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

# 平台把裸邮箱地址自动渲染成 Markdown 链接：`a@b.c` 存成 `[a@b.c](mailto:a@b.c)`。
# 只还原「显示文本与 mailto 目标完全相同」这一形态——人写的 `[联系我](mailto:a@b.c)`
# 显示文本不同，不会被吃掉；非 mailto 链接（手写 `[文](http://…)`）同样不碰。
# 这类链接化不由写入方决定，逐字比对必然不等，会把「卡存对了」报成写失败。
_AUTO_MAILTO = re.compile(r"\[([^\[\]\n]+)\]\(mailto:\1\)")


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

    三、**裸邮箱地址被自动链接化**。正文里写 `UUID@server.domain.local` 这类
    Message-ID 格式示例，云端存回的是 `[UUID@server.domain.local](mailto:…)`——
    实测那一行凭空多出 35 个字符（该地址 24 字符，链接语法本身 11 字符）。
    这也是平台行为、写入方无从决定，故还原「显示文本 = mailto 目标」的自链接；
    人写的 `[联系我](mailto:a@b.c)` 显示文本不同，照旧参与比对（口径见
    `_AUTO_MAILTO`）。**写法上仍建议避开裸邮箱**：宽容只让这一处对账说得过去，
    任何按原文比对的检查都得跟着加同样的特例；保持「写进去什么、存回来什么」
    才是能一直成立的判据（写前提示见 `validate_memo.py` 的同名 WARN）。

    比对单位取「逐行还原转义、删去行内水平空白、再丢弃空行后的行序列」：
    **行界与所有非转义字符一律不宽容**——任何一行文字的增删改动、行序变化、
    错别字、公式内容改动都会判失败（卡片的分段结构由写前质检 `validate_memo.py`
    按 H14 守住，不由回读承担）。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = [ln for ln in lines if ln.strip()]
    return "\n".join(
        re.sub(r"[ \t\u3000]+", "", _AUTO_MAILTO.sub(r"\1", _MD_ESCAPES.sub(r"\1", ln)))
        for ln in kept
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


def _load_arguments(argv: list[str]) -> dict:
    """读工具参数：**参数写错是用法错，不是云端失败**。

    为什么单列这一层：每个工具的入参名各不相同（`memo_search` 要 `keywords`，
    既不是 `keyword` 也不是 `query`），靠记忆敲必然写错。旧实现直接
    `json.loads(argv[1])`，于是「把选项当 JSON 传」这种最常见的写错会抛
    `JSONDecodeError`，走的是 traceback、退出码 1——与「云端调用失败」同形，
    人会去查网络和 token，实际只是参数名写错。用法错要指回参数本身、按用法码结束。
    """
    if len(argv) < 2:
        return {}
    if argv[1] == "--file":
        if len(argv) < 3:
            raise UsageError("--file 缺少文件路径——写成 `--file <参数 JSON 文件的路径>`")
        path = Path(argv[2])
        if not path.exists():
            raise UsageError(f"--file 指向的文件不存在：{path}")
        raw = path.read_text(encoding="utf-8")
    else:
        raw = argv[1]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"参数不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）。"
            f"用法是 `{argv[0]} '{{\"参数名\": 值}}'`，参数名由各工具自定"
            f"（memo_search 用 keywords、memo_create 用 content、memo_batch_get 用 ids），"
            f"长正文改用 `--file <路径>`；不确定就直接 `{argv[0]}` 看用法"
        ) from None
    if not isinstance(parsed, dict):
        raise UsageError(
            f"参数必须是 JSON 对象，读到的是 {type(parsed).__name__}——"
            f"写成 `'{{\"参数名\": 值}}'` 的形式")
    return parsed


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raw_argv = sys.argv[1:]
    brief = "--brief" in raw_argv
    argv = [a for a in raw_argv if a != "--brief"]
    if not argv or argv[0] in ("-h", "--help", "help"):
        return usage()
    name = argv[0]
    # 先判用法、再连云端：参数写错时不发握手请求，错误也更早落地
    arguments = _load_arguments(argv)
    token, src = load_token()
    client = FlomoClient(token)
    client.init()
    if name in WRITE_TOOLS:
        sys.stderr.write(f"[注意] {name} 是写操作，调用前应已征得用户明确授权。\n")
    # 写前最后一道闸：正文里若含截断占位符（`...[此处省略N字]...`），发出去就是把
    # 「内容缺失」永久固化，而且写后回读逐字对账**发现不了**——它比的是同一段坏文本。
    # 故拦在发请求之前，退出码 4（内容受损，与网络失败、参数错、闸门不过都区分开）。
    if name in ("memo_create", "memo_update"):
        # 强制闸门凭证：没有过sop_gate的卡片一律拒绝写云
        # 防止绕过publish.py直接写云导致跳过查重/搜索/质检
        gate_dir = Path(__file__).parent.parent / ".sop_gate"
        if not gate_dir.exists() or not any(gate_dir.glob("*.json")):
            sys.stderr.write(
                "[失败] 未检测到SOP闸门凭证（.sop_gate/*.json），拒绝直接写云。\n"
                "  必须先跑 scripts/sop_gate.py 或 scripts/publish.py 过闸门，再写云。\n"
                "  禁止绕过闸门直接调用 flomo_client.py memo_create/memo_update。\n"
            )
            return 5
        marks = find_truncation(arguments.get("content") or "")
        if marks:
            sys.stderr.write(
                f"[失败] 正文含截断占位符 {marks[0]}，拒绝写入（退出码 4）。\n"
                "  检索类回包（memo_search / memo_recommended）会把长正文中段挖掉，\n"
                "  只应拿来挑 id。请用 memo_batch_get 现取全文后重新成稿。\n"
            )
            return 4
        # 自动还原 batch_get 读回时 flomo 存储层插入的 <br>：
        # 读回的 content 里换行已被转成 <br>，若直接拼接写回会再叠一层，
        # 变成 <br><br>（多余空行）。写前统一还原成换行，调用方无需手动清洗。
        raw = arguments.get("content") or ""
        if "<br" in raw:
            cleaned = raw.replace("<br><br>", "\n\n").replace("<br>", "\n")
            arguments["content"] = cleaned
            sys.stderr.write(
                f"[清洗] 正文含 {raw.count('<br')} 处 <br> 标签，已自动还原为换行"
                f"（{len(raw)}→{len(cleaned)} 字符）。\n"
            )

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
    text = brief_text(name, result) if brief else None
    if text is None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(text)
    # 写操作后强制校验结构化 id，杜绝"看不到 id 就当成失败重发"导致的重复写。
    # flomo 成功响应中 id 位于 result.structuredContent.id（嵌套结构），
    # 取不到即视为失败，退出码非 0，下游自动化不会据此重试。
    if name in ("memo_create", "memo_update"):
        # 取 id 同样走唯一那份回包解析：只认 `structuredContent` 时，云端一旦把数据
        # 放进 `content[].text`，这里会把一次**成功**的写判成「未取到 id」并退出非 0。
        created_id = (tool_data_or_none(result) or {}).get("id")
        if not created_id:
            sys.stderr.write(
                "[失败] 写操作响应中未取到 id，"
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
    sys.exit(cli_entry(main, errors=(FlomoError,)))