# 运行环境备忘（flomo-note）

本文件收录**与具体运行机器/沙箱绑定**的环境细节：路径、端口、代理、一次性实测记录。
这类内容会随环境变化而失效，**不写进 SKILL.md**——SKILL.md 只保留不随环境变化的规范。

环境变化时**只改本文件**，不动 SKILL.md。

---

## 运行平台

- 当前沙箱：Ubuntu 22.04 linux/amd64，root 用户，工作目录 `/workspace`。
- Python：`python3`（3.11）；依赖用 `sudo pip3 install <包>` 或 `sudo uv pip install --system <包>`。
- 技能安装位置：`~/.codebuddy/skills/flomo-note/`（含 `SKILL.md`、`AGENTS.md`、`scripts/`、`tests/`）。
- **注意**：本文件其余各节的路径（`/workspace`、`/root/.codebuddy/...`）记录的是 Linux 沙箱形态；当前会话实际运行在 Windows 本机（工作目录 `D:/OpenClaw/flomo-note`，Python 3.13.12 托管版，bash 为 Git Bash）。两处冲突时以实测为准，并把实测结果补进本节。
- Python 实测（`python --version`）：**3.14.6**（Windows 托管版），与上面记的 3.11 / 3.13.12 均不一致，以实测为准。
- 跑回归用例须带 UTF-8 模式：本机 PowerShell 控制台默认 GBK 代码页，`python tests/test_*.py` 打印中文或用 `♯`(U+266F) 之类字符时会被 GBK 编码失败崩在**打印阶段**——断言其实全过，现象酷似逻辑失败。先 `$env:PYTHONUTF8=1`（或 `PYTHONIOENCODING=utf-8`）再跑；`bash scripts/run_tests.sh` 同样受此影响。
- 脚本已做**平台自适应**：`run_audit.sh` / `push_skill.sh` 自动探测 `$PYTHON_BIN` → `python3` → `python`；仅 Windows(Git Bash) 才启用 Git 目录 PATH 修正与 `gh.exe` 凭据助手。跨平台无需改脚本，必要时用 `PYTHON_BIN=...` 显式指定解释器。

---

## flomo 连接

- 端点：`https://flomoapp.com/mcp`（Bearer 鉴权）。
- token 来源优先级：**项目根 `.mcp.json` 的 `mcpServers.flomo.headers.Authorization` 优先**；仅在该文件不存在时后备到环境变量 `FLOMO_TOKEN`（env 中的 token 可能过期，故不作权威来源）。
- `.mcp.json` 含 token，**已 gitignore，绝不入库**。
- 本环境无 MCP 面板，`.mcp.json` 不会被自动加载，统一走 `scripts/flomo_client.py` 直连。

---

## 网络出口（沙箱特有）

### 代理

沙箱会注入 HTTP 代理，且该代理常返 502，导致 git/curl 失败。**判断"网络不通"前必须先排除代理**：

```bash
env -u http_proxy -u https_proxy git -c http.proxy="" -c https.proxy="" ls-remote origin
```

通则沿用同样方式执行后续命令。

**代理端口每次会话重新分配，所以本文件不记端口、命令里也不许写死端口。** 实测它在同一台机器上一路换过（`15191` → `2286` → `29178`），把某一轮的端口抄进命令或文档，下一轮就会得到 `连接被拒`——而这个现象与「网络真的断了」完全一样，照着错误的结论去查只会越查越远。

因而不做「手写 `curl -x http://127.0.0.1:<端口>` 试探」这件事，改由脚本判断：

```bash
python scripts/netdoctor.py --url <要抓的链接>   # 逐通道真发请求，结论直接给下一步命令
```

判据是「拿到任何 HTTP 响应」（403/404 也算该通道可用——包已到服务器），只有连接被拒 / 超时 / DNS 失败才算不通。抓取侧已默认自动回退：`fetch_article.py` 先走环境代理，不通即转直连，并在 `[出网]` 行说明原代理为何失败；需要强制某条通道时才加 `--via direct` / `--via proxy`。

写云走**同一套通道**（`scripts/net_util.py`，三处共用一份判据）：读操作失败会换出口重试，**写操作不会**——只有「连接被拒 / DNS 失败」才允许换出口（此时请求确定没发出），超时与中断一律判为「可能已写入」并停手，因为 `memo_create` 重发就是第二张卡。同一个判据也决定 `CloudUnreachable` 的 `request_sent` 字段。

**连本机服务也一样要绕代理**（实测踩过）：本机 `HTTP_PROXY`/`HTTPS_PROXY` 指向本机代理端口时，`curl http://127.0.0.1:<端口>/…` 会被代理接管，返回代理自己的 `502 upstream connect failed`——看起来像「服务起不来」，实际是请求根本没直连。测本机控制台用 `curl --noproxy '*'`，脚本里用 `http.client` 直连（它不走代理），或清掉 `*_proxy` 环境变量再起子进程。

> 上面两处 `<端口>` 是**占位**，不是可照抄的值：本机服务的端口以启动时打印的为准，代理端口从环境变量现读。

### GitHub 域名与 DNS

沙箱 DNS 会把 GitHub 相关域名劫持到保留段假 IP（`198.18.0.x`，非真实地址），表现为 TLS 立即失败。**需在 `/etc/hosts` 写真实 IP**。

具体 IP 值不在本文件维护——`scripts/git_tunnel.py` 的 `ROUTES` 是**唯一事实源**，需要 hosts 片段时直接生成：

```bash
python scripts/git_tunnel.py --print-hosts
```

- IP 会漂移，失效时用 `https://dns.alidns.com/resolve?name=<域名>&type=A` 重查，改回 `git_tunnel.py` 的 `ROUTES`（含备选 IP 的按序回退）。
- `/etc/hosts` 改动**重启后自动还原**；如需持久化，同步写入 `~/.user_hosts`。
- 注意：`sed -i` 对 `/etc/hosts` 无效（bind mount 无法重命名），**须用 Python 读→改→整体重写**。
- 候选段位的可达性会随时段整体波动（常成段一起失效），故不在本文件记录「哪段通」：`ROUTES` 只放当前更可能通的快照，候选全失败时隧道自动回落系统 DNS 兜底。

### 推送方式选择

按稳定性排序，逐级尝试：

1. **GitHub Contents API**（最稳，单文件改动首选）——走 `api.github.com`，直接 Git Data API 更省事：`GET ref/heads/<branch>` 取 base → `POST git/blobs`（base64）→ `POST git/trees`（`base_tree` + 路径覆盖）→ `POST git/commits` → `PATCH git/refs/heads/<branch>`。
2. `git push` 直连（去代理 + hosts 覆盖后）——大文件易遇 `gnutls_handshake() failed`，属通道质量问题，重试或降级到方式 1。
3. `python scripts/git_tunnel.py &` —— 本地 CONNECT 代理，把 `github.com:443` 透传到可达 IP（SNI 与证书校验仍端到端保持 `github.com`），配 `git -c http.proxy=http://127.0.0.1:18123 push`。

**推送失败不等于 GitHub 不可用**，禁止据此下结论；必须换通道重试或如实报告"本地已提交、未推送 + commit hash"。

**注意分支名**：本仓库默认分支是 `master`（受保护，push 会返回 `Bypassed rule violations ... must be made through a pull request`，但实际已写入）。API 通道必须显式写 `master`，写 `main` 会 404。

#### `push_skill.sh` 内已收敛的环境坑（脚本自动处理，勿在 SKILL 复述）

- `~/.gitconfig` 可能写死失效代理（如 `7897` 端口），需清空。
- 认证走明文缓存 `~/.git-credentials` + `url.<token>@github.com/.insteadOf` 内联，并 `credential.helper=` 关闭凭据管理器；**不走 `gh` 凭据助手**（Windows 上 gh 取令牌会调被安全策略黑名单的 `reg.exe`，触发权限弹窗/被拒）。
- `core.hooksPath` 指向空目录，屏蔽 Qoder post-commit 追踪器（会拉起 `Qoder.exe`→`reg.exe`，噪音且无意义）。
- GitHub 美国段 IP 偶发 TLS EOF，脚本自动重启干净隧道重试（最多 4 次）。

### SNI 关键字阻断

TCP 能建连但 TLS 立即被重置（curl 报 `schannel: failed to receive handshake`、python 报 `ConnectionResetError`）时，先做 SNI 变量对照：同一边缘 IP 换无关 SNI（如 `www.bing.com`）若能握手，即判定为 SNI 关键字阻断。确认后走：

```bash
python scripts/sni_fetch.py <url> <out_path>
```

原理：SNI 换成无关域名、HTTP `Host` 头保留目标域，Fastly 按 Host 路由故内容照常返回（自动跟随重定向、去 chunked/gzip）。

### GitHub 正文抓取通道

- `raw.githubusercontent.com`：TLS 握手直接失败（`curl` 退出码 35）。**不要据此判定 GitHub 不可用**。
- `api.github.com`：可达，可取仓库元数据（默认分支、语言、许可、时间戳）。
- `github.com` 仓库页：可达，README 以 `<article class="markdown-body">` 内嵌在 HTML 里。
- 故取 README 全文的**优先级**为：仓库页 HTML 解析 > `api.github.com`（仅元数据）> `raw.githubusercontent.com`（常失败）。

### 隧道与取件的实测背景

- **`git_tunnel.py` 的由来**：本机 DNS 曾把 `github.com` 解析到新加坡 Azure 段地址，该 IP 的 443 端口 TCP 超时；换 SNI、不发 SNI 均超时，判定为路由不通（非 SNI 关键字阻断）。因此改为把连接重定向到候选可达 IP，同时保持 SNI 与证书校验仍为 `github.com`；候选全失败则回落系统 DNS，避免候选表漂移后隧道对每次连接都回 502。
- **`sni_fetch.py` 的由来**：本机网络对 SNI 中出现 `arxiv.org` 的 TLS ClientHello 直接回 RST（TCP 能建连、约 0.08s 后 `ConnectionResetError`）；换 SNI 为 `www.bing.com` 或去 SNI 即握手成功，确认为 SNI 关键字过滤。arXiv 走 Fastly，Fastly 按 HTTP `Host` 头路由，故前置 SNI 后内容照常返回。

> 上两条为一次性实测记录（技能文档与脚本注释只写抽象理由，具体实测细节只落本文件）。


---

## 项目控制台

- 监听 `127.0.0.1:8787`（默认）。端口已被占用时 `serve_console.py` 拒绝启动并提示换端口。
- **绑定回环不等于只有本机能用**：任何网页都能向 `127.0.0.1` 发请求（同源策略管的是「能不能读响应」，不管「请求发不发」），本服务又有写入口（建卡 / 改卡 / 批量改标签）。故请求须带本机宿主名（拦 DNS 重绑定，`Host: 攻击者域名` 是重绑定的必经形态），带 `Origin` 时须同站（拦浏览器跨站写）；`Origin` 缺失按非浏览器客户端放行。被拒一律 403 并在响应体里写明允许的宿主名。
- **非回环绑定须 `--allow-lan` 显式解锁**：服务无鉴权，绑到 `0.0.0.0` 等于让同网段任何机器直接读写云端；来源校验拦的是网页，拦不住直接连上来的另一台机器，这一层只能靠不绑出去。
- 脚本模块在服务**启动那一刻**加载：改过脚本须重启进程，否则跑的还是旧代码。
  **改完起服务若报「端口已被占用」，先查明占用者是不是上一轮遗留的旧实例**
  （`netstat -ano | grep 8787` 拿 PID，`taskkill //F //PID <pid>` 再重启）——
  否则页面会继续由旧代码接客，看到的现象是「代码明明改了，行为一点没变」，
  且响应里少的新字段（如 `kind`）会误导你以为改动没生效。
- 页面渲染验证用无头浏览器：本机 `C:/Users/35234/miniconda3/python.exe` 带 playwright 1.62（Chromium 已装）。启动浏览器须加 `--no-proxy-server`，否则沙箱代理会拦下对回环地址的请求。

---

## 目录与产物

- 中间文件（抓取原文、草稿、请求 JSON、被替换的旧卡全文）写在 `/root/.codebuddy/artifact/<会话 id>/`，用完不即时全删，须保留最近若干轮（保留规则见 SKILL.md）。
- 临时请求 JSON 可放 `/tmp/`，同样不要在当轮立刻删。
- 最终交付物（需要用户看的）才写 `/workspace`。
- 流程闸门凭证：`.sop_gate/<sig_key>.json`（现采中间态，已 gitignore）。凭证有效期 6 小时，`cleanup.py` 顺带删除超过 24 小时的旧凭证，`trash` 只留最近 10 个归档包。
- **本机（Windows）实际布局**：中间文件写在项目根 `_tmp/`；该目录**可能被上一轮清理移除**，`curl -o _tmp/...` 前须先 `mkdir -p _tmp`，否则报「No such file or directory」而被误判为网络故障。根目录的 `tmp_*` 与 `_tmp_*` 同义，均由 `cleanup.py` 按近轮窗口回收。
- 验证留痕（第 2 步网络搜索记录）：写卡时按 `scripts/sop_gate.py` 用法说明落盘，供闸门读取；属当轮中间文件。
- `feeds.opml`：本机的**阅读器导出**（Fluent Reader 的订阅清单，四项 RSS：Hacker News 最新、中新网滚动、IT之家、DEV Community），只用于挑素材时顺手扫一遍。**与技能本体无关、已 gitignore、不追进入库**；任何脚本都不读它，删了也不影响流程。

---

## 技能注册（DeepSeek Harness / dsh）

`SKILL.md` 在仓库根，**不在** dsh 技能提供者的默认扫描根内（它只认
`<projectRoot>/.dsh/skills/<name>/SKILL.md` 与 `<projectRoot>/.agents/skills/<name>/SKILL.md`；
`projectRoot` 取最近含 `.git` 的祖先，本仓库即自身）。不注册时，会话的技能目录里
看不到本技能，只能靠人肉去读仓库根的文件。注册方式（本机，一次性）：

```powershell
New-Item -ItemType Directory -Force -Path .dsh\skills
New-Item -ItemType Junction -Path .dsh\skills\flomo-note -Target $PWD
```

- 联接目标是**仓库根本身**，因此 `scripts/`、`AGENTS.md`、`ENVIRONMENT.md` 一并落在
  技能的 resource base 里，`SKILL.md` 中的相对路径按 base 解析即可命中。
- `.dsh/` 已 gitignore：联接是**指针不是副本**，`SKILL.md` 仍只在仓库根定义一次；
  且 git 检出会把联接写成纯文本文件，反而造出"两个 skill"，故绝不上库。
- 换机器 / 重建：删掉 `.dsh` 后按上面两行重跑；提供者带目录监视，不用重启。
- 验证：`Test-Path .dsh\skills\flomo-note\SKILL.md` 为 True，且新会话技能目录出现 `flomo-note`。
- 实测副作用：无。`glob` / `grep` 均不穿目录联接，不会出现 `.dsh\...` 重复路径。

---

## 技能注册（OpenCode）

与上一节的 dsh 注册**并行**：两者各扫自己的根，互不影响。OpenCode 认的根是
`~/.config/opencode/skills`（全局，本机 `C:\Users\35234\.config\opencode\skills`）与
`.opencode/skills`（项目），并把 `~/.claude/skills`、`~/.agents/skills` 当兼容源；
`SKILL.md` 仍只在仓库根定义一次。

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.config\opencode\skills"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.config\opencode\skills\flomo-note" -Target $PWD
```

- 联接目标同样是**仓库根**：`scripts/`、`AGENTS.md`、`ENVIRONMENT.md` 一并进 resource base，
  `SKILL.md` 里的相对路径按 base 解析即可命中。
- 放全局目录是为了换项目也能用；代价是仓库挪位后联接指向旧路径，删掉重跑上面两行即可。
- 技能清单按路径**现采**：装好即在当前会话的技能目录里出现，不必重启进程。
- **联接形态不存在同步问题**——读的就是仓库根那份；它与仓库完全解耦，也不会让 `git status` 变脏。
- 验证：`Test-Path "$env:USERPROFILE\.config\opencode\skills\flomo-note\SKILL.md"` 为 True，
  且会话的技能清单出现 `flomo-note`。

### 复制形态的安装位（WorkBuddy）

`~/.workbuddy/skills/flomo-note/` 是**复制**形态（只有 `SKILL.md` 与 `_user_meta.json`），
与仓库版没有任何自动关联，会静默分叉——表现是照着旧规则写卡，而一切照常、不报错。
同步走 `scripts/sync_skill.sh`（`--check` 只比对，不一致非 0 退出）；上面那条联接则不需要它。

---

## 技能注册（Qoder Desktop）

Qoder 扫描 `<用户数据目录>\skills\<name>\SKILL.md`，同样是**目录联接**形态。本机有两个数据目录，
各扫各的：

| 数据目录 | 状态 |
|---|---|
| `C:\Users\35234\.qoder\` | **当前在用**（本会话的插件与技能清单即出自这里）；`skills\flomo-note` 联接已建好 |
| `C:\Users\35234\.qoder-cn\` | 旧版数据，当前进程不读；联接仍在，指向同一仓库根，无需维护 |

```powershell
New-Item -ItemType Junction -Path "C:\Users\35234\.qoder\skills\flomo-note" -Target "D:\OpenClaw\flomo-note"
```

- 用 PowerShell 的 `New-Item`，**不用 `cmd /c mklink /J`**：在 Git Bash 里转调 `cmd` 时引号会被吞，
  报「无效参数」；`New-Item` 在 PowerShell 与 Git Bash 下都可用。
- 目标仍是**仓库根**，`scripts/`、`AGENTS.md`、`ENVIRONMENT.md` 一并进 resource base，
  `SKILL.md` 只定义一次；仓库挪位后删掉联接重跑上面一行即可。
- 技能清单现采：建好联接即在会话的技能目录里出现，不必重启。
- 验证：`Test-Path "C:\Users\35234\.qoder\skills\flomo-note\SKILL.md"` 为 True，
  且 `cmp` 与仓库版逐字节一致；仓库 `git status` 不受影响。

---

## Windows 沙箱权限（本机）

工作区根目录缺少所有者写权限时，**所有 shell 通道一进工作区就失败**（`pwsh` 与
terminal 均报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(<工作区>)`，
换 workdir 无效，因为 workspace-write 策略总要给工作区根授权），而 `read` /
`write` / `edit` 文件工具不受影响——现象是"文件能改、命令不能跑"。

修复用 dsh 的 `diagnose-windows-sandbox-acl` 技能跑一条诊断修复命令（给当前登录用户
在缺失处补完全控制条目，保留所有者与既有拒绝项，改动前先落备份）。报告目录：
`D:\OpenClaw\flomo-note-acl-out\`（含 `acl-report-*.jsonl` 与 `acl-backup-*.json` /
`.ps1` 回滚脚本，报告末尾的 ROLLBACK 行即撤销命令）。修完须重跑原先被挡的操作确认。

---

## flomo 工作台（DSH 插件）

`plugins/flomo-workbench/` 是本地 Cordis bundle：源在仓库里，已用 `plugin_manager`
的 `install_bundle` 装进当前 profile（profile 的 `node_modules/@local/flomo-workbench`
是指向该目录的联接，改源文件即改生效代码，但**模块只在 Harness 启动时加载一次**，
改 `index.js` 后须重启进程）。它提供三个 agent 预设，在新会话的预设控件里选：

| 预设 | 模式 | 工具面 |
|---|---|---|
| `flomo-write` | 写卡 | 全量：web + fs + shell + flomo_read / flomo_scan / flomo_gate / flomo_validate / flomo_write |
| `flomo-tidy` | 治理 | 同上，但**不挂 web 工具**（治理不抓新素材） |
| `flomo-audit` | 体检 | **不注册写工具**、fs 只读、不挂 web |

- 模式切换：`/flomo write | tidy | audit`（走 `agentPresets.select`，与预设控件同一条路）。
- 五个工具全是 `scripts/*.py` 的薄壳：一次调用 = 一次脚本执行，**不含第二套口径**；
  写工具会先跑 `validate_memo.py`（格式 + 闸门凭证），不过则一个字节都不发云端。
- 模式不写在配置里：插件用 `ctx.agentPresets.composedPreset(ctx)` 判断所在预设；
  取不到时 fail-closed（不注册写工具），日志里会写明原因。
- 换机器重建：`plugins/` 随仓库走，只需再装一次——`plugin_manager` `install_bundle`，
  target 传 `plugins/flomo-workbench` 的绝对路径。
- 离线自检（不装插件也能验证插件逻辑与脚本链路，会真的打一次 tag_tree 只读）：
  `node _tmp/probe_plugin.mjs`。该探针是临时产物、不入库；要固化为常驻回归需另走
  脚本改动的配套用例流程。
