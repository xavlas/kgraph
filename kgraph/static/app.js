"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const NS = "http://www.w3.org/2000/svg";
const NODE_W = 178, NODE_H = 24, GAP_Y = 7, GAP_X = 96;
const color = t => getComputedStyle(document.documentElement).getPropertyValue("--" + t).trim() || "#888";

let META = null;
let G = { nodes: [], edges: [], byId: new Map(), out: new Map(), inn: new Map() };
let view = { mode: "full", ids: null, edges: null, focus: null };
let lastResult = null, selected = null, hidden = new Set();
let tf = { x: 20, y: 40, k: 1 };
let nodeEls = new Map(), edgeEls = [];

/* ------------------------------------------------------------------ helpers */
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-KG-Request": "1" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { suggestions: j.suggestions || [] });
  return j;
}
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function md(src) {
  const inline = s => esc(s).replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*\s][^*]*)\*/g, "<em>$1</em>");
  const lines = String(src ?? "").split("\n");
  let html = "", list = false, code = null, table = null;
  const closeList = () => { if (list) { html += "</ul>"; list = false; } };
  const closeTable = () => {
    if (table) {
      const rows = table.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r));
      html += "<table>" + rows.map((r, i) => "<tr>" + r.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/)
        .map(c => `<${i ? "td" : "th"}>${inline(c.trim().replace(/\\\|/g, "|"))}</${i ? "td" : "th"}>`).join("") + "</tr>").join("") + "</table>";
      table = null;
    }
  };
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (code === null) { closeList(); closeTable(); code = []; } else { html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (/^\s*\|/.test(line)) { closeList(); (table ||= []).push(line); continue; }
    closeTable();
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)/))) { closeList(); html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; }
    else if ((m = line.match(/^\s*[-*]\s+(.*)/))) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(m[1])}</li>`; }
    else if (/^---+$/.test(line.trim())) { closeList(); html += "<hr>"; }
    else if (line.trim() === "") { closeList(); }
    else { closeList(); html += `<p>${inline(line)}</p>`; }
  }
  closeList(); closeTable();
  if (code !== null) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
  return html;
}

function tabs(navSel, paneRoot) {
  const left = navSel.startsWith("#left");
  $$(navSel + " button").forEach(b => b.addEventListener("click", () => {
    if (left && b.classList.contains("active") && !document.body.classList.contains("drawer-off")) { document.body.classList.add("drawer-off"); setTimeout(() => fitView(), 50); return; }
    selectTab(navSel, b.dataset.tab);
  }));
}
function selectTab(navSel, name) {
  if (navSel.startsWith("#left") && document.body.classList.contains("drawer-off")) { document.body.classList.remove("drawer-off"); setTimeout(() => fitView(), 50); }
  const root = navSel.startsWith("#left") ? $("#left") : $("#right");
  $$(navSel + " button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".pane", root).forEach(p => p.classList.toggle("active", p.dataset.pane === name));
}

/* -------------------------------------------------------------------- status */
async function pollStatus() {
  const chip = $("#status");
  for (;;) {
    let s;
    try { s = await api("/api/status"); } catch { await sleep(1500); continue; }
    chip.className = "chip " + s.state;
    updateRulesPanel(s);
    updateEngine(s);
    const dot = $("#bell-dot");
    dot.hidden = !(s.state === "running" || s.state === "error"); dot.className = s.state === "running" ? "run" : "";
    if (s.state === "running") {
      const p = s.progress;
      chip.textContent = p ? `analyzing rules · ${fmtElapsed(p)} · ${p.agents} agents · ${p.tool_calls} tool calls`
        : `${s.stage === "discover_business_rules" ? "analyzing rules" : "scanning"}: ${s.stage || "…"}`;
      chip.title = (s.log || []).slice(-6).join("\n");
      $("#scan-btn").disabled = true;
      await sleep(1000);
      continue;
    }
    $("#scan-btn").disabled = false;
    if (s.state === "error") { chip.textContent = "error: " + s.error; chip.title = s.error; return s; }
    if (s.has_graph) {
      const m = s.meta || {};
      chip.textContent = `${m.repository || "repository"}${m.commit ? " @" + m.commit : ""}`;
      $("#page-title").textContent = m.repository || "Knowledge graph";
      const rs0 = m.rules_source || {};
      $("#side-repo").innerHTML = "";
      $("#side-repo").append(el("div", "side-title", m.repository || "repository"),
        el("div", "side-sub", [m.commit ? "@" + m.commit : "", `rules: ${rs0.engine || "n/a"}`].filter(Boolean).join(" · ")));
      const rs = m.rules_source || {};
      chip.title = `Business rules: ${rs.engine || "?"}` + (rs.command ? `\n${rs.command}\nrun ${rs.started_at} (${rs.duration_s}s), ${rs.imported}/${rs.cards} cards\nproof: ${rs.rules_file}` : "")
        + "\n\n" + (s.log || []).join("\n");
    }
    $("#use-llm").disabled = !s.llm_available;
    return s;
  }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

const PHASES = { starting: "Starting Claude Code…", running: "Extracting business rules…", finishing: "Finishing…",
  importing: "Importing the rules into the graph…", done: "Done" };
const fmtElapsed = p => { const t = Math.max(0, Math.floor(Date.now() / 1000 - p.started)); const m = Math.floor(t / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`; };

function renderProgress(s) {
  const p = s.progress, box = $("#progress");
  box.style.display = p ? "" : "none";
  if (!p) return;
  const running = s.state === "running";
  $("#p-phase").textContent = (PHASES[p.phase] || p.phase) + (p.errors ? `  ·  ${p.errors} tool error(s)` : "") + (p.cost ? `  ·  $${p.cost.toFixed(2)}` : "");
  $("#p-elapsed").textContent = running ? fmtElapsed(p) : "—";
  $("#p-agents").textContent = p.agents;
  $("#p-agents-l").textContent = p.expected_agents ? `agents / ~${p.expected_agents}` : "agents";
  $("#p-tools").textContent = p.tool_calls;
  $("#p-shards").textContent = p.shards ?? "–";
  const bar = $("#p-bar");
  if (p.phase === "done") { bar.className = ""; bar.style.width = "100%"; }
  else if (p.expected_agents && p.agents) {                     // indicative only: the plugin runs ~10-15 agents per module
    bar.className = ""; bar.style.width = Math.min(95, Math.round(100 * p.agents / p.expected_agents)) + "%";
  } else { bar.className = "indeterminate"; bar.style.width = ""; }
  $("#p-last").textContent = p.last ? "Last message: " + p.last : "";
  $("#p-files").textContent = p.files?.length ? "Files produced: " + p.files.join(", ") : "";
}

function updateEngine(s) {
  const chip = $("#engine-chip"), e = s.engine || {}, n = e.neo4j;
  let text = "In memory", cls = "", title = "Queries are answered by the in-memory graph. Set NEO4J_PASSWORD to use Neo4j (./launch.sh full).";
  if (n && n.state === "syncing") { text = "Neo4j · syncing…"; cls = "running"; title = "Loading the graph into Neo4j; queries are answered in memory meanwhile."; }
  else if (n && n.state === "ready" && e.active === "neo4j") { text = "Neo4j"; cls = "done"; title = `Queries run in Neo4j (${n.uri}, repo ${n.repo}, synced ${n.synced_at || "?"}).`; }
  else if (n && n.state === "error") { text = "Neo4j · unavailable"; cls = "error"; title = `Neo4j error: ${n.error}\nQueries are answered in memory.`; }
  else if (n && n.state === "pending") { text = "Neo4j · waiting for a scan"; title = "Neo4j is configured; the graph is loaded after the first scan."; }
  chip.textContent = text; chip.className = "chip " + cls; chip.title = title;
}

function updateRulesPanel(s) {
  renderProgress(s);
  const busy = s.state === "running";
  const loggedOut = s.claude_logged_in === false, runnable = s.modernization_ready && !loggedOut;
  for (const id of ["#analyze-btn", "#analyze-btn2"]) $(id).disabled = busy || !s.has_graph || !(runnable || s.modernization_previous);
  const outdated = s.modernization_ready === undefined;   // server started before this feature existed
  $("#analyze-btn").title = outdated ? "This server runs an older version: stop it and restart it (./run.sh)"
    : !s.has_graph ? "Scan a repository first" : loggedOut && !s.modernization_previous
    ? "The claude CLI is logged out: run  claude auth login  in a terminal"
    : !s.modernization_ready && !s.modernization_previous
    ? "Plugin not installed: claude plugin install code-modernization@claude-plugins-official"
    : "Run the Claude Code code-modernization plugin to extract the business rules";
  $("#plugin-hint").style.display = s.modernization_ready && !loggedOut ? "none" : "";
  $("#plugin-hint").innerHTML = loggedOut && s.modernization_ready
    ? "The <code>claude</code> CLI is not logged in (OAuth session expired). Run once in a terminal:<br><code>claude auth login</code>"
    : "Plugin not detected. Install it once in a terminal:<br><code>claude plugin install code-modernization@claude-plugins-official</code>";
  $("#reuse-rules").disabled = busy || !s.modernization_previous;
  if (!s.modernization_previous) $("#reuse-rules").checked = false;
  $("#cancel-btn").style.display = s.cancellable ? "" : "none";
  const rs = (s.meta || {}).rules_source || {};
  const lines = busy ? ((s.progress?.events?.length ? s.progress.events : s.log) || []) : s.state === "error" ? [`ERROR: ${s.error}`, ...(s.log || []).slice(-6)]
    : [`rules source: ${rs.engine || "n/a"}`, ...(rs.command ? [rs.command, `run ${rs.started_at} · ${rs.duration_s}s · ${rs.imported}/${rs.cards} cards imported`,
        `proof: ${rs.rules_file}`, `transcript: ${rs.transcript}`] : []),
        ...(rs.unverified ? [`⚠ ${rs.unverified} of ${rs.cards} card(s) cite a source that was not found in the repository (kept in the graph, flagged "source not located")`] : [])];
  $("#activity").textContent = lines.join("\n") || "idle";
  $("#activity").scrollTop = $("#activity").scrollHeight;
}

async function analyzeRules() {
  const reuse = $("#reuse-rules").checked, pattern = $("#module-pattern").value.trim();
  if (!reuse && !confirm("The code-modernization plugin starts MANY Claude agents (≈10-15 per module, ~16k tokens each; minutes to hours) and sends the repository source code to Anthropic.\n\n"
      + (pattern ? `Modules: ${pattern}\n\n` : "No module filter: the WHOLE repository will be analysed (tip: start with one module).\n\n")
      + "The graph's business rules will be replaced by the plugin's (no heuristics).\n\nContinue?")) return;
  try {
    selectTab("#left-tabs", "rules");
    await api("/api/rules/analyze", { module_pattern: pattern, reuse });
    const s = await pollStatus();
    if (s.state === "done") { await loadGraph(); showRulesInGraph(); }      // show the freshly extracted rules right away
  } catch (ex) { $("#status").className = "chip error"; $("#status").textContent = "error: " + ex.message; $("#activity").textContent = "ERROR: " + ex.message; }
}

async function loadGraph() {
  const g = await api("/api/graph");
  G.nodes = g.nodes; G.byId = new Map(g.nodes.map(n => [n.id, n]));
  G.edges = g.edges.map(e => ({ src: e.src, dst: e.dst, rel: e.rel, derived: !!(e.props || {}).derived, props: e.props || {} }));
  G.out = new Map(); G.inn = new Map();
  for (const e of G.edges) {
    (G.out.get(e.src) || G.out.set(e.src, []).get(e.src)).push(e);
    (G.inn.get(e.dst) || G.inn.set(e.dst, []).get(e.dst)).push(e);
  }
  $("#empty").style.display = G.nodes.length ? "none" : "flex";
  view = { mode: "full", ids: null, edges: null, focus: null };
  lastResult = null; selected = null;
  buildTypeFilters(g.stats.by_type);
  fillDatalists();
  renderKpis();
  refresh(true);
  initRuleList();
}

/* ------------------------------------------------------------------- layout */
function layout(nodes, edges) {
  const L = META.layers;
  const types = [...new Set(nodes.map(n => n.type))].sort((a, b) => L[a] - L[b]);
  const colIdx = Object.fromEntries(types.map((t, i) => [t, i]));
  const cols = types.map(t => nodes.filter(n => n.type === t).sort((a, b) => a.name.localeCompare(b.name)));
  const yIdx = new Map();
  cols.forEach(c => c.forEach((n, i) => yIdx.set(n.id, i)));
  const adj = new Map(nodes.map(n => [n.id, []]));
  for (const e of edges) { adj.get(e.src)?.push(e.dst); adj.get(e.dst)?.push(e.src); }
  const bary = (n, pred) => {
    const ys = adj.get(n.id).filter(pred).map(i => yIdx.get(i));
    return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : yIdx.get(n.id);
  };
  const colOf = id => colIdx[G.byId.get(id).type];
  for (let pass = 0; pass < 3; pass++) {
    for (let ci = 1; ci < cols.length; ci++) { cols[ci].sort((a, b) => bary(a, i => colOf(i) < ci) - bary(b, i => colOf(i) < ci)); cols[ci].forEach((n, i) => yIdx.set(n.id, i)); }
    for (let ci = cols.length - 2; ci >= 0; ci--) { cols[ci].sort((a, b) => bary(a, i => colOf(i) > ci) - bary(b, i => colOf(i) > ci)); cols[ci].forEach((n, i) => yIdx.set(n.id, i)); }
  }
  const pos = new Map();
  cols.forEach((c, ci) => c.forEach((n, i) => pos.set(n.id, { x: ci * (NODE_W + GAP_X), y: 26 + i * (NODE_H + GAP_Y), ci })));
  return { pos, types, colIdx, width: cols.length * (NODE_W + GAP_X), height: 26 + Math.max(1, ...cols.map(c => c.length)) * (NODE_H + GAP_Y) };
}

function visible() {
  const showDerived = $("#show-derived").checked;
  let nodes, edges;
  if (view.mode === "result" && view.ids) {
    nodes = [...view.ids].map(id => G.byId.get(id)).filter(n => n && !hidden.has(n.type));
    const ids = new Set(nodes.map(n => n.id));
    edges = G.edges.filter(e => ids.has(e.src) && ids.has(e.dst) && (showDerived || !e.derived));
    if (view.edges) {
      const allowed = new Set(view.edges.map(([s, d, r]) => s + "|" + d + "|" + r));
      edges = edges.filter(e => allowed.has(e.src + "|" + e.dst + "|" + e.rel));
    }
  } else {
    nodes = G.nodes.filter(n => !hidden.has(n.type));
    const ids = new Set(nodes.map(n => n.id));
    edges = G.edges.filter(e => ids.has(e.src) && ids.has(e.dst) && (showDerived || !e.derived));
  }
  return { nodes, edges };
}

/* ------------------------------------------------------------------- render */
function refresh(fit = false) {
  const svg = $("#graph");
  svg.replaceChildren();
  nodeEls = new Map(); edgeEls = [];
  const { nodes, edges } = visible();
  if (netActive()) netRebuild(nodes, edges);                       // the Network tab follows the current view and filters
  $("#view-info").textContent = nodes.length ? `${nodes.length} nodes · ${edges.length} links · ${view.mode === "result" ? "result view" : "whole graph"}` : "";
  if (!nodes.length) return;
  const lay = layout(nodes, edges);
  const vp = document.createElementNS(NS, "g"); vp.setAttribute("id", "vp"); svg.appendChild(vp);
  for (const t of lay.types) {
    const h = document.createElementNS(NS, "text");
    h.setAttribute("class", "colhead"); h.setAttribute("x", lay.colIdx[t] * (NODE_W + GAP_X)); h.setAttribute("y", 14); h.textContent = t;
    vp.appendChild(h);
  }
  const eg = document.createElementNS(NS, "g"); vp.appendChild(eg);
  for (const e of edges) {
    const a = lay.pos.get(e.src), b = lay.pos.get(e.dst);
    const fwd = a.ci < b.ci, same = a.ci === b.ci;
    const x1 = fwd || same ? a.x + NODE_W : a.x, x2 = fwd ? b.x : same ? b.x + NODE_W : b.x + NODE_W;
    const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
    const dx = same ? 60 : Math.max(30, Math.abs(x2 - x1) / 2) * (fwd ? 1 : -1);
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${x2 - (same ? -60 : dx)},${y2} ${x2},${y2}`);
    p.setAttribute("class", "edge" + (e.derived ? " derived" : ""));
    const ti = document.createElementNS(NS, "title"); ti.textContent = `${G.byId.get(e.src).name} ─${e.rel}→ ${G.byId.get(e.dst).name}`; p.appendChild(ti);
    eg.appendChild(p); edgeEls.push({ el: p, e });
  }
  const ng = document.createElementNS(NS, "g"); vp.appendChild(ng);
  for (const n of nodes) {
    const p = lay.pos.get(n.id);
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node"); g.setAttribute("transform", `translate(${p.x},${p.y})`);
    const r = document.createElementNS(NS, "rect");
    r.setAttribute("width", NODE_W); r.setAttribute("height", NODE_H); r.setAttribute("stroke", color(n.type));
    const bar = document.createElementNS(NS, "rect");
    bar.setAttribute("width", 5); bar.setAttribute("height", NODE_H); bar.setAttribute("fill", color(n.type)); bar.setAttribute("rx", 2);
    const t = document.createElementNS(NS, "text");
    const isRule = n.type === "BusinessRule", prio = isRule ? n.props.priority : null, warn = isRule && n.props.suspected_defect;
    const maxLen = isRule && (prio || warn) ? 21 : 27;
    t.setAttribute("x", 11); t.setAttribute("y", 16); t.textContent = n.name.length > maxLen ? n.name.slice(0, maxLen - 1) + "…" : n.name;
    const ti = document.createElementNS(NS, "title");
    ti.textContent = `${n.type}: ${n.name}` + (isRule && n.props.business_description ? `\n${n.props.business_description}` : "") + (warn ? "\n⚠ suspected defect" : "");
    g.append(r, bar, t, ti);
    if (prio || warn) {                                   // priority + defect markers on rule nodes
      const tag = document.createElementNS(NS, "text");
      tag.setAttribute("x", NODE_W - 6); tag.setAttribute("y", 16); tag.setAttribute("text-anchor", "end");
      tag.setAttribute("style", `font-weight:700;font-size:10px;fill:${prio === "P0" ? "#dc2626" : prio === "P1" ? "#ea580c" : "#64748b"}`);
      tag.textContent = (warn ? "⚠ " : "") + (prio || "");
      g.appendChild(tag);
    }
    g.addEventListener("click", ev => { ev.stopPropagation(); selectNode(n.id); });
    g.addEventListener("mouseenter", () => hover(n.id));
    g.addEventListener("mouseleave", () => hover(null));
    ng.appendChild(g); nodeEls.set(n.id, g);
  }
  applyHighlight();
  if (fit) fitView(lay); else applyTransform();
  svg.__layout = lay;
}

function applyHighlight() {
  const inResult = view.mode === "full" && lastResult ? new Set(lastResult.graph.nodes) : null;
  for (const [id, el] of nodeEls) {
    el.classList.toggle("dim", !!inResult && !inResult.has(id));
    el.classList.toggle("sel", id === selected);
    el.classList.toggle("focus", id === (view.focus || lastResult?.graph?.focus));
  }
  for (const { el, e } of edgeEls) el.classList.toggle("dim", !!inResult && !(inResult.has(e.src) && inResult.has(e.dst)));
}

function hover(id) {
  if (id === null) { edgeEls.forEach(({ el }) => el.classList.remove("hl")); return; }
  edgeEls.forEach(({ el, e }) => el.classList.toggle("hl", e.src === id || e.dst === id));
}

function applyTransform() { const vp = $("#vp"); if (vp) vp.setAttribute("transform", `translate(${tf.x},${tf.y}) scale(${tf.k})`); }
function fitView(lay) {
  lay = lay || $("#graph").__layout; if (!lay) return;
  const r = $("#graph").getBoundingClientRect();
  tf.k = Math.max(0.5, Math.min((r.width - 40) / lay.width, (r.height - 60) / lay.height, 1.3));   // wide graphs overflow: pan
  tf.x = 20; tf.y = 54; applyTransform();
}
function centerOn(id) {
  const lay = $("#graph").__layout, p = lay?.pos.get(id); if (!p) return;
  const r = $("#graph").getBoundingClientRect();
  tf.x = r.width / 2 - (p.x + NODE_W / 2) * tf.k; tf.y = r.height / 2 - p.y * tf.k; applyTransform();
}

function initPanZoom() {
  const svg = $("#graph"); let drag = null;
  svg.addEventListener("mousedown", e => { drag = { x: e.clientX, y: e.clientY, tx: tf.x, ty: tf.y }; svg.classList.add("panning"); });
  window.addEventListener("mousemove", e => { if (drag) { tf.x = drag.tx + e.clientX - drag.x; tf.y = drag.ty + e.clientY - drag.y; applyTransform(); } });
  window.addEventListener("mouseup", () => { drag = null; svg.classList.remove("panning"); });
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k = Math.min(3, Math.max(0.1, tf.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    tf.x = mx - (mx - tf.x) * (k / tf.k); tf.y = my - (my - tf.y) * (k / tf.k); tf.k = k; applyTransform();
  }, { passive: false });
  svg.addEventListener("click", () => { selected = null; applyHighlight(); });
}

/* ----------------------------------------------------------------- node panel */
const ACTIONS = {
  Screen: ["explain_screen", "show_call_chain", "find_business_rules", "find_data_lineage", "determine_impact"],
  Route: ["explain_screen", "determine_impact"], Component: ["show_call_chain", "determine_impact"],
  API: ["show_call_chain", "find_business_rules", "find_data_lineage", "determine_impact"],
  Controller: ["show_call_chain", "find_business_rules", "determine_impact"],
  Service: ["show_call_chain", "find_business_rules", "determine_impact"],
  Repository: ["show_call_chain", "determine_impact"], Entity: ["find_data_lineage", "find_business_rules", "determine_impact"],
  Table: ["find_data_lineage", "find_business_rules", "determine_impact"], Topic: ["show_call_chain", "determine_impact"],
  BusinessRule: ["determine_impact"], Capability: ["show_capability", "find_business_rules"],
};
const LABEL = { explain_screen: "Explain", show_call_chain: "Call chain", find_business_rules: "Rules", find_data_lineage: "Lineage",
  determine_impact: "Impact", show_capability: "Capability" };
const PARAM = { explain_screen: "name", show_call_chain: "source", find_business_rules: "scope", find_data_lineage: "target",
  determine_impact: "name", show_capability: "name" };

function selectNode(id) {
  selected = id; applyHighlight();
  if (netActive()) netDraw();
  const n = G.byId.get(id); if (!n) return;
  const root = $("#node"); root.replaceChildren();
  const h = document.createElement("h2");
  const b = document.createElement("span"); b.className = "badge"; b.style.background = color(n.type); b.textContent = n.type;
  h.append(b, " " + n.name); root.appendChild(h);
  const acts = document.createElement("div"); acts.className = "actions";
  for (const a of ACTIONS[n.type] || ["determine_impact"]) {
    const btn = document.createElement("button"); btn.textContent = LABEL[a];
    btn.addEventListener("click", () => runQuery(a, { [PARAM[a]]: n.id }));
    acts.appendChild(btn);
  }
  root.appendChild(acts);
  const kv = document.createElement("div"); kv.className = "kv";
  for (const [k, v] of Object.entries(n.props)) {
    if (v === null || v === "" || (Array.isArray(v) && !v.length)) continue;
    const val = typeof v === "object" ? (Array.isArray(v) && v.length && typeof v[0] !== "object" ? v.join(", ") : `${Array.isArray(v) ? v.length + " items" : JSON.stringify(v)}`) : String(v);
    const a = document.createElement("div"), c = document.createElement("div"); a.textContent = k; c.textContent = val.length > 400 ? val.slice(0, 400) + "…" : val; kv.append(a, c);
  }
  root.appendChild(kv);
  for (const [title, list, other] of [["Outgoing", G.out.get(id) || [], "dst"], ["Incoming", G.inn.get(id) || [], "src"]]) {
    if (!list.length) continue;
    const h3 = document.createElement("h3"); h3.textContent = `${title} (${list.length})`; root.appendChild(h3);
    const box = document.createElement("div"); box.className = "edgelist";
    for (const e of list.slice(0, 80)) {
      const d = document.createElement("div"), o = G.byId.get(e[other]);
      d.textContent = `${e.rel}${e.derived ? " (exact)" : ""} ${other === "dst" ? "→" : "←"} ${o.type}: ${o.name}`;
      d.addEventListener("click", () => { selectNode(o.id); centerOn(o.id); });
      box.appendChild(d);
    }
    root.appendChild(box);
  }
  selectTab("#right-tabs", "node");
}

/* ------------------------------------------------------------------- results */
function showResult(res) {
  lastResult = res;
  const root = $("#result"); root.replaceChildren();
  const box = document.createElement("div");
  const eng = res.engine ? `<p class="muted engine-line">Answered by <b>${res.engine === "neo4j" ? "Neo4j" : "the in-memory engine"}</b>` +
    (res.elapsed_ms != null ? ` in ${res.elapsed_ms} ms` : "") + (res.engine_note ? ` — <span class="warn">${esc(res.engine_note)}</span>` : "") + "</p>" : "";
  box.innerHTML = `<h2>${esc(res.title || "Result")}</h2>` + eng + md(res.summary);
  root.appendChild(box);
  if (res.data?.mermaid) {
    const b = document.createElement("button"); b.textContent = "Copy Mermaid diagram";
    b.addEventListener("click", () => navigator.clipboard?.writeText(res.data.mermaid));
    root.appendChild(b);
  }
  const det = document.createElement("details"); const sm = document.createElement("summary"); sm.textContent = "Raw data (JSON)";
  const pre = document.createElement("pre"); pre.textContent = JSON.stringify(res.data, null, 1).slice(0, 60000);
  det.append(sm, pre); root.appendChild(det);
  selectTab("#right-tabs", "result");
  if (res.graph?.nodes?.length) { view = { mode: "result", ids: new Set(res.graph.nodes), edges: res.graph.edges, focus: res.graph.focus }; refresh(true); }
}
function showError(ex) {
  lastResult = null;
  const root = $("#result"); root.replaceChildren();
  root.innerHTML = `<h2>Not found</h2><p>${esc(ex.message)}</p>`;
  if (ex.suggestions?.length) {
    const ul = document.createElement("ul"); ul.className = "links";
    for (const s of ex.suggestions) { const li = document.createElement("li"); li.textContent = s; ul.appendChild(li); }
    root.appendChild(ul);
  }
  selectTab("#right-tabs", "result");
}
async function runQuery(query, params) {
  try { showResult(await api("/api/query", { query, params })); } catch (ex) { showError(ex); }
}
async function ask(q) {
  if (!q.trim()) return;
  try {
    const r = await api("/api/ask", { question: q });
    r.error ? (lastResult = null, $("#result").innerHTML = `<h2>${esc(r.title)}</h2>` + md(r.summary), selectTab("#right-tabs", "result")) : showResult(r);
  } catch (ex) { showError(ex); }
}

/* --------------------------------------------------------------- left panels */
const QDEF = {
  explain_screen: [["name", "Screen or route", "dl-screens"]],
  show_call_chain: [["source", "Source (screen, API, service…)", "dl-any"], ["target", "Target (optional)", "dl-any"]],
  find_business_rules: [["scope", "Scope: screen, API, service, table… (optional)", "dl-any"], ["text", "Text filter (optional)"],
    ["kind", "Kind (optional)", null, ["", "declared", "guard", "existence", "validation", "authorization", "lifecycle", "decision", "calculation", "workflow", "policy", "audit", "integration", "data", "other", "db-check", "db-unique", "db-trigger"]]],
  determine_impact: [["name", "Element you plan to change", "dl-any"], ["direction", "Direction", null, ["upstream", "downstream"]]],
  show_capability: [["name", "Capability (empty = list all)", "dl-caps"]],
  find_data_lineage: [["target", "Table, entity, screen or API", "dl-any"], ["column", "Column (optional)"]],
};
function previewOntology() {
  const v = n => ($(`#q-fields [data-param=${n}]`) || {}).value || "", rel = v("relation"), dir = v("direction") || "out", st = v("source_type"), tt = v("target_type"), links = v("links") || "all";
  let all = 0, exact = 0;
  for (const e of G.edges) {
    if (rel && e.rel !== rel) continue;
    const a = G.byId.get(e.src).type, b = G.byId.get(e.dst).type;
    const fwd = (!st || a === st) && (!tt || b === tt), back = (!st || b === st) && (!tt || a === tt);
    if (!((dir === "out" && fwd) || (dir === "in" && back) || (dir === "both" && (fwd || back)))) continue;
    if (links === "structural" && e.derived || links === "exact" && !e.derived) continue;
    all++; if (e.derived) exact++;
  }
  const box = $("#q-preview"); if (!box) return;
  box.textContent = all ? `${all} link(s) of this shape in the graph (${exact} exact). Depth > 1 follows the relation further.`
    : "No link of this shape in the current graph: try another relation, direction or type.";
}

function buildQueryForm() {
  QDEF.explore_ontology = [
    ["source_type", "Start type", null, ["", ...META.node_types]], ["relation", "Relation", null, ["", ...META.relationships]],
    ["direction", "Direction, as written in the ontology (out: A –REL→ B · in: A ←REL– B)", null, ["out", "in", "both"]],
    ["target_type", "Target type", null, ["", ...META.node_types]], ["source_text", "Start name contains (optional)"],
    ["target_text", "Target name contains (optional)"], ["depth", "Depth (hops along the relation)", null, ["1", "2", "3", "4"]],
    ["links", "Links", null, ["all", "structural", "exact"]],
  ];
  const sel = $("#q-name");
  sel.replaceChildren(...Object.keys(QDEF).map(k => Object.assign(document.createElement("option"), { value: k, textContent: k })));
  const draw = () => {
    const box = $("#q-fields"); box.replaceChildren();
    for (const [name, label, dl, opts] of QDEF[sel.value]) {
      const l = document.createElement("label"); l.className = "lbl"; l.textContent = label; box.appendChild(l);
      let el;
      if (opts) { el = document.createElement("select"); opts.forEach(o => el.appendChild(Object.assign(document.createElement("option"), { value: o, textContent: o || "(any)" }))); }
      else { el = document.createElement("input"); if (dl) el.setAttribute("list", dl); }
      el.dataset.param = name; box.appendChild(el);
    }
    if (sel.value === "explore_ontology") {
      const p = document.createElement("div"); p.id = "q-preview"; p.className = "hint"; box.appendChild(p);
      box.onchange = previewOntology; previewOntology();
    } else box.onchange = null;
  };
  sel.addEventListener("change", draw); draw();
  $("#q-run").addEventListener("click", () => {
    const params = {}; $$("#q-fields [data-param]").forEach(el => { if (el.value) params[el.dataset.param] = el.value; });
    runQuery(sel.value, params);
  });
}
function fillDatalists() {
  const fill = (id, nodes) => $("#" + id).replaceChildren(...[...new Set(nodes.map(n => n.name))].slice(0, 4000).map(v => Object.assign(document.createElement("option"), { value: v })));
  fill("dl-screens", G.nodes.filter(n => n.type === "Screen" || n.type === "Route"));
  fill("dl-any", G.nodes.filter(n => n.type !== "BusinessRule"));
  fill("dl-caps", G.nodes.filter(n => n.type === "Capability"));
}
function buildTypeFilters(counts) {
  const box = $("#type-filters"); box.replaceChildren(); hidden = new Set();
  for (const t of META.node_types) {
    const lab = document.createElement("label"); lab.className = "tf";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = true;
    cb.addEventListener("change", () => { cb.checked ? hidden.delete(t) : hidden.add(t); refresh(); });
    const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = color(t);
    const sm = document.createElement("small"); sm.textContent = counts[t] || 0;
    lab.append(cb, dot, document.createTextNode(t), sm); box.appendChild(lab);
  }
}
function initSearch() {
  const input = $("#search"), ul = $("#search-results");
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase(); ul.replaceChildren();
    if (q.length < 2) return;
    G.nodes.filter(n => n.name.toLowerCase().includes(q)).slice(0, 30).forEach(n => {
      const li = document.createElement("li"); li.textContent = `${n.type} · ${n.name}`;
      li.addEventListener("click", () => {
        if (view.mode === "result" && !view.ids.has(n.id)) { view = { mode: "full", ids: null, edges: null, focus: null }; refresh(true); }
        selectNode(n.id); centerOn(n.id);
      });
      ul.appendChild(li);
    });
  });
}
function initDocs() {
  $("#docs-gen").addEventListener("click", async () => {
    try {
      const { files } = await api("/api/docs"); const ul = $("#docs-list"); ul.replaceChildren();
      Object.keys(files).sort().forEach(name => {
        const li = document.createElement("li"); li.textContent = name;
        li.addEventListener("click", () => {
          lastResult = null; $("#result").innerHTML = `<h2>${esc(name)}</h2>` + md(files[name]); selectTab("#right-tabs", "result");
        });
        ul.appendChild(li);
      });
    } catch (ex) { showError(ex); }
  });
}
const EXAMPLES = [
  "Which services write to orders?",
  "Explain the screen OrderCreateComponent",
  "What is the impact if I change the table customers?",
  "Which screens write to orders.status?",
  "Show the call chain from /orders/new to the database",
  "What business rules apply to POST /api/orders?",
  "Show capability Orders",
  "Quelles règles métier s'appliquent à l'écran de création de commande ?",
];

/* ----------------------------------------------------------------- rules: list + graph */
const ruleNodes = () => G.nodes.filter(n => n.type === "BusinessRule");
const PRIO_ORDER = { P0: 0, P1: 1, P2: 2 };
let ruleSort = { key: "id", dir: 1 }, openRules = new Set();

function ruleInfo(n) {
  const p = n.props, uniq = a => [...new Set(a)];
  const inc = (G.inn.get(n.id) || []).filter(e => e.rel === "EXECUTES");
  const enforcers = uniq(inc.filter(e => !e.derived && G.byId.get(e.src).type !== "BusinessRule").map(e => G.byId.get(e.src).name));
  const apis = uniq(inc.filter(e => G.byId.get(e.src).type === "API").map(e => e.src));
  const screens = uniq(apis.flatMap(a => (G.inn.get(a) || []).filter(e => e.rel === "CALLS" && G.byId.get(e.src).type === "Screen").map(e => G.byId.get(e.src).name)));
  const impacts = uniq((G.out.get(n.id) || []).filter(e => e.rel === "IMPACTS").map(e => G.byId.get(e.dst).name));
  const id = p.rule_id || n.name;
  return { n, p, id, title: p.description || n.name, enforcers, apis: apis.map(a => G.byId.get(a).name), screens, impacts,
    where: p.owner ? `${p.owner}${p.method ? "." + p.method : ""}` : "", loc: p.file ? `${p.file}:${p.line ?? ""}` : "(not located)",
    defect: !!p.suspected_defect, sme: !!p.sme_question, prio: p.priority || "" };
}

function initRuleList() {
  const rules = ruleNodes();
  $("#rules-count").textContent = rules.length;
  const kinds = [...new Set(rules.map(r => r.props.kind).filter(Boolean))].sort();
  const sel = $("#rl-kind"), cur = sel.value;
  sel.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "All kinds" }),
    ...kinds.map(k => Object.assign(document.createElement("option"), { value: k, textContent: k })));
  sel.value = kinds.includes(cur) ? cur : "";
  renderRuleList();
}

function filteredRules() {
  const q = $("#rl-search").value.trim().toLowerCase(), kind = $("#rl-kind").value, prio = $("#rl-prio").value;
  const defect = $("#rl-defect").checked, sme = $("#rl-sme").checked;
  const rows = ruleNodes().map(ruleInfo).filter(r =>
    (!kind || r.p.kind === kind) && (!prio || r.prio === prio) && (!defect || r.defect) && (!sme || r.sme) &&
    (!q || [r.id, r.title, r.p.description, r.p.specification, r.where, r.loc, r.impacts.join(" "), r.screens.join(" "), r.p.suspected_defect, r.p.parameters]
      .join(" ").toLowerCase().includes(q)));
  const key = ruleSort.key, dir = ruleSort.dir;
  const val = r => key === "prio" ? (PRIO_ORDER[r.prio] ?? 9) : key === "conf" ? -(r.p.confidence || 0) : key === "kind" ? r.p.kind || "" : key === "where" ? r.where : r.id;
  return rows.sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : a.id.localeCompare(b.id)) * dir);
}

function cell(tr, content, cls) {
  const td = document.createElement("td"); if (cls) td.className = cls;
  if (content instanceof Node) td.appendChild(content); else td.textContent = content ?? "";
  tr.appendChild(td); return td;
}
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

function renderRuleList() {
  const table = $("#rl-table"), rows = filteredRules();
  $("#rl-count").textContent = `${rows.length} / ${ruleNodes().length} rules`;
  table.replaceChildren();
  const head = table.insertRow();
  for (const [label, key] of [["Rule", "id"], ["Kind", "kind"], ["Prio", "prio"], ["Conf.", "conf"], ["Where", "where"], ["Triggered from screens", null], ["Impacts", null], ["Flags", null]]) {
    const th = document.createElement("th"); th.textContent = label + (key && ruleSort.key === key ? (ruleSort.dir > 0 ? " ▲" : " ▼") : "");
    if (key) th.addEventListener("click", () => { ruleSort = { key, dir: ruleSort.key === key ? -ruleSort.dir : 1 }; renderRuleList(); });
    head.appendChild(th);
  }
  if (!rows.length) { const tr = table.insertRow(); const td = cell(tr, ruleNodes().length ? "No rule matches these filters." : "No business rule in the graph yet. Use “Analyze business rules” (Rules tab) or scan with the built-in heuristics.", "muted"); td.colSpan = 8; return; }
  for (const r of rows) {
    const tr = table.insertRow(); tr.className = "rule";
    const t = el("div"); t.append(el("b", "", r.id === r.n.name ? "" : r.id + "  "), document.createTextNode(r.title));
    if (r.p.business_description && r.p.business_description !== r.title) {
      const s = r.p.business_description.replace(/[*`]/g, "");
      t.appendChild(el("div", "sub", s.length > 170 ? s.slice(0, 169) + "…" : s));
    }
    cell(tr, t);
    cell(tr, r.p.kind); cell(tr, r.prio ? el("span", "tag " + r.prio, r.prio) : "");
    cell(tr, r.p.confidence != null ? Math.round(r.p.confidence * 100) + "%" : "");
    const w = el("div"); w.append(document.createTextNode(r.where || "—"), el("div", "sub", r.loc.split("/").slice(-1)[0])); w.title = r.loc; cell(tr, w);
    cell(tr, r.screens.join(", ") || (r.apis.length ? "(API only)" : "—")); cell(tr, r.impacts.join(", "));
    const fl = el("div"); if (r.defect) fl.appendChild(el("div", "warn", "⚠ suspected defect")); if (r.sme) fl.appendChild(el("div", "", "SME question"));
    if (r.p.source === "llm") fl.appendChild(el("div", "", "Claude")); if (r.p.source_verified === false) fl.appendChild(el("div", "warn", "source not located"));
    cell(tr, fl);
    tr.addEventListener("click", () => { openRules.has(r.n.id) ? openRules.delete(r.n.id) : openRules.add(r.n.id); renderRuleList(); });
    if (openRules.has(r.n.id)) {
      const dr = table.insertRow(); dr.className = "detail"; const td = cell(dr, ""); td.colSpan = 8;
      const add = (title, text, pre) => {
        if (!text) return; td.appendChild(el("h4", "", title));
        if (pre) { td.appendChild(el("pre", "", text)); return; }
        const d = el("div", "md"); d.innerHTML = md(text); td.appendChild(d);     // md() escapes HTML first
      };
      add("Business wording", r.p.business_description);
      add("Specification (Given / When / Then)", r.p.specification, true); add("Parameters", r.p.parameters); add("Edge cases", r.p.edge_cases);
      add("Notes", r.p.notes);
      add("⚠ Suspected defect", r.p.suspected_defect); add("Question for the business expert / confidence note", r.p.sme_question);
      add("Sources", (r.p.sources || [r.loc]).join("\n")); add("Enforced by", r.enforcers.join(", ")); add("Reached through APIs", r.apis.join(", "));
      const actions = el("div", "actions");
      for (const [label, fn] of [["Show in graph", () => showRulesInGraph(r.n.id)], ["Impact of changing it", () => { setMainTab("graph"); runQuery("determine_impact", { name: r.n.id }); }],
                                  ["Node details", () => { selectNode(r.n.id); }]]) {
        const b = el("button", "", label); b.addEventListener("click", ev => { ev.stopPropagation(); fn(); }); actions.appendChild(b);
      }
      td.appendChild(actions);
    }
  }
}

function setMainTab(name) {
  const rules = name === "rules", network = name === "network";
  $("#tab-graph").classList.toggle("active", name === "graph"); $("#tab-rules").classList.toggle("active", rules);
  $("#tab-network").classList.toggle("active", network);
  $("#rulelist").style.display = rules ? "flex" : "none";
  netShow(network);
  if (rules) renderRuleList();
}

/* Rules + what enforces them / what they impact + the screens that reach them (one rule, or all of them). */
function showRulesInGraph(onlyId, subset) {
  const rules = onlyId ? [G.byId.get(onlyId)] : (subset || ruleNodes());
  setMainTab("graph");
  if (!rules.length || !rules[0]) { $("#view-info").textContent = "no business rule in the graph yet"; return; }
  const ids = new Set(rules.map(r => r.id));
  for (const r of rules) for (const e of [...(G.out.get(r.id) || []), ...(G.inn.get(r.id) || [])]) { ids.add(e.src); ids.add(e.dst); }
  for (const id of [...ids]) if (G.byId.get(id)?.type === "API")
    for (const e of G.inn.get(id) || []) if (e.rel === "CALLS" && G.byId.get(e.src).type === "Screen") ids.add(e.src);
  lastResult = null; selected = onlyId || null;
  view = { mode: "result", ids, edges: null, focus: onlyId || null };
  refresh(true);
  if (onlyId) selectNode(onlyId);
}

/* ------------------------------------------------------------ dashboard chrome */
function renderKpis() {
  const box = $("#kpis"), by = t => G.nodes.filter(n => n.type === t);
  if (!G.nodes.length) { box.style.display = "none"; return; }
  const apis = by("API"), rules = by("BusinessRule"), unresolved = apis.filter(a => a.props.unresolved).length;
  const defects = rules.filter(r => r.props.suspected_defect).length, p0 = rules.filter(r => r.props.priority === "P0").length;
  const fk = G.edges.filter(e => e.rel === "DEPENDS_ON" && G.byId.get(e.src).type === "Table").length;
  const cards = [
    { label: "Screens", value: by("Screen").length, chip: `${by("Route").length} routes`, tone: "good", cap: "entry points users can open" },
    { label: "APIs", value: apis.length, chip: unresolved ? `${unresolved} unlinked` : "all linked", tone: unresolved ? "bad" : "good", cap: "endpoints called by the frontend" },
    { label: "Tables", value: by("Table").length, chip: `${fk} FK`, tone: "good", cap: "data model" },
    { label: "Business rules", value: rules.length, click: () => setMainTab("rules"),
      chip: !rules.length ? "none yet" : defects ? `${defects} suspected defect${defects > 1 ? "s" : ""}` : p0 ? `${p0} P0` : "no defect flagged",
      tone: defects ? "bad" : "good", cap: rules.length ? "click to open the list" : "run “Analyze business rules”" },
  ];
  box.replaceChildren();
  for (const c of cards) {
    const k = el("div", "kpi" + (c.click ? " click" : ""));
    const row = el("div", "k-row");
    row.append(el("span", "k-value", String(c.value)), el("span", "pill " + c.tone, (c.tone === "good" ? "▲ " : "▼ ") + c.chip));
    k.append(el("div", "k-label", c.label), row, el("div", "k-cap", c.cap));
    if (c.click) k.addEventListener("click", c.click);
    box.appendChild(k);
  }
  box.style.display = "";
}

function initChrome() {
  const input = $("#top-search"), list = $("#top-results");
  let items = [], sel = -1;
  const close = () => { list.style.display = "none"; sel = -1; };
  const go = n => { close(); input.blur();
    if (view.mode === "result" && !view.ids?.has(n.id)) { view = { mode: "full", ids: null, edges: null, focus: null }; refresh(true); }
    selectNode(n.id); if (netActive()) netCenterOn(n.id); else centerOn(n.id); };
  const draw = () => {
    list.replaceChildren(...items.map((n, i) => { const d = el("div", i === sel ? "sel" : "");
      const dotc = el("span", "dot"); dotc.style.background = color(n.type); d.append(dotc, document.createTextNode(n.name.length > 38 ? n.name.slice(0, 37) + "…" : n.name), el("small", "", n.type));
      d.addEventListener("mousedown", ev => { ev.preventDefault(); go(n); }); return d; }));
    list.style.display = items.length ? "block" : "none";
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    items = q.length < 2 ? [] : G.nodes.filter(n => n.name.toLowerCase().includes(q)).sort((a, b) => a.name.length - b.name.length).slice(0, 12); sel = -1; draw();
  });
  input.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); draw(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); ev.preventDefault(); }
    else if (ev.key === "Enter" && items.length) go(items[Math.max(0, sel)]);
    else if (ev.key === "Escape") { close(); input.blur(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 120));
  window.addEventListener("keydown", ev => { if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); input.focus(); input.select(); } });
  $("#btn-drawer").addEventListener("click", () => { document.body.classList.toggle("drawer-off"); setTimeout(() => fitView(), 50); });
  $("#btn-activity").addEventListener("click", () => selectTab("#left-tabs", "rules"));
}

/* ---------------------------------------------------------------------- boot */
async function boot() {
  META = await api("/api/meta");
  tabs("#left-tabs"); tabs("#right-tabs");
  buildQueryForm(); initPanZoom(); initSearch(); initDocs();
  $("#examples").replaceChildren(...EXAMPLES.map(t => { const li = document.createElement("li"); li.textContent = t;
    li.addEventListener("click", () => { $("#nlq").value = t; ask(t); }); return li; }));
  $("#nlq-btn").addEventListener("click", () => ask($("#nlq").value));
  $("#nlq").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#nlq").value); } });
  $("#btn-fit").addEventListener("click", () => (netActive() ? netFit() : fitView()));
  $("#tab-network").addEventListener("click", () => setMainTab("network"));
  $("#tab-graph").addEventListener("click", () => setMainTab("graph"));
  $("#tab-rules").addEventListener("click", () => setMainTab("rules"));
  $("#btn-rules-graph").addEventListener("click", () => showRulesInGraph());
  $("#rl-graph").addEventListener("click", () => showRulesInGraph(null, filteredRules().map(r => r.n)));
  for (const id of ["#rl-search", "#rl-kind", "#rl-prio", "#rl-defect", "#rl-sme"]) $(id).addEventListener(id === "#rl-search" ? "input" : "change", renderRuleList);
  $("#btn-full").addEventListener("click", () => { view = { mode: "full", ids: null, edges: null, focus: null }; refresh(true); });
  $("#btn-result").addEventListener("click", () => { if (lastResult?.graph?.nodes?.length) showResult(lastResult); });
  $("#show-derived").addEventListener("change", () => refresh());
  $("#analyze-btn").addEventListener("click", analyzeRules);
  $("#analyze-btn2").addEventListener("click", analyzeRules);
  $("#cancel-btn").addEventListener("click", async () => { try { await api("/api/cancel", {}); } catch (ex) { $("#activity").textContent = "ERROR: " + ex.message; } });
  $("#scan-form").addEventListener("submit", async e => {
    e.preventDefault();
    try {
      const engine = "heuristic", useLlm = $("#use-llm").checked;
      if (useLlm && !confirm("Claude enrichment sends the source code of the business classes (services, controllers, entities) to Anthropic.\n\nContinue only if your client / project data policy allows it.")) return;
      await api("/api/scan", { source: $("#source").value, branch: $("#branch").value, llm: useLlm, rules_engine: engine });
      const s = await pollStatus();
      if (s.state === "done") await loadGraph();
    } catch (ex) { $("#status").className = "chip error"; $("#status").textContent = "error: " + ex.message; }
  });
  window.addEventListener("resize", () => fitView());
  initChrome();
  if (window.kgraphDesktop) {                          // running inside the Electron shell: native folder picker + bundled example
    const b = el("button", "", "Browse…"); b.type = "button";
    b.addEventListener("click", async () => { const d = await window.kgraphDesktop.pickFolder(); if (d) $("#source").value = d; });
    $("#source").after(b);
    window.kgraphDesktop.samplePath().then(p => { if (p && !$("#source").value) $("#source").value = p; });
  }
  const s = await pollStatus();
  if (s.has_graph) { await loadGraph(); selectTab("#left-tabs", "ask"); }      // otherwise the Repository pane stays open
}
boot();
