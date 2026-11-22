/* A small first screen; detailed endpoints load only when requested. */
async function dashboardFetch(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, {signal: controller.signal, cache: "no-store"});
    if (!response.ok) throw new Error("Data could not be loaded");
    const value = await response.json();
    if (value.error) throw new Error("Data is temporarily unavailable");
    return value;
  } finally { clearTimeout(timeout); }
}

function dashboardDisclosure(title, description, ...content) {
  const body = h("div.disclosure-body", ...content);
  const node = h("details.disclosure", h("summary", h("span", h("strong", title), h("span.disclosure-caption", description))), body);
  node.addEventListener("toggle", () => {
    if (node.open) requestAnimationFrame(() => {
      if (window.Chart) body.querySelectorAll("canvas").forEach(c => Chart.getChart(c)?.resize());
    });
  });
  return node;
}

function dashboardStatus(text, tone) { return h("span.plain-status." + tone, text); }

async function renderOverview() {
  const refresh = h("button.quiet-button", {type: "button"}, "Refresh");
  const updated = h("span.overview-updated", "Loading account snapshot…");
  const metrics = h("div.overview-metrics", {"aria-live": "polite"}, h("p.dim", "Loading account performance…"));
  const operations = h("div.overview-operations", {"aria-live": "polite"});
  const research = h("div.overview-research", h("p.dim", "Checking research…"));
  const accounting = h("div", {"aria-live":"polite"});
  const holdings = h("div.card.card-pad"), strategies = h("div.card.card-pad");
  const chartHost = h("div.overview-chart");
  const chartCaption = h("p.caption", "Completed trading days. Today's live account value is shown above.");
  const chartButtons = h("div.segmented", {"aria-label": "Chart view"});
  const chartCard = h("div.card.card-pad", h("div.overview-card-head",
    h("div", h("h2.h-md", "How the account is doing"), chartCaption), chartButtons), chartHost);
  const root = h("section.tab", {id: "tab-live", role: "tabpanel", "aria-labelledby": "nav-live"},
    h("div.overview-heading", h("div", h("p.eyebrow", "Private · paper trading"),
      h("h1.h-lg", "Your portfolio, at a glance"), h("p.lead", "What it’s worth. Where it’s invested. What happens next.")), refresh),
    updated, metrics, operations, accounting, research,
    h("div.section", chartCard),
    h("div.section.overview-columns", holdings, strategies),
    h("p.overview-footnote", "Want the full record? ", h("a", {href: "#crash"}, "Open trade history, risk checks and diagnostics →")));
  let pending = false, chart = null, chartMode = "account", performance = null, benchmark = null;
  let chartRequest = 0;

  async function drawChart() {
    const request = ++chartRequest;
    chart?.destroy(); chart = null;
    chartHost.replaceChildren();
    [...chartButtons.children].forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === chartMode)));
    if (chartMode === "market") {
      chartCaption.textContent = "Same dates, same starting value. Compares the bot with the S&P 500 (SPY) and Nasdaq-100 (QQQ).";
      chartHost.appendChild(h("p.dim", "Loading market comparison…"));
      try { benchmark = await dashboardFetch("/api/equity_vs_indices"); }
      catch (_) { if (request === chartRequest) chartHost.replaceChildren(h("p.dim", "Market comparison is unavailable. Account results above are still valid.")); return; }
      if (request !== chartRequest) return;
      if (!benchmark.dates?.length || !window.Chart) {
        chartHost.replaceChildren(h("p.dim", "Market chart unavailable. Try refreshing.")); return;
      }
      const canvas = h("canvas", {role: "img", "aria-label": "Bot, S&P 500 and Nasdaq returns over the same dates"});
      chartHost.replaceChildren(h("div.chart-box", {style: "height:280px"}, canvas),
        legend([{label:"Bot",color:"var(--accent)"},{label:"S&P 500",color:"var(--amber)"},{label:"Nasdaq-100",color:"var(--text-2)"}]),
        h("p.caption", "Starts at the first recorded close, so this chart's return can differ from the return on the original $100,000."));
      chart = makeLineChart(canvas, benchmark.dates.map(d => new Date(d + "T12:00:00Z")),
        [{label:"Bot", data:benchmark.bot.map(v=>v-100),color:"var(--accent)"},
          {label:"S&P 500",data:benchmark.spy.map(v=>v-100),color:"var(--amber)"},
          {label:"Nasdaq-100",data:benchmark.qqq.map(v=>v-100),color:"var(--text-2)"}],
        {yfmt:v=>v.toFixed(1)+"%",tfmt:v=>v.toFixed(2)+"%"});
      return;
    }
    chartCaption.textContent = "Completed trading days. The dotted line is the original $100,000.";
    const rows = performance?.curve || [];
    if (rows.length < 2 || !window.Chart) {
      chartHost.appendChild(h("p.dim", rows.length < 2 ? "Waiting for enough account history to draw the chart." : "Chart could not load. The numbers above remain available.")); return;
    }
    const canvas = h("canvas", {role: "img", "aria-label": "Account value across completed trading days; original funding $100,000"});
    chartHost.appendChild(h("div.chart-box", {style: "height:280px"}, canvas));
    chart = makeAreaChart(canvas, rows.map(r=>Dashboard.date(r.date)), rows.map(r=>r.equity), {
      color:cssColor("var(--accent)"),baseline:100000,yfmt:v=>"$"+(v/1000).toFixed(0)+"k",tfmt:v=>Dashboard.money(v)});
    chart.options.scales.y.suggestedMax = Math.max(100000, ...rows.map(r=>r.equity));
    chart.update();
  }
  [["account", "Account value"], ["market", "Compare with market"]].forEach(([mode,label]) => {
    chartButtons.appendChild(h("button", {type:"button", "data-mode":mode, "aria-pressed":String(mode===chartMode),
      onclick:()=>{chartMode=mode;drawChart();}},label));
  });

  function renderMetrics(account, curve) {
    performance = Dashboard.performance(account || {}, curve?.curve || []);
    const p = performance;
    const small = (title, value, note) => h("div.overview-metric",
      h("span.metric-label",title), h("strong.metric-value."+Dashboard.tone(value?.fraction),Dashboard.percent(value?.fraction)),
      h("span.metric-detail",value ? Dashboard.money(value.amount,true)+" · "+note : "Data unavailable"));
    metrics.replaceChildren(h("div.overview-metric.overview-balance",
      h("span.metric-label","Account value"), h("strong.metric-value",Dashboard.money(p.equity)),
      h("span.metric-detail."+Dashboard.tone(p.total?.amount), p.total
        ? Dashboard.money(p.total.amount,true)+" ("+Dashboard.percent(p.total.fraction)+") since the $100,000 start"
        : "Account snapshot unavailable. Refresh to retry.")),
      small(p.intraday ? "Today · market open" : "Last trading day",p.day,p.intraday ? "since the prior close" : Dashboard.date(p.day?.end)),
      small("Last 5 trading days",p.fiveDays, p.fiveDays ? Dashboard.date(p.fiveDays.start)+" – "+Dashboard.date(p.fiveDays.end) : ""));
    drawChart();
  }

  function renderOperations(account, automation) {
    const state = Dashboard.health(automation || {}), market = account?.valuation?.market_open;
    const phase = automation?.next_cycle?.phase;
    const phaseNames = {premarket:"Pre-market check",open:"Portfolio review",risk:"Risk check",close:"Closing check"};
    operations.replaceChildren(h("div", dashboardStatus(state.label,state.tone),h("p",state.text)),
      h("div",h("strong",market===true?"Market open":market===false?"Market closed":"Market hours unavailable"),
        h("p",(phaseNames[phase] || "Next check")+" · "+Dashboard.time(automation?.next_cycle?.at))),
      h("div",h("strong","Next portfolio review"),h("p",Dashboard.time(automation?.next_rebalance?.at))));
    if ((automation?.issues||[]).length) operations.appendChild(
      h("div.operations-issue",h("strong","Check needed: "),automation.issues.join(" · ")));
  }

  function renderHoldings(account, positions) {
    holdings.replaceChildren(sectionHead("Where the money is", "Current holdings and their change in price since purchase."));
    if (!positions || !Array.isArray(positions.positions)) {
      holdings.appendChild(h("p.dim","Holdings could not be loaded. Refresh to retry.")); return;
    }
    const rows = positions.positions.filter(p=>Math.abs(p.market_value)>=1).sort((a,b)=>b.market_value-a.market_value);
    const names = {BIL:"Short-term U.S. Treasuries",DBMF:"Managed futures",KMLM:"Managed futures",XLE:"Energy companies",QLD:"2× Nasdaq-100",QQQ:"Nasdaq-100",SPY:"S&P 500",DIS:"Disney",IWM:"Small U.S. companies",MDY:"Mid-sized U.S. companies",XLP:"Consumer staples",XLU:"Utilities"};
    holdings.appendChild(buildTable([
      {h:"Investment",cell:r=>h("span.holding-name",h("strong",r.symbol),h("small",names[r.symbol]||"Current position"))},
      {h:"Value / share",cell:r=>h("span.holding-number",h("strong",Dashboard.money(r.market_value)),
        h("small",account?.equity>0?(r.market_value/account.equity*100).toFixed(1)+"% of account":"Share unavailable"))},
      {h:"Price gain / loss",cell:r=>h("span."+Dashboard.tone(r.unrealized_pl),Dashboard.money(r.unrealized_pl,true))}
    ],rows));
    if (!rows.length) holdings.appendChild(h("p.dim","No investment positions in the account."));
    holdings.append(h("div.holdings-cash",h("span","Cash available"),h("strong",Dashboard.money(account?.cash))),
      h("p.caption","Holding gains exclude dividends and closed trades. Account value includes credited income; these rows are not total account profit."));
  }

  function renderStrategies(allocations, positions, champions) {
    strategies.replaceChildren(sectionHead("What the bot is doing", "The strategies allowed to manage this paper account."));
    const verdicts = allocations?.verdicts || {}, rows = positions?.positions;
    const held = syms => Array.isArray(rows) ? rows.some(p=>syms.includes(p.symbol)&&Math.abs(p.market_value)>=1) : null;
    const specs = [
      ["managed_futures","Managed futures",["DBMF","KMLM"],"Diversifies across global markets using DBMF and KMLM."],
      ["cash_park","Treasury reserve",["BIL"],"Keeps part of the account in short-term U.S. Treasury exposure."],
      ["etf","ETF rotation",(champions?.champions||[]).map(c=>c.symbol),"Selects eligible funds and trades their quantitative signals."],
      ["regime","Leveraged Nasdaq · QLD",["QLD"],"Buys only when the Nasdaq trend and strategy risk checks allow it."],
      ["qqq","Nasdaq reserve · QQQ",["QQQ"],"Can invest part of otherwise unused capital when the market trend qualifies."]
    ];
    specs.forEach(([key,name,symbols,description])=>{
      const isHeld = key==="etf" && !champions?.available ? null : held(symbols);
      const state = Dashboard.strategyState(verdicts[key],isHeld);
      const paused = ["FAST_TRIPPED","TRIPPED"].includes(verdicts[key]);
      const text = paused ? "Paused by its risk checks. It must qualify again before new exposure is allowed." : description;
      strategies.appendChild(h("div.strategy-summary",h("div.strategy-summary-head",h("h3",name),dashboardStatus(state.label,state.tone)),
        h("p",text),isHeld===null?h("small.dim","Current holdings unavailable"):null));
    });
    const off = [["crypto","Crypto"],["pead","earnings trades"]].filter(([key])=>verdicts[key]==="DISABLED").map(([,name])=>name);
    if (off.length) strategies.appendChild(h("p.caption",off.join(" and ")+" are switched off for this account. ",h("a",{href:"#strategy"},"See research →")));
  }

  async function refreshResearch() {
    let lab = null;
    try { lab = await dashboardFetch("/api/portfolio_lab"); } catch (_) { /* Explicit unavailable state below. */ }
    const state = Dashboard.research(lab);
    research.replaceChildren(h("div",h("strong."+state.tone,state.label),h("p",state.text)),h("a",{href:"#strategy"},"See research →"));
  }

  async function checkAccounting() {
    try {
      const result = await dashboardFetch("/api/reconciliation");
      accounting.replaceChildren();
      if (result.status!=="MATCHED") accounting.appendChild(h("p.callout.warn",
        result.status==="MISMATCH" ? "Holdings need checking: the broker positions do not match the trade record. Open Details → Automation checks."
          : "The holdings check could not confirm agreement with the trade record. Open Details → Automation checks."));
    } catch (_) {
      accounting.replaceChildren(h("p.callout.warn","The holdings check is unavailable. Bot schedule status does not confirm that the trade record matches the account."));
    }
  }

  async function refreshOverview() {
    if (pending) return;
    pending = true; refresh.disabled = true; refresh.textContent = "Refreshing…";
    const researchReady = refreshResearch();
    const accountingReady = checkAccounting();
    const names = ["account","equity_curve","automation","positions","allocations","champions"];
    const results = await Promise.allSettled(names.map(name=>dashboardFetch("/api/"+name)));
    const [account, curve, automation, positions, allocations, champions] = results.map(r=>r.status==="fulfilled"?r.value:null);
    renderMetrics(account,curve); renderOperations(account,automation); renderHoldings(account,positions); renderStrategies(allocations,positions,champions);
    const observed = account?.valuation?.observed_at;
    const stale = observed && Date.now()-new Date(observed).getTime()>5*60000;
    updated.textContent = (stale?"Older account snapshot · ":"Account snapshot · ")+ (observed?Dashboard.time(observed):"unavailable")+
      (results.some(r=>r.status==="rejected")?" · Some data unavailable. Refresh to retry.":" · Refreshes every minute while this tab is visible.");
    updated.classList.toggle("amber",Boolean(stale));
    pending = false; refresh.disabled = false; refresh.textContent = "Refresh";
    await Promise.all([researchReady,accountingReady]);
  }
  refresh.addEventListener("click",refreshOverview);
  root._asyncReady = refreshOverview();
  setInterval(()=>{if(root.classList.contains("active")&&!document.hidden)refreshOverview();},60000);
  return root;
}
