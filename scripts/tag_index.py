#!/usr/bin/env python3
"""tag_index.py — 按顶层前缀现采标签，不取全量树。

为什么要有它：`tag_tree` 封顶 1000 条（`limit` 有效，不传默认 200、超过 1000 被夹到
1000），而 `total` 报真实值 1025，于是「取全量再比对」在标签过千后永远取不全。此前两个脚本
（`tagcheck`、`sop_gate`）都走「拉一次全树 + 发现截断再逐名 `tag_search` 补查」，
补查只能补回本卡那几个簇，兄弟清单仍是残缺的，还容易把「被截掉」误报成「没有」。

实测该工具**认 `prefix`**（按顶层收窄）：`{"prefix":"教育"}` → 17/17、
`{"prefix":"政策"}` → 64/64、`{"prefix":"AI"}` → 93/93，都是完整返回。也就是说
取不到的只有「不分范围地一次拉完」，切开就都取得全。

于是判定路改成按需取：一张卡通常只有 1–3 个顶层，逐个 `prefix` 取，既完整又不撞上限。
「全量」只在巡检与模糊改名时才有意义，那时逐顶层拼（顶层几十个，可接受）。

`depth` 参数实测无效（`{"depth":1}` 返回空树，顶层不单独成条），别试。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from flomo_client import tool_data  # noqa: E402

DEFAULT_LIMIT = 1000
SEARCH_LIMIT = 50

# 全角斜杠也是分隔符：写成 `AI／Agent` 时若只按半角切，会把整串当成顶层，
# 于是 prefix 取回空、写法重复也查不出来（实测服务端把两者当两个簇）。
_SLASH_RE = re.compile(r"[/／]")


def _belongs(name: str, top: str) -> bool:
    """标签名是否属于该顶层。

    prefix 的服务端语义未在文档里写明，实测按顶层过滤（且大小写敏感）；
    这里再本地过一遍，防止宽松前缀（如 `AI` 命中 `AIxyz/…`）把别的顶层混进兄弟清单。
    """
    return name == top or name.startswith(top + "/")


@dataclass
class TopSlice:
    """一个顶层下的采集结果。

    `total` 为 None 表示回包没给总数（无法判断是否取全，按未知处理，不假装完整）；
    `truncated` 只在能确定「返回条数 < 服务端总数」时为 True。
    """

    top: str
    names: list[str] = field(default_factory=list)
    total: int | None = None
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.truncated and self.total is not None


def fetch_top(client, top: str, limit: int = DEFAULT_LIMIT) -> TopSlice:
    """取一个顶层下的全部标签路径。"""
    try:
        sc = tool_data(client.tool("tag_tree", {"prefix": top, "limit": limit}))
    except Exception as exc:  # 读不到≠空簇，交给调用方按 error 判
        return TopSlice(top=top, error=str(exc))
    raw = [str(t) for t in (sc.get("tags") or [])]
    total = sc.get("total")
    total = int(total) if total is not None else None
    # 截断按「服务端返回条数 vs 服务端总数」判，再本地过滤：过滤掉的属于别的顶层，
    # 若先过滤再比，宽松前缀会让每次都被误判成截断。
    truncated = total is not None and len(raw) < total
    return TopSlice(
        top=top,
        names=[t for t in raw if _belongs(t, top)],
        total=total,
        truncated=truncated,
    )


def search_names(client, keyword: str, limit: int = SEARCH_LIMIT) -> list[str]:
    """按关键词搜标签名（子串匹配），返回命中的标签路径。

    只用来做两件事：跨顶层查同名写法、以及某个顶层取不到时逐名兜底。
    服务端单次上限 50 条，宽泛词只能取下界——不要拿它当全量枚举。
    """
    try:
        sc = tool_data(client.tool("tag_search", {"keywords": keyword, "limit": limit}))
    except Exception:
        return []
    out: list[str] = []
    for t in (sc.get("tags") or []):
        name = t.get("name") if isinstance(t, dict) else str(t)
        if name:
            out.append(str(name))
    return out


@dataclass
class TagIndex:
    """一次核对/查重需要的标签集合，按顶层拼出来。"""

    slices: dict[str, TopSlice] = field(default_factory=dict)
    extra: list[str] = field(default_factory=list)  # 跨顶层同名补查到的

    def names(self) -> list[str]:
        seen: list[str] = []
        for sl in self.slices.values():
            for n in sl.names:
                if n not in seen:
                    seen.append(n)
        for n in self.extra:
            if n not in seen:
                seen.append(n)
        return seen

    def siblings_of(self, top: str) -> list[str]:
        return [n for n in self.names() if n.startswith(top + "/") and n.count("/") == 1]

    def problems(self) -> list[str]:
        """取不完整 / 取不到的顶层，供调用方据实说明覆盖范围。"""
        out = []
        for top, sl in self.slices.items():
            if sl.error:
                out.append(f"顶层「{top}」取不到（{sl.error}）——该簇的兄弟清单与存在性不可信")
            elif sl.truncated:
                out.append(
                    f"顶层「{top}」被服务端截断（返回 {len(sl.names)} / total {sl.total}）"
                    "——该簇的兄弟清单可能不全"
                )
            elif sl.total is None:
                out.append(f"顶层「{top}」回包缺 total，无法确认是否取全")
        return out


def collect(client, tops: list[str], leaf_names: list[str] = ()) -> TagIndex:
    """按顶层取标签；`leaf_names` 给的二级名各做一次跨顶层同名补查。

    跨顶层补查是 H18 的一条实证判据（`金融/宏观审慎` 撞过 `宏观/宏观审慎`）：
    同名的二级挂在两个顶层下是合法的，但只差写法的同名会分裂，必须能看见。
    只按顶层取时看不见别的顶层，所以对每个二级名单独搜一次。

    **prefix 大小写敏感、tag_search 不敏感**（实测：`{"prefix":"ai"}` 回 0 条，
    而搜 `ai` 能命中 `AI/…`）。所以 prefix 取回空时不能直接判「该顶层不存在」——
    用不敏感的 tag_search 再确认一次，否则 `#ai/新二级` 会被报成「顶层也不存在，
    须连同顶层一起新建」，等于把已有的簇连顶层重开一遍。

    反方向也一样：全角斜杠写成 `AI／Agent` 时 prefix 取不到任何东西，靠二级名
    `Agent` 的补查才能看见库里的 `AI/Agent`，进而判出「只差写法」。
    """
    idx = TagIndex()
    for top in tops:
        if top and top not in idx.slices:
            sl = fetch_top(client, top)
            idx.slices[top] = sl
            if not sl.names and not sl.error:
                for hit in search_names(client, top):
                    if hit not in idx.extra:
                        idx.extra.append(hit)
    for name in leaf_names:
        for hit in search_names(client, name):
            if hit not in idx.extra:
                idx.extra.append(hit)
    return idx


def split_tag(tag: str) -> tuple[str, str]:
    """`顶层/二级` → (顶层, 二级)；全角斜杠同样按分隔符处理。"""
    parts = _SLASH_RE.split(tag)
    if len(parts) < 2:
        return tag.strip(), ""
    return parts[0].strip(), "/".join(parts[1:]).strip()


def tops_of(tags) -> list[str]:
    out: list[str] = []
    for t in tags:
        top, _leaf = split_tag(t)
        if top and top not in out:
            out.append(top)
    return out


def leaf_names_of(tags) -> list[str]:
    out: list[str] = []
    for t in tags:
        _top, leaf = split_tag(t)
        if leaf and leaf not in out and "/" not in leaf:
            out.append(leaf)
    return out
