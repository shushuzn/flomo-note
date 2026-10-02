#!/usr/bin/env python3
"""现采全库正文存成本地快照，供离线分析（避免反复打云端）。

**只读**：不写云端、不改任何卡。快照是瞬时产物，cleanup 按近轮窗口回收。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient, load_token  # noqa: E402
from scan_qualified_concepts import _memos  # noqa: E402


def main():
    root = Path(__file__).resolve().parent.parent
    tags = [t.strip() for t in (root / "_tmp_alltags.txt").read_text(
        encoding="utf-8").splitlines() if t.strip()]

    c = FlomoClient.__new__(FlomoClient)
    token, _ = load_token()
    c.__init__(token)
    c.init()

    seen = {}
    for t in tags:
        try:
            for m in _memos(c.tool("memo_search", {"tag": t, "limit": 50})):
                if m.get("id") and m["id"] not in seen:
                    seen[m["id"]] = m
        except Exception as e:
            print(f"[warn] 标签 {t!r} 失败：{e}", file=sys.stderr)

    out = root / "_tmp_audit_snapshot.json"
    out.write_text(json.dumps(
        {mid: {"content": m.get("content", ""),
               "tags": m.get("tags", []),
               "created": m.get("created_at", "")}
         for mid, m in seen.items()}, ensure_ascii=False), encoding="utf-8")
    print(f"[scan] {len(seen)} 张→ {out}")


if __name__ == "__main__":
    main()
