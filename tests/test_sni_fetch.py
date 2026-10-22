#!/usr/bin/env python3
"""sni_fetch.py 的解析层回归用例（离线，不联网、不建连）。

这个脚本平时跑不通没关系（它专治 SNI 阻断），但它的**解析层**必须一直是对的：
取回的正文要经过 chunked 解码与响应头解析才落到文件，解错的表现不是报错，
而是「落盘了一个看起来很正常的短文件」——比失败更难发现。

覆盖：
  - chunked 正常多块；块扩展（`4;ext=1`）不参与长度解析；终止块即停；
  - 声明长度大于实际剩余字节时**返回已解部分**，不补齐、不装作完整
    （旧写法按声明长度切片会静默取半截并照样报成功）；
  - 响应头解析大小写不敏感，值里含冒号时只切第一个冒号（Location 里全是冒号）。

用法：python tests/test_sni_fetch.py   退出码 0=全过，1=有失败。
"""

COVERS = ("sni_fetch",)

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location("sni_fetch_mod", HERE / "sni_fetch.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

RESULTS = []


def check(label, cond, actual=None):
    RESULTS.append(bool(cond))
    tail = "" if cond or actual is None else f"  实际={actual!r}"
    print(f"{'PASS' if cond else 'FAIL'}  {label}{tail}")


def run_cases():
    # 1) 正常多块
    body = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    check("多块 chunked 拼回完整正文", S._dechunk(body) == b"Wikipedia", S._dechunk(body))

    # 2) 块扩展（chunk extension）不参与长度解析
    with_ext = b"4;ext=1\r\nWiki\r\n0\r\n\r\n"
    check("块扩展被忽略", S._dechunk(with_ext) == b"Wiki", S._dechunk(with_ext))

    # 3) 声明长度大于实际：返回已得部分，不补齐、不假装完整
    truncated = b"10\r\nabc\r\n0\r\n\r\n"
    got = S._dechunk(truncated)
    check("截断时返回实际已得字节（不按声明长度补齐）", got == b"abc\r\n0\r\n\r\n", got)
    check("截断时长度不被凑成声明值", len(got) != 16, len(got))

    # 4) 没有长度行：直接收尾，不抛
    check("无长度行返回空而不抛", S._dechunk(b"garbage") == b"", S._dechunk(b"garbage"))
    check("长度行非十六进制返回已解部分", S._dechunk(b"zz\r\nabc") == b"", S._dechunk(b"zz\r\nabc"))

    # 5) 响应头
    head = "HTTP/1.1 302 Found\r\nContent-Type: text/html\r\nLocation: https://x.test/a?b=1"
    check("响应头大小写不敏感", S._header(head, "content-type") == "text/html",
          S._header(head, "content-type"))
    check("值里的冒号不被切开", S._header(head, "location") == "https://x.test/a?b=1",
          S._header(head, "location"))
    check("缺失的头返回 None", S._header(head, "content-length") is None,
          S._header(head, "content-length"))
    check("不误命中前缀相同的头名", S._header(head, "content") is None,
          S._header(head, "content"))


if __name__ == "__main__":
    run_cases()
    print("---")
    ok = all(RESULTS)
    print(f"{'全部通过' if ok else '存在失败用例'}（{len(RESULTS)} 项）")
    sys.exit(0 if ok else 1)
