#!/usr/bin/env python3
"""cleanup.py — 每轮收尾强制清理残留临时文件（H27）。

背景：写卡 / 改技能文档会产生大量中间产物——工作区 `_tmp_*`、临时目录里的抓取件
（`arxiv_*` / `ithome_*` / `flomo_*`）、历轮请求与回执。这些必须每轮归档删除，
否则堆积会被判定严重违规。此前每次手搓清理脚本，既重复又易漏。
本脚本把"显式列名→打包 trash→断言成员数→删除→复核残留 0"固化。

**平台自适应**：临时目录按 `tempfile.gettempdir()` 解析（Linux/macOS 为 `/tmp`，
Windows 为 `%TEMP%`）。同时兼容历史遗留的 `D:\\tmp` —— 若该目录存在则一并纳入扫描
（Windows 老环境），不存在则自动跳过（Linux 下即为此情形，不报错）。

近轮窗口
    只清「与近轮无关的历史残留」（H24/H27）：`collect()` 跳过修改时间落在
    `RECENT_WINDOW_MINUTES` 之内的项，故当轮与近轮的抓取原文、草稿、请求 JSON
    不会被同一轮收尾即刻删掉；窗口之外才参与归档删除。窗口可用 `--keep-minutes`
    覆盖（0 = 关闭窗口，全部参与清理）。

保留项（绝不删）：
  - `.workbuddy/trash/*`（历史备份包，即归档目标本身）
  - 临时目录内的他项目旧件（`arxiv_test.xml`、`arxiv_vibe.xml`）
  - 临时目录内的活动工作区（`flomo-push` 仓库镜像、`sounding_flomo` 审计目录）
  - 落在近轮窗口内的全部项（见上）

删除范围（文件与目录一并处理）：
  - 项目根：`_tmp_*`、`tmp_*`、`memo_body*.txt`（写卡草稿，内容已入云）
    **`tmp_*` 与 `_tmp_*` 是同义词**：两者都属历轮写卡/审查的瞬时产物，
    `.gitignore` 早已把两类一并忽略。此前项目根规则只列 `_tmp_*`，导致
    365 个 `tmp_*` 文件长期堆在根目录，且 `--verify` 复用同一份 SCAN，
    报出的「残留 = 0」与事实不符（校验器与被校验对象用了两套口径）。
    两处口径必须同源，新增前缀时同时改 `SCAN` 与本段说明。
  - 临时目录：抓取件（`arxiv_*` / `ithome_*`）、flomo 包与备份（`flomo_*` /
              `flomo-note*`）、sounding 链（`sounding_*` / `sounding.tgz`）、
              历轮中间产物（`h15*` / `kilo_removed_archive` / `*_body.txt` /
              `*_create.json` / `*_update.txt` / `*_upd.json` / `all_memos.json` /
              `agi_*`），显式排除上述保留项
  - `.workbuddy/lb_out/`：`*_create.json`、`*_memo.txt`、`*.err`

保留策略（每轮清理后顺带执行，防无界增长）：
  - `.workbuddy/trash/` 只留最近 `ARCHIVE_KEEP` 个 `temp-cleanup-*.tar.gz`
  - `.sop_gate/` 删除修改时间超过 `GATE_CRED_MAX_AGE_HOURS` 的凭证
    （凭证本身有效期 6 小时，超期文件只占地方、不可能再被放行）

用法：
  python scripts/cleanup.py                    # 执行清理（默认）
  python scripts/cleanup.py --dry-run          # 只列名不删
  python scripts/cleanup.py --verify           # 只复核残留是否为 0（CI / 自校，非 0 则退出码 1）
  python scripts/cleanup.py --list-rules       # 打印生效的扫描与保留规则
  python scripts/cleanup.py --keep-minutes N   # 覆盖近轮窗口（0 = 关闭）
"""
import argparse
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRASH_DIR = PROJECT_ROOT / ".workbuddy" / "trash"
LB_OUT = PROJECT_ROOT / ".workbuddy" / "lb_out"

# 临时目录候选：当前平台的标准临时目录，外加历史遗留的 Windows D:\tmp（存在才用）
_TMP_CANDIDATES = [Path(tempfile.gettempdir())]
_LEGACY_TMP = Path("D:/tmp")
if _LEGACY_TMP.is_dir() and _LEGACY_TMP not in _TMP_CANDIDATES:
    _TMP_CANDIDATES.append(_LEGACY_TMP)

# (基准目录, [glob 模式...])
# 临时目录规则按"本项目产物特征"覆盖，而非零散白名单——白名单漏项会让
# `--verify` 在残留尚存时误报「残留 = 0」（曾漏掉目录形态的中间产物）。
SCAN = [
    (PROJECT_ROOT, ["_tmp_*", "tmp_*", "memo_body*.txt"]),
    # `_tmp/` 是写卡与改文档的实际工作区（草稿、请求体、抓取件都落这里）。
    # 此前规则只写了 `_tmp_*`（前缀形态），**目录形态的 `_tmp/` 一个都匹配不上**——
    # 实测堆了 580 个历轮中间产物，而 --verify 与清理共用这份 SCAN，
    # 于是每轮都报「残留 0」：漏项让检查恒真，比不检查更坏。
    (PROJECT_ROOT / "_tmp", ["*"]),
    (LB_OUT, ["*_create.json", "*_memo.txt", "*.err"]),
]
for _t in _TMP_CANDIDATES:
    SCAN.append((_t, [
        # 网页/文章抓取件
        "arxiv_*.html", "arxiv_*_abs.html", "arxiv_*.pdf", "ithome_*.html",
        # flomo 相关：dl / 备份 / 技能包（flomo-push 为活动镜像，见 KEEP）
        "flomo_*", "flomo-note*",
        # sounding 审计工具链（sounding_flomo 为审计保留目录，见 KEEP）
        "sounding_*", "sounding.tgz",
        # 历轮中间产物：改动集工作目录、抽取正文/请求体、批量导出
        "h15*", "kilo_removed_archive",
        "*_body.txt", "*_create.json", "*_update.txt", "*_upd.json",
        "all_memos.json", "all.json",
        "agi_body.txt", "agi_create.json", "agi_full.txt",
        # 写卡工作区（每次写卡建的 /tmp/wrN 目录）
        "wr[0-9]*", "wr[0-9]*/",
        # 裸请求体小文件（q*_tmp.json / yA_tmp.json 之类）
        "*_tmp.json", "*_tmp[0-9A-Z].json",
    ]))

# 显式保留（命中 glob 也不删）：键为所在目录，值为文件名集合
KEEP = {}
for _t in _TMP_CANDIDATES:
    # sounding_flomo：审计工具保留目录；flomo-push：仓库活动镜像（推送用，勿删）
    KEEP[_t] = {"arxiv_test.xml", "arxiv_vibe.xml", "sounding_flomo", "flomo-push"}


# 近轮窗口（分钟）：修改时间落在此窗口内的项视为当轮/近轮素材，按 H24/H27 保留，
# 不参与清理；窗口之外的才属「与近轮无关的历史残留」。0 = 关闭窗口（全部参与清理）。
# 窗口值属脚本参数，随环境调整时只改此处或走 --keep-minutes，文档不复述。
RECENT_WINDOW_MINUTES = 24 * 60

# 保留策略：清理只保证「本轮残留归零」，不解决「归档与凭证长期累积」。
# 实测 19 天留下 126 个归档（42 MB）、150 份凭证（1.3 MB，有效期仅 6 小时）。
# 二者都是可再生的中间态，按数量/时长收敛，避免工作区无界膨胀。
GATE_DIR = PROJECT_ROOT / ".sop_gate"
ARCHIVE_KEEP = 10                    # trash 里保留最近多少个归档案
GATE_CRED_MAX_AGE_HOURS = 24         # 凭证文件保留时长（凭证本身 6 小时失效）


def _arcname(p: Path) -> str:
    """归档条目名：统一为 POSIX 分隔符，并剥掉盘符与前导斜杠。

    tarfile 存储成员名时会把 `os.sep` 归一化为 `/`，再剥掉前导 `/`。生成端
    必须按同一口径产出条目名：若只对 `str(p)` 去冒号，Windows 上的反斜杠路径
    会生成 `D\\OpenClaw\\...` 形式的条目名，与归档内实际写入的 `D/OpenClaw/...`
    永不相等，于是「归档完整性」断言恒失败，而该断言位于删除循环之前——
    结果是每次收尾都归档成功却删除不到任何文件，残留清零永远达不成。
    """
    return p.as_posix().replace(":", "").lstrip("/")


def _force_remove(p: Path):
    """删除文件或目录树。

    目录形态的残留同样要能清（如改动集工作目录），此前只 os.remove 文件，
    遇目录直接抛错 → 残留永远清不掉。
    """
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
        return
    try:
        p.chmod(stat.S_IWRITE)
    except OSError:
        pass
    try:
        os.remove(p)
    except FileNotFoundError:
        pass


def collect(keep_minutes=None, skipped=None, now=None):
    """收集应清理的残留；修改时间落在近轮窗口内的项跳过（H24/H27）。

    `keep_minutes=None` 取默认窗口，0 表示关闭窗口。命中的近轮项追加进
    `skipped`（调用方传列表时），供打印可见性，不做静默放行。

    `now` 是本轮的基准时刻：清理与复核必须共用同一个 cutoff。此前两处各调
    `time.time()`，一轮里跨过窗口边界的项会出现「收集时还算近轮被跳过、几秒后
    复核时已过期算残留」——打包 24 个文件耗掉的几秒就足以让删除成功被判成失败
    （实测 it_283.txt 距边界 1 秒，清理报「残留 1 个」而文件根本没被列入候选）。
    """
    if keep_minutes is None:
        keep_minutes = RECENT_WINDOW_MINUTES
    base = time.time() if now is None else now
    cutoff = base - keep_minutes * 60 if keep_minutes > 0 else None
    candidates = []
    for base, patterns in SCAN:
        if not base.exists():
            continue
        for pat in patterns:
            for p in base.glob(pat):
                keep = KEEP.get(base)
                if keep is not None and p.name in keep:
                    continue
                if cutoff is not None:
                    try:
                        if p.stat().st_mtime >= cutoff:
                            if skipped is not None:
                                skipped.append(p)
                            continue
                    except OSError:
                        pass
                candidates.append(p)
    # 去重并排序，稳定输出
    uniq = sorted({str(p) for p in candidates})
    return [Path(p) for p in uniq]


def verify(keep_minutes=None, now=None):
    return collect(keep_minutes, now=now)


def prune(keep_archives=ARCHIVE_KEEP, cred_max_age_hours=GATE_CRED_MAX_AGE_HOURS,
          dry_run=False):
    """收敛归档与凭证：trash 只留最近 N 个包，过期凭证删除。

    两条都只碰**可再生中间态**：归档包里的文件早已删除、凭证 6 小时即失效。
    删除前不归档（否则归档目录自身会再膨胀一轮），但会打印被删项供核对。
    返回 (删掉的归档案列表, 删掉的凭证列表)。
    """
    gone_archives, gone_creds = [], []

    if TRASH_DIR.is_dir():
        arcs = sorted(
            TRASH_DIR.glob("temp-cleanup-*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in arcs[keep_archives:]:
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    continue
            gone_archives.append(p)

    if GATE_DIR.is_dir():
        cutoff = time.time() - cred_max_age_hours * 3600
        for p in sorted(GATE_DIR.glob("*.json")):
            try:
                if p.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    continue
            gone_creds.append(p)

    return gone_archives, gone_creds


def list_rules():
    """打印当前生效的扫描/保留规则，供文档指向本脚本自描述（勿在文档里复述清单）。"""
    print("[cleanup] 扫描规则（基准目录 → glob 模式）：")
    for base, pats in SCAN:
        print(f"  {base}")
        for pat in pats:
            print(f"    {pat}")
    print("[cleanup] 显式保留（命中 glob 也不删）：")
    for base, names in KEEP.items():
        if names:
            print(f"  {base}: {', '.join(sorted(names))}")
    print("[cleanup] 归档目录:", TRASH_DIR)
    print(f"[cleanup] 近轮窗口: 窗口内（mtime ≥ now - {RECENT_WINDOW_MINUTES} 分钟）的项保留，不参与清理")
    print(f"[cleanup] 保留策略: trash 留最近 {ARCHIVE_KEEP} 个归档；"
          f"{GATE_DIR} 删除超过 {GATE_CRED_MAX_AGE_HOURS} 小时的凭证")


def _report_prune(gone_archives, gone_creds, dry_run):
    verb = "将删除" if dry_run else "已删除"
    if not gone_archives and not gone_creds:
        print("[cleanup] 保留策略：归档与凭证均未超限，无需收敛")
        return
    print(f"[cleanup] 保留策略：{verb} {len(gone_archives)} 个旧归档、"
          f"{len(gone_creds)} 份过期凭证")
    for p in gone_archives:
        print(f"  归档 {p.name}")
    for p in gone_creds[:5]:
        print(f"  凭证 {p.name}")
    if len(gone_creds) > 5:
        print(f"  凭证 ……另有 {len(gone_creds) - 5} 份")


def main():
    ap = argparse.ArgumentParser(description="每轮收尾强制清理残留临时文件 (H27)")
    ap.add_argument("--dry-run", action="store_true", help="只列名不删")
    ap.add_argument("--verify", action="store_true", help="只复核残留是否为 0")
    ap.add_argument("--list-rules", action="store_true", help="打印生效的扫描与保留规则后退出")
    ap.add_argument("--keep-minutes", type=int, default=RECENT_WINDOW_MINUTES,
                    help=f"近轮窗口（分钟）：窗口内视为近轮素材不清理；0 = 关闭窗口（默认 {RECENT_WINDOW_MINUTES}）")
    args = ap.parse_args()
    # 本轮基准时刻：收集、复核共用（见 collect 的 now 参数说明）。
    run_time = time.time()

    if args.list_rules:
        list_rules()
        return

    if args.verify:
        skipped = []
        left = collect(args.keep_minutes, skipped, now=run_time)
        if left:
            print(f"[cleanup] 残留 {len(left)} 个（应清未清）：")
            for p in left:
                print(f"  {p}")
            sys.exit(1)
        print(f"[cleanup] 残留 = 0，OK（近轮素材保留 {len(skipped)} 个）")
        return

    skipped = []
    candidates = collect(args.keep_minutes, skipped, now=run_time)
    print(f"[cleanup] 待清理 {len(candidates)} 个（近轮素材保留 {len(skipped)} 个）：")
    for p in candidates:
        print(f"  {p}")

    if args.dry_run:
        print("[cleanup] --dry-run，未删除")
        _report_prune(*prune(dry_run=True), dry_run=True)
        return

    if not candidates:
        print("[cleanup] 无残留，无需清理")
        _report_prune(*prune(dry_run=False), dry_run=False)
        return

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = TRASH_DIR / f"temp-cleanup-{ts}.tar.gz"

    want = {_arcname(p) for p in candidates}
    with tarfile.open(archive, "w:gz") as tf:
        for p in candidates:
            tf.add(p, arcname=_arcname(p))
    # 断言归档完整：每个候选的顶层条目都必须出现在归档中。
    # （不能拿 len(getmembers()) 比候选数——目录会被展开成多个成员，那是必然不等。）
    with tarfile.open(archive) as tf:
        have = {m.name for m in tf.getmembers()}
        members = len(have)
    missing = want - have
    assert not missing, f"归档缺失 {len(missing)} 项：{sorted(missing)[:5]}"

    for p in candidates:
        _force_remove(p)

    left = verify(args.keep_minutes, now=run_time)
    assert len(left) == 0, f"复核失败，残留 {len(left)} 个"
    print(f"[cleanup] 已归档 {archive}（顶层 {members} 个），删除并复核残留 = 0，OK")
    _report_prune(*prune(dry_run=False), dry_run=False)


if __name__ == "__main__":
    main()
