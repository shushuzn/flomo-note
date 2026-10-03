/* flomo-note 控制台 · 前端逻辑
   把云端 MCP 的能力全部摆出来，分四个视图：云端笔记 / 标签 / 参考 / 能力。
   读随时现采；**写不是直通**——提交后由服务侧强制过格式与流程闸门，
   并在写入后回读全文验收；页面只是把这两步做成可操作的按钮，不提供跳过开关。
   所有插入文本一律先转义，避免正文里的尖括号破坏结构。 */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const VIEW_META = {
  notes:     { title: "云端笔记", sub: "列 / 搜卡片：关键词 · 标签 · 起止日期 · 来源 · 是否含标签；点开读全文、相关笔记，或新建与编辑" },
  tags:      { title: "标签", sub: "云端实时标签树 + 标签名搜索 + 标签重命名" },
  reference: { title: "参考", sub: "云端返回的四份文本：记忆文档、用户画像、笔记格式规范、标签使用规范" },
  tools:     { title: "能力", sub: "云端 MCP 暴露的全部工具：读写均已接出，写工具标注写入前的门槛" },
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
  // 编辑器：mode=create|edit；raw=是否切到「完整卡片文本」直编
  editor: null,
  // 标签视图
  liveTags: null,
  liveNames: null,
  renOld: "",
  renNew: "",
  renMax: "",
  renConfirm: false,     // 重命名二次确认：首次点击只进入待确认态
  renameResult: null,
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

// 写接口：闸门未过时服务回 422 并带上待修项，这里把状态码与文案一并交出，
// 编辑器据此把「哪一条不过」直接摆到眼前。
async function cloudPost(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch { /* 非 JSON 响应 */ }
    if (!res.ok) return { error: data.error || `${res.status} ${res.statusText}`, status: res.status };
    return data;
  } catch (e) {
    return { error: `本地服务无响应：${e.message}` };
  }
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
      <div class="hint">控制台靠项目内的凭证配置访问 flomo。</div>
    </div></div>`;
  }

  const bar = `
    <div class="memo-bar">
      <input class="input" id="cloud-kw" placeholder="搜索正文或标签…" value="${esc(state.cloudQuery)}">
      <input class="input" id="cloud-tag" placeholder="标签（完整路径，如 AI/RAG）" value="${esc(state.cloudTag)}">
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
      <button class="btn btn-accent" id="btn-new-memo">＋ 新建笔记</button>
    </div>`;

  if (!state.cloudData) state.cloudData = await fetchCloud();
  const data = state.cloudData;
  if (data.error) {
    renderStats([{ n: "—", l: "本次加载" }]);
    return bar + failCard(`云端读取失败：${data.error}`);
  }

  const memos = data.memos || [];
  renderStats([
    { n: memos.length,        l: "本次加载" },
    { n: cloud.max_limit ?? "—", l: "单次上限" },
    { n: "可读写",            l: "云端权限" },
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
    : `<div class="hint hint-foot">点卡片读全文，可继续编辑或新建；写入须过格式与流程闸门，写完自动回读验收。</div>`;

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
    <div class="drawer-actions">
      <button class="btn btn-accent" data-edit="${esc(m.id)}">编辑这张卡</button>
      <span class="hint">改动落库前须过格式与流程闸门</span>
    </div>
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

/* ---------- 编辑器（新建 / 编辑；写入由服务侧过闸门） ---------- */
// 与后端 split_card 同构的轻量解析：首行标签段 → 概念名 → 正文。
// 只用于把已有卡片预填进表单；写入判定一律以后端为准，前端不做闸门判断。
function splitCard(content) {
  const lines = String(content || "").split("\n");
  const tags = [];
  let i = 0;
  while (i < lines.length) {
    const s = lines[i].trim();
    if (!s) { i++; continue; }
    if (/^#[^\s#]+(?:\s+#[^\s#]+)*$/.test(s)) {
      s.split(/\s+/).forEach((t) => tags.push(t));
      i++;
      continue;
    }
    break;
  }
  while (i < lines.length && !lines[i].trim()) i++;
  const concept = i < lines.length ? lines[i].trim() : "";
  const body = lines.slice(i + 1).join("\n").replace(/\s+$/, "");
  return { tags, concept, body };
}

// 编辑器当前内容：原始文本模式直接取文本域，三段式则按卡片格式拼装。
// 注意概念名紧接标签段的下一行（不是空行隔开），空行只出现在概念名与正文之间。
function editorText() {
  if (state.editor?.raw) return $("#ed-raw")?.value || "";
  const tags = ($("#ed-tags")?.value || "").trim();
  const concept = ($("#ed-concept")?.value || "").trim();
  const body = ($("#ed-body")?.value || "").replace(/\s+$/, "");
  return `${tags}\n${concept}\n\n${body}\n`;
}

function updateEditorPreview() {
  const box = $("#ed-preview"), cnt = $("#ed-chars");
  if (!box) return;
  const text = editorText();
  box.innerHTML = memoHtml(text);
  if (cnt) cnt.textContent = `${text.length} 字`;
}

function issueList(errors, warnings) {
  const block = (arr, cls) => (arr || []).length
    ? `<ul class="issues ${cls}">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : "";
  return block(errors, "is-err") + block(warnings, "is-warn");
}

function editorResult(html) {
  const box = $("#editor-result");
  if (!box) return;
  box.hidden = false;
  box.innerHTML = html;
}

function syncVerifyOpt() {
  const wrap = $("#ed-verify-wrap");
  if (wrap) wrap.hidden = !!$("#ed-skip-web")?.checked;
}

function setEditorRaw(on) {
  if (!state.editor) return;
  if (on && !state.editor.raw) {
    $("#ed-raw").value = editorText();       // 切过去：带上三段拼好的完整文本
  } else if (!on && state.editor.raw) {
    const p = splitCard($("#ed-raw").value); // 切回来：从完整文本解析回三段
    $("#ed-tags").value = p.tags.join(" ");
    $("#ed-concept").value = p.concept;
    $("#ed-body").value = p.body;
  }
  state.editor.raw = !!on;
  $("#editor-form").hidden = !!on;
  $("#editor-raw-wrap").hidden = !on;
  $("#btn-ed-raw").classList.toggle("is-on", !!on);
  updateEditorPreview();
}

function openEditor(mode, memo) {
  closeMemo();
  state.editor = { mode, id: memo?.id || "", raw: false };
  const dr = $("#editor");
  $("#scrim-editor").hidden = false;
  dr.classList.add("is-on");
  dr.setAttribute("aria-hidden", "false");

  $("#editor-kind").textContent = mode === "edit" ? "编辑笔记" : "新建笔记";
  $("#editor-sub").textContent = mode === "edit"
    ? "更新前先回读原文，更新后回读验收"
    : "写入前须过格式与流程闸门";

  const parsed = splitCard(memo?.content || "");
  $("#ed-tags").value = parsed.tags.join(" ");
  $("#ed-concept").value = parsed.concept;
  $("#ed-body").value = parsed.body;
  $("#ed-skip-web").checked = true;    // 默认按「非术语卡」声明；含术语时取消勾选并填验证记录
  $("#ed-verify").value = "";
  syncVerifyOpt();
  $("#editor-result").hidden = true;
  $("#editor-result").innerHTML = "";
  setEditorRaw(false);
  setTimeout(() => $("#ed-tags")?.focus(), 60);
}

function closeEditor() {
  const dr = $("#editor");
  if (!dr) return;
  dr.classList.remove("is-on");
  dr.setAttribute("aria-hidden", "true");
  $("#scrim-editor").hidden = true;
  state.editor = null;
}

async function editorCheck() {
  const r = await cloudPost("/api/cloud/validate", { content: editorText() });
  if (r.error) {
    editorResult(`<div class="result-head is-err">校验未完成</div><div class="result-sub">${esc(r.error)}</div>`);
    return null;
  }
  const sig = r.tagline ? `<div class="result-sub">签名：<code>${esc(r.tagline)}</code> · ${esc(r.concept || "(取不到概念名)")}</div>` : "";
  editorResult(
    `<div class="result-head${r.ok ? " is-ok" : " is-err"}">格式闸门 · ${r.ok ? "通过" : "未通过"}</div>` +
    sig + issueList(r.errors, r.warnings) +
    (r.ok && !(r.warnings || []).length ? '<div class="ok-note">没有待修项，可以写入。</div>' : "")
  );
  return r;
}

async function editorGate() {
  const skipWeb = !!$("#ed-skip-web")?.checked;
  let verify = null;
  if (!skipWeb) {                       // 不声明「非术语卡」就必须给验证记录
    const raw = ($("#ed-verify")?.value || "").trim();
    if (!raw) {
      editorResult('<div class="result-head is-err">缺少第 ② 步验证记录</div><div class="result-sub">要么勾选「非术语卡（跳过网络验证）」，要么把网络搜索的验证记录 JSON 填进来。</div>');
      return null;
    }
    try {
      verify = JSON.parse(raw);
    } catch (e) {
      editorResult(`<div class="result-head is-err">验证记录不是合法 JSON</div><div class="result-sub">${esc(e.message)}</div>`);
      return null;
    }
  }

  editorResult('<div class="result-head">正在跑流程闸门…</div>');
  const r = await cloudPost("/api/cloud/gate", {
    content: editorText(),
    skip_web: skipWeb,
    anchor_id: state.editor?.id || null,
    verify,
  });
  if (r.error) {
    editorResult(`<div class="result-head is-err">流程闸门未通过</div><div class="result-sub">${esc(r.error)}</div>`);
    return null;
  }
  const out = (r.lines || []).map((l) => `<div>${esc(l)}</div>`).join("");
  editorResult(
    `<div class="result-head${r.ok ? " is-ok" : " is-err"}">流程闸门 · ${r.ok ? "全过，凭证已出" : "未全过"}</div>` +
    `<pre class="gate-out">${out}</pre>` +
    (r.ok
      ? (r.draft ? `<div class="result-sub">草稿 ${esc(r.draft)}（收尾清理按近轮窗口回收）</div>` : "")
      : `<div class="result-sub">按上面的阻塞项逐条处理后再跑一次；凭证不出，写入会被拒。</div>`)
  );
  return r;
}

async function editorSave() {
  const ed = state.editor;
  if (!ed) return;
  const isEdit = ed.mode === "edit";
  const btn = $("#btn-ed-save");
  btn.disabled = true;
  editorResult('<div class="result-head">正在写入并回读验收…</div>');
  const text = editorText();
  // 两条写路径分开写全：路径以字面量出现在调用处，便于前后端对齐回归逐一比对
  const r = isEdit
    ? await cloudPost("/api/cloud/memo/update", { id: ed.id, content: text })
    : await cloudPost("/api/cloud/memo/create", { content: text });
  btn.disabled = false;
  if (r.error) {
    editorResult(
      `<div class="result-head is-err">写入未完成</div><div class="result-sub">${esc(r.error)}</div>` +
      (r.status === 422 ? '<div class="result-sub">闸门未过，云端未收到任何写请求。先点「校验」或「跑闸门」看待修项。</div>' : "")
    );
    toast(r.status === 422 ? "闸门未过，未写入云端" : "写入失败");
    return;
  }
  const v = r.verified || {};
  const before = r.before ? `<span>更新前 ${esc(r.before.chars)} 字</span>` : "";
  editorResult(
    `<div class="result-head is-ok">已写入，并回读全文验收</div>
     <div class="result-sub"><code>${esc(r.id || "")}</code> · 云端字数 ${esc(r.word_count ?? "—")} · 回读 ${esc(v.chars ?? "—")} 字 ${before}</div>
     <div class="${v.matched ? "ok-note" : "result-head is-err"}">${v.matched
        ? "回读正文与提交内容逐字一致。"
        : "回读正文与提交内容不一致，请点开该卡核对后再决定是否重写。"}</div>
     <div class="result-sub">列表已标记为待刷新，按「刷新」即取回最新云端状态。</div>`
  );
  toast(isEdit ? "已更新并验收" : "已新建并验收");
  state.cloudData = null;
  state.cache = {};
  state.liveTags = null;
  state.liveNames = null;
}


/* ---------- 视图：标签 ---------- */
function chipList(items, q = "") {
  if (!items.length) return `<div class="empty">没有匹配的标签</div>`;
  return `<div class="chips">${items.map((t) => `<span class="chip">${hl(t, q)}</span>`).join("")}</div>`;
}

async function viewTags() {
  const live = state.liveTags;
  const liveBody = live
    ? (live.error
        ? `<div class="empty">云端标签树读取失败：${esc(live.error)}</div>`
        : `${chipList(live.tags)}
           <div class="hint">云端共 ${esc(live.total ?? "—")} 个标签，本次返回 ${esc(live.returned ?? live.tags.length)} 个${live.truncated ? "（已截断，可用前缀 / 深度收窄）" : ""}${live.hint ? ` · ${esc(live.hint)}` : ""}</div>`)
    : `<div class="empty">按「拉取」从云端现采标签树</div>`;

  const liveBlock = `
    <div class="section-head">
      <h2>云端实时标签树</h2>
      <span class="count">现采 · 云端为唯一事实源</span>
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

  const ren = state.renameResult;
  const renBody = ren
    ? (ren.error
        ? `<div class="empty">重命名未完成：${esc(ren.error)}</div>`
        : `<div class="result-sub"><code>${esc(ren.old_tag)}</code> → <code>${esc(ren.new_tag)}</code>${
             ren.max_memos ? ` · 本次规模 ${esc(ren.max_memos)}` : " · 默认规模 200"}</div>
           <pre class="gate-out">${esc(JSON.stringify(ren.result || {}, null, 2))}</pre>`)
    : `<div class="empty">把挂旧标签的卡片整批改挂新标签。云端默认只处理 200 条，超出时回报实际匹配数而不写；确认规模后再填「规模上限」（绝对上限 2000）</div>`;

  const renameBlock = `
    <div class="section-head">
      <h2>标签重命名</h2>
      <span class="count">云端批量写入 · 全库标签改名</span>
    </div>
    <div class="tagtool">
      <input class="input" id="ren-old" placeholder="原标签，如 测试/闸门连通" value="${esc(state.renOld)}">
      <span class="ren-arrow">→</span>
      <input class="input" id="ren-new" placeholder="新标签，如 工程/工具" value="${esc(state.renNew)}">
      <input class="input input-sm" id="ren-max" placeholder="规模上限" value="${esc(state.renMax)}">
      <button class="btn ${state.renConfirm ? "btn-danger" : "btn-accent"}" id="btn-tag-rename">
        ${state.renConfirm ? "确认执行重命名" : "重命名"}
      </button>
    </div>
    <div class="card card-pad">${renBody}</div>`;

  return liveBlock + nameBlock + renameBlock;
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

/* ---------- 视图：能力（工具台，可直接执行） ---------- */
// 参数表单按云端给的 inputSchema 规格生成：声明的类型决定控件，
// 于是云端加了参数、界面自动跟着长出来，不需要在这里逐工具写死表单。
// 类型已由服务侧归一化为单个 token（boolean / integer / number / array / string），
// 这里只认这张小词表；联合类型不再需要前端自己判断。
function toolField(toolName, spec) {
  const id = `arg-${toolName}-${spec.name}`;
  const head = `<span class="field-label">${esc(spec.name)}${spec.required ? "<em>必填</em>" : ""}</span>`;
  const attr = `data-tool="${esc(toolName)}" data-key="${esc(spec.name)}"`;
  if (spec.type === "boolean") {
    return `<label class="field field-inline" for="${id}">${head}
      <select class="input input-sel" id="${id}" ${attr}>
        ${[["", "不传"], ["true", "真"], ["false", "假"]].map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}
      </select></label>`;
  }
  if (spec.type === "array") {
    // 数组型参数（如 memo_batch_get 的 ids）：用逗号 / 空格分隔，执行时切成数组
    return `<label class="field field-inline" for="${id}">${head}
      <input class="input" id="${id}" type="text" ${attr} data-list="1"
             placeholder="${esc(spec.desc || "多个值用逗号分隔")}"></label>`;
  }
  const isNum = spec.type === "integer" || spec.type === "number";
  return `<label class="field field-inline" for="${id}">${head}
    <input class="input" id="${id}" type="${isNum ? "number" : "text"}" ${attr}
           placeholder="${esc(spec.desc || spec.type)}"></label>`;
}

function toolPanel(t) {
  const fields = (t.arg_specs || []).map((s) => toolField(t.name, s)).join("");
  return `
    <section class="tool-panel">
      <div class="tool-head">
        <code class="tool-name">${esc(t.name)}</code>
        <button class="btn btn-primary btn-sm" data-run="${esc(t.name)}">执行</button>
      </div>
      <p class="tool-sum">${esc(t.summary || "—")}</p>
      ${fields ? `<div class="tool-fields">${fields}</div>` : '<p class="hint">该工具不需要参数</p>'}
      <div class="tool-out" id="out-${esc(t.name)}" hidden></div>
    </section>`;
}

function writePanel(t) {
  const go = t.name === "tag_rename"
    ? { label: "去「标签」页重命名", to: "tags" }
    : t.name === "memo_create"
      ? { label: "去「云端笔记」新建", to: "notes-new" }
      : { label: "去「云端笔记」打开一张卡再编辑", to: "notes" };
  return `
    <section class="tool-panel is-write">
      <div class="tool-head">
        <code class="tool-name">${esc(t.name)}</code>
        <button class="btn btn-accent btn-sm" data-goto="${esc(go.to)}">${esc(go.label)}</button>
      </div>
      <p class="tool-sum">${esc(t.summary || "—")}</p>
      <p class="tool-note">${esc(t.reason || "写入前须过格式与流程闸门")}</p>
      <div class="tool-fields">${(t.arg_specs || []).map((s) => `<span class="chip chip-sm">${esc(s.name)}</span>`).join("")}</div>
    </section>`;
}

async function viewTools() {
  if (!state.catalog) state.catalog = await cloudApi("/api/cloud/tools");
  const c = state.catalog;
  if (c.error) return failCard(`工具清单读取失败：${c.error}`);

  renderStats([
    { n: c.count, l: "云端工具总数" },
    { n: c.read,  l: "只读 · 可直接执行" },
    { n: c.write, l: "写 · 须过闸门" },
    { n: `${c.wired}/${c.count}`, l: "已接出" },
  ]);

  const reads = (c.tools || []).filter((t) => t.kind === "read" && t.wired);
  const writes = (c.tools || []).filter((t) => t.kind === "write");
  const rest = (c.tools || []).filter((t) => !t.wired);

  return `
    <div class="section-head">
      <h2>只读工具 · 直接执行</h2>
      <span class="count">${reads.length} 个 · 填参数点「执行」，结果就地回显</span>
    </div>
    <div class="tool-grid">${reads.map(toolPanel).join("")}</div>

    <div class="section-head">
      <h2>写工具</h2>
      <span class="count">${writes.length} 个 · 须过格式与流程闸门，改写后回读验收</span>
    </div>
    <div class="tool-grid">${writes.map(writePanel).join("")}</div>

    <div class="hint hint-foot">
      清单由云端 <code>tools/list</code> 现取，表单按云端声明的参数规格生成，不靠本地写死。
      读工具在这里直接跑；写工具走专属入口（新建 / 编辑 / 标签重命名），闸门与回读验收由服务侧强制。
    </div>
    ${rest.length ? `<div class="hint">另有 ${rest.length} 个云端工具未接出：${rest.map((t) => `<code>${esc(t.name)}</code>`).join(" ")}</div>` : ""}`;
}

async function runTool(name, btn) {
  const args = {};
  $$(`[data-tool="${name}"]`).forEach((el) => {
    const raw = (el.value || "").trim();
    if (raw === "") return;
    if (el.tagName === "SELECT") args[el.dataset.key] = raw === "true";
    else if (el.dataset.list === "1") args[el.dataset.key] = raw.split(/[,\s]+/).filter(Boolean);
    else args[el.dataset.key] = el.type === "number" ? Number(raw) : raw;
  });
  const box = $(`#out-${name}`);
  btn.disabled = true;
  box.hidden = false;
  box.innerHTML = '<div class="empty">正在执行…</div>';
  const r = await cloudPost("/api/cloud/tool", { name, arguments: args });
  btn.disabled = false;
  if (r.error) {
    box.innerHTML = `<div class="empty">执行失败：${esc(r.error)}</div>`;
    toast("执行失败");
    return;
  }
  box.innerHTML = toolResult(r);
  toast(`${name} 已执行`);
}

function toolResult(r) {
  const args = Object.keys(r.arguments || {}).length
    ? `<div class="tool-args">参数：${Object.entries(r.arguments).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</div>`
    : "";
  if ((r.memos || []).length) {
    return args + `<div class="tool-count">返回 ${r.memos.length} 条</div>
      <div class="tool-memos">${r.memos.map((m) => `
        <article class="tool-memo" data-memo="${esc(m.id)}" tabindex="0">
          <div class="memo-top">
            ${(m.tags || []).slice(0, 3).map((t) => `<span class="chip chip-sm">${esc(t)}</span>`).join("")}
            <span class="memo-time">${esc(fmtTime(m.created_at))}</span>
          </div>
          <h4 class="tool-memo-title">${esc(m.title)}</h4>
          <p class="memo-excerpt">${esc(m.excerpt)}</p>
        </article>`).join("")}</div>`;
  }
  if (r.text) return args + `<div class="tool-text">${memoHtml(r.text)}</div>`;
  return args + `<pre class="gate-out">${esc(JSON.stringify(r.raw ?? {}, null, 2)).slice(0, 4000)}</pre>`;
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

  // 工具台里跑出来的卡片同样可点开读全文
  const toolMemo = e.target.closest(".tool-memo[data-memo]");
  if (toolMemo) { openMemo(toolMemo.dataset.memo); return; }

  const runBtn = e.target.closest("[data-run]");
  if (runBtn) { runTool(runBtn.dataset.run, runBtn); return; }

  const goto = e.target.closest("[data-goto]");
  if (goto) {
    const to = goto.dataset.goto;
    closeMemo();
    if (to === "tags") { state.view = "tags"; render(); return; }
    state.view = "notes";
    render();
    if (to === "notes-new") setTimeout(() => openEditor("create"), 140);
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

  if (e.target.closest("#btn-new-memo")) { openEditor("create"); return; }
  const editBtn = e.target.closest("[data-edit]");
  if (editBtn) { if (state.memo) openEditor("edit", state.memo); return; }

  if (e.target.closest("#editor-close") || e.target.closest("#scrim-editor")) { closeEditor(); return; }
  if (e.target.closest("#btn-ed-raw"))   { setEditorRaw(!state.editor?.raw); return; }
  if (e.target.closest("#btn-ed-check")) { editorCheck(); return; }
  if (e.target.closest("#btn-ed-gate"))  { editorGate(); return; }
  if (e.target.closest("#btn-ed-save"))  { editorSave(); return; }

  if (e.target.closest("#btn-tag-rename")) { runTagRename(); return; }

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
    toast("已重新读取云端数据");
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

// 批量重命名是覆盖面最广的写操作，故用两步确认：首次点击只进入待确认态，
// 再点一次才真的发请求；规模超过云端默认 200 条时，须自行填「规模上限」明示。
async function runTagRename() {
  state.renOld = ($("#ren-old")?.value || "").trim().replace(/^#/, "");
  state.renNew = ($("#ren-new")?.value || "").trim().replace(/^#/, "");
  state.renMax = ($("#ren-max")?.value || "").trim();
  if (!state.renOld || !state.renNew) { toast("原标签与新标签都要填"); return; }
  if (!state.renConfirm) {
    state.renConfirm = true;
    state.renameResult = null;
    render();
    toast(`再点一次确认：${state.renOld} → ${state.renNew}`);
    return;
  }
  state.renConfirm = false;
  const r = await cloudPost("/api/cloud/tag/rename", {
    old_tag: state.renOld,
    new_tag: state.renNew,
    max_memos: state.renMax ? Number(state.renMax) : null,
  });
  state.renameResult = r.error ? { error: r.error } : r;
  state.cache = {};
  state.liveTags = null;
  render();
  toast(r.error ? "重命名未完成" : "重命名已下发，请按「拉取」现采标签树");
}

document.addEventListener("change", (e) => {
  if (e.target.id === "ed-skip-web") {
    syncVerifyOpt();
    return;
  }
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
  // 编辑器输入只刷新预览，不重渲染——否则每敲一个字光标都会跳回行首
  if (["ed-tags", "ed-concept", "ed-body", "ed-raw"].includes(e.target.id)) {
    updateEditorPreview();
    return;
  }
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
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (state.editor) { closeEditor(); return; }   // 编辑器在最上层，先关它
    closeMemo();
    return;
  }
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
