## 1. 使用的方法

仓库是一个以 Python 脚本和文档为主的技能（skill）仓库，没有语言级编译器、打包器或依赖清单。构建体系由以下组件组成：

- **CI**：GitHub Actions（`.github/workflows/ci.yml`），在 push 到 `master` 和所有 PR 上运行。
- **测试入口**：`scripts/run_tests.sh`，统一串行执行 `scripts/check_skill_docs.py` 与 `tests/test_*.py` 下的全部用例。
- **技能文档审计**：`scripts/audit_skill.sh`，通过 git clone `alinotfoundbtw/sounding` 并对 SKILL.md 跑 sounding 静态审计，要求 score=100/100。
- **Node 侧**：仅 `web/` 下三个静态文件和一个本地 HTTP 代理 `opencode-go-proxy.js`，无 package.json / webpack / npm 脚本，不需要构建步骤。

因此该仓库不存在 Makefile、Dockerfile、cross-compile、版本发布流水线等典型“构建系统”工件——它把“构建”简化为“跑测试 + 跑文档审计”。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `.github/workflows/ci.yml` | GitHub Actions 入口，只有一项 `test` job，复用 `scripts/run_tests.sh` |
| `scripts/run_tests.sh` | 测试 + 文档纪律的统一入口 |
| `scripts/audit_skill.sh` | 对 SKILL.md 做 sounding 审计（score≥100） |
| `scripts/check_skill_docs.py` | 被 run_tests.sh 调用的文档自检脚本 |
| `tests/test_*.py` | 全部离线回归用例 |
| `plugins/plugins/flomo-workbench/index.js` | 插件入口，无 Node 构建流程 |

## 3. 架构与设计决策

### 3.1 CI 与本地命令合一

`ci.yml` 的注释明确表达了设计约束：

> “不在 CI 里另写一套判据——两套判据必然分叉，分叉之后就没人知道到底哪个算过。”

CI job 只做四件事：checkout → setup-python@v5 (Python 3.12) → 不配置任何 secrets → `bash scripts/run_tests.sh`。这意味着 **CI 不是独立的构建管线，而是本地命令的远程镜像**。

### 3.2 测试发现策略

`run_tests.sh` 使用 bash glob `tests/test_*.py` 枚举用例。这同时是**命名约定**和**可检测约束**：

- 如果 `tests/` 下没有匹配 `test_*.py` 的文件，glob 展开失败，脚本显式报错退出；
- `check_skill_docs.py` 把这条规则也实现为 ERR 级别错误，双重保障。

用例按模块组织（如 `test_console_data.py`、`test_flomo_client.py`），与 `scripts/` 中的同名脚本一一对应。

### 3.3 环境隔离与临时目录

脚本层自行处理受限环境（agent 沙箱、只读容器）中 `tempfile` 的 PermissionError：先探测 TMPDIR 是否可写，不可写时回退到仓库内 `_tmp/.tmp`（已忽略进 git）。这是通过运行时探测实现的容错，而非构建期声明。

### 3.4 技能文档审计（sounding）

`audit_skill.sh` 将外部审计工具 as code：

1. 优先复用 $SOUNDING_TMP 中可用的 sounding 副本（`import sounding.cli` 成功即视为可用）；
2. 否则 `git clone --depth 1` 克隆 `alinotfoundbtw/sounding`；
3. 用 `PYTHONPATH="$SOUNDING_TMP_WIN/src" python -m sounding.cli audit "$SKILL_DIR"` 跑审计；
4. 解析输出中的 `score <number>`，小于 100 即返回非 0。

支持 `--keep`（保留临时克隆调试）和 `--mcp`（导出 `FLOMO_MCP_TOOLS` descriptor 并审计）两个可选参数。

### 3.5 跨平台考量

- `audit_skill.sh` 在有 `cygpath` 时把路径转为 Windows 风格，以兼容 cygwin/git-bash；
- Python 解释器通过 `SOUNDING_PY` 环境变量或 `python3`/`python` 自动探测；
- 测试入口通过 `PYTHON_BIN` 指定解释器。

### 3.6 未使用的构建概念

仓库中没有以下内容：

- 依赖管理清单（无 `requirements.txt`、`pyproject.toml`、`package.json`、`go.mod`）；
- 编译/链接步骤（纯 Python + JS 静态文件）；
- Docker 镜像或容器化；
- 版本号、changelog、发布标签或产物上传；
- Makefile / CMake / Gradle / Maven / Rake 等任务编排器。

## 4. 约定与约束

| 约定 | 来源 | 性质 |
|---|---|---|
| 用例必须放在 `tests/` 下并以 `test_` 前缀命名 | `scripts/run_tests.sh` 的 glob + `check_skill_docs.py` | 运行时强制（找不到文件直接失败） |
| CI 只运行 `scripts/run_tests.sh`，不得维护另一套测试命令 | `.github/workflows/ci.yml` 注释 | 约定（无 lint 强制，但注释明确禁止） |
| 测试必须离线运行（不联网、不读 token） | `ci.yml` 注释 | 约定（CI 不配 secrets，真实凭据依赖会暴露） |
| SKILL.md 审计 score 必须 ≥100 | `scripts/audit_skill.sh` 的 `run_audit` | 运行时强制（<100 返回非 0） |
| `scripts/` 是入口，`tests/` 是验证器，二者分离 | `run_tests.sh` 注释 | 约定（混放会导致“看不出哪些是入口”） |
| 临时构建产物放入 `_tmp/`（git ignore） | `run_tests.sh` 回退逻辑 | 约定（`_tmp/.tmp` 作为默认 TMPDIR） |
| 外部审计工具通过 git clone --depth 1 获取，不提交源码 | `audit_skill.sh` | 行为事实（clone 源为 `alinotfoundbtw/sounding`） |

## 5. 总结

这个仓库的“构建系统”本质上是一套 Bash 脚本集合，核心思想是：**本地命令即 CI 唯一真相源**。没有抽象出任务图、依赖解析或包分发；“构建 = 跑完 `scripts/run_tests.sh`，审计 = 跑完 `scripts/audit_skill.sh`”。其可靠性来自两个层次的重复校验：shell glob 与 Python 检查器共同强制执行文件名约定，CI 与本地命令完全同构。