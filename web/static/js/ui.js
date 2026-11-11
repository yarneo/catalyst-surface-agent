/* ============================================================
   ui.js — DOM helpers, glossary tooltips, builders, chart theme
   ============================================================ */

/* hyperscript: h('div.card', {id:'x'}, child, child) */
function h(tag, props, ...kids) {
  if (tag && tag.nodeType) return tag;   // already a node — pass through
  let tagName = "div", cls = "";
  const m = tag.match(/^([a-z0-9]+)?((?:\.[^.#]+)*)$/i);
  if (m) { tagName = m[1] || "div"; cls = (m[2] || "").replace(/\./g, " ").trim(); }
  const el = document.createElement(tagName);
  if (cls) el.className = cls;
  if (props && (props.nodeType || Array.isArray(props) || typeof props !== "object")) {
    kids.unshift(props); props = null;
  }
  if (props) for (const k in props) {
    const v = props[k];
    if (k === "class") el.className += " " + v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v != null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  const add = (c) => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
const $ = (s, r = document) => r.querySelector(s);

/* ---- glossary term: dotted-underline span with hover tooltip ---- */
function term(label, key) {
  const g = GLOSSARY[key];
  const span = h("span.term", label || (g ? g[0] : key));
  span.dataset.tipKey = key;
  return span;
}
function initTooltips() {
  const tip = h("div", { id: "tip" });
  document.body.appendChild(tip);
  let active = null;

  function showFor(t) {
    const g = GLOSSARY[t.dataset.tipKey];
    if (!g) return;
    active = t;
    tip.innerHTML = "";
    tip.appendChild(h("span.tip-term", g[0]));
    tip.appendChild(document.createTextNode(g[1]));
    const r = t.getBoundingClientRect();
    requestAnimationFrame(() => {
      let left = r.left + r.width / 2 - tip.offsetWidth / 2;
      left = Math.max(12, Math.min(left, innerWidth - tip.offsetWidth - 12));
      let top = r.top - tip.offsetHeight - 10;
      if (top < 12) top = r.bottom + 10;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
      tip.classList.add("show");
    });
  }
  function hide() { tip.classList.remove("show"); active = null; }

  // Desktop hover
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest(".term");
    if (!t || t === active) return;
    showFor(t);
  });
  document.addEventListener("mouseout", (e) => {
    if (e.target.closest(".term") === active) hide();
  });
  // Touch: tap a term to show/toggle, tap outside to dismiss
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".term");
    if (t) {
      if (t === active) hide();
      else showFor(t);
      e.stopPropagation();
      e.preventDefault();
      return;
    }
    if (active) hide();
  });
  addEventListener("scroll", hide, true);
}

/* ---- section header (eyebrow + title + caption) ---- */
function sectionHead(title, caption, eyebrow) {
  return h("div.section-head",
    eyebrow ? h("p.eyebrow", eyebrow) : null,
    h("h2.h-md", title),
    caption ? (caption.nodeType ? caption : h("p.caption", caption)) : null
  );
}

/* ---- badge ---- */
function badge(text, kind) {
  return h("span.badge." + kind, h("span.b-dot"), text);
}

/* ---- a generic table from columns + rows ---- */
/* On mobile we restyle this table as a list of stacked cards (see app.css).
   For that to work, each <td> needs to know its column label, which we
   stash in a data-label attribute taken from the column's `h`. */
function buildTable(cols, rows) {
  const thead = h("tr", cols.map(c => h("th", c.h)));
  const tbody = rows.map(r => h("tr", r.hi ? { class: "row-hi" } : null,
    cols.map(c => {
      const cell = c.cell(r);
      const cls = c.tdClass ? c.tdClass(r) : "";
      const props = { "data-label": c.h };
      if (cls) props.class = cls;
      return h("td", props, cell && cell.nodeType ? cell : cell);
    })
  ));
  return h("div.tbl-wrap", h("table.tbl", h("thead", thead), h("tbody", tbody)));
}

/* ---- legend ---- */
function legend(items) {
  return h("div.legend", items.map(it => {
    let sw;
    if (it.dash) sw = h("span.legend-swatch.dash", { style: `color:${it.color}` });
    else if (it.dot) sw = h("span.legend-swatch.dot", { style: `background:${it.color}` });
    else sw = h("span.legend-swatch", { style: `background:${it.color}` });
    return h("span.legend-item", sw, it.label, it.tag ? badge(it.tag, "new") : null);
  }));
}

/* resolve a CSS var() string to a real color (Chart.js needs concrete colors) */
function cssColor(v) {
  if (!v || !v.startsWith("var(")) return v;
  const name = v.slice(4, -1).trim();
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ---- Chart.js shared theme ---- */
function chartTheme() {
  if (!window.Chart) return;
  const grid = "rgba(255,255,255,0.055)";
  const tickCol = cssColor("var(--text-3)");
  // On mobile, bump the default font size — Chart.js doesn't read CSS, so
  // ticks would otherwise stay tiny no matter what the rest of the page does.
  const isMobile = window.matchMedia("(max-width: 640px)").matches;
  Chart.defaults.font.family = "'IBM Plex Sans', sans-serif";
  Chart.defaults.font.size = isMobile ? 12 : 11.5;
  Chart.defaults.color = tickCol;
  Chart.defaults.borderColor = grid;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = false;
  Chart.defaults.animations = false;
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;
  Chart.defaults.elements.line.borderJoinStyle = "round";
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.plugins.tooltip.backgroundColor = cssColor("var(--surface-3)");
  Chart.defaults.plugins.tooltip.borderColor = "rgba(255,255,255,0.15)";
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleColor = cssColor("var(--text)");
  Chart.defaults.plugins.tooltip.bodyColor = cssColor("var(--text-2)");
  Chart.defaults.plugins.tooltip.padding = 11;
  Chart.defaults.plugins.tooltip.cornerRadius = 9;
  Chart.defaults.plugins.tooltip.titleFont = { family: "'IBM Plex Mono', monospace", size: 12 };
  Chart.defaults.plugins.tooltip.bodyFont = { family: "'IBM Plex Mono', monospace", size: 12 };
  Chart.defaults.plugins.tooltip.boxPadding = 5;
  Chart.GRID = grid;
}

/* x-axis time scale config helper (category of Date) */
function yearTicks(labels) {
  // return callback that shows only Jan-ish ticks as the year
  return function (val, idx) {
    const d = labels[idx];
    if (!d) return "";
    if (idx === 0 || (d.getMonth() === 0 && labels[idx - 1] && labels[idx - 1].getMonth() === 11)) {
      return d.getFullYear();
    }
    return "";
  };
}

/* gradient fill for area charts */
function vGradient(ctx, area, color, topA, botA) {
  const g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
  g.addColorStop(0, color.replace(/[\d.]+\)$/, topA + ")"));
  g.addColorStop(1, color.replace(/[\d.]+\)$/, botA + ")"));
  return g;
}
