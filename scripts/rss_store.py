#!/usr/bin/env python3
"""rss_store.py — RSS 条目的本地存储、读/未读状态与保留期清理。

背景：订阅源拉回来的条目如果只落在 `_tmp/`，会被 `cleanup.py` 按近轮窗口一并删
掉——下一轮想挑素材时早已无据可查。而若无限期留着，几百个源每天新增的条目会把
「挑今天的」变成体力活。故单独立一份存储，用「读没读过」区分保留时长。

**保留期**（`gc()` 是唯一实现，fetch 与 `gc` 子命令都走它）：
  - 未读：入库满 `UNREAD_KEEP_DAYS` 天即删。没读过说明没进入过选做流程，一周足够
    回头捞；再老只会变成噪音。
  - 已读：标记读满 `READ_KEEP_DAYS` 天即删。读过意味着已按 SOP 处理完（建卡或退出
    留痕），正文与结论都已落云端，本地只剩索引价值，留一天够回看「昨天处理过哪条」。

**「读过」不是猜的**：本模块不推断任何处理痕迹，只认显式的 `mark_read()`。判据是
「按流程对这个处理过了」——建卡（`publish.py`）或退出留痕（`exit_confirm.py`）之后
由调用方标记。自动推断会把「看过标题」误判成「处理完」，保留期随即失效。

**存储位置**：默认 `.rss/feeds.db`（项目根下）。它是有状态的本地数据，不是流程中间
产物，故不放进 `_tmp/`（那里归 cleanup 管），并在 `.gitignore` 里忽略。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 自备兄弟模块导入路径：直接执行时 Python 会把脚本目录摆进 sys.path，
# runpy / importlib 加载不会——不自备就只在调用方恰好摆好时才跑得住。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli_util import Opt, UsageError, cli_entry, parse_options

UNREAD_KEEP_DAYS = 7
READ_KEEP_DAYS = 1

DEFAULT_DB = ".rss/feeds.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    feed        TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    read_at     TEXT
);
CREATE INDEX IF NOT EXISTS items_fetched ON items(fetched_at);
CREATE INDEX IF NOT EXISTS items_read ON items(read_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def item_id(feed: str, link: str) -> str:
    """条目 id：源地址加链接取摘要。同一源同一条目重复拉取落到同一行。"""
    return hashlib.sha1(f"{feed}\n{link}".encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """开库并建表。调用方负责 close；`gc` 的写入在同一连接内完成。"""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_items(conn: sqlite3.Connection, feed: str, items: list[dict]) -> tuple[int, int]:
    """写入一批条目，返回 `(新增数, 重复数)`。

    重复不覆盖：同一 id 已存在就跳过，保留首次抓到的 `fetched_at`——保留期从首次
    见到它算起，反复刷新不能把未读条目的寿命无限续期。
    """
    added = dup = 0
    for item in items:
        link = (item.get("link") or "").strip()
        if not link:
            continue
        key = item_id(feed, link)
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (id, feed, title, link, author, published, fetched_at, summary)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                key,
                feed,
                (item.get("title") or "").strip(),
                link,
                (item.get("author") or "").strip(),
                (item.get("published") or "").strip(),
                _now(),
                (item.get("summary") or "").strip(),
            ),
        )
        if cur.rowcount:
            added += 1
        else:
            dup += 1
    conn.commit()
    return added, dup


def mark_read(conn: sqlite3.Connection, keys: list[str]) -> tuple[list[str], list[str]]:
    """按 id 或链接标记已读，返回 `(命中的 id, 未命中的 key)`。"""
    hit: list[str] = []
    miss: list[str] = []
    for key in keys:
        row = conn.execute(
            "SELECT id FROM items WHERE id = ? OR link = ?", (key, key)
        ).fetchone()
        if row is None:
            miss.append(key)
            continue
        conn.execute("UPDATE items SET read_at = ? WHERE id = ?", (_now(), row["id"]))
        hit.append(row["id"])
    conn.commit()
    return hit, miss


def list_items(conn: sqlite3.Connection, *, unread_only: bool = False,
               feed: str = "", limit: int = 50) -> list[dict]:
    """按抓取时间倒序列条目（新的在前）。`feed` 按源地址子串过滤。"""
    sql = "SELECT id, feed, title, link, author, published, fetched_at, read_at FROM items"
    where: list[str] = []
    args: list[object] = []
    if unread_only:
        where.append("read_at IS NULL")
    if feed:
        # 子串而非全等：源地址带查询参数时（?format=rss 之类）全等很难打中
        where.append("instr(feed, ?) > 0")
        args.append(feed)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY fetched_at DESC, id LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def gc(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict:
    """按保留期清理，返回计数。这是保留规则的唯一实现。

    未读按 `fetched_at` 判（「获取到的保留一周」从拿到它算起），已读按 `read_at`
    判。两个阈值都是常量而非参数：调用方不该各自解释「一周」是几天。
    """
    now = now or datetime.now(UTC)
    unread_cut = (now - timedelta(days=UNREAD_KEEP_DAYS)).isoformat(timespec="seconds")
    read_cut = (now - timedelta(days=READ_KEEP_DAYS)).isoformat(timespec="seconds")
    cur = conn.execute(
        "DELETE FROM items WHERE (read_at IS NULL AND fetched_at < ?)"
        " OR (read_at IS NOT NULL AND read_at < ?)",
        (unread_cut, read_cut),
    )
    removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return {"removed": removed, "unread_cut": unread_cut, "read_cut": read_cut}


def record_fetch(conn: sqlite3.Connection, *, at: str | None = None) -> str:
    """记下本次拉取时刻，返回该时刻。供 `--if-older-than` 判断新旧。"""
    stamp = at or _now()
    conn.execute("INSERT INTO meta(key, value) VALUES('last_fetch', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (stamp,))
    conn.commit()
    return stamp


def last_fetch(conn: sqlite3.Connection) -> str | None:
    """上次成功拉取的时刻；从未拉过返回 None。"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_fetch'").fetchone()
    return row["value"] if row else None


def fetch_age_hours(conn: sqlite3.Connection, *, now: datetime | None = None) -> float | None:
    """距上次拉取多少小时；从未拉过返回 None（调用方按「需要拉」处理）。"""
    stamp = last_fetch(conn)
    if not stamp:
        return None
    parsed = _parse(stamp)
    if parsed is None:
        return None
    now = now or datetime.now(UTC)
    return (now - parsed).total_seconds() / 3600.0


def counts(conn: sqlite3.Connection) -> dict:
    """未读 / 已读 / 总数，供 `fetch` 末尾汇报。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread"
        " FROM items"
    ).fetchone()
    return {"total": row["total"], "unread": row["unread"] or 0,
            "read": (row["total"] or 0) - (row["unread"] or 0)}


SPEC = [
    Opt("--db", "db", default=DEFAULT_DB, hint="（可用 --db 指定别的库文件）"),
    Opt("--unread", "unread", "flag"),
    Opt("--feed", "feed", default="",
        hint="（源地址子串，多源时只看其中一个）"),
    Opt("--limit", "limit", "int", low=1, high=1000, default=50),
]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, positional = parse_options(argv, SPEC, positional_ok=True)
    if not positional:
        raise UsageError("缺子命令：list / mark-read / gc / counts")
    cmd, keys = positional[0], positional[1:]

    conn = connect(opts["db"])
    try:
        if cmd == "gc":
            result = gc(conn)
            print(f"[清理] 删除 {result['removed']} 条"
                  f"（未读早于 {result['unread_cut'][:10]}、"
                  f"已读早于 {result['read_cut'][:10]}）")
        elif cmd == "list":
            rows = list_items(conn, unread_only=opts["unread"], feed=opts["feed"],
                              limit=opts["limit"])
            for row in rows:
                flag = "未读" if row["read_at"] is None else f"已读 {row['read_at'][:10]}"
                # 带上来源：多源时「这条是谁给的」决定了值不值得信，省不掉
                print(f"{row['id'][:8]} {flag} | {row['title'][:44]}"
                      f" | {row['feed'][:38]} | {row['link']}")
            print(f"[清单] {len(rows)} 条（库内共 {counts(conn)['total']} 条）")
        elif cmd == "mark-read":
            if not keys:
                raise UsageError("mark-read 至少要给一个条目 id 或链接")
            hit, miss = mark_read(conn, keys)
            print(f"[标记] 已读 {len(hit)} 条" + (f"；未找到 {len(miss)} 条：{miss}" if miss else ""))
            if miss:
                return 1
        elif cmd == "counts":
            print(json.dumps(counts(conn), ensure_ascii=False))
        else:
            raise UsageError(f"未知子命令：{cmd}（可用 list / mark-read / gc / counts）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(cli_entry(main, errors=(), usage_code=1))
