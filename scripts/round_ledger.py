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


def _save(path: Path, data: dict) -> None:
    rounds = data["rounds"]
    closed = [r for r in rounds if r.get("closed_at")]
    opened = [r for r in rounds if not r.get("closed_at")]
    data["rounds"] = opened + closed[-KEEP_CLOSED:]
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
    }
    data["rounds"].append(entry)
    _save(path, data)
    return entry


def close_round(url: str, outcome: str, detail: str = "") -> dict | None:
    """按 URL 关账。账上没有该 URL 时返回 None（记账晚于抓取的旧轮次）。"""
    path = ledger_path()
    data = _load(path)
    closed = None
    for r in data["rounds"]:
        if r.get("url") == url and not r.get("closed_at"):
            r["closed_at"] = datetime.now().isoformat(timespec="seconds")
            r["outcome"] = outcome
            r["detail"] = detail
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
