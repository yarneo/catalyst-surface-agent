/* ============================================================
   app.js — boot: build tabs, wire nav, lazy-render tab content
   ============================================================ */
(function () {
  const tabs = [
    { id: "live",     label: "Overview",        render: renderOverview },
    { id: "strategy", label: "Research",        render: renderStrategy },
    { id: "crash",    label: "Details",         render: renderLive },
  ];

  const page = $("#page");
  const nav  = $("#nav");
  const built = {};   // id -> { node, def }
  const building = {};
  let activation = 0;

  async function ensureBuilt(id) {
    if (built[id]) return built[id];
    if (!building[id]) building[id] = (async()=>{
      const def = tabs.find(t => t.id === id);
      const node = await def.render();
      page.appendChild(node);
      built[id] = { node, def };
      return built[id];
    })();
    return building[id];
  }

  async function activate(id) {
    const turn = ++activation;
    const b = await ensureBuilt(id);
    if (turn !== activation) return;
    Object.values(built).forEach(x => x.node.classList.remove("active"));
    b.node.classList.add("active");
    [...nav.children].forEach(btn => {
      btn.setAttribute("aria-selected", btn.dataset.id === id);
      btn.tabIndex = btn.dataset.id === id ? 0 : -1;
    });
    history.replaceState(null, "", "#" + id);
    window.scrollTo({top:0,behavior:"instant"});
    b.node._onActivate?.();
  }

  // build nav
  tabs.forEach(t => {
    const btn = h("button.nav-tab", { id: "nav-"+t.id, role: "tab", "aria-controls":"tab-"+t.id, "data-id": t.id, type: "button" },
      t.live ? h("span.dot") : null, t.label);
    btn.addEventListener("click", () => activate(t.id));
    nav.appendChild(btn);
  });
  nav.addEventListener("keydown", e=>{
    const buttons=[...nav.children], index=buttons.indexOf(document.activeElement);
    if(index<0)return;
    let next;
    if(e.key==="ArrowRight")next=(index+1)%buttons.length;
    if(e.key==="ArrowLeft")next=(index+buttons.length-1)%buttons.length;
    if(e.key==="Home")next=0;
    if(e.key==="End")next=buttons.length-1;
    if(next!=null){e.preventDefault();buttons[next].focus();activate(buttons[next].dataset.id);}
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
