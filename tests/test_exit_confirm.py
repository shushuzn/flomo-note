#!/usr/bin/env python3
"""exit_confirm.py 回归用例（离线，不联网）。

exit_confirm 是「不建卡也必须留痕」的唯一机械闸门：对象清单、搜索记录、
查重输出、退出理由四项缺一即拦。本文件钉住它的每一条拦截与放行分支——
这些判据只能靠假数据构造，故全部走临时文件，不碰真实 _tmp 与 .sop_gate。
退出码 0 = 全部通过。
"""

COVERS = ("exit_confirm",)

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "exit_confirm.py"

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    line = f"{'PASS' if ok else 'FAIL'}  {label}"
    if not ok and detail:
        line += f"\n      {detail}"
    print(line)


def run(args):
    p = subprocess.run([sys.executable, str(SCRIPT), *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def good_search(tmp):
    f = tmp / "search.json"
    f.write_text(json.dumps({
        "searched": True,
        "terms": [{"term": "某对象", "query": "某对象 定义",
                   "conclusion": "查无此概念", "search_url": "https://example.com/a",
                   "in_body": []}]}, ensure_ascii=False), encoding="utf-8")
    return str(f)


def good_dedup(tmp):
    f = tmp / "dedup.txt"
    f.write_text("── 关键词 1/1：某对象 → 0 条\n", encoding="utf-8")
    return str(f)


REASON = "对象清单已逐个审视，均为纯事件或查无此概念，不建卡"

tmp = Path(tempfile.mkdtemp(prefix="exit_confirm_"))
try:
    # ① 四个必填参数一个都不能省
    rc, out, err = run([])
    check("不带任何参数：拒绝执行", rc != 0 and "--objects" in (err + out),
          f"rc={rc}, err={err[:160]!r}")
    for miss in ("--objects", "--search-log", "--dedup-log", "--reason"):
        args = {"--objects": "某对象", "--search-log": good_search(tmp),
                "--dedup-log": good_dedup(tmp), "--reason": REASON}
        args.pop(miss)
        rc, out, err = run([x for kv in args.items() for x in kv])
        check(f"缺 {miss}：拒绝执行", rc != 0 and miss in (err + out),
              f"rc={rc}, err={err[:160]!r}")

    base = ["--search-log", good_search(tmp), "--dedup-log", good_dedup(tmp),
            "--reason", REASON]

    # ② 对象清单为空 → 拦
    rc, out, err = run(["--objects", " , , ", *base])
    check("空对象清单被拦", rc == 1 and "对象清单为空" in err, f"rc={rc}, err={err[:160]!r}")

    # ③ 搜索记录的四条判据
    bad = tmp / "not_searched.json"
    bad.write_text(json.dumps({"searched": False,
                               "terms": [{"term": "x", "search_url": "u"}]}),
                   encoding="utf-8")
    rc, out, err = run(["--objects", "某对象", "--search-log", str(bad),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("searched 不为 true 被拦", rc == 1 and "searched" in err, f"rc={rc}, err={err[:160]!r}")

    empty_terms = tmp / "empty_terms.json"
    empty_terms.write_text(json.dumps({"searched": True, "terms": []}), encoding="utf-8")
    rc, out, err = run(["--objects", "某对象", "--search-log", str(empty_terms),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("terms 为空被拦", rc == 1 and "terms 为空" in err, f"rc={rc}, err={err[:160]!r}")

    no_url = tmp / "no_url.json"
    no_url.write_text(json.dumps({"searched": True,
                                  "terms": [{"term": "某对象", "query": "q",
                                             "conclusion": "无"}]},
                                 ensure_ascii=False), encoding="utf-8")
    rc, out, err = run(["--objects", "某对象", "--search-log", str(no_url),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("缺 search_url 被拦", rc == 1 and "search_url" in err, f"rc={rc}, err={err[:160]!r}")

    broken = tmp / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    rc, out, err = run(["--objects", "某对象", "--search-log", str(broken),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("搜索记录解析失败被拦", rc == 1 and "解析失败" in err, f"rc={rc}, err={err[:160]!r}")

    rc, out, err = run(["--objects", "某对象", "--search-log", str(tmp / "none.json"),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("搜索记录文件不存在被拦", rc == 1 and "不存在" in err, f"rc={rc}, err={err[:160]!r}")

    # ④ 查重输出：必须真的跑过 dedup
    fake = tmp / "fake_dedup.txt"
    fake.write_text("随便一段与查重无关的文字\n", encoding="utf-8")
    rc, out, err = run(["--objects", "某对象", "--search-log", good_search(tmp),
                       "--dedup-log", str(fake), "--reason", REASON])
    check("查重输出无「关键词」段被拦", rc == 1 and "关键词" in err, f"rc={rc}, err={err[:160]!r}")

    rc, out, err = run(["--objects", "某对象", "--search-log", good_search(tmp),
                       "--dedup-log", str(tmp / "none.txt"), "--reason", REASON])
    check("查重记录文件不存在被拦", rc == 1 and "dedup记录不存在" in err,
          f"rc={rc}, err={err[:160]!r}")

    # ⑤ 理由太短 → 拦
    rc, out, err = run(["--objects", "某对象", "--search-log", good_search(tmp),
                       "--dedup-log", good_dedup(tmp), "--reason", "不建卡"])
    check("退出理由太短被拦", rc == 1 and "太短" in err, f"rc={rc}, err={err[:160]!r}")

    # ⑥ 四项齐备 → 放行，并回显对象数
    rc, out, err = run(["--objects", "对象甲, 对象乙 ,对象丙", "--search-log", good_search(tmp),
                       "--dedup-log", good_dedup(tmp), "--reason", REASON])
    check("四项齐备：放行", rc == 0 and "退出留痕" in out, f"rc={rc}, out={out[:160]!r}")
    check("放行时按去空白后的对象计数", "3个对象" in out, f"out={out[:160]!r}")

    # ⑦ 拦截时把正确流程一并打出来（口头退出要能被机器驳回）
    rc, out, err = run(["--objects", "某对象", "--search-log", good_search(tmp),
                       "--dedup-log", good_dedup(tmp), "--reason", "不建卡"])
    check("拦截文案附正确流程指引", "fetch_article" in err, f"err={err[:200]!r}")

    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
