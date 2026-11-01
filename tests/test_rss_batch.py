"""rss_batch 的回归用例（离线，不起服务、不联网）。

rss_batch 把「一次 N 条、处理前先拉取」固化成机制：prepare 拉取→取未读→抓
正文→写 manifest，done 按 manifest 标记已读。本文件钉住四件容易静默出错的事：

① prepare 默认必须拉取（未读保留期只有 7 天，拿旧快照会让条目没见到就过期），
  `--no-fetch` 是显式跳过而不是默认行为；
② 抓取失败的条目在 manifest 里 fetched=false 且 prepare 退出码非 0——否则
  一个源挂了会被当成「这条处理完了」；
③ done 只标记 manifest 里点到的那些，不能整库乱标；
④ done 的键（编号/链接）在 manifest 里找不到时报错，不静默跳过。

全部离线：进程内替换 `_run` 与 `_fetch_once`，不真起子进程、不联网。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rss_batch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import rss_store  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("rss_batch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RB = _load()
COVERS = ("rss_batch",)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    line = f"{'PASS' if ok else 'FAIL'}  {label}"
    if not ok and detail:
        line += f"\n      {detail}"
    print(line)


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss_batch_test_")
        self.db_path = Path(self.tmp) / "feeds.db"
        self.conn = rss_store.connect(self.db_path)
        self.manifest = Path(self.tmp) / "manifest.json"
        self.pulls = 0
        self.fetch_ok = True
        self._orig_run = RB._run
        self._orig_fetch = RB._fetch_once
        RB._run = self._fake_run
        RB._fetch_once = self._fake_fetch

    def tearDown(self):
        RB._run = self._orig_run
        RB._fetch_once = self._orig_fetch
        self.conn.close()

    def _fake_run(self, cmd, timeout=300):
        """顶掉 rss_fetch 子进程：只计数，返回成功。"""
        self.pulls += 1
        return 0, "[入库] 合计新增 0、重复 0\n[清理] 删除 0 条\n", ""

    def _fake_fetch(self, url):
        if not self.fetch_ok:
            return False, "", "连接失败"
        local = Path(self.tmp) / f"article_{abs(hash(url)) % 99999}.txt"
        local.write_text("正文", encoding="utf-8")
        return True, str(local), f"标题-{url.rsplit('/', 1)[-1]}"

    def _add(self, link, title="标题"):
        rss_store.add_items(self.conn, "http://feed/x",
                            [{"title": title, "link": link}])
        return link

    def _prepare(self, *extra):
        argv = ["prepare", "--db", str(self.db_path), "--manifest", str(self.manifest),
                *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = RB.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _done(self, *keys):
        argv = ["done", "--db", str(self.db_path), "--manifest", str(self.manifest),
                *keys]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = RB.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_prepare_writes_manifest(self):
        for i in range(3):
            self._add(f"http://a/{i}", f"条目{i}")
        code, out, _ = self._prepare("--size", "3", "--no-fetch")
        check("prepare 退出码 0", code == 0, out[-200:])
        check("manifest 已写", self.manifest.exists())
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        check("manifest 条数等于 size", len(data["items"]) == 3, data.get("size"))
        check("manifest 带 prepared_at", bool(data.get("prepared_at")))
        check("manifest 条目带链接", all(i.get("link") for i in data["items"]))
        check("manifest 记录本地正文路径", all(i.get("local") for i in data["items"]))

    def test_prepare_respects_size(self):
        for i in range(5):
            self._add(f"http://a/{i}")
        self._prepare("--size", "2", "--no-fetch")
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        check("只取 size 条", len(data["items"]) == 2, len(data["items"]))

    def test_prepare_fetches_by_default(self):
        self._add("http://a/1")
        self._prepare("--size", "1")
        check("默认调用了拉取", self.pulls == 1, self.pulls)

    def test_prepare_no_fetch_skips_pull(self):
        self._add("http://a/1")
        self._prepare("--size", "1", "--no-fetch")
        check("--no-fetch 不拉取", self.pulls == 0, self.pulls)

    def test_prepare_failed_fetch_marks_and_fails(self):
        self._add("http://a/1")
        self.fetch_ok = False
        code, _out, err = self._prepare("--size", "1", "--no-fetch")
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        check("失败条目 fetched=false", data["items"][0]["fetched"] is False, data)
        check("有失败时退出码非 0", code != 0, code)
        check("stderr 提示勿标记", "不要标记" in err, err[:160])

    def test_done_marks_only_listed(self):
        for i in range(3):
            self._add(f"http://a/{i}")
        self._prepare("--size", "3", "--no-fetch")
        code, _out, _err = self._done("1")
        check("done 退出码 0", code == 0)
        counts = rss_store.counts(self.conn)
        check("只标记 1 条", counts["read"] == 1, counts)

    def test_done_all_marks_whole_batch(self):
        for i in range(3):
            self._add(f"http://a/{i}")
        self._prepare("--size", "3", "--no-fetch")
        self._done("all")
        counts = rss_store.counts(self.conn)
        check("all 标记整批", counts["read"] == 3 and counts["unread"] == 0, counts)

    def test_done_accepts_link_as_key(self):
        self._add("http://a/7")
        self._prepare("--size", "1", "--no-fetch")
        code, _out, _err = self._done("http://a/7")
        check("链接作键可标记",
              code == 0 and rss_store.counts(self.conn)["read"] == 1)

    def test_done_unknown_key_fails(self):
        self._add("http://a/1")
        self._prepare("--size", "1", "--no-fetch")
        code, _out, err = self._done("99")
        check("未知编号报错", code != 0 and "找不到" in err, err[:160])

    def test_done_without_manifest_fails(self):
        code, _out, err = RB.main(["done", "all", "--db", str(self.db_path),
                                   "--manifest", str(Path(self.tmp) / "nope.json")])
        check("manifest 缺失报错", code != 0 and "不存在" in err, err[:160])

    def test_missing_subcommand_fails(self):
        raised = False
        try:
            RB.main([])
        except Exception:
            raised = True
        check("缺子命令抛错", raised)


def main():
    suite = unittest.TestLoader().loadTestsFromTestCase(BatchTest)
    unittest.TextTestRunner(verbosity=0).run(suite)
    ok = all(RESULTS)
    print(f"\n{'全部通过' if ok else '存在失败'}（{len(RESULTS)} 项）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
