## 1. 采用的方式

仓库没有引入任何第三方配置框架。配置由 Python 脚本自行读取，核心模式是：**优先从项目内 JSON 文件取配置，回退到进程环境变量**。凭据类文件通过 `.gitignore` 排除，运行时路径、端口等机器相关细节集中在 `ENVIRONMENT.md`。

## 2. 关键文件

- `scripts/flomo_client.py` — 凭据加载入口，集中实现 token 解析。
- `.gitignore` — 声明所有凭据/敏感文件不得入库。
- `ENVIRONMENT.md` — 记录与具体机器绑定的路径、端口、代理、域名、DNS、技能注册位置等运行期事实。
- `opencode-go-proxy.js` / `plugins/plugins/flomo-workbench/cordis.patch.yml` — 少量 JS/YAML 配置用于 OpenCode 插件与本地 HTTP 代理注入 session header。
- `tests/test_flomo_client.py` — 对 token 来源优先级的测试覆盖。

## 3. 架构与约定

### 3.1 凭据加载顺序（单一事实源）

`flomo_client.load_token()` 的实现为：

1. 若项目根存在 `.mcp.json`，按 `mcpServers.flomo.headers.Authorization` 路径解析 Bearer token；
2. 否则读取环境变量 `FLOMO_TOKEN`；
3. 两者均缺失抛出 `FlomoError`。

文档注释明确 `.mcp.json` 是「配置事实源」，env 只是后备（因为 env 中的 token 可能过期）。该顺序在 `test_flomo_client.py` 中有对应断言。

### 3.2 配置项的存放位置

| 配置项 | 来源 | 说明 |
|---|---|---|
| Flomo MCP token | `.mcp.json` → `FLOMO_TOKEN` | 唯一凭据入口 |
| HTTP(S) 代理 | 进程环境变量 `http_proxy` / `https_proxy` | 由 `net_util` / `netdoctor.py` 统一探测并处理 |
| GitHub DNS 映射 | `scripts/git_tunnel.py` 内的 `ROUTES` 常量 | 被视作「唯一事实源」，hosts 片段由此生成 |
| 控制台端口 | 命令行参数 `--port`（默认 `8787`） | 启动时拒绝已占用端口 |
| 是否允许外网绑定 | 命令行参数 `--allow-lan` | 显式解锁才监听非回环地址 |
| 中间文件目录 | `scripts/publish.py` 中 `PROJECT_ROOT / "_tmp"` 与 `scripts/fetch_article.py` 中 `PROJECT_ROOT / "_tmp"` | 固定命名空间，由 `cleanup.py` 按近轮窗口回收 |
| SOP gate 凭证 | `.sop_gate/<sig_key>.json`（已 gitignore） | 有效期 6h，`cleanup.py` 清理超 24h 旧凭证 |

### 3.3 运行期环境与代码分离

`ENVIRONMENT.md` 的职责是把「随机器变化的事实」（沙箱 IP、端口、Git Bash 行为、Windows 托管版 Python、Qoder Desktop 数据目录 `.qoder` 等）与不随环境变化的规范（`SKILL.md`）解耦。文档规定：环境变化时只改本文件，不动 `SKILL.md`。

### 3.4 脚本内路径计算

多个脚本用 `Path(__file__).resolve().parent.parent`（或 `SCRIPT_DIR.parent`）算出 `PROJECT_ROOT`，再拼接 `_tmp/`、`.mcp.json` 等相对路径，避免依赖调用方 cwd。

## 4. 约定与约束

- **凭据不上库**：`.mcp.json`、`.flomo_token`、`.env` 均在 `.gitignore` 中显式列出，且 `ENVIRONMENT.md` 写明“.mcp.json 含 token，已 gitignore，绝不入库”。
- **token 来源优先级不可颠倒**：`.mcp.json` 优先于 `FLOMO_TOKEN`，这是 `flomo_client.load_token()` 的强制实现，并由测试覆盖。
- **网络出口异常必须区分“连接失败”和“服务端拒绝”**：`CloudUnreachable.request_sent` 字段决定写操作能否换通道重试——连接被拒/DNS 失败可重发，超时/中断视为“可能已发出”，禁止重发（`memo_create` 重发即产生第二张卡）。
- **本机服务无鉴权**：绑定 `0.0.0.0` 需 `--allow-lan` 显式解锁；仅靠 Host/Origin 校验不足以保护，不能把服务暴露到局域网。
- **SOP gate 凭证生命周期**：`.sop_gate/<sig_key>.json` 有效期 6 小时，`cleanup.py` 顺带删除超过 24 小时的旧凭证，`trash` 只保留最近 10 个归档包。
- **GitHub 推送通道降级顺序**：Contents API > `git push` 直连 > 本地 SNI 隧道（`git_tunnel.py`），每步失败都换下一通道重试，而非直接报错。
- **Python 输出编码**：PowerShell 下须设置 `PYTHONUTF8=1` 或 `PYTHONIOENCODING=utf-8`，否则中文打印阶段 GBK 编码失败会误判逻辑错误。
- **OpenCode 技能注册位**：全局 `~/.config/opencode/skills` 与项目 `.opencode/skills`，通过目录联接指向仓库根；复制形态（WorkBuddy）`~/.workbuddy/skills/flomo-note/` 与仓库不同步，需 `scripts/sync_skill.sh --check` 比对。
- **脚本模块在服务启动时一次性加载**：改过脚本须重启服务进程，否则页面仍由旧代码接客。