#!/usr/bin/env python3
"""round_ledger.py 的离线回归用例。

账本只在两处被信：抓取时的拦截、关账时的清理。两条都靠真文件状态，
故全部在临时目录里跑，不碰真实 `.workbuddy/rounds.json`。
"""
COVERS = ("round_ledger",)

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("round_ledger", ROOT / "scripts" / "round_ledger.py")
RL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RL)

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


tmp = Path(tempfile.mkdtemp(prefix="round_ledger_"))
os.environ["FLOMO_ROUND_LEDGER"] = str(tmp / "rounds.json")

# ① 空账
check("初始无待关账", RL.pending() == [], RL.pending())

# ② 开账后可查，重复开同一条不叠加
RL.open_round("https://example.com/a", "/tmp/a.txt")
RL.open_round("https://example.com/a", "/tmp/a.txt")
check("同一链接重复开账只记一条", len(RL.pending()) == 1, RL.pending())
check("开账带正文路径", RL.pending()[0]["article"] == "/tmp/a.txt", RL.pending())

# ③ 关账：按 URL 精确关，不影响别的
RL.open_round("https://example.com/b", "/tmp/b.txt")
RL.close_round("https://example.com/a", "published", "id1,id2")
check("关账后只剩另一条", [r["url"] for r in RL.pending()] == ["https://example.com/b"],
      RL.pending())

# ④ 关不存在的账返回 None，不报错
check("关账不存在的 URL 返回 None", RL.close_round("https://example.com/x", "exit") is None)

# ⑤ 拦截文案点名未关链接
msg = RL.pending_block_message()
check("拦截文案含未关链接", "example.com/b" in msg, msg)
check("拦截文案含关账命令", "exit_confirm.py" in msg, msg)

# ⑥ 全部关掉后拦截文案为空
RL.close_round("https://example.com/b", "exit", "2 个对象")
check("全部关账后无拦截文案", RL.pending_block_message() == "", RL.pending_block_message())

# ⑦ 账本被倒序写入时，裁剪必须保留最新：旧实现直接切「最后 KEEP_CLOSED 条」，
#    行序一反就会把刚关账的那一轮切掉，report_check 随后报「账本里没有这条素材」。
import json

_ledger = Path(os.environ["FLOMO_ROUND_LEDGER"])
_rows = [
    {"url": f"https://example.com/old{i}", "article": "",
     "opened_at": f"t{i:03d}",
     "closed_at": f"c{i:03d}",
     "outcome": "exit", "detail": ""}
    for i in range(RL.KEEP_CLOSED + 2)
]
_rows.reverse()  # 倒序：最新在最前
_ledger.write_text(json.dumps({"rounds": _rows}, ensure_ascii=False), encoding="utf-8")
RL.open_round("https://example.com/newest", "")
_kept = [r["url"] for r in RL._load(_ledger)["rounds"]]
check("倒序账本裁剪后仍留着刚开的那一轮", "https://example.com/newest" in _kept, _kept)
check("倒序账本裁剪后留下的是最新几条", "https://example.com/old0" not in _kept, _kept)
check("关账条数不超过上限", len([u for u in _kept if "old" in u]) <= RL.KEEP_CLOSED, len(_kept))

print("---")
print("全部通过" if all(RESULTS) else "存在失败用例")
sys.exit(0 if all(RESULTS) else 1)
