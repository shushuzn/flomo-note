#!/usr/bin/env python3
"""console_cloud.py — 控制台的云端访问层（把 MCP 的只读能力全部接出来）。

控制台要看的主体是**云端笔记本身**（卡片正文、标签、时间、字数），外加标签体系、
相关笔记、记忆与规范等参考文本。本模块把「读云」这一件事收敛到一处：
调 flomo 的只读工具取回数据，精简成前端可直接渲染的字段。

**已接出的只读能力**（与云端 MCP 暴露的只读工具一一对应，一个不少）：

| 能力 | 云端工具 | 方法 |
|------|----------|------|
| 列 / 搜卡片 | `memo_search` | `list_memos`（关键词、标签、起止日期、来源、是否含标签） |
| 单卡全文 | `memo_batch_get` | `memo_detail` |
| 批量全文 | `memo_batch_get` | `memo_batch`（单次上限 10 条） |
| 相关笔记 | `memo_recommended` | `recommended` |
| 今日回顾 | `get_daily_review` | `daily_review` |
| 标签树 | `tag_tree` | `tag_tree`（可按前缀 / 深度 / 条数收窄） |
| 标签名搜索 | `tag_search` | `tag_names` |
| 记忆文档 | `memory_context` | `memory_doc` |
| 用户画像 | `memory_user` | `user_profile` |
| 笔记格式规范 | `get_format_guide` | `format_guide` |
| 标签使用规范 | `get_tag_guide` | `tag_guide` |
| 工具清单 | `tools/list`（协议发现，非工具） | `tool_catalog` |

**写工具（`memo_create` / `memo_update` / `tag_rename`）不接**：写卡须走九步管线与
流程闸门（见 SKILL），控制台不代写。它们在 `tool_catalog` 里如实列出并标注未接入，
能力面貌完整，但没有任何可触发的写路径。

三条边界：

1. **只读白名单硬拦**。只允许调用 `READONLY_TOOLS` 中列出的工具，且该集合**恰等于**
   本模块各读方法实际调用的工具（一个不多、一个不少，回归用例按源码扫描锁定）；
   写工具与白名单外的任何工具名一律拒调用并抛错。
2. **不落盘**（H26）。卡片正文只在进程内存与 HTTP 响应之间传递，不写任何本地文件、
   不写缓存文件、不写访问日志。每次请求重新现采云端，与「云端状态现查」一致。
3. **token 不出进程**。token 仅在建立连接时使用，绝不进入任何返回值、响应体或日志；
   对外只暴露「可用 / 不可用」这一布尔事实。

用法：
    from console_cloud import CloudReader
    r = CloudReader()
    r.list_memos(keywords="标签树", limit=20)

退出：本模块只提供函数与类，不直接可执行。
"""
from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient, _result_memos, load_token  # noqa: E402

# 云端只读工具白名单：**恰好等于**本模块各读方法实际调用的工具，一个不多。
# 允许调用的即「明确有人用」的——权限面不预留空位；要加读能力，先加方法再加此处，
# 回归用例按源码扫描锁定这层对等关系（见 test_console_cloud.py）。
READONLY_TOOLS = frozenset(
    {
        "memo_search",
        "memo_batch_get",
        "memo_recommended",
        "get_daily_review",
        "tag_tree",
        "tag_search",
        "memory_context",
        "memory_user",
        "get_format_guide",
        "get_tag_guide",
    }
)

# 云端写工具：显式列出，用于给出更明确的中文拒绝理由（白名单之外的同样拒）。
# 控制台不接写路径——写卡须走九步管线与闸门；此集合仅用于「能力清单里如实标注」。
WRITE_TOOLS = frozenset({"memo_create", "memo_update", "tag_rename"})

# 写工具未接入的原因（出现在能力清单里，说明为什么置灰）。
WRITE_TOOLS_REASON = "写操作须走九步管线与流程闸门（见 SKILL），控制台不代写"

# `memo_search` 单次返回的云端上限：给再大也只会回这么多，界面须据此提示「还能继续筛」。
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
# `memo_batch_get` 单次上限（云端硬限 10 条）。
BATCH_MAX = 10
# `tag_tree` 的条数上限与默认值（云端 limit 最多 1000）。
TAG_MAX_LIMIT = 1000
TAG_DEFAULT_LIMIT = 200
# 摘要长度（字符）。列表只给摘要，全文由单卡接口现取。
EXCERPT_CHARS = 200

# 纯标签行：整行由一个或多个 `#标签` 组成（卡片首部的标签段）。
_TAG_LINE_RE = re.compile(r"^(?:#[^\s#]+)(?:\s+#[^\s#]+)*$")
_TAG_RE = re.compile(r"#[^\s#]+")
# 云端截断正文时插入的省略标记，摘要里压成单个省略号。
_OMIT_RE = re.compile(r"\.{2,}\[此处省略\s*\d+\s*字\]\.{2,}")
# 工具描述里截取一句「人话摘要」用（描述多在首句给用途）。
_SENT_END_RE = re.compile(r"[。；\n]")


class CloudError(RuntimeError):
    """云端访问失败（token 缺失、网络异常、工具报错、越权调用）。"""


def _clamp(limit, default: int, low: int, high: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(low, min(n, high))


def _clamp_limit(limit, default: int = DEFAULT_LIMIT) -> int:
    return _clamp(limit, default, 1, MAX_LIMIT)


def _tri_bool(value):
    """查询参数转三态开关：真 / 假 / 未指定（None 表示不传该条件）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def _structured(result) -> dict:
    """取工具返回的结构化数据（包裹结构见 H4）。"""
    if not isinstance(result, dict):
        return {}
    sc = result.get("structuredContent")
    return sc if isinstance(sc, dict) else {}


def _result_text(result) -> str:
    """取纯文本工具（指南 / 记忆 / 画像）的正文。"""
    content = _structured(result).get("content")
    if isinstance(content, str):
        return content
    for block in (result.get("content") or []):
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
        except Exception:  # noqa: BLE001 — 内层不是 JSON 时按原文返回
            return text
        if isinstance(inner, str):
            return inner
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            return inner["content"]
    return ""


def split_card(content: str):
    """拆卡片正文，返回 (标签列表, 概念名, 正文)。

    卡片格式：首部连续若干「纯标签行」，其后空行接概念名，再空行接正文。
    概念名取标签段之后的第一个非空行；不假设正文与标签之间恰好空一行。
    返回的正文不含概念名，供摘要渲染。
    """
    lines = (content or "").splitlines()
    tags: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if _TAG_LINE_RE.match(s):
            tags.extend(t.lstrip("#") for t in _TAG_RE.findall(s))
            i += 1
            continue
        break

    while i < len(lines) and not lines[i].strip():
        i += 1
    title = lines[i].strip() if i < len(lines) else ""
    body = "\n".join(lines[i + 1:]).strip()
    return tags, title, body


def _excerpt(body: str, limit: int = EXCERPT_CHARS) -> str:
    """正文摘要：压平省略标记与多余空行，截到 limit 字并加省略号。"""
    text = _OMIT_RE.sub(" … ", body or "")
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def slim_memo(m: dict, excerpt_chars: int = EXCERPT_CHARS) -> dict:
    """把云端原始 memo 精简为前端需要的字段（不含全文）。

    正文里 `content_truncated` 为真时说明云端只给了部分正文，界面须提示「点开看全文」。
    """
    content = m.get("content") or ""
    tags, title, body = split_card(content)
    tags = list(m.get("tags") or tags)
    return {
        "id": m.get("id") or "",
        "tags": tags,
        "title": title or "(无概念名)",
        "excerpt": _excerpt(body, excerpt_chars),
        "word_count": m.get("word_count"),
        "created_at": m.get("created_at") or "",
        "updated_at": m.get("updated_at") or "",
        "url": m.get("url") or "",
        "from": m.get("from") or "",
        "truncated": bool(m.get("content_truncated")),
        "has_image": bool(m.get("has_image")),
        "has_link": bool(m.get("has_link")),
    }


def full_memo(m: dict) -> dict:
    """单卡全文：精简字段 + 完整正文（`content`）+ 云端截断标记。"""
    out = slim_memo(m, excerpt_chars=0)
    out["content"] = m.get("content") or ""
    out["content_truncated"] = bool(m.get("content_truncated"))
    return out


def _tool_summary(desc: str) -> str:
    """工具描述取首句，作为能力清单里的一行摘要（首句太短则退回截断）。"""
    text = " ".join((desc or "").split())
    if not text:
        return ""
    m = _SENT_END_RE.search(text)
    if m and m.start() >= 4:
        return text[: m.start()]
    return text[:80]


def cloud_status() -> dict:
    """云端可用性（只报布尔与理由，**永不返回 token 本身**）。

    `max_limit` 一并给出：界面上的「单次上限」提示取自这里，不在前端另写一个常量。
    """
    try:
        _token, src = load_token()
    except SystemExit as e:
        return {"available": False, "reason": str(e), "source": "", "max_limit": MAX_LIMIT}
    return {"available": True, "reason": "凭证就位", "source": src, "max_limit": MAX_LIMIT}


class CloudReader:
    """云端只读访问器。连接懒建立，失败即重置以便下次重连。

    HTTP 服务是多线程的，连接与重置都在锁内进行，避免并发首请求把手握重复做两遍。

    `factory` 可注入一个具备 `init()` / `tool(name, args)` 的对象（回归用例用桩替换），
    注入时不再读取 token。
    """

    def __init__(self, factory=None):
        self._factory = factory
        self._client = None
        self._lock = threading.Lock()

    def _connect(self):
        with self._lock:
            if self._client is not None:
                return self._client
            if self._factory is not None:
                client = self._factory()
            else:
                try:
                    token, _src = load_token()
                except SystemExit as e:
                    raise CloudError(str(e)) from None
                client = FlomoClient(token)
            client.init()
            self._client = client
            return client

    def _reset(self):
        with self._lock:
            self._client = None

    def call(self, name: str, arguments: dict | None = None):
        """调一个云端工具，越权与失败一律抛 `CloudError`。"""
        if name not in READONLY_TOOLS:
            kind = "写工具" if name in WRITE_TOOLS else "白名单外工具"
            raise CloudError(f"控制台为只读，拒绝调用{kind}：{name}")
        try:
            return self._connect().tool(name, arguments or {})
        except CloudError:
            raise
        except Exception as e:  # noqa: BLE001 — 网络/协议异常统一转 CloudError
            # 会话可能已失效：丢弃连接，下次请求重连
            self._reset()
            raise CloudError(f"云端调用失败（{name}）：{type(e).__name__}: {e}") from None

    # ---- 卡片 ----------------------------------------------------------- #
    def list_memos(
        self,
        keywords=None,
        tag=None,
        start_date=None,
        end_date=None,
        source=None,
        has_tag=None,
        limit=DEFAULT_LIMIT,
    ) -> dict:
        """列 / 搜卡片：无参即最近若干条，其余条件为可选收窄。

        `tag` 须是**完整标签路径**（如 `AI/决策模型`）——云端不接受裸顶层名。
        `source` 对应云端的 `from`（渠道，如 `ai`）。`has_tag` 为三态：真 / 假 / 不传。
        """
        n = _clamp_limit(limit)
        args: dict = {"limit": n}
        if keywords:
            args["keywords"] = keywords
        if tag:
            args["tag"] = tag
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
        if source:
            args["from"] = source
        tri = _tri_bool(has_tag)
        if tri is not None:
            args["has_tag"] = tri
        memos = _result_memos(self.call("memo_search", args))
        return {
            "count": len(memos),
            "limit": n,
            "capped": len(memos) >= MAX_LIMIT,
            "keywords": keywords or "",
            "tag": tag or "",
            "start_date": start_date or "",
            "end_date": end_date or "",
            "source": source or "",
            "has_tag": tri,
            "memos": [slim_memo(m) for m in memos],
        }

    def memo_detail(self, memo_id: str) -> dict:
        """按 id 取单卡全文（列表里的正文可能被云端截断，全文走这里）。"""
        if not memo_id:
            raise CloudError("缺少 memo id")
        result = self.call("memo_batch_get", {"ids": [memo_id]})
        memos = _result_memos(result)
        if not memos:
            raise CloudError(f"未取到 memo：{memo_id}")
        return {"memo": full_memo(memos[0]), "omitted_ids": list(_structured(result).get("omitted_ids") or [])}

    def memo_batch(self, ids) -> dict:
        """批量取全文（多选对比用）。云端单次上限 10 条，超出部分忽略。"""
        clean = [i for i in (ids or []) if i and str(i).strip()][:BATCH_MAX]
        if not clean:
            raise CloudError("缺少 memo id")
        result = self.call("memo_batch_get", {"ids": clean})
        memos = _result_memos(result)
        return {
            "count": len(memos),
            "requested": len(clean),
            "memos": [full_memo(m) for m in memos],
            "omitted_ids": list(_structured(result).get("omitted_ids") or []),
        }

    def recommended(self, memo_id: str, limit: int = 10, no_same_tag=None) -> dict:
        """相关笔记：与指定卡片内容相近的其他卡片。"""
        if not memo_id:
            raise CloudError("缺少 memo id")
        args: dict = {"id": memo_id, "limit": _clamp_limit(limit, 10)}
        tri = _tri_bool(no_same_tag)
        if tri is not None:
            args["no_same_tag"] = tri
        memos = _result_memos(self.call("memo_recommended", args))
        return {"id": memo_id, "count": len(memos), "memos": [slim_memo(m) for m in memos]}

    def daily_review(self) -> dict:
        """今日回顾：云端挑选的历史卡片，与列表同构。"""
        memos = _result_memos(self.call("get_daily_review", {}))
        return {"count": len(memos), "memos": [slim_memo(m) for m in memos]}

    # ---- 标签 ----------------------------------------------------------- #
    def tag_tree(self, prefix: str | None = None, depth=None, limit=TAG_DEFAULT_LIMIT) -> dict:
        """云端标签树（可按前缀 / 深度 / 条数收窄）。

        与本地快照（`tag_tree.txt`）的区别：这里是**现采**，不依赖快照是否最新；
        条数上限 1000，到顶时云端会给出提示，界面须如实转达。
        """
        n = _clamp(limit, TAG_DEFAULT_LIMIT, 1, TAG_MAX_LIMIT)
        args: dict = {"limit": n}
        if prefix:
            args["prefix"] = prefix
        if depth:
            args["depth"] = _clamp(depth, 1, 1, 10)
        sc = _structured(self.call("tag_tree", args))
        tags = [t for t in (sc.get("tags") or []) if isinstance(t, str)]
        return {
            "tags": tags,
            "total": sc.get("total"),
            "returned": sc.get("returned", len(tags)),
            "limit": sc.get("limit", n),
            "truncated": bool(sc.get("truncated")),
            "hint": sc.get("hint") or "",
        }

    def tag_names(self, keywords: str, limit: int = 20) -> dict:
        """按关键词搜标签名（标签多时比拉全树轻）。"""
        if not keywords:
            return {"tags": []}
        sc = _structured(self.call("tag_search", {"keywords": keywords, "limit": _clamp(limit, 20, 1, TAG_MAX_LIMIT)}))
        names = [t.get("name") for t in (sc.get("tags") or []) if isinstance(t, dict) and t.get("name")]
        return {"keywords": keywords, "count": len(names), "tags": names}

    # ---- 参考文本（记忆 / 画像 / 指南） ---------------------------------- #
    def memory_doc(self) -> dict:
        """记忆文档（memory.md）：当前的工作、兴趣、项目、目标等动态信息。"""
        return {"doc": "memory", "content": _result_text(self.call("memory_context", {}))}

    def user_profile(self) -> dict:
        """用户画像（user.md）：身份、性格、价值观、偏好等稳定信息。"""
        return {"doc": "user", "content": _result_text(self.call("memory_user", {}))}

    def format_guide(self) -> dict:
        """笔记格式规范（flomo 支持的标记与写法）。"""
        return {"doc": "format", "content": _result_text(self.call("get_format_guide", {}))}

    def tag_guide(self) -> dict:
        """标签使用规范（命名约定与层级组织建议）。"""
        return {"doc": "tag", "content": _result_text(self.call("get_tag_guide", {}))}

    def reference(self) -> dict:
        """四份参考文本一次拉齐（记忆 / 画像 / 格式规范 / 标签规范），供「参考」视图。"""
        return {
            "memory": self.memory_doc(),
            "user": self.user_profile(),
            "format": self.format_guide(),
            "tag": self.tag_guide(),
        }

    # ---- 能力清单 ------------------------------------------------------- #
    def tool_catalog(self) -> dict:
        """云端 MCP 暴露的全部工具，并标注本控制台是否已接出。

        走 `tools/list` 协议发现（只读元信息，不读任何卡片数据）：能力面貌以云端
        实际暴露为准，不靠本地写死的名单，避免与云端漂移。写工具如实列出但标注
        未接入及其原因——控制台没有任何可触发的写路径。
        """
        try:
            raw = self._connect().tools_list()
        except Exception as e:  # noqa: BLE001 — 统一转 CloudError，交给上层降级
            self._reset()
            raise CloudError(f"取工具清单失败：{type(e).__name__}: {e}") from None

        tools = []
        for t in (raw.get("tools") or []):
            if not isinstance(t, dict) or not t.get("name"):
                continue
            name = t["name"]
            is_write = name in WRITE_TOOLS
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            tools.append(
                {
                    "name": name,
                    "kind": "write" if is_write else "read",
                    "wired": not is_write,
                    "summary": _tool_summary(t.get("description") or ""),
                    "args": list(props.keys()),
                    "reason": WRITE_TOOLS_REASON if is_write else "",
                }
            )
        tools.sort(key=lambda x: (x["kind"] != "read", x["name"]))
        return {
            "count": len(tools),
            "read": sum(1 for t in tools if t["kind"] == "read"),
            "write": sum(1 for t in tools if t["kind"] == "write"),
            "wired": sum(1 for t in tools if t["wired"]),
            "tools": tools,
        }
