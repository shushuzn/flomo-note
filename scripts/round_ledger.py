#!/usr/bin/env python3
"""round_ledger.py — 抓取轮次的开关账本（防止「读过但没关账」悄悄消失）。

背景：写卡路径有 validate → sop_gate → publish → verify_card 一串机械闸门，
退出路径却只有一条**自愿调用**的 exit_confirm。结果是同一类事故反复出现——
抓完、读完、觉得「没什么可写」，就在回复里口头退出；搜索、查重、退出留痕
一个都没跑。机器侧看不出区别：没有产物，就没有痕迹。

本模块把「一份材料」记成一条轮次：`fetch_article.py` 抓到正文即开账，
`exit_confirm.py` 以「建卡」或「退出」之一关账。开着的账会挡住下一次抓取
（同一批多链接用 `--batch` 显式放行），所以漏关不会静默留到下一轮。

存放：`.workbuddy/rounds.json`（中间态，不入库）。测试用
`FLOMO_ROUND_LEDGER` 指到临时文件，不碰真实账本。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEEP_CLOSED = 50


def ledger_path() -> Path:
    override = os.environ.get("FLOMO_ROUND_LEDGER")
    if override:
        return Path(override)
    return PROJECT_ROOT / ".workbuddy" / "rounds.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {"rounds": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rounds": []}
    if not isinstance(data, dict) or not isinstance(data.get("rounds"), list):
        return {"rounds": []}
    return data


def _next_seq(rounds: list[dict]) -> int:
    """单调递增序号：同一秒内的先后只有它能分辨（时间戳精确到秒）。"""
    return max((r.get("seq") or 0 for r in rounds), default=0) + 1


def _save(path: Path, data: dict) -> None:
    rounds = data["rounds"]
    # 整体保持时间正序，**不按「未关闭 / 已关闭」分段拼接**。
    #
    # 不能写成 `opened + closed[-KEEP_CLOSED:]`（未关闭的排前面）。那有一个真实后果：
    # 分段拼接会把刚 open 的那条挪到已关闭条目的**前面**，从此账本不再时间有序；
    # 而 `closed_at` 只精确到秒、`opened_at` 同样只到秒，同一秒里连做几轮（用例里
    # 就是连做五轮）时三个排序键全部相等，稳定排序保持的正是这个被打乱的顺序。
    # 于是 `[r for r ...][-1]` 取到最旧那一轮——报告闸门读到的是上一版的
    # `updated`/`detail`，据此判「新建/更新」两段会全判错，而两边看上去都对。
    #
    # 现在：先按 (closed_at, opened_at, seq) 排正序，`seq` 兜住同秒；截断只动最旧的
    # 已关闭条目，未关闭的一律保留（它们代表待关账的轮次，不能丢）。
    ordered = sorted(rounds, key=lambda r: (r.get("opened_at") or "", r.get("seq") or 0))
    # 裁「最旧」要按关账时间判，不按 opened_at：某条可能开得很早却关得很晚。
    closed_sorted = sorted((r for r in ordered if r.get("closed_at")),
                           key=lambda r: (r.get("closed_at") or "", r.get("seq") or 0))
    overflow = len(closed_sorted) - KEEP_CLOSED
    if overflow > 0:
        drop = {id(r) for r in closed_sorted[:overflow]}
        ordered = [r for r in ordered if id(r) not in drop]
    data["rounds"] = ordered
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rounds.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def pending() -> list[dict]:
    return [r for r in _load(ledger_path())["rounds"] if not r.get("closed_at")]


def open_round(url: str, article: str = "") -> dict:
    """记一条开着的轮次；同一 URL 已开着则复用，不叠两条。"""
    path = ledger_path()
    data = _load(path)
    for r in data["rounds"]:
        if r.get("url") == url and not r.get("closed_at"):
            return r
    entry = {
        "url": url,
        "article": article,
        "opened_at": datetime.now().isoformat(timespec="seconds"),
        "closed_at": None,
        "outcome": "",
        "detail": "",
        # 时间戳只到秒，同一秒里连开几轮就分不出先后；排序拿它做末级键。
        "seq": _next_seq(data["rounds"]),
    }
    data["rounds"].append(entry)
    _save(path, data)
    return entry


def close_round(url: str, outcome: str, detail: str = "", updated: str = "") -> dict | None:
    """按 URL 关账。账上没有该 URL 时返回 None（记账晚于抓取的旧轮次）。

    `updated` 记的是 `detail` 里哪几个 id 属于「更新既有卡」，其余按新建算。
    报告要求「新建与更新分开写」，而云端看不出这件事（新建卡的 created_at 也可能
    就是今天），所以只能由关账这一手写进账本，报告闸门再据此对账。
    """
    path = ledger_path()
    data = _load(path)
    closed = None
    for r in data["rounds"]:
        if r.get("url") == url and not r.get("closed_at"):
            r["closed_at"] = datetime.now().isoformat(timespec="seconds")
            r["outcome"] = outcome
            r["detail"] = detail
            r["updated"] = updated
            closed = r
    if closed is not None:
        _save(path, data)
    return closed


def pending_block_message() -> str:
    """开账未关时的拦截文案：直接点名是哪条、哪份材料。"""
    rows = pending()
    if not rows:
        return ""
    lines = [f"上一轮还有 {len(rows)} 条材料未关账，先关账再抓新链接："]
    for r in rows:
        lines.append(f"  - {r['url']}（开于 {r['opened_at']}，正文 {r.get('article') or '未记'}）")
    lines.append("关账：python scripts/exit_confirm.py --url <链接> --published \"id,…\"")
    lines.append("　　　或退出留痕：… --objects … --search-log … --dedup-log … --reason …")
    lines.append("同一批多链接：本轮首次抓取加 --batch，一次放行。")
    return "\n".join(lines)
