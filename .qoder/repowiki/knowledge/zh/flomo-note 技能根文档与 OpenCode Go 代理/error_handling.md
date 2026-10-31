## 1. 采用的方案

仓库由 Python 脚本（`scripts/`）和少量 JS（`web/app.js`、`plugins/flomo-workbench/index.js`）组成，错误处理采用**自定义异常类型 + 统一 CLI 入口**的两层模式：
- Python 侧定义业务异常类，在 `cli_entry` 中捕获并转为非零退出码；
- JS 侧直接 `throw new Error(...)`，由调用方或 Node 运行时接管。

没有使用任何日志框架或中间件——该仓库是脚本集合，不是服务。HTTP 请求失败通过 `requests.exceptions` / `urllib` 的异常转成业务异常，不存在 panic/recover 语义。

## 2. 关键文件与异常类型

| 文件 | 异常/函数 | 语义 |
|---|---|---|
| `scripts/cli_util.py` | `UsageError(Exception)` | 命令行参数本身写错（缺值、重复、未知选项、越界），由 `cli_entry` 以 `usage_code=2` 退出，打印 `[失败] ...` |
| `scripts/cli_util.py` | `cli_entry(main, *, errors, code=1, prefix="[失败]", usage_code=2)` | 所有脚本 `__main__` 的统一出口；声明式接收本脚本抛出的「预期内」异常类型元组 |
| `scripts/flomo_client.py` | `FlomoError(RuntimeError)` | 凭据缺失、HTTP 错误、MCP JSON-RPC 协议错误等「调用无法进行」场景 |
| `scripts/flomo_client.py` | `CloudUnreachable(FlomoError)` | 出网通道不可用（连接被拒/超时/DNS/中断），附带 `request_sent: bool` 字段用于判断能否重发 |
| `scripts/net_util.py` | `TransportError(RuntimeError)` | 底层网络传输失败，携带 `request_sent` 供上层区分“可安全重试”与“可能已到达服务端” |
| `scripts/console_cloud.py` | `CloudError(RuntimeError)` | 控制台云调用的业务失败（白名单拒绝、工具清单取不到、流程闸门未过等） |
| `scripts/fetch_article.py` | `FetchError(Exception)` | 网页抓取失败的专用异常 |
| `web/app.js` / `plugins/flomo-workbench/index.js` | `throw new Error(...)` | JS 端统一抛出原生 `Error`，无自定义类 |

## 3. 架构与约定

### 3.1 异常层次不继承业务异常

`UsageError` 显式只继承 `Exception`，**不继承** `FlomoError`。文档明确说明原因：如果继承，`except FlomoError` 会误把参数错误当成云端失败，导致分类失效。这一约束由类文档注释表达，并由 `cli_entry` 对 `UsageError` 单独分支处理来保证。

### 3.2 可重试 vs 不可重试的网络错误

`CloudUnreachable.request_sent` 字段区分两类失败：
- `False`（连接被拒/DNS 解析失败）→ 确定没发出请求，换通道重发安全；
- `True`（超时/连接中断）→ 可能已到达服务端，**不许重发**（例如 `memo_create` 重发会产生第二张卡）。

调用方据此决定重试策略，见 `net_util.py` 与 `flomo_client.py` 中的对应分支。

### 3.3 统一 CLI 出口

`cli_entry` 是仓库中各脚本 `__main__` 的固定形态：
```
cli_entry(main, errors=(FlomoError,), prefix="xxx")
```
它要求每个脚本显式声明自己还会抛哪些异常（`errors` 必填，无默认值），这样漏写会在调用处立刻 `TypeError`，而不是运行期吞掉 traceback。用法类异常一律走 `UsageError` 分支，其他异常带 `prefix` 拼接后 stderr 输出，返回 `code=1`。

### 3.4 错误消息格式

- 用法错误统一以 `[失败] ` 开头；
- 业务错误以传入的 `prefix`（如 "查重未能进行："、"云端调用失败（memo_update）："）+ 冒号 + 原因，不再额外补空格；
- 网络/云端错误常包含 `type(e).__name__: e` 以便定位根因。

### 3.5 JS 端风格

JS 代码直接使用 `throw new Error('...')`，不做封装；常见触发点包括：找不到仓库根、找不到 Python 解释器、未知 action、缺少必填参数、JSON 解析失败、HTTP 响应非 `ok` 等。错误向上冒泡到插件调度层或 Node 运行时。

## 4. 观察到的约定与约束

- **约定**：CLI 脚本的 `__main__` 都通过 `cli_util.cli_entry` 包裹主逻辑，而非直接 `sys.exit`。（证据：`cli_util.py` 的 docstring 写明「所有命令行脚本的 `__main__` 都走这里」。）
- **约定**：需要网络访问的模块抛出 `FlomoError`（或其子类 `CloudUnreachable`），纯本地脚本抛出 `UsageError` 或 `FetchError`。（证据：`flomo_client.py`、`fetch_article.py`、`console_cloud.py` 的分类。）
- **约定**：`CloudUnreachable` 必须附带 `request_sent` 布尔位，以便上层判断能否重发。（证据：类的 `__init__` 签名及多处 `from exc` 传递位置。）
- **约束**：不要用 `SystemExit` 表示这类失败——`SystemExit` 继承自 `BaseException`，会被 `except Exception` 漏掉，在 HTTP 服务里表现为连接被掐断且错误信息丢失。该规则由 `flomo_client.py` 中 `FlomoError` 的类文档明确陈述。
- **约束**：`cli_entry` 的 `errors` 参数必填、无默认值，调用方漏传会立即 `TypeError`，避免隐式吞掉异常。（证据：函数签名与 docstring 中对设计动机的解释。）
- **约束**：`UsageError` 不得继承 `FlomoError`，否则 `except FlomoError` 会把参数错误误判为云端失败，破坏错误分类。（证据：`UsageError` 类文档的专门论述。）
- **约束**：JS 侧所有配置/路径校验失败统一用 `throw new Error('...')`，没有自定义 Error 子类；错误信息需包含缺失的具体项名（如 `old_tag`、`new_tag`、`verify`、action 名称）以便上游诊断。

## 5. 不适用的部分

仓库没有 Go 代码，也没有 HTTP 服务的中间件链——因此不存在「middleware error handling」「panic/recover」主题下的相关实践。错误传播主要发生在脚本内部调用链和 Python → JS 子进程调用边界上。