#!/usr/bin/env python3
"""退出留痕 —— 任何"不建卡"的决定都必须经过本脚本，禁止嘴上说退出就退出。

背景：反复出现"未搜索、未查重就凭判断窄口径退出"的事故。
此前退出完全靠自觉，没有任何机械约束。本脚本要求：
  1. 必须传入 --objects 列出全部对象清单（每个对象都被审视过）
  2. 必须传入 --search-log 指向搜索记录JSON（证明真的搜过）
  3. 必须传入 --dedup-log 指向dedup输出文件（证明真的查重过）
  4. 必须传入 --reason 明确退出理由

缺一即退出码非0，禁止在对话里口头说"窄口径退出"。

用法：
  python exit_confirm.py \
    --objects "对象1,对象2,对象3" \
    --search-log /tmp/search_xxx.json \
    --dedup-log /tmp/dedup_xxx.txt \
    --reason "XXX是纯事件，无可独立成立的技术/机制概念"
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--objects", required=True, help="逗号分隔的对象清单")
    ap.add_argument("--search-log", required=True, help="搜索记录JSON路径")
    ap.add_argument("--dedup-log", required=True, help="dedup输出文件路径")
    ap.add_argument("--reason", required=True, help="退出理由")
    args = ap.parse_args()

    errors = []

    # 1. 对象清单不能为空
    objs = [o.strip() for o in args.objects.split(",") if o.strip()]
    if not objs:
        errors.append("对象清单为空——必须先列对象清单，逐个审视")

    # 2. 搜索记录必须存在且合法
    sp = Path(args.search_log)
    if not sp.exists():
        errors.append(f"搜索记录不存在: {sp}")
    else:
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if not data.get("searched"):
                errors.append("搜索记录里 searched 不为 true——必须真的搜过")
            terms = data.get("terms", [])
            if not terms:
                errors.append("搜索记录里 terms 为空——至少搜过一个对象")
            for t in terms:
                if not t.get("search_url"):
                    errors.append(f"术语 {t.get('term')} 缺 search_url——搜索必须留痕真实URL")
        except Exception as e:
            errors.append(f"搜索记录JSON解析失败: {e}")

    # 3. dedup输出必须存在且非空
    dp = Path(args.dedup_log)
    if not dp.exists():
        errors.append(f"dedup记录不存在: {dp}")
    else:
        content = dp.read_text(encoding="utf-8", errors="replace")
        if "关键词" not in content:
            errors.append("dedup输出里没有'关键词'段——dedup没有真的跑过")

    # 4. 退出理由不能太短
    if len(args.reason) < 10:
        errors.append("退出理由太短——必须明确说明为什么每个对象都不建卡")

    if errors:
        print("[退出拦截] 不允许就这样退出：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("正确流程：", file=sys.stderr)
        print("  1. fetch_article 抓取并读回全文", file=sys.stderr)
        print("  2. 列出全部对象清单", file=sys.stderr)
        print("  3. 对每个对象做网络搜索，写search_log.json", file=sys.stderr)
        print("  4. 跑dedup.py查重，保存输出到文件", file=sys.stderr)
        print("  5. 确认确实无建卡价值，再跑本脚本留痕", file=sys.stderr)
        return 1

    print(f"[退出留痕] 已记录：{len(objs)}个对象审视完毕，理由：{args.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
