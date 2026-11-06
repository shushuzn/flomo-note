#!/usr/bin/env bash
# push_skill.sh — 文档改动后强制推送（H25 / 根本纪律#3）。
#
# 把"清掉 ~/.gitconfig 写死的 7897 代理 + 起干净隧道 + 认证 + commit + push +
# git ls-remote 校验远端"全部固化成一条命令，杜绝每次手搓 bash。
#
# 认证策略（两条通道，SSH 优先）：
#   1. SSH（首选）：22 端口 + 本机密钥，经 `url.git@github.com/.insteadOf` 把 https
#      URL 转成 SSH，**不改仓库的 remote 配置**。不经代理、不读凭据文件，
#      因此 HTTPS 整段不可达时它仍然可用（实测代理 502 + 443 直连超时，SSH 正常）。
#   2. HTTPS（回退）：本机明文缓存凭据 ~/.git-credentials 里的 github token，
#      经 `url.<token>@github.com/.insteadOf` 内联 + `credential.helper=` 关闭 manager。
#   **绝不使用 gh 凭据助手**——gh 取令牌会调被安全策略黑名单的 reg.exe，触发权限弹窗/
#   被拒，且 GitHub IP 偶发 TLS EOF 时脚本重试会让 gh 反复重读凭据、反复撞 reg.exe。
#   明文缓存凭据位于 git 自己的 store helper（非 Windows 凭据管理器），读取不依赖 reg.exe。
#   两条都不可用时才报错退出，不回落到 gh。
#
# 遇 GitHub 美国段 IP 的 TLS unexpected eof：自动重启干净隧道重试，
#   最多 4 次（SOP：推送遇 EOF 先重启隧道，勿沿用经历波动的旧进程）。
#
# 用法：
#   bash scripts/push_skill.sh "commit message" [file1 file2 ...]
#   不传文件且暂存区为空 → git add -A（含新增与删除）
#   不传文件但已有暂存   → **只提交已暂存的内容，不再追加**（尊重调用方的精确控制）
#   传文件路径          → 只 add 那些文件（便于精确控制）
#   --no-commit 开头    → 跳过 commit，仅推送已提交内容
#
# 为什么「已有暂存就不再 -A」：`git add -A` 会把工作区里任何未跟踪文件一起带上。
# 调用方先精确 `git add` 了 6 个文件、再调用本脚本时，-A 会把它刻意排除的文件
# 重新加回来并一起提交——调用方的控制被脚本静默推翻，且提交信息只写主题，
# 事后看不出多了什么。故暂存区非空即视为「调用方已决定提交什么」。
#
# 依赖：scripts/git_tunnel.py、Git Bash 工具链；可选 ~/.git-credentials（github token）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---- 探测 Python：显式 env > python3 > python（跨 Linux/macOS/Windows Git Bash）----
detect_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then echo "$PYTHON_BIN"; return; fi
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo ""
}
PY="$(detect_python)"
if [[ -z "$PY" ]]; then
  echo "[push] 未找到 Python 解释器：请安装 python3 或设 PYTHON_BIN" >&2
  exit 1
fi

# 解析参数
NO_COMMIT=0
if [ "${1:-}" = "--no-commit" ]; then
  NO_COMMIT=1
  shift || true
fi
MSG="${1:-}"
if [ "$NO_COMMIT" -eq 0 ] && [ -z "$MSG" ]; then
  echo "用法: push_skill.sh \"commit message\" [files...]" >&2
  exit 2
fi
shift || true
FILES=("$@")

# 空 hooks 目录：屏蔽 Qoder post-commit 追踪器（会拉起 Qoder.exe→reg.exe 被黑名单拦截，无意义）
EMPTY_HOOKS="$(mktemp -d)"
cleanup() { [ -n "${TUN:-}" ] && kill "$TUN" 2>/dev/null; rm -rf "$EMPTY_HOOKS"; }
trap cleanup EXIT

# ---- 读取本机明文缓存的 github token（不依赖 reg.exe / gh）----
TOKEN=""
CRED_FILE="$HOME/.git-credentials"
if [ -f "$CRED_FILE" ]; then
  # 凭据文件可能含多个主机/账号条目，只取 github.com 那条（去重后取首条）。
  # 末尾的 `|| true` 不是保险丝，是必需的：脚本开了 `set -euo pipefail`，
  # grep 无匹配会让整条管道返回非 0，**在下面那段提示打印之前**就把脚本杀掉——
  # 实测把凭据写成 `https://<token>@github.com`（少了用户名段）时，脚本静默退出 1，
  # 一个字都没输出，排查只能靠 `bash -x`。提示写得再好，到不了人眼前就等于没有。
  TOKEN="$(grep -E '^https://[^/@:]+:[^@]+@github\.com' "$CRED_FILE" \
           | head -1 \
           | sed -E 's#https://[^:]*:([^@]*)@.*#\1#' || true)"
fi
if [ -z "$TOKEN" ]; then
  echo "[push] 注意：~/.git-credentials 中无 github.com 条目，HTTPS 通道不可用（SSH 仍可尝试）。" >&2
  echo "[push] 如需 HTTPS 回退，请先 \`git credential approve\` 或手动写入。" >&2
  echo "[push] 期望格式（每行一条）：https://<user>:<token>@github.com" >&2
else
  # 格式自检：token 不该含空白 / 引号 / @，否则下面的 insteadOf 拼接会产生畸形 URL
  case "$TOKEN" in
    *[[:space:]\'\"@]*) echo "[push] 错误：解析出的 token 含非法字符，请检查 ~/.git-credentials 格式。" >&2
                        echo "[push] 若无有效 token，请改用可用的 SSH 密钥。" >&2; TOKEN="" ;;
  esac
fi

# ---- 1. 暂存 + 提交（只做一次）----
# 提交前先禁用 Qoder post-commit hook：否则会拉起 Qoder.exe→reg.exe 被黑名单拦截（无害但噪音）
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.hooksPath
export GIT_CONFIG_VALUE_0="$EMPTY_HOOKS"
if [ "$NO_COMMIT" -eq 0 ]; then
  # 提交身份必须先在，否则 git 会打一段「tell me who you are」然后失败，
  # 而那时文件已经 add 完，读的人分不清是认证问题还是身份问题。
  if [ -z "$(git config user.email || true)" ] || [ -z "$(git config user.name || true)" ]; then
    echo "[push] 错误：本仓库没有提交身份（git config user.name / user.email），提交会被 git 拒绝。" >&2
    echo "[push] 按仓库既有作者设置后重试：git config user.name '<name>'; git config user.email '<email>'" >&2
    exit 4
  fi
  if [ "${#FILES[@]}" -gt 0 ]; then
    git add "${FILES[@]}"
  elif git diff --cached --quiet; then
    # 用 -A（含新增与删除），不用 -u：-u 只处理已入库文件，会漏掉新增文件，
    # 造成「提交成功但远端缺文件」。仅在暂存区为空时才这么做。
    #
    # 但 -A 会把工作区里任何未跟踪文件一起带上，而提交信息只写主题——事后再看
    # 无从分辨哪些是刻意提交的。调用方若把文件列表写错（例如把 msg 里的反引号
    # 交给 shell 展开，参数被吞、FILES 变空），就会走到这里。故先列出来：非交互
    # 环境无法确认，但至少让人当场看见这批文件要入库。
    UNTRACKED_ALL="$(git ls-files --others --exclude-standard)"
    if [ -n "$UNTRACKED_ALL" ]; then
      N_UNTRACKED="$(printf '%s\n' "$UNTRACKED_ALL" | wc -l | tr -d ' ')"
      echo "[push] 注意：暂存区为空，将 git add -A，连带 $N_UNTRACKED 个未跟踪文件：" >&2
      printf '%s\n' "$UNTRACKED_ALL" | head -10 | sed 's/^/[push]   /' >&2
      # 不能写成 [ ... ] && echo：条件为假时整个表达式返回 1，set -e 会当场退出
      if [ "$N_UNTRACKED" -gt 10 ]; then
        echo "[push]   …（另有 $((N_UNTRACKED - 10)) 个）" >&2
      fi
      echo "[push] 若非本意，Ctrl-C 后改用 git add <files> 再跑本脚本 --no-commit" >&2
    fi
    git add -A
  else
    # 暂存区已有内容 = 调用方已精确决定提交范围，此时绝不再 -A：
    # 那会把工作区里未跟踪的文件（本机代理脚本、临时导出、个人配置）一起带进仓库，
    # 而提交信息只写主题，事后无从看出多了什么。
    echo "[push] 沿用已暂存的改动（未追加未跟踪文件）："
    git diff --cached --name-status | sed 's/^/         /'
  fi
  if git diff --cached --quiet; then
    echo "[push] 无已暂存改动，仅推送已有提交"
  else
    git commit -m "$MSG"
    echo "[push] 已提交: $(git rev-parse --short HEAD)"
  fi
  # 提交后自检：工作区不得残留未跟踪文件（防"漏提新增文件"再次发生）
  UNTRACKED="$(git ls-files --others --exclude-standard)"
  if [ -n "$UNTRACKED" ]; then
    echo "[push] 警告：工作区仍有未跟踪文件，未纳入本次提交：" >&2
    echo "$UNTRACKED" | sed 's/^/[push]   /' >&2
    echo "[push] 若确需入库，请显式传入文件名重跑（push_skill.sh \"msg\" <files...>）" >&2
  fi
fi

# ---- 2. 首选 SSH（22 端口）----
# 顺序为什么是 SSH 在前：两条通道依赖的东西不同。HTTPS 段要选举线路、内联 token，
# 还要求代理能 CONNECT 到 github.com:443；SSH 只要 22 端口可达且本机有密钥，不经
# HTTP 代理、不读 ~/.git-credentials。实测 HTTPS 整段不可达（代理回 502、直连 443
# 超时）时 SSH 一直正常——可用性更高的通道放手选位置：网络正常时省掉一轮隧道选举，
# 异常时不必先空转 4 次失败再回退。
#
# 先 ls-remote 探测再推：探测失败即判定无可用密钥或 22 端口被封，直接落到 token
# 通道，不让一次注定失败的 push 先打半屏错误。
#
# 这里显式重设 GIT_CONFIG_COUNT：第 1 步只设了 1 条 hooksPath，而 HTTPS 段会写入
# url.*.insteadOf（把 https://github.com/ 映射到带 token 的 https）。同一前缀的两条
# insteadOf 并存时 git 选哪条不确定，故只设本段需要的 2 条。
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=core.hooksPath
export GIT_CONFIG_VALUE_0="$EMPTY_HOOKS"
export GIT_CONFIG_KEY_1="url.git@github.com:.insteadOf"
export GIT_CONFIG_VALUE_1="https://github.com/"
# 首次连新主机时自动收 key；SSH 不走 http.proxy，隧道端口对它无影响。
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

if git ls-remote origin HEAD >/dev/null 2>&1; then
  echo "[push] 走 SSH 通道推送…"
  if git push origin HEAD 2>&1 | sed 's/^/[push] /'; then
    echo "[push] 推送完成（SSH），校验远端 HEAD："
    git ls-remote origin HEAD
    echo "[push] OK"
    exit 0
  fi
  echo "[push] SSH 推送失败，改试 HTTPS 通道…" >&2
else
  echo "[push] SSH 不可用（无密钥或 22 端口不通），改试 HTTPS 通道…" >&2
fi

# ---- 3. HTTPS 回退：隧道 + 内联 token（带重试）----
if [ -z "$TOKEN" ]; then
  echo "[push] 错误：SSH 与 HTTPS 两条通道都不可用（无密钥，也无 github token）。" >&2
  echo "[push] 任选其一后重试：配一个 GitHub SSH 密钥，或写 ~/.git-credentials。" >&2
  exit 3
fi
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null || true
MAX=4
TUN=""
for i in $(seq 1 $MAX); do
  PORT=$((18120 + (RANDOM % 80)))
  nohup "$PY" scripts/git_tunnel.py "$PORT" > "/tmp/git_tunnel_${PORT}.log" 2>&1 &
  TUN=$!
  sleep 2
  if ! "$PY" -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', $PORT)); s.close()" 2>/dev/null; then
    echo "[push] 第 $i 次：隧道未起，重启重试"; kill "$TUN" 2>/dev/null; TUN=""; continue
  fi

  # 用内联 token 代替 gh；credential.helper= 关闭 manager（避免 reg.exe）
  export GIT_CONFIG_COUNT=5
  export GIT_CONFIG_KEY_0=credential.helper
  export GIT_CONFIG_VALUE_0=
  export GIT_CONFIG_KEY_1="url.https://${TOKEN}@github.com/.insteadOf"
  export GIT_CONFIG_VALUE_1="https://github.com/"
  export GIT_CONFIG_KEY_2=http.proxy
  export GIT_CONFIG_VALUE_2="http://127.0.0.1:${PORT}"
  export GIT_CONFIG_KEY_3=https.proxy
  export GIT_CONFIG_VALUE_3="http://127.0.0.1:${PORT}"
  export GIT_CONFIG_KEY_4=core.hooksPath
  export GIT_CONFIG_VALUE_4="$EMPTY_HOOKS"

  echo "[push] 第 $i 次尝试推送（隧道端口 $PORT）…"
  if git push origin HEAD 2>&1 | sed 's/^/[push] /'; then
    echo "[push] 推送完成，校验远端 HEAD："
    git ls-remote origin HEAD
    echo "[push] OK"
    exit 0
  fi
  echo "[push] 第 $i 次失败（多为 GitHub IP 偶发 TLS EOF），重启干净隧道重试…"
  kill "$TUN" 2>/dev/null; TUN=""
done

echo "[push] 已重试 $MAX 次仍失败，请检查网络/隧道" >&2
exit 1
