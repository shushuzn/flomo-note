#!/usr/bin/env python3
"""找「同一对象因切片限定不同被切成多张卡」的实证。

判法：把概念名里的**切片限定抹掉**（归一化）——时间、版本、代次、轮次、地域、事件动作
都是同一类切片（对象 + 切片），不是时间专属。若归一化后出现同名簇（≥2 张不同 id），
即说明同一对象被按切片切成了平行卡。这是只读取证脚本，不改云端。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flomo_client import FlomoClient  # noqa: E402
from scan_qualified_concepts import (  # noqa: E402
    _memos, concept_of, resolve_tags, collect_memos,
)

STRIP = [
    (r"[（(]\s*(19|20)\d{2}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}\s*(发布|推送|上线|更新)?\s*[)）]", ""),
    (r"[（(]\s*(19|20)\d{2}\s*[-/]\s*\d{1,2}(\s*[-/]\s*\d{1,2})?\s*[)）]", ""),
    (r"[（(]\s*(19|20)\d{2}\s*[)）]", ""),
    (r"[（(][^()（）]{0,40}(19|20)\d{2}[^()（）]{0,40}[)）]", ""),
    (r"(19|20)\d{2}\s*年\s*\d{0,2}\s*月?\s*\d{0,2}\s*日?", ""),
    (r"(19|20)\d{2}\s*[-/]\s*\d{1,2}(\s*[-/]\s*\d{1,2})?", ""),
    (r"(?<![0-9])[vV]\s?\d+(\.\d+)+((-[a-z0-9]+)*)", ""),
    (r"(?<![0-9])\d+(\.\d+){1,3}\s*(版|版本)", ""),
    (r"\b[QqEe]\s?[1-4]\b", ""),
    (r"(最新|近期|当前|如今|今日|本日|本周|本月|今年|去年|今早|刚刚)", ""),
    # —— 阶段号（RC2 / beta3 / Preview3）：与版本同属切片 ——
    (r"(?<![A-Za-z0-9])(?:RC|rc|Beta|beta|alpha|Alpha|preview|Preview)\s*\d+(?![0-9])", ""),
    # —— 代次/轮次/批次：与时间同属切片，不因出现的不是年份就放行——
    (r"第\s*[0-9一二三四五六七八九十]+\s*(?:代|版|阶段|期|轮|届|批)", ""),
    # —— 地域/语种切片 ——
    (r"(?<![A-Za-z0-9])(?:中文版|英文版|国内版|海外版|欧洲版|亚洲版|美版|日版|韩版)(?![A-Za-z0-9])", ""),
    # —— 事件动作（结尾）——
    (r"(?:发布|推出|上线|更新|回顾|总结|盘点|举办|召开|夺冠|夺标|获奖)\s*$", ""),
    (r"\s*[-—–~至]+\s*", "-"),
    (r"\s+", " "),
]


def normalize(name):
    s = name
    for pat, rep in STRIP:
        s = re.sub(pat, rep, s)
    return s.strip(" -—–·、,，.。:：;；/（()）")


def main():
    from flomo_client import load_token
    c = FlomoClient.__new__(FlomoClient)
    token, _ = load_token()
    c.__init__(token)
    c.init()
    # 标签解析与取卡逻辑同源（scan_qualified_concepts），不另写第二份实现。
    tags = resolve_tags(sys.argv[1:])
    seen = collect_memos(c, tags)

    clusters = {}
    for mid, m in seen.items():
        name = concept_of(m.get("content", ""))
        key = normalize(name)
        if key:
            clusters.setdefault(key, []).append((mid, name))

    dupes = {k: v for k, v in clusters.items() if len(v) > 1}
    print(f"[scan] 样本 {len(seen)} 张，归一化后同名簇 {len(dupes)} 组，涉及 {sum(len(v) for v in dupes.values())} 张")
    print("=" * 72)
    for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1])):
        print(f"\n【{k}】{len(v)} 张")
        for mid, name in v:
            print(f"    id={mid}  {name[:90]}")
    out = Path(__file__).resolve().parent.parent / "_tmp_concept_dupes.json"
    out.write_text(json.dumps(dupes, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"[out] {out}")


if __name__ == "__main__":
    main()
