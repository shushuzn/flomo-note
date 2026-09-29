#!/usr/bin/env python3
"""flomo_client.py 写后回读验收（H11）的离线回归用例。

全部用例**不联网**：`readback_check` 通过注入假 client 桩验证，
只测「比对与判定」这套机械逻辑本身。

退出码 0 = 全部通过。
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FC = _load("flomo_client", "flomo_client.py")

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))


class FakeClient:
    """桩客户端：只实现回读用到的 memo_batch_get，返回可注入的响应。"""

    def __init__(self, memos=None, raises=None):
        self.calls = []
        self._memos = memos if memos is not None else []
        self._raises = raises

    def tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if self._raises is not None:
            raise self._raises
        return {"content": [{"type": "text",
                             "text": json.dumps({"memos": self._memos}, ensure_ascii=False)}],
                "structuredContent": {"memos": self._memos}}


BODY = "#科技/机器人\n某测试概念名\n\n结论句。\n\n要点：\n- 要点一\n"


def run_norm_cases():
    """_norm_body 只忽略空白差异，非空白字符与行序一律不宽容。"""
    check("_norm_body 空行不参与比对（连续空行与首尾空行一并不计）",
          FC._norm_body("A  \n\n\nB\n") == "A\nB",
          repr(FC._norm_body("A  \n\n\nB\n")))
    check("_norm_body 统一 CRLF",
          FC._norm_body("A\r\nB") == FC._norm_body("A\nB"))
    check("_norm_body 无空行与有空行等价（云端会自行补空行）",
          FC._norm_body("A\nB") == FC._norm_body("A\n\nB"),
          repr(FC._norm_body("A\n\nB")))
    check("_norm_body 统一行尾空格",
          FC._norm_body("A  \nB\t") == FC._norm_body("A\nB"))
    check("_norm_body 合并成一行必须暴露（行序/行界变化是内容差异）",
          FC._norm_body("A\nB") != FC._norm_body("AB"),
          repr(FC._norm_body("AB")))
    check("_norm_body 不改动非空白字符（错别字必须暴露）",
          FC._norm_body("续航 430 公里") != FC._norm_body("续航 450 公里"))


def run_readback_cases():
    """readback_check 的五类判定。"""
    # 1) 一致（含空白差异）→ ok，且带实测字数
    c = FakeClient([{"id": "M1", "content": BODY.replace("\n", "\r\n") + "\n\n"}])
    ok, detail = FC.readback_check(c, "M1", BODY)
    check("回读一致（仅空白差异）判过", ok and "回读一致" in detail, detail)
    check("回读按 id 拉全文（调 memo_batch_get）",
          c.calls == [("memo_batch_get", {"ids": ["M1"]})], str(c.calls))

    # 1b) 云端在标签段后与列表引导行后自行补空行 → 仍判过（否则每张新卡都误报失败）
    cloud_style = BODY.replace("#科技/机器人\n", "#科技/机器人\n\n").replace("要点：\n", "要点：\n\n")
    check("云端补出的空行（标签段后 / 要点后）不影响判定",
          FC.readback_check(FakeClient([{"id": "M1", "content": cloud_style}]), "M1", BODY)[0],
          cloud_style.replace("\n", "\\n"))

    # 2) 内容不一致 → 判失败，且给出字数与首个差异位置（可定位）
    c = FakeClient([{"id": "M1", "content": BODY.replace("要点一", "要点二")}])
    ok, detail = FC.readback_check(c, "M1", BODY)
    check("回读内容不一致判失败", not ok and "不一致" in detail, detail)
    check("失败信息含字数与首个差异位置",
          "个字符" in detail and "写入" in detail, detail)
    check("失败信息禁止据此重发写请求", "禁止据此重发写请求" in detail, detail)

    # 3) 回读为空（id 对不上库 / 写没落库）→ 判失败
    ok, detail = FC.readback_check(FakeClient([]), "M1", BODY)
    check("回读为空判失败", not ok and "未取到 memo" in detail, detail)

    # 4) 回读返回的是另一张卡 → 判失败
    ok, detail = FC.readback_check(FakeClient([{"id": "M9", "content": BODY}]), "M1", BODY)
    check("回读 id 不符判失败", not ok and "另一张卡" in detail, detail)

    # 5) 正文被云端截断 → 判失败（截断就没法逐字比对，不得当成功）
    ok, detail = FC.readback_check(
        FakeClient([{"id": "M1", "content": BODY, "content_truncated": True}]), "M1", BODY)
    check("回读被截断判失败", not ok and "截断" in detail, detail)

    # 6) 云端调用抛错应向上传播（由 CLI 入口转退出码），不得被吞成「验收通过」
    try:
        FC.readback_check(FakeClient(raises=FC.FlomoError("boom")), "M1", BODY)
        raised = False
    except FC.FlomoError:
        raised = True
    check("回读调用失败不得被吞（抛 FlomoError）", raised)


def run_write_path_cases():
    """写命令内部必须真的调用回读（结构锁定，防被无意摘掉）。"""
    src = (SCRIPT_DIR / "flomo_client.py").read_text(encoding="utf-8")
    m = src[src.index("def main():"):]
    check("写成功分支内调用 readback_check",
          "readback_check(client, created_id" in m, "main() 内未调用")
    check("回读未过时非 0 退出",
          "回读验收未过" in m and "return 1" in m)
    check("写入正文取自本次请求参数（验收对象正确）",
          'arguments.get("content")' in m)


if __name__ == "__main__":
    run_norm_cases()
    run_readback_cases()
    run_write_path_cases()
    print("---")
    print("全部通过" if all(RESULTS) else "存在失败用例")
    sys.exit(0 if all(RESULTS) else 1)
