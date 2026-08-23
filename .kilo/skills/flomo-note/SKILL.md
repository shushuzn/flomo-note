---
name: flomo-note
description: 通过 flomo MCP 把网页、文章、想法整理成 flomo 卡片，直接写入云端 flomo 账号。本地不存任何笔记。当用户发送链接（网页/arXiv/GitHub/清单站）、要求保存文章、抓取网页、随手记想法、整理资料、新建或更新云端 memo 时使用；纯闲聊不触发。轻量卡片 + 标签聚合，去文件夹、去双链。
---

flomo 卡片笔记（云端）

本文件是执行细则的唯一来源；项目总则见 AGENTS.md，两者不重复记录。
体系：flomo 极简卡片 + 标签聚合，笔记只存云端 flomo，本地不落任何卡片正文。本目录仅存放技能文档与 MCP 配置。

定位与理念

- 笔记在云端：每张卡片就是一条 flomo memo，通过 flomo MCP 的 memo_create 直接写入你的 flomo 账号；本地（d:\OpenClaw\flomo-note）不存储、不落盘任何卡片正文。
- 一事一卡：一条 memo 一个念头/一条摘录/一个观点/一条事实，简短独立（约 40–300 字）。不建本地文件夹，不做双链。
- 标签即结构：卡片靠 #标签 聚合成簇，可层层细分（#AI/AgentHarness、#经济/税制）。标签层级以云端 flomo 的标签树为准。
- 为浮墨式回顾而写：一句话说清"这条讲什么 / 为什么记它"，让未来的自己扫一眼标签和首句就能捞起来。不追求维基式"定义+解释"三段式，够清楚即可。
- 本地只留工具：本目录 = 本技能 + AGENTS.md(总则) + .mcp.json(含 token 的 MCP 配置)；git 只追踪这些技能/配置文档，不追踪任何卡片内容（没有也不应有）。

红线与许可

防错红线：
- 不外传你的笔记内容：写入云端前把 memo 正文展示给用户确认，不得静默写云端。
- 来源 URL 必须真实可核、严禁生造：memo 里出现的链接必须是确实抓取/核验过的。无真实来源时写"（非网络抓取，来源为 X）"，生造 URL 视同编造来源。
- 外部事实先核验再下结论：涉及"某物是否真实存在"的判定（模型/仓库/事件）先取证再表态。

操作许可边界（抓取/写云前先分类）：
- 只读 / 轻量（可直接做）：webfetch、websearch、读取官方只读接口、flomo.memo_search、flomo.tag_tree、flomo.memo_batch_get、flomo.memo_recommended。
- 需先征得用户明确允许：① flomo.memo_create、flomo.memo_update、flomo.tag_rename（写/改云端数据，每次执行前说明将写入的内容并等同意）；② 下载/保存文件到本地；③ 调用需下载或消耗资源的本地重型工具；④ 任何用户此前明确反对的路径。
- 每步动作开头先自标类别（"只读"或"需授权"），对需授权动作说明后果并停下等待同意；拿不准按需授权处理。

flomo 对接（本环境实测可行的连接方式）

- flomo 提供 streamable-http 的 MCP 端点 https://flomoapp.com/mcp，用 Bearer 鉴权（token 由用户提供，记在项目 .mcp.json）。本环境 TRAE 无 MCP 面板，且 .mcp.json 不会被加载（run_mcp 报 server not found），MCP 框架直连走不通。
- 实测可用方式：用 curl 直连该端点，先 MCP 握手拿会话，再 tools/call 调工具。
- 调用步骤：
  1. initialize：curl.exe -s -X POST https://flomoapp.com/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "Authorization: Bearer <token>" --data-binary "MCP initialize 请求 JSON"，从响应头 Mcp-Session-Id 拿会话 ID。
  2. notifications/initialized（带同一会话）。
  3. tools/call：body {method:tools/call, params:{name,arguments}}，带 Mcp-Session-Id 头；响应为 SSE，结果在 data 行的 result 里。
  4. 写 memo 前先调 get_format_guide 确认最新格式规范（含加粗/高亮/下划线/列表标签写法）。
- 工具：memo_create（写）、memo_update（改）、memo_search（查）、memo_batch_get（批读）、memo_recommended（联想）、tag_tree（标签树）、tag_search（定位）、tag_rename（改标签名）、memory_context / memory_user（读记忆档案）。依赖 flomo Max 会员。
- 权限边界：只读（tag_tree、memo_search、memo_batch_get、memo_recommended、get_format_guide、get_tag_guide）可直接做；写操作（memo_create、memo_update、tag_rename）须把内容/改动完整展示给用户并征得明确授权后执行，绝不静默写云。

卡片格式（写到云端 memo 的格式）

云端 memo 的标签放在正文开头第一行单独成段，空一行接正文，flomo 自动把首行 #标签 识别为标签。

卡片示例（这是写入云端的 memo 正文）：

#AI/AgentHarness #工程/落地

一句话核心结论：是什么 / 最想记住的点——一眼定位，不强求分段

要点：
- 要点一（为什么 / 依据）
- 要点二
- 可选：边界 / 反例——"不是什么 / 何时会失效"

来源: https://example.com

flomo 富文本边界（官方 get_format_guide 实测）：
- 支持并渲染：加粗 **文字**、高亮 <mark>文字</mark>、下划线 <u>文字</u>、无序列表 - 项目（两级嵌套用两空格缩进）、有序列表 1. 项目；段落用空行分隔。
- 不渲染：标题、引用、代码块、链接、表格、图片等其他语法（只作纯文本；外部来源链接按用户要求保留为纯文本行，不追求可点链接）。
- 正文按需用这些原生格式，避免写不生效的 Markdown 排版符号。

标签规则（flomo 官方）：
- 第一行即标签段：一个或多个 #标签，空格分隔。
- 多级用 /：#标签/子标签/孙标签。
- 禁 emoji / 特殊字符：#😁开心、#读书&学习 会导致无法检索，禁用。
- 英文词用下划线连接（如 #Apple_seed）。
- 首个标签为主标签（领域/主题词），细分往后。

记录流程（每次经 MCP 写云端）

1. 抓取（只读）——网页 webfetch；GitHub 优先 raw.githubusercontent.com/用户/仓库/main/README.md + websearch 补 stars；arXiv 取标题作者摘要；清单站与 SPA 站 curl.exe -L -A "Mozilla/5.0" 抓内嵌；403 或超时则 websearch 多源交叉兜底。涉及内容在音/视频口播或需转写时，先判断动作类别并征求同意。
2. 提炼：滤营销话术，留事实/数据/因果链，压成十几到几百字；不编造，未抓细节标"（待核实）"。
3. 定标签：先 flomo.tag_tree 采云端完整标签树（不能用本地文件代偿），命中既有簇；顶层新主标签先问用户，避免碎片化。
4. 查重（阻塞式，写云前必须完成并显式报告）：用 flomo.memo_search 按关键词/标签/时间（含语义）扫云端，命中候选即通读判断实质重复。判定：无命中→新建；完全重复→不再写、改指向原 memo；部分重叠→更新原 memo 正文；清单类一行提及→可单独建并从原处回链。回复必须写明查重结论（"查重：云端命中 X 篇（列名）或无重复"），不得用"已查重"三字敷衍。
5. 写作：按"卡片格式"写成 1 条（一事一卡）；可用 flomo.memo_recommended、memo_search 调出相关旧记录，让新 memo 与既往上下文衔接。
6. 写云（新建，需授权）：把最终正文 + 首行标签段完整展示给用户，说明将写入你的 flomo 账号；经同意后调 flomo.memo_create（正文=memo 内容，标签=首行标签段，内容与展示一致）。除非已明确约定"写卡直接落云"，否则必须停下等同意；本次不写云须明示。若第 4 步查重判定为"部分重叠"需更新，则本步不新建，计入第 8 步统一更新。
7. 复盘建议（收口后必做）：结合当轮所写 memo，提出 ≥1 条对项目（云端标签体系、写法、方向）的可执行改进建议，随收口交用户；须具体可落地（如"新增某顶层主标签""合并某簇标签""某标签已厚可建索引"），说明触发理由，不得空泛。
8. 更新云端 memo（常驻最后一步，需授权）：这是收尾的维护步骤，不固定顺序前置——当第 4 步查重判定"部分重叠"、或你点名修改某条（改正文 / 补标签 / 改来源 / 去重）、或需要合并/整理标签时，走到这一步执行。做法：先用 flomo.memo_search + flomo.memo_batch_get 定位并通读原 memo；明确要改动的那一点；向用户展示改动前后；获同意后调 flomo.memo_update（保持首行标签段）。若本轮无更新需求，明确告知"本轮无 memo 需更新"即可，不必硬走。memo_update 是写操作需授权，绝不静默改云端；改前必须让你看清拟改动内容。

写作规范

- memo 无文件名；结尾即正文，#标签/子标签 单独放第一行成段，空一行接正文。
- 结论先行：首句是卡片一句核心结论。
- 简短优先：40–300 字，超约 400 字就拆成多条 memo 而非挤进一条。
- 公式与数学：flomo 刻意不支持 LaTeX（官方为保证简洁拒绝公式排版，同不留 Markdown 语法/图片入正文），故不写美元符号定界符，数学公式一律用纯文本 / Unicode 表达（x²、√2、a/b、∑、μ、→）。中文为主，术语保留英文。
- 英文专名不硬译：直接用原文专有名词（Agent Harness、MCP、Skill），不硬译成中文打头。
- 成组数字用列表分行（flomo 无表格渲染，不用表格）：≥3 个并列数值指标（带同比/环比/占比/金额等口径）用 flomo 无序/有序列表逐行拆开，如 "- 营收 12.5 亿，+8%"，列清指标/数值/变化。
- 时序新闻去热（高时效必做）：来源是"本周发布/某模型上线"这类高时效内容时，memo 主语落在可复用的机制/概念/规格而非事件时间点，并留一个反时效持久锚点（归入既有主标签或抽象出可复用模式）；无法抽象的时效快照判不值得独立成 memo，并入上位或舍弃。

标签规范

- 层级用 /：#AI/AgentHarness、#经济/税制。首段（主标签）是领域/主题词。
- 顶层禁止泛指词：不用 #概念/、#方法/、#主题/ 这类无信息量词头——"用概念分类"指归属到知识点本身，不是套一层"概念/方法"标签（会占用词头、把所有卡挤进笼统口袋）。顶层直接用领域或主题名。
- 新建主标签、重命名/合并标签（tag_rename 会全库同步改名）都是需授权操作，先问用户再执行。

本地技能与仓库

- d:\OpenClaw\flomo-note 只存放：本 SKILL.md、AGENTS.md（总则）、.mcp.json（flomo MCP 配置，含 Bearer token；已 gitignore，不入库）。
- 本地不存储任何卡片正文；不建卡片目录、无卡片文件。卡片全部存在于云端 flomo 账号。
- git 仅用于追踪技能文档与配置文件（.mcp.json 例外，因含 token 被忽略）。提交只针对技能/文档改动，不应出现任何笔记内容。
- 运维动作（如重命名某标签、批量整理）通过 flomo.tag 系列在云端进行，不落本地。