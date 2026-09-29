#!/usr/bin/env python3
"""console_cloud.py — 控制台的云端笔记只读访问层。

控制台要看的主体是**云端笔记本身**（卡片正文、标签、时间、字数），仓库文档的
元信息只是辅助。本模块把「读云」这一件事收敛到一处：调 flomo 的只读工具取回
卡片，精简成前端可直接渲染的字段。

三条边界：

1. **只读白名单硬拦**。只允许调用 `READONLY_TOOLS` 中列出的工具；写工具
   （`memo_create` / `memo_update` / `tag_rename`）与任何白名单外的工具名一律
   拒调用并抛错——控制台在结构上不可能误触写云。
2. **不落盘**（H26）。卡片正文只在进程内存与 HTTP 响应之间传递，不写任何本地
   文件、不写缓存文件、不写访问日志。每次请求重新现采云端，与「云端状态现查」一致。
3. **token 不出进程**。token 仅在建立连接时使用，绝不进入任何返回值、响应体或
   日志；对外只暴露「可用 / 不可用」这一布尔事实。

用法：
    from console_cloud import CloudReader
    r = CloudReader()
    r.list_memos(keywords="标签树", limit=20)

退出：本模块只提供函数与类，不直接可执行。
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient, _result_memos, load_token  # noqa: E402

# 云端只读工具白名单：控制台能调用的全部工具，一个不多。
READONLY_TOOLS = frozenset(
    {
        "memo_search",
        "memo_batch_get",
        "memo_recommended",
        "tag_tree",
        "tag_search",
        "get_daily_review",
        "get_format_guide",
        "get_tag_guide",
        "memory_context",
    }
)

# 云端写工具：显式列出，用于给出更明确的中文拒绝理由（白名单之外的同样拒）。
WRITE_TOOLS = frozenset({"memo_create", "memo_update", "tag_rename"})

# `memo_search` 单次返回的云端上限：给再大也只会回这么多，界面须据此提示「还能继续筛」。
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
# 摘要长度（字符）。列表只给摘要，全文由单卡接口现取。
EXCERPT_CHARS = 200

# 纯标签行：整行由一个或多个 `#标签` 组成（卡片首部的标签段）。
_TAG_LINE_RE = re.compile(r"^(?:#[^\s#]+)(?:\s+#[^\s#]+)*$")
_TAG_RE = re.compile(r"#[^\s#]+")
# 云端截断正文时插入的省略标记，摘要里压成单个省略号。
_OMIT_RE = re.compile(r"\.{2,}\[此处省略\s*\d+\s*字\]\.{2,}")


class CloudError(RuntimeError):
    """云端访问失败（token 缺失、网络异常、工具报错、越权调用）。"""


def _clamp_limit(limit, default: int = DEFAULT_LIMIT) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_LIMIT))


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
        "truncated": bool(m.get("content_truncated")),
        "has_image": bool(m.get("has_image")),
        "has_link": bool(m.get("has_link")),
    }


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

    # ---- 只读能力 ------------------------------------------------------- #
    def list_memos(self, keywords=None, tag=None, limit=DEFAULT_LIMIT) -> dict:
        """列卡片：无参即最近若干条；给 keywords 走关键词检索；给 tag 走标签检索。

        `tag` 须是**完整标签路径**（如 `AI/决策模型`）——云端不接受裸顶层名。
        """
        n = _clamp_limit(limit)
        args: dict = {"limit": n}
        if keywords:
            args["keywords"] = keywords
        if tag:
            args["tag"] = tag
        memos = _result_memos(self.call("memo_search", args))
        return {
            "count": len(memos),
            "limit": n,
            "capped": len(memos) >= MAX_LIMIT,
            "keywords": keywords or "",
            "tag": tag or "",
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
        m = memos[0]
        out = slim_memo(m, excerpt_chars=0)
        out["content"] = m.get("content") or ""
        out["content_truncated"] = bool(m.get("content_truncated"))
        out["omitted_ids"] = list(
            (result.get("structuredContent") or {}).get("omitted_ids") or []
        )
        return {"memo": out}

    def daily_review(self) -> dict:
        """今日回顾：云端挑选的历史卡片，与列表同构。"""
        memos = _result_memos(self.call("get_daily_review", {}))
        return {"count": len(memos), "memos": [slim_memo(m) for m in memos]}

    def tag_names(self, keywords: str, limit: int = 20) -> dict:
        """按关键词搜标签名（轻量，比拉全树省）。"""
        if not keywords:
            return {"tags": []}
        result = self.call("tag_search", {"keywords": keywords, "limit": _clamp_limit(limit)})
        sc = result.get("structuredContent") or {}
        names = [t.get("name") for t in (sc.get("tags") or []) if t.get("name")]
        return {"tags": names}
