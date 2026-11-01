"""rss_fetch 的回归用例：RSS/Atom 归一、OPML 读取、单源失败不阻塞。

离线：不打网络，用内联 XML 喂 `parse_feed` / `read_opml`。要锁住的是格式差异被
正确抹平（否则 Atom 源会静默拉不到条目——RSS 的 link 是元素文本、Atom 在 href
属性里，写错只表现为「某个源总是空的」），以及坏源不拖垮整批。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rss_fetch

COVERS = ("rss_fetch",)

RSS_SAMPLE = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>源</title>'
    '<item><title>甲文</title><link>http://a/1</link>'
    '<pubDate>Sat, 01 Jan 2026 10:00:00 +0000</pubDate>'
    '<dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">张三</dc:creator>'
    '<description>&lt;p&gt;摘要 &amp; 至少&lt;/p&gt;正文</description></item>'
    '<item><title>乙文</title><link>http://a/2</link></item>'
    '</channel></rss>'
)

ATOM_SAMPLE = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>源</title>'
    '<entry><title>丙文</title><link href="http://a/3"/>'
    '<updated>20260101T000000Z</updated><author><name>李四</name></author>'
    '<summary>&lt;b&gt;摘&lt;/b&gt;要</summary></entry>'
    '</feed>'
)


class ParseFeedTest(unittest.TestCase):
    def test_rss20_fields(self) -> None:
        items = rss_fetch.parse_feed(RSS_SAMPLE.encode())
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["title"], "甲文")
        self.assertEqual(first["link"], "http://a/1")
        self.assertEqual(first["author"], "张三")
        self.assertIn("摘要", first["summary"])
        self.assertNotIn("<p>", first["summary"])

    def test_atom_fields(self) -> None:
        items = rss_fetch.parse_feed(ATOM_SAMPLE.encode())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "http://a/3")
        self.assertEqual(items[0]["author"], "李四")
        self.assertEqual(items[0]["title"], "丙文")

    def test_both_formats_yield_same_shape(self) -> None:
        rss = rss_fetch.parse_feed(RSS_SAMPLE.encode())[0]
        atom = rss_fetch.parse_feed(ATOM_SAMPLE.encode())[0]
        self.assertEqual(set(rss), set(atom))

    def test_broken_xml_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            rss_fetch.parse_feed(b"<not xml")

    def test_item_without_link_is_dropped(self) -> None:
        raw = ('<?xml version="1.0"?><rss version="2.0"><channel>'
               '<item><title>无链接</title></item></channel></rss>').encode()
        self.assertEqual(rss_fetch.parse_feed(raw), [])

    def test_html_is_stripped_from_summary(self) -> None:
        items = rss_fetch.parse_feed(RSS_SAMPLE.encode())
        self.assertNotIn("<", items[0]["summary"])


class OpmlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="rss_opml_test_")

    def tearDown(self) -> None:
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _write(self, body: str) -> str:
        path = os.path.join(self.dir, "feeds.opml")
        Path(path).write_text(body, encoding="utf-8")
        return path

    def test_reads_xmlurl_from_nested_outline(self) -> None:
        path = self._write(
            '<?xml version="1.0"?><opml version="1.0"><body>'
            '<outline text="分组"><outline type="rss" xmlUrl="http://s/1"/>'
            '<outline type="rss" xmlUrl="http://s/2"/></outline></body></opml>'
        )
        self.assertEqual(rss_fetch.read_opml(path), ["http://s/1", "http://s/2"])

    def test_outline_without_xmlurl_is_skipped(self) -> None:
        path = self._write(
            '<?xml version="1.0"?><opml version="1.0"><body>'
            '<outline text="只是分组"/><outline xmlUrl="http://s/3"/></body></opml>'
        )
        self.assertEqual(rss_fetch.read_opml(path), ["http://s/3"])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(rss_fetch.read_opml(os.path.join(self.dir, "nope.opml")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
