## 1. 使用的系统/方式

本仓库是一个以 Python 脚本为主的技能根文档仓库，外加一个 OpenCode Go 的 JS 代理和一个本地 npm 插件。依赖管理采取“零第三方依赖”策略：

- **Python**：全部 `scripts/*.py`、`tests/*.py` 仅使用 Python 标准库（`argparse`、`ast`、`json`、`pathlib`、`subprocess`、`tarfile`、`threading` 等），没有任何 `requirements.txt`、`Pipfile`、`pyproject.toml`、`poetry.lock` 或虚拟环境文件。Python 解释器由 shell 脚本在运行期探测：优先 `python3`，其次 `python`，也可通过环境变量 `PYTHON_BIN` 覆盖。
- **Node.js**：`plugins/flomo-workbench/package.json` 是唯一的 `package.json`，声明为 `"private": true`，不发布到 npm；它只导出 `index.js`、`locale/*.json`、`icon.svg`，并通过 `dsh.bundle.patch` 指向 `cordis.patch.yml`，作为 Cordis 工作台的本地插件被消费。没有 `node_modules`、`pnpm-lock.yaml`、`yarn.lock` 等锁定文件。
- **Go**：仓库不包含任何 `go.mod` / `go.sum`，Go 代码来自外部 OpenCode 运行时，本仓库仅提供 `opencode-go-proxy.js` 这个轻量 HTTP 代理。
- **CI**：`.github/workflows/ci.yml` 用 `actions/setup-python@v5` 安装 Python 3.12，然后直接执行 `bash scripts/run_tests.sh`，不安装任何额外包——因为测试只依赖标准库和桩客户端。

## 2. 关键文件

- `plugins/flomo-workbench/package.json`：唯一 npm 元数据，定义本地私有包 `@local/flomo-workbench`。
- `plugins/flomo-workbench/index.js`：插件入口。
- `plugins/flomo-workbench/cordis.patch.yml`：Cordis bundle 补丁配置。
- `scripts/audit_skill.sh`、`scripts/run_tests.sh`、`scripts/run_audit.sh`、`scripts/push_skill.sh`：统一通过 `detect_python()` 查找解释器，并设置 `set -euo pipefail` 保证失败即退出。
- `.github/workflows/ci.yml`：固定 Python 3.12，仅跑 `scripts/run_tests.sh`，不引入 secrets。

## 3. 架构与约定

- **标准库优先**：脚本作者刻意避免引入第三方 Python 包，注释中明确说明用例“全部是标准库 + 桩客户端，不读 token、不联网”。这使得 CI 环境只需一个干净的 Python 即可运行所有测试，无需 pip 解析依赖树。
- **Python 解释器可移植性**：shell 脚本不写死 `/usr/bin/python3`，而是通过 `command -v python3` / `python` 探测，并在 Windows Git Bash 下同样生效；使用者可通过 `PYTHON_BIN=/usr/bin/python3 bash scripts/run_tests.sh` 显式指定。
- **npm 包仅内部消费**：`@local/flomo-workbench` 标记 `private: true`，不在 registry 上发布，仅被 Cordis 工作台通过 `dsh.bundle.patch` 引用；版本固定在 `1.0.0`，随源码同步更新。
- **无依赖锁定机制**：仓库不存在 `go.mod`、`go.sum`、`package-lock.json`、`pnpm-lock.yaml`、`yarn.lock`、`requirements.txt` 等任何锁定文件，也没有 GOPRIVATE、私有 PyPI registry 或 npm registry `.npmrc` 的配置痕迹。

## 4. 约定与约束

- **观察到的约定**：
  - Python 脚本一律只 import 标准库模块，项目内模块之间通过相对路径或同目录导入，且通过 `# noqa: E402` 规避循环导入警告（见 `console_cloud.py`、`dedup.py`、`check_skill_docs.py`）。
  - shell 脚本统一以 `set -euo pipefail` 开头，并通过 `detect_python()` 函数查找解释器。
  - CI 固定使用 Python 3.12，与 `audit_skill.sh` 注释中提到的“默认托管 python 3.13”存在差异，但 CI 实际运行的是 3.12。
- **明确的约束**：
  - CI 不会安装任何第三方包，也不会注入 secrets；如果有人在测试中新增真实网络凭据依赖，CI 会直接失败，这是有意为之的设计（见 CI 注释："不要靠加 secret 掩盖"）。
  - `@local/flomo-workbench` 包不对外发布，其 API 契约由 `exports` 字段和 Cordis 插件消费方共同约束。

由于仓库刻意保持“零第三方依赖”，本类别的实际影响范围很小：依赖管理的核心就是“不引入依赖”。