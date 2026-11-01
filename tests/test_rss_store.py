"""rss_store 的回归用例：读/未读状态、保留期清理、重复拉取不续期。

离线：用临时目录里的库文件，不联网、不读 token。要锁住的是三条容易静默失效的
规则——「未读七天 / 已读一天」的界线、「读过只认显式标记」、以及「重复拉取不能
把未读条目的寿命无限续期」（后者一旦失效，热门源会永久占据未读列表）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rss_store

COVERS = ("rss_store",)

FEED = "http://example.com/feed.xml"


def _item(link: str, title: str = "标题") -> dict:
    return {"title": title, "link": link}


class RssStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="rss_store_test_")
        self.db = os.path.join(self.dir, "feeds.db")
        self.conn = rss_store.connect(self.db)

    def tearDown(self) -> None:
        self.conn.close()
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _age(self, link: str, *, fetched_days: float = 0, read_days: float | None = None) -> None:
        now = datetime.now(UTC)
        if fetched_days:
            self.conn.execute(
                "UPDATE items SET fetched_at=? WHERE link=?",
                ((now - timedelta(days=fetched_days)).isoformat(timespec="seconds"), link),
            )
        if read_days is not None:
            self.conn.execute(
                "UPDATE items SET read_at=? WHERE link=?",
                ((now - timedelta(days=read_days)).isoformat(timespec="seconds"), link),
            )
        self.conn.commit()

    # ---- 读 / 未读状态 ----

    def test_last_fetch_absent_then_recorded(self) -> None:
        self.assertIsNone(rss_store.last_fetch(self.conn))
        self.assertIsNone(rss_store.fetch_age_hours(self.conn))
        stamp = rss_store.record_fetch(self.conn)
        self.assertEqual(rss_store.last_fetch(self.conn), stamp)

    def test_fetch_age_hours(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=5)).isoformat(timespec="seconds")
        rss_store.record_fetch(self.conn, at=past)
        self.assertAlmostEqual(rss_store.fetch_age_hours(self.conn), 5.0, delta=0.05)

    def test_record_fetch_overwrites(self) -> None:
        rss_store.record_fetch(self.conn, at="20260101T000000+0000")
        rss_store.record_fetch(self.conn, at="20260601T000000+0000")
        self.assertEqual(rss_store.last_fetch(self.conn), "20260601T000000+0000")

    def test_new_item_is_unread(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        stat = rss_store.counts(self.conn)
        self.assertEqual(stat, {"total": 1, "unread": 1, "read": 0})

    def test_mark_read_by_link(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        hit, miss = rss_store.mark_read(self.conn, ["http://a/1"])
        self.assertEqual(len(hit), 1)
        self.assertEqual(miss, [])
        self.assertEqual(rss_store.counts(self.conn)["unread"], 0)

    def test_mark_read_unknown_key_reports_miss(self) -> None:
        hit, miss = rss_store.mark_read(self.conn, ["http://nope"])
        self.assertEqual(hit, [])
        self.assertEqual(miss, ["http://nope"])

    def test_list_shows_feed_origin(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        rss_store.add_items(self.conn, "http://other/feed", [_item("http://a/2")])
        rows = rss_store.list_items(self.conn)
        self.assertEqual({r["feed"] for r in rows}, {FEED, "http://other/feed"})

    def test_list_filters_by_feed_substring(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        rss_store.add_items(self.conn, "http://other/feed", [_item("http://a/2")])
        rows = rss_store.list_items(self.conn, feed="other")
        self.assertEqual([r["link"] for r in rows], ["http://a/2"])

    def test_list_feed_filter_combined_with_unread(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        rss_store.add_items(self.conn, "http://other/feed", [_item("http://a/2")])
        rss_store.mark_read(self.conn, ["http://a/2"])
        rows = rss_store.list_items(self.conn, unread_only=True, feed="other")
        self.assertEqual(rows, [])

    def test_list_unread_only(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1"), _item("http://a/2")])
        rss_store.mark_read(self.conn, ["http://a/1"])
        unread = rss_store.list_items(self.conn, unread_only=True)
        self.assertEqual([r["link"] for r in unread], ["http://a/2"])

    # ---- 保留期 ----

    def test_unread_kept_within_seven_days(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        self._age("http://a/1", fetched_days=6)
        result = rss_store.gc(self.conn)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(rss_store.counts(self.conn)["total"], 1)

    def test_unread_dropped_after_seven_days(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        self._age("http://a/1", fetched_days=8)
        result = rss_store.gc(self.conn)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(rss_store.counts(self.conn)["total"], 0)

    def test_read_kept_within_one_day(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        rss_store.mark_read(self.conn, ["http://a/1"])
        self._age("http://a/1", fetched_days=30, read_days=0.5)
        self.assertEqual(rss_store.gc(self.conn)["removed"], 0)

    def test_read_dropped_after_one_day(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        rss_store.mark_read(self.conn, ["http://a/1"])
        # 抓取很早但刚标记已读：保留期按 read_at 走，不该被 fetched_at 拖累
        self._age("http://a/1", fetched_days=30, read_days=2)
        self.assertEqual(rss_store.gc(self.conn)["removed"], 1)

    def test_gc_respects_injected_now(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        self._age("http://a/1", fetched_days=8)
        future = datetime.now(UTC) + timedelta(days=365)
        self.assertEqual(rss_store.gc(self.conn, now=future)["removed"], 1)

    # ---- 幂等 ----

    def test_duplicate_fetch_does_not_extend_life(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        self._age("http://a/1", fetched_days=8)
        added, dup = rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        self.assertEqual((added, dup), (0, 1))
        # 重复拉取后保留期仍从首次抓取算起：依旧该删
        self.assertEqual(rss_store.gc(self.conn)["removed"], 1)

    def test_same_link_from_other_feed_is_separate(self) -> None:
        rss_store.add_items(self.conn, FEED, [_item("http://a/1")])
        added, _ = rss_store.add_items(self.conn, "http://other/feed", [_item("http://a/1")])
        self.assertEqual(added, 1)

    def test_item_without_link_is_skipped(self) -> None:
        added, dup = rss_store.add_items(self.conn, FEED, [{"title": "无链接"}])
        self.assertEqual((added, dup), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
