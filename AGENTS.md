# 项目总则（flomo-note）

本文档是项目总则，只约定顶层目标与全局不可违背的约定；具体执行细则一律以 `SKILL.md` 为准（唯一细则源），两者不重复记录，本文件不复制 SKILL 细节。

## 项目是什么

flomo 极简卡片笔记技能：把网页、文章、想法整理成条条 flomo memo，直接写入用户云端 flomo 账号。卡片形态 = 一行标签段 + 简短正文，一事一卡、标签聚合。

## 顶层目标

- 把看到的好内容高效沉淀成云端 flomo 卡片，为往后的回顾与复用服务。
- 记录足够轻、足够快；格式统一、可被工具稳定读写。

## 核心约定（全局不可违背）

1. **笔记只在云端**：卡片正文只存在于 flomo 账号；本地不落盘任何笔记内容，绝不以"本地文件"代存云端卡片。
2. **本地只留工具**：本目录仅存放技能文档（`SKILL.md`）、本总则（`AGENTS.md`）、环境备忘（`ENVIRONMENT.md`）、脚本（`scripts/`）、项目控制台前端（`web/`）与 MCP 配置（`.mcp.json`）。git 只追踪技能/配置文档与工具，不含笔记内容。
3. **不随仓库提交**：任何含凭据的配置、以及各类**现采缓存 / 中间态产物**（标签树快照、流程闸门凭证等）一律不入库。具体清单以 `.gitignore` 为准，细则见 `SKILL.md`。
4. **写云免确认**：对云端数据的常规写操作（建卡、覆盖式更新、清空卡片、改标签）一律免确认，不展示等批。唯一例外是物理删除卡片（从云端彻底移除）须用户明确授权；flomo 无删除 API，删除只能用户在 App 内手动完成，技能侧不调用也不代做。**写操作的前置条件与流程细节以 `SKILL.md` 为准，本文件不复述。**
5. **来源实实在在**：卡片引用的 URL 必须真实、可核、经过抓取或核验；禁止生造来源。
6. **细则听 SKILL**：卡片格式、标签规则、记录流程、硬限清单等可执行细节，唯一以 `SKILL.md` 为准，本文件不掺演、不复制。SKILL.md 中硬限以编号（H1–Hn）单点定义，引用一律用编号。
7. **收口成闭环**：每轮工作结束前把该轮做完整、可交付；有跨轮事项必须明示，不静默丢弃。每轮收尾的固定输出项以 `SKILL.md` 为准，逐项必填、不得静默省略。
   **复盘建议禁区（硬限）**：建议的落点只能是 flomo 云端卡片（标签聚类、补挂、归并），**严禁提议改动 SKILL.md / AGENTS.md / ENVIRONMENT.md / 词表 / 脚本**——这些一律等用户另行指令，或以独立改动流程（commit + 真实 hash + push）单独处理。
8. **文档改动须附真实 hash**：凡称"已提交/已推送"，必须附真实 commit hash 供核对，并（推送后）以 `git ls-remote` / API 回读核实。推送通道选择与环境细节见 `ENVIRONMENT.md`。

## 目录分工（职责分明）

- `AGENTS.md` —— 项目总则（本文件）
- `SKILL.md` —— 执行细则（唯一细则源）
- `ENVIRONMENT.md` —— 环境备忘（路径、代理、端口、推送通道等随环境变化的细节）
- `scripts/` —— 工具脚本（控制台三件套在内：`serve_console.py` 服务、`console_cloud.py` 云端访问、`console_data.py` 本地数据抽取）
- `web/` —— 项目控制台前端（静态资源；配 `scripts/serve_console.py` 起本地服务）
- `.mcp.json` —— flomo MCP 配置（含 Bearer token，已忽略）
- 笔记实体 —— 只在云端 flomo，本地不存在

## 开发维护约定（改脚本/文档时适用，与写卡流程无关）

改动脚本后必须先跑其配套回归用例，退出码 0 才算过。**本节面向维护者，不进入写卡执行路径**；写卡时不需要读本节。

- 改 `scripts/validate_memo.py` → 跑 `python scripts/test_validate_memo.py`。
- 改 `scripts/sop_gate.py` 或闸门校验逻辑 → 跑 `python scripts/test_sop_gate.py`（含 tag_tree 计数 / 查重 / 复盘的假 client 离线桩，这几处不必联网验证；含验证记录的**落点校验**——结论里的关键参数必须能在卡正文里找到，否则阻塞）。落点记录有硬约束：**每个分支一律经 `_landed_record` 返回、显式申报 `landed` 布尔，不得裸 dict 返回**；汇总行与凭证的条数只读这个字段，按键名猜会在新增分支时静默漏计（用例含源码扫描锁定这条结构约束）。
- 改 `scripts/tag_tree_sync.py` → 跑 `python scripts/test_tag_tree_sync.py`（沙箱内离线验证响应解析、核对三类不一致与重写闭环）。
- 改 `scripts/cleanup.py`（扫描规则 / 保留项 / 收集与删除逻辑）→ 跑 `python scripts/test_cleanup.py`（覆盖文件与目录两类残留的收集、保留项豁免、归档断言，临时沙箱内离线进行）。
- 改 `scripts/memo_util.py` → 跑 `python scripts/test_memo_util.py`（它是三处共用的签名口径，且是标签树快照渲染/计数的唯一实现，改动影响面最大）。
- 改 `scripts/git_tunnel.py`（候选 IP / 选路 / 回落逻辑）→ 跑 `python scripts/test_git_tunnel.py`（离线桩验证选路与回落，不联网）。
- 改 `scripts/console_data.py`（控制台本地侧数据抽取：标签树分组解析与统计字段）→ 跑 `python scripts/test_console_data.py`（临时目录内离线验证解析口径，含标签树计数与 `memo_util` 同源断言）。
- 改 `scripts/console_cloud.py`（控制台云端访问：读 / 写两套白名单、全部已接能力、通用只读执行、字段精简、写路径的两道闸门与回读验收）→ 跑 `python scripts/test_console_cloud.py`（全桩离线，不联网、不读 token；覆盖检索条件、批量与相关、标签树与标签名、参考文本、能力清单与参数规格归一化、通用只读执行，以及写闸门放行/拦截、回读验收、重命名边界与「返回值不含 token」）。三条硬约束：**读白名单恰等于实际调用的读工具、写白名单恰等于带 `write=True` 调用的写工具**（用例按源码扫描 + `write=True` 标记分别锁定，不能留无人使用的权限空位、也不能读写混用）；**写工具不得走通用执行入口**——写要过格式与流程闸门、写完回读验收，只能在 `create_memo` / `update_memo` / `rename_tag` 里发生；**闸门不过时一个字节都不发云端**（用例断言桩客户端零调用）。
- 改 `scripts/flomo_client.py`（通道、握手、工具调用、协议发现、写后回读验收）→ 跑 `python scripts/test_flomo_client.py`（离线桩验证回读比对的五类判定：一致 / 内容不符 / 回读为空 / id 对错卡 / 被截断，以及写命令内确实调用了回读）；再跑一次真实只读调用实测（`python scripts/flomo_client.py <tool>` 或起服务点一遍）。两条硬约束：**写后必须回读全文逐字比对**（只宽容空白差异；不一致即非 0 退出——凭 id 宣布成功等于把验收交给运气）；它是 H3 的唯一入口，写侧的幂等查重与成功判定不得削弱。**异常契约不许退回去**：握手失败 / HTTP 错误 / JSON-RPC 协议错误一律抛 `FlomoError`（`RuntimeError` 子类）；**绝不用 `SystemExit` 表达调用失败**——它继承自 `BaseException`，调用方的 `except Exception` 接不住，穿透到 HTTP 服务就是连接被掐断、客户端拿到空响应（`test_console_cloud.py` 已锁住「客户端抛 SystemExit 也必须被边界转成 CloudError」）。CLI 需要退出码，在各自 `__main__` 里把 `FlomoError` 转成 `return 1`。
- 改 `scripts/serve_console.py` 的接口路由或 `web/` 前端（导航项、视图、样式）→ 跑 `python scripts/test_console_web.py`（离线锁定前后端对齐：导航项必须有对应视图、前端调用的接口必须存在于服务路由表、写工具名不得挂在可执行按钮上、前端不得引入外部资源、服务不得出现命令执行入口），再起服务实测一轮；服务是多线程单例，改完须重启进程才生效（模块只在启动时加载一次）。
- `POST` 上只允许两类接口：**改动云端的写接口**与 **`/api/cloud/tool` 这个带参数跑读工具的通用执行入口**。新增任何「执行命令 / 跑脚本」型入口都属越界，`test_console_web.py` 会拦下。
- 改 `scripts/check_skill_docs.py` → 跑 `python scripts/test_check_skill_docs.py`。
- **一键跑全部离线用例：`bash scripts/run_tests.sh`**（各配套用例与内容纪律自检一并跑；改动脚本后首选，避免逐个手跑漏项）。
- 技能文档改动后先跑 `python scripts/check_skill_docs.py`（离线内容纪律自检，须 0 错），再跑 `scripts/run_audit.sh` 自校（已包好环境，勿手搓 `audit_skill.sh`）。
- 环境变化只改 `ENVIRONMENT.md`。
