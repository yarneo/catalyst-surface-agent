/* ============================================================
   app.js — boot: build tabs, wire nav, lazy-render tab content
   ============================================================ */
(function () {
  const tabs = [
    { id: "live",     label: "Live",             render: renderLive,     live: true },
    { id: "strategy", label: "Experiments",      render: renderStrategy },
    { id: "crash",    label: "Risk & stress",    render: renderCrash },
  ];

  const page = $("#page");
  const nav  = $("#nav");
  const built = {};   // id -> { node, def }

  async function ensureBuilt(id) {
    if (built[id]) return built[id];
    const def = tabs.find(t => t.id === id);
    const node = await def.render();        // renderers are async
    page.appendChild(node);
    built[id] = { node, def };
    return built[id];
  }

  async function activate(id) {
    const b = await ensureBuilt(id);
    Object.values(built).forEach(x => x.node.classList.remove("active"));
    b.node.classList.add("active");
    [...nav.children].forEach(btn => btn.setAttribute("aria-selected", btn.dataset.id === id));
    history.replaceState(null, "", "#" + id);
  }

  // build nav
  tabs.forEach(t => {
    const btn = h("button.nav-tab", { role: "tab", "data-id": t.id, type: "button" },
      t.live ? h("span.dot") : null, t.label);
    btn.addEventListener("click", () => activate(t.id));
    nav.appendChild(btn);
  });

  // footer timestamp
  $("#loaded").textContent = "Loaded " + new Date().toLocaleString("en-US");

  chartTheme();
  initTooltips();

  const start = (location.hash || "#live").slice(1);
  activate(tabs.some(t => t.id === start) ? start : "live");

  // Sync URL changes (e.g., manual hash edits or back button) to tab activation
  window.addEventListener("hashchange", () => {
    const id = (location.hash || "#live").slice(1);
    if (tabs.some(t => t.id === id)) activate(id);
  });
})();
