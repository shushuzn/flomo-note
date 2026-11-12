#!/usr/bin/env python3
"""source_hunt.py 的离线回归用例。

「只给一个词」时的红线是两条：不许把检索推回用户（空手退出 = 失败），
也不许把「抓回来 200 却讲别的」当成取到材料（词块不命中就换下一条）。
两条都用假搜索结果 + 假页面离线跑，不联网。
"""
COVERS = ("source_hunt",)

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("source_hunt", ROOT / "scripts" / "source_hunt.py")
SH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SH)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


tmp = Path(tempfile.mkdtemp(prefix="source_hunt_"))
os.environ["FLOMO_ROUND_LEDGER"] = str(tmp / "rounds.json")

_REAL = {}


def _install(results, pages):
    _REAL["run"] = SH.SW.run_search
    _REAL["fetch"] = SH.FA.fetch
    _REAL["extract"] = SH.FA.extract
    _REAL["lead"] = SH.FA.trim_lead_noise
    _REAL["tail"] = SH.FA.trim_tail_noise
    _REAL["root"] = SH.PROJECT_ROOT
    SH.SW.run_search = lambda q, **kw: {
        "engine_used": "fake", "search_url": "https://search.example/?q=" + q,
        "results": [{"url": u, "title": u, "snippet": ""} for u in results],
        "rejected": [],
    }

    def fake_fetch(url, timeout=30.0, via="auto"):
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return f"<html>{page}</html>", "text/html", "direct"

    SH.FA.fetch = fake_fetch
    SH.FA.extract = lambda html: (html.replace("<html>", "").replace("</html>", ""), "fake")
    SH.FA.trim_lead_noise = lambda body, title="": (body, 0)
    SH.FA.trim_tail_noise = lambda body, min_chars=0: (body, "")
    SH.PROJECT_ROOT = tmp


def _restore():
    SH.SW.run_search = _REAL["run"]
    SH.FA.fetch = _REAL["fetch"]
    SH.FA.extract = _REAL["extract"]
    SH.FA.trim_lead_noise = _REAL["lead"]
    SH.FA.trim_tail_noise = _REAL["tail"]
    SH.PROJECT_ROOT = _REAL["root"]


good = "绿债" * 400  # 词块命中、长度过阈
try:
    # ① 首条太短 → 跳过，落到第二条
    _install(["https://a.example/1", "https://b.example/2"],
             {"https://a.example/1": "太短了", "https://b.example/2": good})
    got = SH.hunt("绿债", "", 100, 5, 5.0, "direct")
    check("首条过短被跳过、采用第二条", (got["accepted"] or {}).get("url") == "https://b.example/2",
          [c["reason"] for c in got["candidates"]])
    check("采用后正文落盘", Path(got["accepted"]["file"]).exists(), got["accepted"])
    _restore()

    # ② 正文里没有术语词块 → 判假成功，换下一条
    _install(["https://a.example/1", "https://b.example/2"],
             {"https://a.example/1": "讲的是完全另一件事" * 50, "https://b.example/2": good})
    got = SH.hunt("绿债", "", 100, 5, 5.0, "direct")
    check("词块不命中的候选被否", got["candidates"][0]["ok"] is False
          and "词块" in got["candidates"][0]["reason"], got["candidates"][0])
    check("否掉首条后仍能取到可用材料", (got["accepted"] or {}).get("url") == "https://b.example/2",
          got["accepted"])
    _restore()

    # ③ 抓取失败不终止整轮，记原因后继续
    _install(["https://a.example/1", "https://b.example/2"],
             {"https://a.example/1": SH.FA.FetchError("两条通道都不通"),
              "https://b.example/2": good})
    got = SH.hunt("绿债", "", 100, 5, 5.0, "direct")
    check("抓取失败记原因不抛", got["candidates"][0]["ok"] is False
          and "抓取失败" in got["candidates"][0]["reason"], got["candidates"][0])
    check("失败一条后继续试下一条", (got["accepted"] or {}).get("url") == "https://b.example/2",
          got["accepted"])
    _restore()

    # ④ 全军覆没 → 非 0 退出，不装作取到了
    _install(["https://a.example/1"], {"https://a.example/1": "太短"})
    rc = SH.main(["绿债", "--min-chars", "100", "--no-ledger", "--via", "direct"])
    check("没有候选过判据时非 0 退出", rc == 1, rc)
    _restore()

    # ⑤ 取到即开账，后面接常规流程
    _install(["https://b.example/2"], {"https://b.example/2": good})
    rc = SH.main(["绿债", "--min-chars", "100", "--via", "direct"])
    pending = [r["url"] for r in SH.round_ledger.pending()]
    check("取到材料后自动开账", rc == 0 and pending == ["https://b.example/2"], (rc, pending))
    SH.round_ledger.close_round("https://b.example/2", "exit", "测试")
    _restore()
    # ⑥ 指定搜狗微信入口时，引擎选择必须真的传到 run_search
    _install(["https://b.example/2"], {"https://b.example/2": good})
    seen = []
    orig_run = SH.SW.run_search
    SH.SW.run_search = lambda q, **kw: (seen.append(kw.get("engines")) or {
        "engine_used": "sogou_wechat", "search_url": "https://weixin.sogou.com/weixin?type=2&query=" + q,
        "results": [{"url": "https://b.example/2", "title": "标题", "snippet": ""}],
        "rejected": [],
    })
    got = SH.hunt("绿债", "", 100, 5, 5.0, "direct", ("sogou_wechat",))
    check("指定引擎后传给 run_search", seen == [("sogou_wechat",)], seen)
    check("微信入口仍按普通候选抓取判定", (got["accepted"] or {}).get("url") == "https://b.example/2",
          got["accepted"])
    SH.SW.run_search = orig_run
    _restore()
finally:
    _restore()

print("---")
print("全部通过" if all(RESULTS) else "存在失败用例")
sys.exit(0 if all(RESULTS) else 1)
