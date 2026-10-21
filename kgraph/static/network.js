"use strict";
/* Obsidian-style force-directed view (canvas). Uses app.js globals: G, visible(), selected, hidden, selectNode(), color(), META, $, $$.
   Physics: centre pull + spring links + Barnes-Hut repulsion, d3-force-like cooling. No dependency. */

const net = {
  on: false, nodes: [], links: [], byId: new Map(), adj: new Map(), pos: new Map(),
  alpha: 0, raf: 0, tf: { x: 0, y: 0, k: 1 }, hover: null, drag: null, pan: null, w: 0, h: 0, dpr: 1,
  frozen: false, query: "", fitPending: false, canvas: null, ctx: null,
  o: { labels: true, arrows: false, orphans: true, local: false, depth: 2, fade: 0.6, size: 1, linkW: 1,
       center: 0.05, repel: 1, link: 0.5, dist: 55 },
};
const netActive = () => net.on;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/* ----------------------------------------------------------------- data */
function netRebuild(nodes, edges) {
  if (!net.canvas) return;
  if (!nodes) ({ nodes, edges } = visible());
  const seen = new Set();
  let links = [];
  for (const e of edges) {                       // one link per ordered pair (parallel relations collapse)
    const k = e.src + "|" + e.dst;
    if (!seen.has(k)) { seen.add(k); links.push({ s: e.src, t: e.dst, rel: e.rel, derived: e.derived }); }
  }
  const degree = ls => { const d = new Map(); for (const l of ls) { d.set(l.s, (d.get(l.s) || 0) + 1); d.set(l.t, (d.get(l.t) || 0) + 1); } return d; };
  let deg = degree(links), keep = nodes;
  if (!net.o.orphans) keep = keep.filter(n => deg.get(n.id));
  if (net.o.local && selected && keep.some(n => n.id === selected)) {          // local graph around the selected node
    const adj = new Map();
    for (const l of links) { (adj.get(l.s) || adj.set(l.s, []).get(l.s)).push(l.t); (adj.get(l.t) || adj.set(l.t, []).get(l.t)).push(l.s); }
    const dist = new Map([[selected, 0]]), q = [selected];
    while (q.length) { const c = q.shift(); if (dist.get(c) >= net.o.depth) continue; for (const x of adj.get(c) || []) if (!dist.has(x)) { dist.set(x, dist.get(c) + 1); q.push(x); } }
    keep = keep.filter(n => dist.has(n.id));
  }
  const ids = new Set(keep.map(n => n.id));
  links = links.filter(l => ids.has(l.s) && ids.has(l.t));
  deg = degree(links);
  let fresh = 0;
  net.nodes = keep.map(n => {
    const p = net.pos.get(n.id);
    if (!p) fresh++;
    const a = Math.random() * 6.2832, r = 30 + Math.random() * 180;
    return { id: n.id, type: n.type, name: n.name, props: n.props, x: p ? p.x : Math.cos(a) * r, y: p ? p.y : Math.sin(a) * r,
             vx: 0, vy: 0, deg: deg.get(n.id) || 0, r: 0, fixed: false };
  });
  net.byId = new Map(net.nodes.map(n => [n.id, n]));
  net.links = links.map(l => ({ s: net.byId.get(l.s), t: net.byId.get(l.t), rel: l.rel, derived: l.derived }));
  net.adj = new Map(net.nodes.map(n => [n.id, new Set()]));
  for (const l of net.links) { net.adj.get(l.s.id).add(l.t.id); net.adj.get(l.t.id).add(l.s.id); }
  netSizes();
  if (net.nodes.length && fresh > net.nodes.length / 2) {                       // new layout: settle it before showing it
    net.alpha = 1;
    for (let i = 0; i < 160; i++) netTick();
    net.fitPending = true;
  }
  net.alpha = Math.max(net.alpha, 0.5);
  netLegend();
  $("#net-info").textContent = `${net.nodes.length} nodes · ${net.links.length} links`;
  if (net.fitPending && net.w) { netFit(); net.fitPending = false; }
  netKick();
}

function netSizes() { for (const n of net.nodes) n.r = (3 + Math.sqrt(n.deg) * 1.7) * net.o.size; }

/* -------------------------------------------------------------- physics */
function netTick() {
  const o = net.o, a = net.alpha, N = net.nodes;
  if (!N.length) return;
  for (const n of N) { n.vx -= n.x * o.center * a * 0.1; n.vy -= n.y * o.center * a * 0.1; }
  for (const l of net.links) {
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.hypot(dx, dy) || 0.01;
    const k = (d - o.dist) / d * o.link * a, bias = l.s.deg / (l.s.deg + l.t.deg || 1);
    l.s.vx += dx * k * (1 - bias); l.s.vy += dy * k * (1 - bias);
    l.t.vx -= dx * k * bias;       l.t.vy -= dy * k * bias;
  }
  const root = netQuadtree(N), strength = 260 * o.repel * a;
  for (const n of N) netRepel(n, root, strength);
  for (const n of N) {
    if (n.fixed) { n.vx = n.vy = 0; continue; }
    n.vx *= 0.6; n.vy *= 0.6;
    n.x += clamp(n.vx, -40, 40); n.y += clamp(n.vy, -40, 40);
  }
  net.alpha += (0 - net.alpha) * 0.0228;
}

function netQuadtree(N) {                                                         // Barnes-Hut
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of N) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
  const s = Math.max(x1 - x0, y1 - y0, 1) + 2;
  const mk = (x, y, s) => ({ x, y, s, m: 0, cx: 0, cy: 0, body: null, ch: null });
  const root = mk(x0 - 1, y0 - 1, s);
  const add = (q, n, depth) => {
    q.cx = (q.cx * q.m + n.x) / (q.m + 1); q.cy = (q.cy * q.m + n.y) / (q.m + 1); q.m++;
    if (q.m === 1) { q.body = n; return; }
    if (!q.ch) {
      if (depth > 24) return;                                                      // coincident points
      q.ch = [null, null, null, null];
      const b = q.body; q.body = null; if (b) put(q, b, depth);
    }
    put(q, n, depth);
  };
  const put = (q, n, depth) => {
    const h = q.s / 2, ix = n.x >= q.x + h ? 1 : 0, iy = n.y >= q.y + h ? 1 : 0, i = iy * 2 + ix;
    (q.ch[i] || (q.ch[i] = mk(q.x + ix * h, q.y + iy * h, h)));
    add(q.ch[i], n, depth + 1);
  };
  for (const n of N) add(root, n, 0);
  return root;
}

function netRepel(n, q, strength) {
  if (!q || !q.m) return;
  const dx = q.cx - n.x, dy = q.cy - n.y, d2 = dx * dx + dy * dy + 0.01;
  if (q.body === n && !q.ch) return;
  if (!q.ch || q.s * q.s / d2 < 0.81) {                                            // leaf, or far enough: treat as one mass
    if (q.body === n) return;
    const f = strength * q.m / d2, d = Math.sqrt(d2);
    n.vx -= dx / d * f * 8; n.vy -= dy / d * f * 8;
    return;
  }
  for (const c of q.ch) if (c) netRepel(n, c, strength);
}

/* ------------------------------------------------------------ rendering */
function netResize() {
  const c = net.canvas, box = $("#network");
  if (!c || !box) return;
  net.dpr = window.devicePixelRatio || 1;
  net.w = box.clientWidth; net.h = box.clientHeight;
  c.width = Math.max(1, net.w * net.dpr); c.height = Math.max(1, net.h * net.dpr);
  c.style.width = net.w + "px"; c.style.height = net.h + "px";
}

function netFit() {
  if (!net.nodes.length || !net.w) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of net.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
  const bw = x1 - x0 + 120, bh = y1 - y0 + 120, k = clamp(Math.min(net.w / bw, net.h / bh), 0.05, 2.5);
  net.tf = { k, x: net.w / 2 - (x0 + x1) / 2 * k, y: net.h / 2 - (y0 + y1) / 2 * k };
  netDraw();
}

function netMatches() {
  const q = net.query.trim().toLowerCase();
  if (!q) return null;
  return new Set(net.nodes.filter(n => n.name.toLowerCase().includes(q) || n.type.toLowerCase() === q).map(n => n.id));
}

function netDraw() {
  const c = net.ctx; if (!c || !net.on) return;
  const { k, x, y } = net.tf, o = net.o;
  c.setTransform(net.dpr, 0, 0, net.dpr, 0, 0);
  c.fillStyle = "#fbfbfe"; c.fillRect(0, 0, net.w, net.h);
  c.save(); c.translate(x, y); c.scale(k, k);
  const focus = net.hover || (net.byId.has(selected) ? selected : null), nb = focus ? net.adj.get(focus) : null, match = netMatches();
  const dimNode = n => (focus && n.id !== focus && !nb.has(n.id)) || (match && !match.has(n.id));
  for (const l of net.links) {                                                       // links
    const hot = focus && (l.s.id === focus || l.t.id === focus);
    const dim = (focus && !hot) || (match && !(match.has(l.s.id) || match.has(l.t.id)));
    c.strokeStyle = hot ? "rgba(79,70,229,.9)" : `rgba(110,110,140,${dim ? 0.07 : 0.32})`;
    c.lineWidth = (hot ? 1.8 : 1) * o.linkW / k;
    c.setLineDash(l.derived ? [4 / k, 3 / k] : []);
    c.beginPath(); c.moveTo(l.s.x, l.s.y); c.lineTo(l.t.x, l.t.y); c.stroke();
    if (o.arrows && k > 0.35) {
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d, ex = l.t.x - ux * l.t.r, ey = l.t.y - uy * l.t.r, s = 6 / k;
      c.fillStyle = c.strokeStyle; c.setLineDash([]);
      c.beginPath(); c.moveTo(ex, ey); c.lineTo(ex - ux * s - uy * s * 0.45, ey - uy * s + ux * s * 0.45); c.lineTo(ex - ux * s + uy * s * 0.45, ey - uy * s - ux * s * 0.45); c.fill();
    }
  }
  c.setLineDash([]);
  for (const n of net.nodes) {                                                       // nodes
    const dim = dimNode(n);
    c.globalAlpha = dim ? 0.16 : 1;
    c.fillStyle = color(n.type);
    c.beginPath(); c.arc(n.x, n.y, n.r, 0, 6.2832); c.fill();
    if (n.id === selected || n.id === net.hover) { c.lineWidth = 2.2 / k; c.strokeStyle = "#1d1d2b"; c.stroke(); }
    else if (n.type === "BusinessRule" && n.props && n.props.suspected_defect) { c.lineWidth = 1.6 / k; c.strokeStyle = "#fbbf24"; c.stroke(); }
  }
  c.globalAlpha = 1;
  if (o.labels) {                                                                    // labels fade in with zoom (Obsidian "text fade threshold")
    const base = clamp((k - o.fade) * 2.5, 0, 1);
    c.font = `${12 / k}px -apple-system, "Segoe UI", sans-serif`; c.textAlign = "center"; c.textBaseline = "top";
    for (const n of net.nodes) {
      const isFocus = focus && (n.id === focus || nb.has(n.id)), isMatch = match && match.has(n.id);
      const a = isFocus || isMatch ? 1 : dimNode(n) ? base * 0.2 : base;
      if (a < 0.03) continue;
      c.fillStyle = `rgba(29,29,43,${a})`;
      c.fillText(n.name.length > 30 ? n.name.slice(0, 29) + "…" : n.name, n.x, n.y + n.r + 2 / k);
    }
  }
  c.restore();
}

function netKick() {
  if (net.raf || !net.on) return;
  const loop = () => {
    net.raf = 0;
    if (!net.on) return;
    const moving = net.alpha > 0.003 && !net.frozen;
    if (moving) netTick();
    netDraw();
    if (moving || net.drag) net.raf = requestAnimationFrame(loop);
  };
  net.raf = requestAnimationFrame(loop);
}

/* ----------------------------------------------------------- interaction */
function netWorld(ev) {
  const r = net.canvas.getBoundingClientRect();
  return { sx: ev.clientX - r.left, sy: ev.clientY - r.top, x: (ev.clientX - r.left - net.tf.x) / net.tf.k, y: (ev.clientY - r.top - net.tf.y) / net.tf.k };
}
function netHit(p) {
  let best = null, bd = Infinity;
  for (const n of net.nodes) {
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d <= n.r + 4 / net.tf.k && d < bd) { best = n; bd = d; }
  }
  return best;
}
function netCenterOn(id) {
  const n = net.byId.get(id); if (!n) return;
  net.tf.x = net.w / 2 - n.x * net.tf.k; net.tf.y = net.h / 2 - n.y * net.tf.k; netDraw();
}

function netInit() {
  net.canvas = $("#net-canvas"); net.ctx = net.canvas.getContext("2d");
  const cv = net.canvas;
  cv.addEventListener("mousedown", ev => {
    const p = netWorld(ev), n = netHit(p);
    if (n) { net.drag = { n, moved: false, sx: p.sx, sy: p.sy }; n.fixed = true; net.alpha = Math.max(net.alpha, 0.3); netKick(); }
    else net.pan = { sx: ev.clientX, sy: ev.clientY, tx: net.tf.x, ty: net.tf.y };
  });
  window.addEventListener("mousemove", ev => {
    if (!net.on) return;
    if (net.drag) {
      const p = netWorld(ev);
      if (Math.hypot(p.sx - net.drag.sx, p.sy - net.drag.sy) > 3) net.drag.moved = true;
      net.drag.n.x = p.x; net.drag.n.y = p.y; net.alpha = Math.max(net.alpha, 0.3); netKick();
    } else if (net.pan) {
      net.tf.x = net.pan.tx + ev.clientX - net.pan.sx; net.tf.y = net.pan.ty + ev.clientY - net.pan.sy; netDraw();
    } else if (ev.target === cv) {
      const h = netHit(netWorld(ev)), id = h ? h.id : null;
      if (id !== net.hover) { net.hover = id; cv.style.cursor = id ? "pointer" : "grab"; netDraw(); }
    }
  });
  window.addEventListener("mouseup", () => {
    if (net.drag) { const d = net.drag; d.n.fixed = false; net.drag = null; if (!d.moved) { selectNode(d.n.id); netDraw(); } }
    net.pan = null;
  });
  cv.addEventListener("dblclick", ev => {                                            // double click: local graph around that node
    const n = netHit(netWorld(ev)); if (!n) return;
    selectNode(n.id); $("#net-local").checked = true; net.o.local = true; netRebuild(); netCenterOn(n.id);
  });
  cv.addEventListener("mouseleave", () => { if (net.hover) { net.hover = null; netDraw(); } });
  cv.addEventListener("wheel", ev => {
    ev.preventDefault();
    const p = netWorld(ev), k = clamp(net.tf.k * (ev.deltaY < 0 ? 1.15 : 1 / 1.15), 0.03, 6);
    net.tf.x = p.sx - p.x * k; net.tf.y = p.sy - p.y * k; net.tf.k = k; netDraw();
  }, { passive: false });
  window.addEventListener("resize", () => { if (net.on) { netResize(); netDraw(); } });

  const num = (id, key, after) => $(id).addEventListener("input", e => { net.o[key] = parseFloat(e.target.value); after ? after() : (net.alpha = Math.max(net.alpha, 0.5), netKick()); });
  const chk = (id, key, rebuild) => $(id).addEventListener("change", e => { net.o[key] = e.target.checked; rebuild ? netRebuild() : netDraw(); });
  num("#net-center", "center"); num("#net-repel", "repel"); num("#net-link", "link"); num("#net-dist", "dist");
  num("#net-fade", "fade", netDraw); num("#net-linkw", "linkW", netDraw);
  num("#net-size", "size", () => { netSizes(); netDraw(); }); num("#net-depth", "depth", () => net.o.local && netRebuild());
  chk("#net-labels", "labels"); chk("#net-arrows", "arrows"); chk("#net-orphans", "orphans", true); chk("#net-local", "local", true);
  $("#net-search").addEventListener("input", e => {
    net.query = e.target.value; netDraw();
    const m = netMatches(); if (m && m.size === 1) netCenterOn([...m][0]);
  });
  $("#net-restart").addEventListener("click", () => { net.frozen = false; $("#net-freeze").textContent = "Freeze"; net.alpha = 1; netKick(); });
  $("#net-freeze").addEventListener("click", () => { net.frozen = !net.frozen; $("#net-freeze").textContent = net.frozen ? "Resume" : "Freeze"; if (!net.frozen) { net.alpha = Math.max(net.alpha, 0.3); netKick(); } });
  $("#net-fit").addEventListener("click", netFit);
  $("#net-toggle").addEventListener("click", () => $("#net-panel").classList.toggle("collapsed"));
}

function netLegend() {
  const box = $("#net-legend"); if (!box || !META) return;
  const counts = {}; for (const n of G.nodes) counts[n.type] = (counts[n.type] || 0) + 1;
  box.replaceChildren();
  META.node_types.forEach((t, i) => {
    if (!counts[t]) return;
    const row = document.createElement("div"); row.className = "lg" + (hidden.has(t) ? " off" : "");
    const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = color(t);
    row.append(dot, document.createTextNode(` ${t} `), Object.assign(document.createElement("small"), { textContent: counts[t] }));
    row.title = "Click to show / hide this type";
    row.addEventListener("click", () => { const cb = $$("#type-filters input")[i]; if (cb) cb.click(); });   // same switch as the Filters tab
    box.appendChild(row);
  });
}

function netShow(on) {
  net.on = on;
  $("#network").style.display = on ? "block" : "none";
  if (!on) { cancelAnimationFrame(net.raf); net.raf = 0; return; }
  if (!net.canvas) netInit();
  netResize();
  net.fitPending = true;
  netRebuild();
  if (net.w) { netFit(); net.fitPending = false; }
}

/* remember positions between rebuilds so the layout does not jump */
setInterval(() => { if (net.on) for (const n of net.nodes) net.pos.set(n.id, { x: n.x, y: n.y }); }, 1000);
