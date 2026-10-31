## 1. 使用的体系与方法

- **零依赖原生 CSS + 原生 JavaScript**：`web/index.html` 仅引入 `styles.css` 与 `app.js`，无 Tailwind、Sass、CSS Modules、组件库或网络字体加载。
- **设计变量集中在 `:root`**：通过 CSS Custom Properties（`--bg`、`--accent`、`--radius`、`--serif`/`--sans`/`--mono` 等）集中管理颜色、圆角、阴影和字体栈，作为仓库内唯一的“设计令牌”。
- **BEM 风格的命名约定**：类名使用 `.sidebar`、`.nav-item`、`.btn-primary`、`.stat.is-alert`、`.badge.badge-ok` 这类“模块+修饰符”的扁平命名，没有嵌套选择器链，也没有 CSS-in-JS。
- **响应式策略极简**：未使用媒体查询。布局基于 CSS Grid（`.app { grid-template-columns: 244px 1fr; }`）与 Flexbox；统计条用 `grid-template-columns: repeat(auto-fit, minmax(126px, 1fr))` 自适应列数。
- **视觉风格**：注释写明「Claude 风格 · 暖米白底 + 赤陶强调色 + 衬线标题」，主色 `#C96442`，语义色 `--ok` / `--warn` / `--danger` 三态齐全。

## 2. 关键文件

| 路径 | 作用 |
|---|---|
| `web/styles.css` | 全部样式与设计令牌（约 534 行） |
| `web/index.html` | 单页骨架：侧栏导航、主内容区、抽屉、编辑器、遮罩、Toast |
| `web/app.js` | 视图渲染、MCP API 调用、DOM 交互（与样式强耦合于类名） |

## 3. 架构与约定

### 设计令牌（Design Tokens）

所有主题变量在 `:root` 中声明，按用途分组：

- 背景/表面：`--bg`、`--bg-side`、`--surface`、`--surface-sunk`
- 线条/边框：`--line`、`--line-soft`
- 文字：`--ink`、`--ink-soft`、`--ink-faint`
- 强调色：`--accent`、`--accent-soft`、`--accent-line`
- 语义态：`--ok`/`--ok-soft`、`--warn`/`--warn-soft`、`--danger`/`--danger-soft`
- 形状：`--radius`、`--radius-sm`、`--shadow`
- 字体：`--serif`（Tiempos Text / Songti SC / Source Han Serif SC …）、`--sans`（Styrene B / system-ui …）、`--mono`（SF Mono / JetBrains Mono …）

修改主题只需改 `:root`，不触碰业务类。

### 组件级样式块

样式以“功能块”组织，每个块对应一个 UI 概念：

- `.sidebar` + `.brand` + `.nav` + `.nav-item` + `.nav-dot`：左侧固定宽 244px 的导航栏
- `.main` + `.topbar` + `.page-title`：右侧主区域
- `.btn` + `.btn-primary`：通用按钮（含 `disabled` 态）
- `.stats` + `.stat` + `.stat.is-alert`：统计卡片网格
- `.card` + `.section-head`：笔记/标签卡片容器
- `.badge` + `.badge-ok/.badge-warn/.badge-err/.badge-quiet`：状态徽标
- `.drawer` / `.editor` / `.scrim` / `.toast`：覆盖层组件

### 交互与样式解耦

`app.js` 通过切换类名驱动样式变化，而非直接操作 `style`：

- `.is-active` 加在 `.nav-item` 上切换选中态
- `.is-on` 加在 `#toast` 上触发显示
- `hidden` 属性控制 `.drawer`、`.editor`、`.scrim` 的显隐

### 可访问性

- 使用 `aria-hidden="true"`、`aria-label="关闭"`、`role="status" aria-live="polite"` 等 ARIA 属性。
- HTML lang 为 `zh-CN`，viewport 设置完整。

## 4. 约定与约束

- **无外部样式依赖**：`index.html` 仅 `<link rel="stylesheet" href="styles.css">`，无 CDN、无 npm 包。
- **字体全部本地回退**：`--serif` 优先尝试 Tiempos Text / Songti SC / Source Han Serif SC，再回退到 Georgia/Times New Roman；`--sans` 优先 Styrene B，再回退 Apple/Windows/system-ui；均不通过网络加载。
- **颜色与间距走变量**：业务样式只引用 `var(--*)`，不出现硬编码颜色值（除注释外的正文）。例如按钮 hover 用 `rgba(201,100,66,.07)` 直接引用 accent RGB 透明度，是少数例外。
- **类名即契约**：JS 通过选择器 `querySelector('.nav-item[data-view=…]'`、`$('.stat')` 等绑定 DOM，新增 UI 元素需同时提供对应 CSS 类并遵循已有命名规范（模块名 + 可选修饰符）。
- **无媒体查询**：当前布局不针对移动端做适配；`index.html` 虽设了 viewport，但 `styles.css` 中没有任何 `@media` 规则。
- **全局 reset 最小化**：仅 `* { box-sizing: border-box; }` 与 `html,body { height: 100%; }`，无 normalize/reset 库。
- **行内代码统一样式**：`code` 使用 `--mono` 字体、`--surface-sunk` 背景与 `--line-soft` 边框，确保笔记正文中的反引号片段与 UI 一致。

本仓库的前端样式系统是轻量且自洽的：一个 CSS 文件承载全部设计令牌与组件样式，一个 JS 文件负责渲染与交互，HTML 仅提供结构标记，三者构成单一静态站点控制台。