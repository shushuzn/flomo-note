/* flomo-note 控制台 · 前端逻辑
   把云端 MCP 的只读能力全部摆出来，分四个视图：云端笔记 / 标签 / 参考 / 能力。
   数据全部来自本地服务 /api/*；页面不发起任何写操作（服务侧也没有写入口）。
   所有插入文本一律先转义，避免正文里的尖括号破坏结构。 */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const VIEW_META = {
  notes:     { title: "云端笔记", sub: "列 / 搜卡片：关键词 · 标签 · 起止日期 · 来源 · 是否含标签；点开读全文与相关笔记" },
  tags:      { title: "标签", sub: "本地快照分组速览 + 云端实时标签树 + 标签名搜索" },
  reference: { title: "参考", sub: "云端返回的四份文本：记忆文档、用户画像、笔记格式规范、标签使用规范" },
  tools:     { title: "能力", sub: "云端 MCP 暴露的全部工具；读工具已接出，写工具如实标注未接入" },
};

const state = {
  view: "notes",
  cache: {},
  // 笔记视图
  cloudMode: "recent",   // recent | search | review
  cloudQuery: "",
  cloudTag: "",
  cloudStart: "",
  cloudEnd: "",
  cloudSource: "",
  cloudHasTag: "",
  cloudLimit: 20,
  cloudData: null,
  selectMode: false,
  selected: new Set(),
  relNoSame: false,
  // 标签视图
  tagQuery: "",
  liveTags: null,
  liveNames: null,
  // 参考 / 能力视图
  refData: null,
  catalog: null,
};

/* ---------- 工具 ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// 行内格式：`code` 与 **粗体**（先转义，再替换，顺序不可颠倒）
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("is-on");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("is-on"), 2200);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function get(endpoint) {
  if (!state.cache[endpoint]) state.cache[endpoint] = api(`/api/${endpoint}`);
  return state.cache[endpoint];
}

/* ---------- 统计条 ---------- */
function renderStats(cells) {
  $("#stats").innerHTML = cells.map((c) => `
    <div class="stat${c.alert ? " is-alert" : ""}">
      <div class="stat-num">${esc(c.n ?? "—")}</div>
      <div class="stat-label">${esc(c.l)}</div>
    </div>`).join("");
}

/* ---------- 云端接口 ---------- */
// 云端接口的失败会被服务降级成 JSON（502 + error），这里统一取错误文案
async function cloudApi(path) {
  try {
    const res = await fetch(path);
    let data = {};
    try { data = await res.json(); } catch { /* 非 JSON 响应 */ }
    if (!res.ok) return { error: data.error || `${res.status} ${res.statusText}` };
    return data;
  } catch (e) {
    return { error: `本地服务无响应：${e.message}` };
  }
}

function failCard(text) {
  return `<div class="card"><div class="empty">${esc(text)}</div></div>`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 笔记正文的行内格式：`code`、**粗体**、[文本](链接)、flomo 的 <mark> 高亮与下划线
function mdInline(s) {
  return inline(s)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/&lt;mark&gt;([\s\S]*?)&lt;\/mark&gt;/g, "<mark>$1</mark>")
    .replace(/&lt;u&gt;([\s\S]*?)&lt;\/u&gt;/g, "<u>$1</u>");
}

// 云端文本渲染：标签段单独成行，`- ` 起列表，其余按段落
function memoHtml(text) {
  let html = "", inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const raw of String(text || "").split("\n")) {
    const t = raw.trim();
    if (!t) continue;
    if (/^(#[^\s#]+\s*)+$/.test(t)) {
      closeList();
      html += `<div class="memo-tagline">${esc(t)}</div>`;
      continue;
    }
    if (/^[-*]\s+/.test(t)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${mdInline(t.replace(/^[-*]\s+/, ""))}</li>`;
      continue;
    }
    closeList();
    html += `<p>${mdInline(t)}</p>`;
  }
  closeList();
  return html || `<p class="muted">（云端返回空文本）</p>`;
}

/* ---------- 视图：云端笔记 ---------- */
async function fetchCloud() {
  if (state.cloudMode === "review") return cloudApi("/api/cloud/review");
  const p = new URLSearchParams();
  if (state.cloudQuery)  p.set("keywords", state.cloudQuery);
  if (state.cloudTag)    p.set("tag", state.cloudTag);
  if (state.cloudStart)  p.set("start_date", state.cloudStart);
  if (state.cloudEnd)    p.set("end_date", state.cloudEnd);
  if (state.cloudSource) p.set("source", state.cloudSource);
  if (state.cloudHasTag) p.set("has_tag", state.cloudHasTag);
  p.set("limit", String(state.cloudLimit));
  return cloudApi(`/api/cloud/memos?${p}`);
}

function cloudHeadline() {
  if (state.cloudMode === "review") return "今日回顾";
  const bits = [];
  if (state.cloudQuery)  bits.push(`「${state.cloudQuery}」`);
  if (state.cloudTag)    bits.push(state.cloudTag);
  if (state.cloudStart || state.cloudEnd) bits.push(`${state.cloudStart || "…"} ~ ${state.cloudEnd || "…"}`);
  if (state.cloudSource) bits.push(`来源 ${state.cloudSource}`);
  if (state.cloudHasTag === "1") bits.push("含标签");
  if (state.cloudHasTag === "0") bits.push("无标签");
  return bits.length ? `筛选 ${bits.join(" · ")}` : "最近笔记";
}

function memoCard(m, picked) {
  return `
    <article class="memo${picked ? " is-picked" : ""}" data-memo="${esc(m.id)}" tabindex="0">
      <div class="memo-top">
        ${(m.tags || []).map((t) => `<span class="chip chip-sm">${esc(t)}</span>`).join("")}
        ${state.selectMode ? `<span class="memo-pick${picked ? " is-on" : ""}" title="选中以便批量取全文"></span>` : ""}
        <span class="memo-time">${esc(fmtTime(m.created_at))}</span>
      </div>
      <h3 class="memo-title">${esc(m.title)}</h3>
      <p class="memo-excerpt">${esc(m.excerpt)}</p>
      <div class="memo-foot">
        <span>${esc(m.word_count ?? "—")} 字</span>
        ${m.from ? `<span class="badge badge-quiet">${esc(m.from)}</span>` : ""}
        ${m.truncated ? '<span class="badge badge-quiet">已截断 · 点开看全文</span>' : ""}
      </div>
    </article>`;
}

async function viewNotes() {
  const ov = await get("overview");
  const cloud = ov.cloud || {};
  if (!cloud.available) {
    renderStats([{ n: "—", l: "云端笔记" }]);
    return `<div class="card"><div class="empty">
      云端不可用：${esc(cloud.reason || "未知原因")}
      <div class="hint">控制台靠项目内的凭证配置访问 flomo；凭证缺失时「标签」视图的本地快照部分仍可用。</div>
    </div></div>`;
  }

  const tree = await get("tagtree");
  const tagOptions = tree && tree.present
    ? tree.groups.flatMap((g) => g.children).concat(tree.bare || [])
    : [];

  const bar = `
    <div class="memo-bar">
      <input class="input" id="cloud-kw" placeholder="搜索正文或标签…" value="${esc(state.cloudQuery)}">
      <input class="input" id="cloud-tag" list="cloud-tags" placeholder="标签（完整路径，如 AI/RAG）" value="${esc(state.cloudTag)}">
      <datalist id="cloud-tags">${tagOptions.map((t) => `<option value="${esc(t)}"></option>`).join("")}</datalist>
      <input class="input input-date" id="cloud-start" placeholder="起 YYYY-MM-DD" value="${esc(state.cloudStart)}">
      <input class="input input-date" id="cloud-end" placeholder="止 YYYY-MM-DD" value="${esc(state.cloudEnd)}">
      <input class="input input-sm" id="cloud-source" placeholder="来源 from" value="${esc(state.cloudSource)}">
      <select class="input input-sel" id="cloud-has-tag">
        ${[["", "标签不限"], ["1", "仅含标签"], ["0", "仅无标签"]].map(([v, l]) =>
          `<option value="${v}"${state.cloudHasTag === v ? " selected" : ""}>${l}</option>`).join("")}
      </select>
      <select class="input input-sel" id="cloud-limit">
        ${[20, 50].map((n) => `<option value="${n}"${state.cloudLimit === n ? " selected" : ""}>${n} 条</option>`).join("")}
      </select>
      <button class="btn btn-primary" id="btn-cloud-run">查询</button>
      <button class="btn${state.cloudMode === "recent" ? " is-on" : ""}" id="btn-cloud-recent">最近</button>
      <button class="btn${state.cloudMode === "review" ? " is-on" : ""}" id="btn-cloud-review">今日回顾</button>
      <button class="btn${state.selectMode ? " is-on" : ""}" id="btn-select-mode">多选取全文</button>
    </div>`;

  if (!state.cloudData) state.cloudData = await fetchCloud();
  const data = state.cloudData;
  if (data.error) {
    renderStats([{ n: "—", l: "本次加载" }]);
    return bar + failCard(`云端读取失败：${data.error}`);
  }

  const memos = data.memos || [];
  renderStats([
    { n: tree && tree.present ? tree.total : "—", l: "标签总数" },
    { n: memos.length,        l: "本次加载" },
    { n: cloud.max_limit ?? "—", l: "单次上限" },
    { n: "只读",              l: "云端权限" },
    { n: "在线",              l: "连接" },
  ]);

  const head = `
    <div class="section-head">
      <h2>${esc(cloudHeadline())}</h2>
      <span class="count">${memos.length} 条${data.capped ? ` · 已达单次上限 ${cloud.max_limit ?? ""}，请用关键词 / 标签 / 日期继续筛` : ""}</span>
    </div>`;

  const body = memos.length
    ? `<div class="memo-grid">${memos.map((m) => memoCard(m, state.selected.has(m.id))).join("")}</div>`
    : failCard("没有匹配的笔记");

  const note = state.selectMode
    ? `<div class="hint hint-foot">多选模式：点卡片可选中，选好后按下方按钮一次取回全文（云端单次上限 ${10} 条）。</div>`
    : `<div class="hint hint-foot">只读浏览：正文按需现取，不落盘、不写云端。</div>`;

  const picks = state.selectMode
    ? `<div class="batchbar">
         <span class="batch-count">已选 ${state.selected.size} 条</span>
         <button class="btn btn-primary" id="btn-batch-open"${state.selected.size ? "" : " disabled"}>取回全文</button>
         <button class="btn" id="btn-batch-clear"${state.selected.size ? "" : " disabled"}>清空</button>
       </div>`
    : "";

  return bar + head + body + note + picks;
}

async function openMemo(id) {
  const dr = $("#drawer"), body = $("#drawer-body");
  $("#scrim").hidden = false;
  dr.classList.add("is-on");
  dr.setAttribute("aria-hidden", "false");
  $("#drawer-kind").textContent = "memo";
  body.innerHTML = `<div class="empty">正在读取全文…</div>`;
  const data = await cloudApi(`/api/cloud/memo?id=${encodeURIComponent(id)}`);
  if (data.error) {
    body.innerHTML = `<div class="empty">读取失败：${esc(data.error)}</div>`;
    return;
  }
  const m = data.memo;
  state.memo = m;
  body.innerHTML = `
    <div class="memo-top">
      ${(m.tags || []).map((t) => `<span class="chip chip-sm">${esc(t)}</span>`).join("")}
      <span class="memo-time">${esc(fmtTime(m.created_at))}</span>
    </div>
    <h2 class="drawer-title">${esc(m.title)}</h2>
    <div class="memo-meta">${esc(m.word_count ?? "—")} 字 · <code>${esc(m.id)}</code>${m.content_truncated ? " · 云端仍截断" : ""}</div>
    <div class="memo-body">${memoHtml(m.content)}</div>
    ${m.url ? `<a class="memo-link" href="${esc(m.url)}" target="_blank" rel="noreferrer">在 flomo 中打开</a>` : ""}
    <div class="rel-block" id="rel-block">
      <div class="rel-head">
        <span>相关笔记</span>
        <label class="rel-opt"><input type="checkbox" id="rel-nosame"${state.relNoSame ? " checked" : ""}> 不含同标签</label>
      </div>
      <div class="rel-list" id="rel-list"><span class="muted">正在取相关笔记…</span></div>
    </div>`;
  loadRelated(id);
}

async function loadRelated(id) {
  const box = $("#rel-list");
  if (!box) return;
  const q = `/api/cloud/related?id=${encodeURIComponent(id)}&limit=8${state.relNoSame ? "&no_same_tag=1" : ""}`;
  const data = await cloudApi(q);
  const box2 = $("#rel-list");
  if (!box2) return;
  if (data.error) { box2.innerHTML = `<span class="muted">取相关笔记失败：${esc(data.error)}</span>`; return; }
  const memos = data.memos || [];
  box2.innerHTML = memos.length
    ? memos.map((m) => `
        <button class="rel" data-memo="${esc(m.id)}">
          <span class="rel-title">${esc(m.title)}</span>
          <span class="rel-tags">${(m.tags || []).slice(0, 3).map((t) => esc(t)).join(" · ")}</span>
        </button>`).join("")
    : `<span class="muted">云端没有返回相关笔记</span>`;
}

async function openBatch() {
  const ids = [...state.selected].slice(0, 10);
  const dr = $("#drawer"), body = $("#drawer-body");
  $("#scrim").hidden = false;
  dr.classList.add("is-on");
  dr.setAttribute("aria-hidden", "false");
  $("#drawer-kind").textContent = "batch";
  body.innerHTML = `<div class="empty">正在取回 ${ids.length} 条全文…</div>`;
  const data = await cloudApi(`/api/cloud/memos/batch?ids=${encodeURIComponent(ids.join(","))}`);
  if (data.error) {
    body.innerHTML = `<div class="empty">读取失败：${esc(data.error)}</div>`;
    return;
  }
  body.innerHTML = `
    <h2 class="drawer-title">多选取全文</h2>
    <div class="memo-meta">请求 ${esc(data.requested)} 条 · 取回 ${esc(data.count)} 条${data.omitted_ids?.length ? ` · 云端未给 ${esc(data.omitted_ids.length)} 条` : ""}</div>
    ${(data.memos || []).map((m) => `
      <section class="batch-item">
        <div class="memo-top">
          ${(m.tags || []).map((t) => `<span class="chip chip-sm">${esc(t)}</span>`).join("")}
          <span class="memo-time">${esc(fmtTime(m.created_at))}</span>
        </div>
        <h3 class="batch-title">${esc(m.title)}</h3>
        <div class="memo-meta">${esc(m.word_count ?? "—")} 字 · <code>${esc(m.id)}</code>${m.content_truncated ? " · 云端仍截断" : ""}</div>
        <div class="memo-body">${memoHtml(m.content)}</div>
      </section>`).join("")}`;
}

function closeMemo() {
  const dr = $("#drawer");
  dr.classList.remove("is-on");
  dr.setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
}

function refreshCloud() {
  state.cloudData = null;
  render();
}

// 云端对不合法的日期不报错、直接返回 0 条——这种「静默空结果」最难查，故在本地先判一次：
// 格式不对、或形如 9 月 31 日这种不存在的日期，一律拦下并说明，不发无意义的请求。
function badDate(v) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) return true;
  const d = new Date(`${v}T00:00:00Z`);
  return Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== v;
}

function runCloudQuery() {
  const start = ($("#cloud-start")?.value || "").trim();
  const end   = ($("#cloud-end")?.value || "").trim();
  for (const [v, label] of [[start, "起始"], [end, "截止"]]) {
    if (v && badDate(v)) {
      toast(`${label}日期要用 YYYY-MM-DD，且须是真实存在的日期`);
      return;
    }
  }
  state.cloudQuery  = ($("#cloud-kw")?.value || "").trim();
  state.cloudTag    = ($("#cloud-tag")?.value || "").trim();
  state.cloudStart  = start;
  state.cloudEnd    = end;
  state.cloudSource = ($("#cloud-source")?.value || "").trim();
  state.cloudMode = "search";
  refreshCloud();
}

/* ---------- 视图：标签 ---------- */
function chipList(items, q = "") {
  if (!items.length) return `<div class="empty">没有匹配的标签</div>`;
  return `<div class="chips">${items.map((t) => `<span class="chip">${hl(t, q)}</span>`).join("")}</div>`;
}

async function viewTags() {
  const t = await get("tagtree");
  if (!t.present) {
    renderStats([{ n: "—", l: "标签总数" }]);
    return `<div class="empty">未找到标签树快照（tag_tree.txt）</div>`;
  }

  renderStats([
    { n: t.total,         l: "标签总数", alert: !t.consistent },
    { n: t.listed,        l: "列出条目" },
    { n: t.groups.length, l: "分组" },
    { n: t.bare_count,    l: "裸顶层" },
    { n: "现采",          l: "云端实时" },
  ]);

  const q = state.tagQuery.trim().toLowerCase();
  const groups = t.groups
    .map((g) => ({ ...g, children: g.children.filter((c) => !q || c.toLowerCase().includes(q)) }))
    .filter((g) => !q || g.name.toLowerCase().includes(q) || g.children.length);

  const snapshot = `
    <div class="section-head">
      <h2>本地快照</h2>
      <span class="count">${esc(t.path || "tag_tree.txt")} · 分组速览（计数与闸门同源）</span>
    </div>
    <div class="tagtool">
      <input class="input" id="tag-q" placeholder="搜索顶层或二级标签…" value="${esc(state.tagQuery)}">
      <span class="badge ${t.consistent ? "badge-ok" : "badge-err"}">
        列出 ${t.listed} / total ${t.total}${t.consistent ? " · 自洽" : " · 不一致"}
      </span>
      <span class="badge badge-quiet">${t.groups.length} 组 · ${t.bare_count} 裸顶层</span>
    </div>
    <div class="card">${
      groups.length
        ? groups.map((g) => `
          <div class="tgroup" data-group="${esc(g.name)}">
            <button class="tgroup-head">
              <span class="caret"></span>
              <span class="tgroup-name">${hl(g.name, q)}</span>
              <span class="badge badge-quiet">${g.children.length}${g.bare ? " + 裸顶层" : ""}</span>
            </button>
            <div class="tgroup-body">
              ${g.bare ? `<span class="chip chip-bare">${hl(g.name + "/", q)}</span>` : ""}
              ${g.children.map((c) => `<span class="chip">${hl(c, q)}</span>`).join("")}
            </div>
          </div>`).join("")
        : `<div class="empty">没有匹配的标签</div>`
    }</div>`;

  const live = state.liveTags;
  const liveBody = live
    ? (live.error
        ? `<div class="empty">云端标签树读取失败：${esc(live.error)}</div>`
        : `${chipList(live.tags)}
           <div class="hint">云端共 ${esc(live.total ?? "—")} 个标签，本次返回 ${esc(live.returned ?? live.tags.length)} 个${live.truncated ? "（已截断，可用前缀 / 深度收窄）" : ""}${live.hint ? ` · ${esc(live.hint)}` : ""}</div>`)
    : `<div class="empty">按「拉取」从云端现采标签树（不改动本地快照）</div>`;

  const liveBlock = `
    <div class="section-head">
      <h2>云端实时标签树</h2>
      <span class="count">现采 · 不依赖本地快照是否最新</span>
    </div>
    <div class="tagtool">
      <input class="input input-sm" id="live-prefix" placeholder="前缀，如 AI/" value="${esc(state.livePrefix)}">
      <input class="input input-sm" id="live-depth" placeholder="深度，如 2" value="${esc(state.liveDepth)}">
      <select class="input input-sel" id="live-limit">
        ${[200, 500, 1000].map((n) => `<option value="${n}"${Number(state.liveLimit) === n ? " selected" : ""}>${n} 个</option>`).join("")}
      </select>
      <button class="btn btn-primary" id="btn-live-tree">拉取</button>
    </div>
    <div class="card card-pad">${liveBody}</div>`;

  const names = state.liveNames;
  const nameBody = names
    ? (names.error
        ? `<div class="empty">标签名搜索失败：${esc(names.error)}</div>`
        : `${chipList(names.tags, state.liveNames.q)}<div class="hint">命中 ${esc(names.count ?? names.tags.length)} 个标签名</div>`)
    : `<div class="empty">标签多时用它快速定位，比拉全树轻</div>`;

  const nameBlock = `
    <div class="section-head">
      <h2>标签名搜索</h2>
      <span class="count">云端 tag_search</span>
    </div>
    <div class="tagtool">
      <input class="input" id="tag-name-q" placeholder="关键词，如 反腐 / RAG…" value="${esc(state.liveNames?.q || "")}">
      <button class="btn btn-primary" id="btn-tag-name">搜索</button>
    </div>
    <div class="card card-pad">${nameBody}</div>`;

  return snapshot + liveBlock + nameBlock;
}

function hl(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return safe;
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
}

/* ---------- 视图：参考 ---------- */
async function viewReference() {
  if (!state.refData) state.refData = await cloudApi("/api/cloud/reference");
  const d = state.refData;
  if (d.error) return failCard(`云端参考文本读取失败：${d.error}`);

  renderStats([
    { n: "记忆", l: "memory.md" },
    { n: "画像", l: "user.md" },
    { n: "格式", l: "get_format_guide" },
    { n: "标签", l: "get_tag_guide" },
  ]);

  const block = (title, sub, doc) => {
    const len = (doc?.content || "").length;
    return `
      <section class="card ref-card">
        <div class="ref-head">
          <h2>${esc(title)}</h2>
          <span class="count">${esc(len)} 字${sub ? ` · ${esc(sub)}` : ""}</span>
        </div>
        <div class="ref-body">${memoHtml(doc?.content || "")}</div>
      </section>`;
  };

  return `<div class="ref-grid">
    ${block("记忆文档", "memory_context", d.memory)}
    ${block("用户画像", "memory_user", d.user)}
    ${block("笔记格式规范", "get_format_guide", d.format)}
    ${block("标签使用规范", "get_tag_guide", d.tag)}
  </div>
  <div class="hint hint-foot">四份文本均由云端现取，只读展示；控制台不改写任何一份。</div>`;
}

/* ---------- 视图：能力 ---------- */
async function viewTools() {
  if (!state.catalog) state.catalog = await cloudApi("/api/cloud/tools");
  const c = state.catalog;
  if (c.error) return failCard(`工具清单读取失败：${c.error}`);

  renderStats([
    { n: c.count, l: "云端工具总数" },
    { n: c.read,  l: "只读" },
    { n: c.write, l: "写操作" },
    { n: `${c.wired}/${c.count}`, l: "已接出" },
  ]);

  const rows = (list) => list.map((t) => `
    <div class="trow">
      <div class="trow-name"><code>${esc(t.name)}</code></div>
      <div class="trow-sum">${esc(t.summary || "—")}</div>
      <div class="trow-args">${(t.args || []).map((a) => `<span class="chip chip-sm">${esc(a)}</span>`).join("") || '<span class="muted">无参数</span>'}</div>
      <div class="trow-state">${t.wired
        ? '<span class="badge badge-ok">已接出</span>'
        : `<span class="badge badge-warn">未接入</span>`}</div>
    </div>${t.reason ? `<div class="trow-reason">${esc(t.reason)}</div>` : ""}`).join("");

  const reads = (c.tools || []).filter((t) => t.kind === "read");
  const writes = (c.tools || []).filter((t) => t.kind === "write");

  return `
    <div class="section-head"><h2>只读工具</h2><span class="count">${reads.length} 个 · 已在各视图接出</span></div>
    <div class="card">${rows(reads)}</div>
    <div class="section-head"><h2>写工具</h2><span class="count">${writes.length} 个 · 控制台不代写</span></div>
    <div class="card">${rows(writes)}</div>
    <div class="hint hint-foot">清单由云端 <code>tools/list</code> 现取，不靠本地写死；写工具如实列出但不可触发。</div>`;
}

/* ---------- 渲染调度 ---------- */
const VIEWS = {
  notes:     viewNotes,
  tags:      viewTags,
  reference: viewReference,
  tools:     viewTools,
};

async function render() {
  const meta = VIEW_META[state.view];
  $("#page-title").textContent = meta.title;
  $("#page-sub").textContent = meta.sub;
  $$("#nav .nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.view === state.view));

  const view = $("#view");
  view.innerHTML = `<div class="card"><div class="empty"><div class="skeleton" style="width:60%;margin:0 auto 9px"></div>
    <div class="skeleton" style="width:40%;margin:0 auto"></div></div></div>`;
  try {
    view.innerHTML = await VIEWS[state.view]();
  } catch (e) {
    view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

/* ---------- 事件 ---------- */
document.addEventListener("click", (e) => {
  const nav = e.target.closest("#nav .nav-item");
  if (nav) {
    state.view = nav.dataset.view;
    closeMemo();          // 切视图时顺手收起详情，不必先点遮罩
    render();
    return;
  }

  const rel = e.target.closest(".rel[data-memo]");
  if (rel) {
    openMemo(rel.dataset.memo);
    return;
  }

  const memo = e.target.closest(".memo[data-memo]");
  if (memo) {
    if (state.selectMode) {
      const id = memo.dataset.memo;
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
      render();
    } else {
      openMemo(memo.dataset.memo);
    }
    return;
  }

  if (e.target.closest("#drawer-close") || e.target.closest("#scrim")) {
    closeMemo();
    return;
  }

  if (e.target.closest("#btn-cloud-run"))   { runCloudQuery(); return; }
  if (e.target.closest("#btn-cloud-recent")) {
    state.cloudMode = "recent";
    state.cloudQuery = state.cloudTag = state.cloudStart = state.cloudEnd = state.cloudSource = "";
    state.cloudHasTag = "";
    state.selected.clear();
    refreshCloud();
    return;
  }
  if (e.target.closest("#btn-cloud-review")) {
    state.cloudMode = "review";
    state.cloudQuery = state.cloudTag = state.cloudStart = state.cloudEnd = state.cloudSource = "";
    state.cloudHasTag = "";
    refreshCloud();
    return;
  }
  if (e.target.closest("#btn-select-mode")) {
    state.selectMode = !state.selectMode;
    state.selected.clear();
    render();
    return;
  }
  if (e.target.closest("#btn-batch-clear")) { state.selected.clear(); render(); return; }
  if (e.target.closest("#btn-batch-open"))  { openBatch(); return; }

  if (e.target.closest("#btn-live-tree")) {
    state.livePrefix = ($("#live-prefix")?.value || "").trim();
    state.liveDepth  = ($("#live-depth")?.value || "").trim();
    state.liveLimit  = Number($("#live-limit")?.value) || 200;
    state.liveTags = { loading: true };
    render();
    loadLiveTags();
    return;
  }
  if (e.target.closest("#btn-tag-name")) {
    const kw = ($("#tag-name-q")?.value || "").trim();
    if (!kw) { toast("先填关键词"); return; }
    state.liveNames = { loading: true, q: kw };
    render();
    loadTagNames(kw);
    return;
  }

  const ghead = e.target.closest(".tgroup-head");
  if (ghead) {
    ghead.closest(".tgroup").classList.toggle("is-open");
    return;
  }

  if (e.target.closest("#btn-refresh")) {
    state.cache = {};
    state.cloudData = null;
    state.liveTags = null;
    state.liveNames = null;
    state.refData = null;
    state.catalog = null;
    render();
    toast("已重新读取云端与本地快照");
  }
});

async function loadLiveTags() {
  const p = new URLSearchParams({ limit: String(state.liveLimit) });
  if (state.livePrefix) p.set("prefix", state.livePrefix);
  if (state.liveDepth)  p.set("depth", state.liveDepth);
  state.liveTags = await cloudApi(`/api/cloud/tagtree?${p}`);
  render();
}

async function loadTagNames(kw) {
  const data = await cloudApi(`/api/cloud/tags?keywords=${encodeURIComponent(kw)}&limit=60`);
  state.liveNames = { ...data, q: kw };
  render();
}

document.addEventListener("change", (e) => {
  if (e.target.id === "cloud-limit") {
    state.cloudLimit = Number(e.target.value) || 20;
    refreshCloud();
  }
  if (e.target.id === "cloud-has-tag") {
    state.cloudHasTag = e.target.value;
    refreshCloud();
  }
  if (e.target.id === "rel-nosame") {
    state.relNoSame = e.target.checked;
    if (state.memo) loadRelated(state.memo.id);
  }
});

document.addEventListener("input", (e) => {
  // 检索框只更新状态、不重渲染，避免每敲一个字就打一次云端、并丢掉焦点
  switch (e.target.id) {
    case "cloud-kw":     state.cloudQuery = e.target.value; return;
    case "cloud-tag":    state.cloudTag = e.target.value; return;
    case "cloud-start":  state.cloudStart = e.target.value; return;
    case "cloud-end":    state.cloudEnd = e.target.value; return;
    case "cloud-source": state.cloudSource = e.target.value; return;
    case "live-prefix":  state.livePrefix = e.target.value; return;
    case "live-depth":   state.liveDepth = e.target.value; return;
  }
  if (e.target.id === "tag-q") {
    state.tagQuery = e.target.value;
    const pos = e.target.selectionStart;
    render().then(() => {
      const box = $("#tag-q");
      if (box) { box.focus(); box.setSelectionRange(pos, pos); }
      if (state.tagQuery) $$(".tgroup").forEach((g) => g.classList.add("is-open"));
    });
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMemo(); return; }
  const ids = ["cloud-kw", "cloud-tag", "cloud-start", "cloud-end", "cloud-source"];
  if (e.key === "Enter" && ids.includes(e.target.id)) {
    e.preventDefault();
    runCloudQuery();
  }
  if (e.key === "Enter" && e.target.id === "tag-name-q") {
    e.preventDefault();
    const kw = ($("#tag-name-q")?.value || "").trim();
    if (kw) { state.liveNames = { loading: true, q: kw }; render(); loadTagNames(kw); }
  }
});

/* ---------- 启动 ---------- */
(async function boot() {
  try {
    const overview = await get("overview");
    const cloud = overview.cloud || {};
    $("#conn-badge").className = "badge " + (cloud.available ? "badge-ok" : "badge-warn");
    $("#conn-text").textContent = cloud.available ? "本地 + 云端已连接" : "本地已连接 · 云端不可用";
  } catch {
    $("#conn-badge").className = "badge badge-err";
    $("#conn-text").textContent = "未连接";
    $$(".stat-num").forEach((n) => (n.textContent = "—"));
  }
  render();
})();
