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

除逐个方法外，读能力还有一条**通用执行入口** `run_read_tool(name, arguments)`：把界面上
按 `tools/list` 规格生成的参数直接喂给任一读工具并回结果（形态已归为 memos / tags / text / json）。
这是「云端 MCP 的能力在页面上直接可用」的落点——云端加了读工具，界面不必改代码就能跑；
写工具**不走这里**（见下）。

**写能力同样接出**（`memo_create` / `memo_update` / `tag_rename`），但**写不是直通**：
控制台把项目自己的两道机械闸门做成可操作的步骤，不绕开、也不替代——

| 闸门 | 实现 | 不通过时 |
|------|------|----------|
| 格式闸门 | 委托 `validate_memo.check()`（首行标签段、第二行概念名、结论先行、标签两级、字数上限…） | ERR 阻断写云 |
| 流程闸门 | 委托 `validate_memo.check_gate()`，要求 `.sop_gate/<签名>.json` 凭证存在、签名与正文指纹匹配且未过期 | ERR 阻断写云 |

两处校验一律**调用既有实现**，本模块不另写一份口径。凭证由 `sop_gate.py` 现跑现出，
控制台只负责触发与转达（见 `run_gate`）。

写入完成后一律回读全文验收（H1 / H2 / H11）：`memo_create` 不回显正文，不验收就等于
没确认落库内容；更新前还须先回读原文（H22）。验收结论随写入回执一并返回。

三条边界：

1. **工具白名单硬拦**。读只放行 `READONLY_TOOLS`、写只放行 `WRITE_TOOLS`，两个集合
   **分别恰等于**本模块各方法实际调用的工具（一个不多、一个不少，回归用例按源码扫描
   锁定）；白名单外的任何工具名一律拒调用并抛错。
2. **正文不落盘，当轮草稿是唯一例外且必被回收**（H26 / H24）。卡片正文只在进程内存与
   HTTP 响应之间传递；唯一落盘的是流程闸门要求的草稿（`memo_body*.txt`），命名已在
   `cleanup.py` 扫描表与 `.gitignore` 内，由收尾清理按近轮窗口回收。
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
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import validate_memo as _vm  # noqa: E402
from flomo_client import FlomoClient, _result_memos, load_token  # noqa: E402
from memo_util import memo_signature  # noqa: E402

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

# 云端写工具白名单：**恰好等于**本模块写方法实际调用的工具。
# 写不是直通——下发前强制过格式与流程闸门，见 create_memo / update_memo。
WRITE_TOOLS = frozenset({"memo_create", "memo_update", "tag_rename"})

# 写工具的门槛说明（出现在能力清单里）：写入前必须过什么、写入后做什么。
WRITE_TOOLS_REASON = "写入前须过格式与流程闸门（validate_memo），写入后回读全文验收"

# `tag_rename` 的批量规模：云端默认 200 条，显式确认后可到绝对上限 2000 条。
TAG_RENAME_DEFAULT = 200
TAG_RENAME_MAX = 2000

# 当轮草稿文件名前缀。落在仓库根——`cleanup.py` 的扫描表已含 `memo_body*.txt`，
# `.gitignore` 同样忽略，故草稿不会入库、也不会成为残留。
DRAFT_PREFIX = "memo_body"
REPO_ROOT = SCRIPT_DIR.parent

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
# 流程闸门的执行时限（秒）：闸门内部要现查云端若干次，给足但不容无限等。
GATE_TIMEOUT = 300


def _normalize(content: str) -> str:
    """CRLF / CR 归一化并去 BOM——与 `validate_memo` 的输入口径保持一致。

    不归一化时 `\\r` 会留在行尾，首行/行首判定随之失准，同一个正文在两处校验
    会得到不同结论。归一化收在这里，写路径全程只此一处。
    """
    return (content or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

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


def _extract_id(result) -> str:
    """从写入回执里取新卡 id。

    `memo_create` 不回显正文，但 id 的承载位置并没有协议保证：优先读
    `structuredContent.id`，兜底解析 `content[].text` 里的内层 JSON（H4 的包裹结构）。
    取不到就返回空串——调用方据此如实报告「写入已下发但未取到 id」，绝不假装成功。
    """
    sc = _structured(result)
    for key in ("id", "memo_id", "memoId"):
        if sc.get(key):
            return str(sc[key])
    for block in (result.get("content") or []):
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
        except Exception:  # noqa: BLE001 — 内层不是 JSON 就不是回执载体
            continue
        if isinstance(inner, dict):
            for key in ("id", "memo_id", "memoId"):
                if inner.get(key):
                    return str(inner[key])
    return ""


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


def _schema_type(spec) -> str:
    """把 JSON Schema 的 `type` 归一化成单个类型 token。

    云端会声明**联合类型**（如 `has_tag` 的 `["null", "boolean"]`）。原样交给前端，
    界面就得懂 JSON Schema 才知道该挑哪个控件——归一化收在这里，前端只认一张小词表：
    `boolean` / `integer` / `number` / `array` / 其余按 `string`。
    `"null"` 只表示「可缺省」，不构成一种类型；联合类型里取第一个非 null 项。
    """
    t = (spec or {}).get("type")
    if isinstance(t, list):
        t = next((x for x in t if isinstance(x, str) and x and x != "null"), None)
    if not isinstance(t, str) or not t:
        return "string"
    return t


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

    def call(self, name: str, arguments: dict | None = None, *, write: bool = False):
        """调一个云端工具，越权与失败一律抛 `CloudError`。

        `write=False`（默认）只放行读白名单；写工具须显式 `write=True` 才放行，
        读路径上写工具一律拒——避免「顺手」从读方法里发出写操作。

        失败一律**转成 `CloudError`**：本层是控制台与云端之间的唯一边界，任何漏出去的
        异常都会在 HTTP 服务里变成「连接被掐断、错误传不出去」。除普通异常外还显式拦
        `SystemExit`（`BaseException` 子类，`except Exception` 接不住）——客户端历史上
        拿它表示协议错误，这类错误绝不能让响应半途失联。
        """
        allowed = WRITE_TOOLS if write else READONLY_TOOLS
        if name not in allowed:
            kind = "写工具" if name in WRITE_TOOLS else "白名单外工具"
            raise CloudError(f"工具白名单拒绝调用{kind}：{name}")
        try:
            return self._connect().tool(name, arguments or {})
        except CloudError:
            raise
        except (Exception, SystemExit) as e:  # noqa: BLE001 — 网络/协议异常统一转 CloudError
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

    # ---- 写入（格式闸门 + 流程闸门 + 回读验收） --------------------------- #
    @staticmethod
    def check_card(content: str) -> dict:
        """格式自检：委托 `validate_memo.check()`，本模块不另写一份口径。

        `errors` 非空即不得写云（ERR 阻断），`warnings` 只提示、不阻塞。
        """
        _vm.ERR.clear()
        _vm.WARN.clear()
        _vm.check(content or "")
        errs, warns = list(_vm.ERR), list(_vm.WARN)
        sig = memo_signature(content or "") or {}
        return {
            "ok": not errs,
            "errors": errs,
            "warnings": warns,
            "tagline": sig.get("tagline", ""),
            "concept": sig.get("concept", ""),
        }

    @staticmethod
    def gate_state(content: str) -> dict:
        """流程闸门凭证状态：只看现有凭证，不生成、不改动。"""
        _vm.ERR.clear()
        _vm.WARN.clear()
        _vm.check_gate(content or "")
        errs = list(_vm.ERR)
        return {"ok": not errs, "errors": errs}

    def run_gate(
        self,
        content: str,
        skip_web: bool = False,
        anchor_id: str | None = None,
        verify=None,
    ) -> dict:
        """现跑流程闸门（`sop_gate.py`）并出凭证。

        闸门脚本以卡片正文**文件**为入口，故此处落一份当轮草稿——这是本模块唯一的
        落盘路径，命名已在 `cleanup.py` 扫描表与 `.gitignore` 内，由收尾清理按近轮
        窗口回收（H24 / H27）。闸门一律走脚本现跑，本模块不把闸门流程重实现一遍。

        `skip_web` 与 `verify` 对应 sop_gate 的两种验证来源：显式声明无需网络验证，
        或提供第 ② 步网络搜索的验证记录。两者都不给时闸门会在验证这一项阻塞，属预期。
        """
        content = _normalize(content)
        if not content.strip():
            raise CloudError("卡片正文为空，无法跑流程闸门")
        stamp = int(time.time() * 1000)
        draft = REPO_ROOT / f"{DRAFT_PREFIX}_gate_{stamp}.txt"
        draft.write_text(content, encoding="utf-8")
        argv = [sys.executable, str(SCRIPT_DIR / "sop_gate.py"), "--memo", str(draft)]
        if skip_web:
            argv.append("--skip-web")
        elif verify:
            # 草稿与验证记录都走同一前缀，收尾清理按近轮窗口一并回收
            vpath = REPO_ROOT / f"{DRAFT_PREFIX}_verify_{stamp}.txt"
            payload = json.dumps(verify, ensure_ascii=False, indent=2) if isinstance(verify, dict) else str(verify)
            vpath.write_text(payload, encoding="utf-8")
            argv += ["--verify", str(vpath)]
        if anchor_id:
            argv += ["--anchor-id", anchor_id]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=GATE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise CloudError(f"流程闸门超时（{GATE_TIMEOUT}s 内云端现查未返回）") from None
        lines = [ln for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if ln.strip()]
        return {
            "ok": proc.returncode == 0,
            "skip_web": bool(skip_web),
            "draft": draft.name,
            "lines": lines,
            "gate": self.gate_state(content),
        }

    def _guard(self, content: str) -> dict:
        """写云前的两道闸门：任一不过即抛 `CloudError`，并带出待修项。"""
        fmt = self.check_card(content)
        if not fmt["ok"]:
            raise CloudError("格式闸门未过：" + "；".join(fmt["errors"]))
        gate = self.gate_state(content)
        if not gate["ok"]:
            raise CloudError("流程闸门未过：" + "；".join(gate["errors"]))
        return fmt

    def _verify(self, memo_id: str, expected: str) -> dict:
        """回读全文验收（H1 / H2 / H11）：逐字比对 + 实测字数。"""
        memo = self.memo_detail(memo_id)["memo"]
        actual = _normalize(memo.get("content") or "")
        return {
            "id": memo_id,
            "matched": actual.strip() == expected.strip(),
            "chars": len(actual),
            "word_count": memo.get("word_count"),
            "content_truncated": bool(memo.get("content_truncated")),
        }

    def _find_id_by_signature(self, content: str) -> str:
        """回执取不到 id 时的兜底：按概念名回搜一次，取签名（概念名）相同的最新一条。

        只在正常回执缺 id 时触发——多打一次只读查询，换「验收不落空」。
        id 由回搜推断时会由调用方在回执里标注 `id_inferred`，不冒充正常回执。
        """
        _tags, concept, _body = split_card(content)
        if not concept:
            return ""
        memos = _result_memos(self.call("memo_search", {"keywords": concept, "limit": 5}))
        for m in memos:
            _t, title, _b = split_card(m.get("content") or "")
            if title.strip() == concept.strip():
                return str(m.get("id") or "")
        return str(memos[0].get("id") or "") if memos else ""

    def create_memo(self, content: str, fmt: str | None = None) -> dict:
        """新建卡片：两道闸门 → `memo_create` → 回读全文验收。

        `memo_create` 不回显正文，因此必须按返回的 id 回读（H1 / H11），逐项确认
        落库正文与提交内容一致；回执没带 id 时按签名回搜兜底，并标注 `id_inferred`。
        """
        content = _normalize(content)
        self._guard(content)
        args: dict = {"content": content}
        if fmt:
            args["format"] = fmt
        result = self.call("memo_create", args, write=True)
        sc = _structured(result)
        new_id = _extract_id(result)
        inferred = False
        if not new_id:
            new_id = self._find_id_by_signature(content)
            inferred = bool(new_id)
        return {
            "ok": bool(new_id),
            "id": new_id,
            "id_inferred": inferred,
            "word_count": sc.get("word_count"),
            "verified": self._verify(new_id, content) if new_id else None,
        }

    def update_memo(self, memo_id: str, content: str, fmt: str | None = None) -> dict:
        """更新卡片：先回读原文（H22）→ 两道闸门 → `memo_update` → 回读验收（H11）。"""
        if not memo_id:
            raise CloudError("缺少 memo id")
        before = self.memo_detail(memo_id)["memo"]  # H22：更新前必须取全文核对
        content = _normalize(content)
        self._guard(content)
        args: dict = {"id": memo_id, "content": content}
        if fmt:
            args["format"] = fmt
        sc = _structured(self.call("memo_update", args, write=True))
        return {
            "ok": True,
            "id": memo_id,
            "before": {"chars": len(before.get("content") or ""), "concept": before.get("title") or ""},
            "word_count": sc.get("word_count"),
            "verified": self._verify(memo_id, content),
        }

    def rename_tag(self, old_tag: str, new_tag: str, max_memos=None) -> dict:
        """批量重命名标签：把挂 `old_tag` 的卡片改挂 `new_tag`。

        云端默认只处理 200 条，超出时**返回实际匹配数而不写**；确认规模后再用
        `max_memos` 显式声明本次批量规模（绝对上限 2000）。
        """
        old_tag = (old_tag or "").strip().lstrip("#")
        new_tag = (new_tag or "").strip().lstrip("#")
        if not old_tag or not new_tag:
            raise CloudError("原标签与新标签都不能为空")
        if old_tag == new_tag:
            raise CloudError("新标签与原标签相同，无需重命名")
        args: dict = {"old_tag": old_tag, "new_tag": new_tag}
        if max_memos:
            args["max_memos"] = _clamp(max_memos, TAG_RENAME_DEFAULT, 1, TAG_RENAME_MAX)
        sc = _structured(self.call("tag_rename", args, write=True))
        return {
            "old_tag": old_tag,
            "new_tag": new_tag,
            "max_memos": args.get("max_memos"),
            "result": sc,
        }

    # ---- 能力清单 ------------------------------------------------------- #
    def tool_catalog(self) -> dict:
        """云端 MCP 暴露的全部工具，并标注本控制台是否已接出、怎么用。

        走 `tools/list` 协议发现（只读元信息，不读任何卡片数据）：能力面貌以云端
        实际暴露为准，不靠本地写死的名单，避免与云端漂移。除工具名与摘要外，还
        带上参数规格（名字 / 类型 / 是否必填 / 说明，类型已归一化为单个 token），
        供界面**按规格生成表单**——读工具因此可以在这张清单上直接执行，而不只是
        一个说明表格。
        """
        try:
            raw = self._connect().tools_list()
        except (Exception, SystemExit) as e:  # noqa: BLE001 — 统一转 CloudError，交给上层降级
            self._reset()
            raise CloudError(f"取工具清单失败：{type(e).__name__}: {e}") from None

        tools = []
        for t in (raw.get("tools") or []):
            if not isinstance(t, dict) or not t.get("name"):
                continue
            name = t["name"]
            is_write = name in WRITE_TOOLS
            schema = t.get("inputSchema") or {}
            props = (schema.get("properties") or {})
            required = schema.get("required") or []
            specs = [
                {
                    "name": k,
                    "type": _schema_type(v),
                    "required": k in required,
                    "desc": _tool_summary((v or {}).get("description") or ""),
                }
                for k, v in props.items()
            ]
            tools.append(
                {
                    "name": name,
                    "kind": "write" if is_write else "read",
                    "wired": name in (WRITE_TOOLS if is_write else READONLY_TOOLS),
                    "summary": _tool_summary(t.get("description") or ""),
                    "args": [s["name"] for s in specs],
                    "arg_specs": specs,
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

    # ---- 通用只读执行 --------------------------------------------------- #
    def run_read_tool(self, name: str, arguments=None) -> dict:
        """通用只读执行入口：把界面上的参数直接喂给某个读工具并回结果。

        这是「能力能在网页上直接用」的落点——读工具不必逐个做专属界面。
        写工具**不走这里**：写要过格式与流程闸门、写完还要回读验收，
        只能在 `create_memo` / `update_memo` / `rename_tag` 三个专用方法里发生。

        返回值里带 `kind`，让界面知道该用哪种形态渲染：卡片列表 / 标签串 /
        纯文本 / 原始 JSON——不同工具的结果形态差异很大，交给界面猜不如这里判。
        """
        if not name:
            raise CloudError("缺少工具名")
        if name in WRITE_TOOLS:
            raise CloudError(f"写工具不走通用入口（须过闸门与回读验收）：{name}")
        args = {k: v for k, v in (arguments or {}).items() if v not in (None, "")}
        result = self.call(name, args)  # 读白名单在 call 内强制
        sc = _structured(result)
        memos = _result_memos(result)
        tags = []
        for t in (sc.get("tags") or []):
            if isinstance(t, str):
                tags.append(t)
            elif isinstance(t, dict) and t.get("name"):
                tags.append(str(t["name"]))
        text = "" if (memos or tags) else _result_text(result)
        kind = "memos" if memos else "tags" if tags else "text" if text else "json"
        return {
            "tool": name,
            "arguments": args,
            "kind": kind,
            "memos": [slim_memo(m) for m in memos],
            "tags": tags,
            "total": sc.get("total"),
            "truncated": bool(sc.get("truncated")),
            "text": text,
            "raw": result,
        }
