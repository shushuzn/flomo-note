/* flomo-note 控制台 · 前端逻辑
   只读展示：数据全部来自本地服务 /api/*，页面不发起写操作。
   所有插入文本一律先转义，避免仓库文档里的尖括号破坏结构。 */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const VIEW_META = {
  pipeline: { title: "九步管线", sub: "从抓取到写云的强制执行顺序，标「阻塞」的步骤未完成不得进入下一步" },
  limits:   { title: "硬限清单", sub: "SKILL.md 中编号即引用点的硬性规则（H1–Hn）" },
  scripts:  { title: "脚本与回归", sub: "工具脚本、配套回归用例，可按需在本机跑一遍离线回归" },
  tags:     { title: "标签树", sub: "本地快照的分组速览；计数与闸门同源（memo_util）" },
  docs:     { title: "文档", sub: "技能文档的规模速览" },
};

const state = { view: "pipeline", cache: {}, limitsOpen: new Set(), tagQuery: "" };

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
function renderStats(stats) {
  const cells = [
    { n: stats.tags,           l: "标签总数", alert: stats.tags_consistent === false },
    { n: stats.steps,          l: "管线步骤" },
    { n: stats.steps_blocking, l: "其中阻塞" },
    { n: stats.limits,         l: "硬限条目" },
    { n: stats.limits_core,    l: "写卡硬限" },
    { n: stats.tests,          l: "回归用例" },
  ];
  $("#stats").innerHTML = cells.map((c) => `
    <div class="stat${c.alert ? " is-alert" : ""}">
      <div class="stat-num">${esc(c.n ?? "—")}</div>
      <div class="stat-label">${esc(c.l)}</div>
    </div>`).join("");
}

/* ---------- 视图：管线 ---------- */
async function viewPipeline() {
  const steps = await get("pipeline");
  if (!steps.length) return `<div class="empty">未解析到管线步骤</div>`;
  return `<div class="card">${steps.map((s) => `
    <div class="step${s.blocking ? " is-blocking" : ""}">
      <div class="step-no">${esc(s.n)}</div>
      <div>
        <div class="step-title">
          ${esc(s.title)}
          ${s.blocking ? '<span class="badge badge-block">阻塞</span>' : ""}
        </div>
        <div class="step-body">${inline(s.body)}</div>
      </div>
    </div>`).join("")}</div>`;
}

/* ---------- 视图：硬限 ---------- */
async function viewLimits() {
  const limits = await get("limits");
  if (!limits.length) return `<div class="empty">未解析到硬限条目</div>`;

  let html = "", lastGroup = null;
  for (const l of limits) {
    if (l.group_name && l.group_name !== lastGroup) {
      lastGroup = l.group_name;
      html += `<div class="group-head">${esc(l.group_name)}</div>`;
    }
    const open = state.limitsOpen.has(l.id);
    const long = (l.body || "").length > 120;
    html += `
      <div class="limit">
        <div class="limit-head">
          <span class="limit-id">${esc(l.id)}</span>
          <span class="limit-title">${esc(l.title)}</span>
        </div>
        <div class="limit-body${open ? " is-open" : ""}">${inline(l.body || "（无正文）")}</div>
        ${long ? `<button class="limit-more" data-limit="${esc(l.id)}">${open ? "收起" : "展开全文"}</button>` : ""}
      </div>`;
  }
  return `<div class="card">${html}</div>`;
}

/* ---------- 视图：脚本 ---------- */
async function viewScripts() {
  const scripts = await get("scripts");
  const tools = scripts.filter((s) => s.kind === "tool");
  const tests = scripts.filter((s) => s.kind === "test");

  const row = (s) => `
    <div class="row">
      <div class="row-name">${esc(s.name)}</div>
      <div class="row-desc">${esc(s.summary || "—")}</div>
      <div class="row-tags">
        ${s.kind === "test"
          ? '<span class="badge">用例</span>'
          : s.has_test
            ? '<span class="badge badge-ok">测试 ✓</span>'
            : '<span class="badge badge-quiet">—</span>'}
      </div>
    </div>`;

  return `
    <div class="section-head"><h2>工具脚本</h2><span class="count">${tools.length} 个</span></div>
    <div class="card">${tools.map(row).join("")}</div>
    <div class="section-head"><h2>回归用例</h2><span class="count">${tests.length} 个</span></div>
    <div class="card">${tests.map(row).join("")}</div>
    <div class="section-head">
      <h2>一键回归</h2>
      <span class="count">离线执行 scripts/run_tests.sh</span>
    </div>
    <div class="card">
      <div class="row" style="grid-template-columns:1fr auto">
        <div class="row-desc">在本机跑一遍全部离线用例与文档纪律自检，不改动任何文件。</div>
        <button class="btn btn-primary" id="btn-run">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round"><path d="M7 4l12 8-12 8z"/></svg>
          运行回归
        </button>
      </div>
      <pre class="run-out" id="run-out" hidden></pre>
    </div>`;
}

/* ---------- 视图：标签树 ---------- */
async function viewTags() {
  const t = await get("tagtree");
  if (!t.present) return `<div class="empty">未找到标签树快照（tag_tree.txt）</div>`;

  const q = state.tagQuery.trim().toLowerCase();
  const groups = t.groups
    .map((g) => ({ ...g, children: g.children.filter((c) => !q || c.toLowerCase().includes(q)) }))
    .filter((g) => !q || g.name.toLowerCase().includes(q) || g.children.length);

  const head = `
    <div class="tagtool">
      <input class="input" id="tag-q" placeholder="搜索顶层或二级标签…" value="${esc(state.tagQuery)}">
      <span class="badge ${t.consistent ? "badge-ok" : "badge-err"}">
        列出 ${t.listed} / total ${t.total}${t.consistent ? " · 自洽" : " · 不一致"}
      </span>
      <span class="badge badge-quiet">${t.groups.length} 组 · ${t.bare_count} 裸顶层</span>
    </div>`;

  const body = groups.length
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
    : `<div class="empty">没有匹配的标签</div>`;

  return head + `<div class="card">${body}</div>`;
}

function hl(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return safe;
  return esc(text.slice(0, i)) + "<mark>" + esc(text.slice(i, i + q.length)) + "</mark>" + esc(text.slice(i + q.length));
}

/* ---------- 视图：文档 ---------- */
async function viewDocs() {
  const overview = await get("overview");
  const docs = overview.docs || [];
  if (!docs.length) return `<div class="empty">未找到技能文档</div>`;
  const kb = (n) => (n / 1024).toFixed(1) + " KB";
  return `<div class="card">${docs.map((d) => `
    <div class="doc-row">
      <span class="doc-name">${esc(d.name)}</span>
      <span class="doc-meta">${esc(d.lines)} 行 · ${esc(kb(d.bytes))}</span>
    </div>`).join("")}</div>`;
}

/* ---------- 渲染调度 ---------- */
const VIEWS = {
  pipeline: viewPipeline,
  limits:   viewLimits,
  scripts:  viewScripts,
  tags:     viewTags,
  docs:     viewDocs,
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
    render();
    return;
  }

  const more = e.target.closest(".limit-more");
  if (more) {
    const id = more.dataset.limit;
    state.limitsOpen.has(id) ? state.limitsOpen.delete(id) : state.limitsOpen.add(id);
    render();
    return;
  }

  const ghead = e.target.closest(".tgroup-head");
  if (ghead) {
    ghead.closest(".tgroup").classList.toggle("is-open");
    return;
  }

  if (e.target.closest("#btn-refresh")) {
    state.cache = {};
    render();
    toast("已重新读取仓库数据");
    return;
  }

  if (e.target.closest("#btn-run")) {
    runRegression();
  }
});

document.addEventListener("input", (e) => {
  if (e.target.id === "tag-q") {
    state.tagQuery = e.target.value;
    const pos = e.target.selectionStart;
    render().then(() => {
      const box = $("#tag-q");
      if (box) { box.focus(); box.setSelectionRange(pos, pos); }
      // 搜索态默认展开有命中的分组
      if (state.tagQuery) $$(".tgroup").forEach((g) => g.classList.add("is-open"));
    });
  }
});

async function runRegression() {
  const btn = $("#btn-run");
  const out = $("#run-out");
  btn.disabled = true;
  out.hidden = false;
  out.textContent = "正在运行离线回归…";
  try {
    const r = await api("/api/tests/run", { method: "POST" });
    const text = (r.error ? `[错误] ${r.error}\n\n` : "") + (r.lines || []).join("\n");
    out.textContent = text || "(无输出)";
    out.style.color = r.ok ? "#B7D2B9" : "#E8B4AC";
    toast(r.ok ? "回归全部通过" : "回归存在失败，详见输出");
  } catch (e) {
    out.textContent = `调用失败：${e.message}`;
    out.style.color = "#E8B4AC";
    toast("调用失败");
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 启动 ---------- */
(async function boot() {
  try {
    const overview = await get("overview");
    renderStats(overview.stats || {});
    $("#conn-badge").className = "badge badge-ok";
    $("#conn-text").textContent = "已连接本地服务";
  } catch {
    $("#conn-badge").className = "badge badge-err";
    $("#conn-text").textContent = "未连接";
    $$(".stat-num").forEach((n) => (n.textContent = "—"));
  }
  render();
})();
