/* ============================================================
   live.js — Live tab (adapted from design; async-loaded data)
   ============================================================ */
function fmtUSD(n) { return "$" + Math.round(n).toLocaleString("en-US"); }

async function renderLive() {
  const root = h("section.tab", { id: "tab-live", role: "tabpanel" },
    h("div.section",
      h("div.section-head",
        h("p.eyebrow", "Live · updated just now"),
        h("h1.h-lg", "Today at a glance"),
        h("p.lead", "A quick read on what the bot is worth, how it's allocated, and whether each sub-strategy is healthy.")),
      h("div", { id: "live-body" }, h("p.dim", { style: "padding:24px 0" }, "Loading live data…")))
  );

  // After the shell is created, fetch and render. Charts are wired through
  // root._charts to follow the design's lazy chart-build pattern.
  root._charts = () => {};   // overwritten after async load
  root._asyncReady = (async () => {
    let LIVE;
    try { LIVE = await loadLive(); }
    catch (e) {
      $("#live-body", root).innerHTML = "";
      $("#live-body", root).appendChild(h("div.callout.warn",
        h("span.ci", "⚠"), h("span", "Failed to load live data: " + e.message)));
      return;
    }
    const body = $("#live-body", root);
    body.innerHTML = "";

    const s = LIVE.stats;

    /* ---- hero stat strip ---- */
    const strip = h("div.statstrip",
      h("div.stat",
        h("div.stat-label", "Broker account value"),
        h("div.stat-value", fmtUSD(s.portfolio)),
        h("div.stat-sub" + (s.portfolioPos ? ".pos" : ".neg"), s.portfolioSub)),
      h("div.stat",
        h("div.stat-label", "Total return"),
        h("div.stat-value" + (s.totalReturn >= 0 ? ".pos" : ".neg"),
          (s.totalReturn >= 0 ? "+" : "") + s.totalReturn + "%"),
        h("div.stat-sub", "since inception")),
      h("div.stat",
        h("div.stat-label", "Today"),
        h("div.stat-value" + (s.todayPos ? ".pos" : ".neg"),
          (s.todayUsd >= 0 ? "+$" : "−$") + Math.abs(s.todayUsd).toFixed(0)),
        h("div.stat-sub" + (s.todayPos ? ".pos" : ".neg"),
          (s.todayPct >= 0 ? "+" : "") + s.todayPct + "%")),
      h("div.stat",
        h("div.stat-label", "Cash"),
        h("div.stat-value", fmtUSD(s.cash)),
        h("div.stat-sub", s.cashPct + "% idle")),
      h("div.stat",
        h("div.stat-label", "Invested"),
        h("div.stat-value", fmtUSD(s.invested)),
        h("div.stat-sub", s.investedPct + "% deployed"))
    );
    const valuation = s.valuation;
    const valuationNote = valuation ? h("div.callout.info.mt-16",
      h("span.ci", "ℹ️"),
      h("span",
        h("b", valuation.label + ": "),
        "equity includes broker-credited income. No estimated yield is added. Observed ",
        new Date(valuation.observed_at).toLocaleString(), ". Alpaca broker mark: ",
        h("b", fmtUSD(valuation.broker_mark)), ". Price-mark adjustment: ",
        h("b", { class: valuation.fresh_price_adjustment >= 0 ? "pos" : "neg" },
          (valuation.fresh_price_adjustment >= 0 ? "+" : "−") +
          "$" + Math.abs(valuation.fresh_price_adjustment).toFixed(0)),
        "; model adjustment: $0", ".")) : null;

    /* ---- strategy control tower: every live, parked, and shadow system ---- */
    const tower = LIVE.controlTower || { production: [], parked: [], shadows: [], intelligence: [], summary: {} };
    const towerStatusKind = status => ({
      RUNNING: "neutral", HEALTHY: "healthy", MONITORING: "healthy",
      MEASURING: "neutral", OBSERVING: "neutral", STARTING: "neutral",
      VIRTUAL_POSITION_OPEN: "decaying", DECAYING: "decaying",
      DISABLED: "tripped", TRIPPED: "tripped", FAST_TRIPPED: "tripped",
      KILLED: "tripped", HALTED_TODAY: "tripped", STATE_ERROR: "tripped",
    })[status] || "neutral";
    const towerTime = value => {
      if (!value) return "—";
      const parsed = new Date(value);
      return isNaN(parsed) ? value : parsed.toLocaleString("en-US", {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
      });
    };
    const towerMoney = value => {
      if (value == null) return h("span.tower-empty", "Not marked yet");
      const n = Number(value || 0);
      return h("span.tower-money." + (n > 0 ? "pos" : n < 0 ? "neg" : "flat"),
        (n >= 0 ? "+" : "−") + "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 }));
    };
    const towerRow = row => h("div.tower-row." + row.lane.toLowerCase().replaceAll(" ", "-"),
      h("div.tower-strategy",
        h("div.tower-name", row.name),
        h("div.tower-meta", row.lane, " · ", row.activity)),
      h("div.tower-status", badge(String(row.status).replaceAll("_", " "), towerStatusKind(row.status))),
      h("div.tower-capital",
        h("div.tower-cell-label", "Capital"),
        row.capital == null
          ? h("div.tower-empty", "Portfolio-wide")
          : h("div",
              h("div.tower-number", fmtUSD(row.capital)),
              h("div.tower-bar", h("span", { style: `width:${Math.min(100, Math.max(0, row.capitalPct * 100))}%` })),
              h("div.tower-small", `${(row.capitalPct * 100).toFixed(1)}% ${row.lane === "SHADOW" ? "virtual" : "of account"}`))),
      h("div.tower-pnl",
        h("div.tower-cell-label", "P/L"),
        towerMoney(row.pnl),
        row.riskContribution == null ? null : h("div.tower-small", `${(Number(row.riskContribution) * 100).toFixed(1)}% of measured risk`)),
      h("div.tower-now", h("div.tower-cell-label", "Now"), row.signal),
      h("div.tower-next", h("div.tower-cell-label", "Next"), row.next,
        row.lastActivity ? h("div.tower-small", "Last activity " + towerTime(row.lastActivity)) : null));
    const towerLane = (title, caption, rows, kind) => h("div.tower-lane." + kind,
      h("div.tower-lane-head", h("div", h("div.tower-lane-title", title), h("div.tower-lane-caption", caption))),
      h("div.tower-columns",
        h("span", "Strategy"), h("span", "State"), h("span", "Capital"),
        h("span", "P/L"), h("span", "What it sees"), h("span", "What happens next")),
      rows.map(towerRow));
    const summary = tower.summary || {};
    const nextCycleText = summary.nextCycle
      ? `${summary.nextCycle.phase} · ${towerTime(summary.nextCycle.at)}` : "Schedule unavailable";
    const summaryKind = towerStatusKind(summary.automation);
    const eventCandidates = (((LIVE.eventOptionsShadow || {}).plan || {}).candidate_details || []);
    const rejectedEvents = eventCandidates.filter(row => !row.promoted);
    const rejectionText = rejectedEvents.length ? rejectedEvents.map(row => {
      const reason = (row.reasons || [])[0] || "did not clear every gate";
      return `${row.symbol}: ${reason}`;
    }).join(" · ") : null;
    const towerCard = h("div.control-tower.card",
      h("div.tower-summary",
        h("div.tower-summary-cell", h("div.tower-summary-k", "Automation"),
          h("div.tower-summary-v", badge(summary.automation || "UNKNOWN", summaryKind)),
          h("div.tower-summary-sub", nextCycleText)),
        h("div.tower-summary-cell", h("div.tower-summary-k", "Governance"),
          h("div.tower-summary-v", badge(summary.governance || "UNKNOWN", towerStatusKind(summary.governance === "OK" ? "HEALTHY" : summary.governance))),
          h("div.tower-summary-sub", "Most-conservative source wins")),
        h("div.tower-summary-cell", h("div.tower-summary-k", "Live allocation"),
          h("div.tower-summary-n", String(summary.enabledProduction || 0)),
          h("div.tower-summary-sub", "enabled capital routes")),
        h("div.tower-summary-cell", h("div.tower-summary-k", "Parked"),
          h("div.tower-summary-n", String(summary.parked || 0)),
          h("div.tower-summary-sub", "visible, zero authority")),
        h("div.tower-summary-cell", h("div.tower-summary-k", "Shadow lab"),
          h("div.tower-summary-n", String(summary.shadows || 0)),
          h("div.tower-summary-sub", "observers, no orders"))),
      h("div.tower-flow",
        h("span.flow-node", "Market + broker data"), h("span.flow-arrow", "→"),
        h("span.flow-node", "Quant signals + gated AI proposal"), h("span.flow-arrow", "→"),
        h("span.flow-node", "Governance + risk guard"), h("span.flow-arrow", "→"),
        h("span.flow-node.live", "Paper execution"),
        h("span.flow-branch", "Shadow lab reads the same evidence but has no path to orders")),
      summary.topRisk && Math.abs(summary.topRisk.riskContribution) >= .50
        ? h("div.tower-alert",
            h("span", "⚠"),
            h("span", h("b", `${summary.topRisk.name} is the dominant risk driver. `),
              `It represents ${(summary.topRisk.capitalPct * 100).toFixed(1)}% of capital but ${(summary.topRisk.riskContribution * 100).toFixed(1)}% of measured portfolio risk.`))
        : null,
      towerLane("Live capital", "These sleeves can place paper orders through the unified portfolio process.", tower.production, "live"),
      towerLane("Parked strategies", "Still monitored and revalidated, but deliberately receive no capital.", tower.parked, "parked"),
      towerLane("Shadow laboratory", "Forward evidence only. These systems cannot reach the broker order path.", [...tower.shadows, ...tower.intelligence], "shadow"),
      rejectionText ? h("div.tower-evidence", h("b", "Latest event scan: "), rejectionText) : null);

    /* ---- strategy health sleeves ---- */
    const sleeveCards = LIVE.sleeves.map(sl => {
      const kindMap = { HEALTHY: "healthy", DECAYING: "decaying", TRIPPED: "tripped",
                        DISABLED: "tripped", FAST_TRIPPED: "tripped" };
      const k = kindMap[sl.status] || "healthy";
      const noteIcon = { good: "✓", warn: "⚠", bad: "⏸" }[sl.note.kind];
      const nameTermKey = { "QLD-regime": "qldregime", "PEAD equity": "pead",
                            "Crypto trend": null, "ETF mean-reversion": null,
                            "Idle router / managed futures": null }[sl.name];
      return h("div.card.sleeve." + k,
        h("div.sleeve-top",
          h("div.sleeve-name", nameTermKey ? term(sl.name, nameTermKey) : sl.name),
          badge(sl.status, k)),
        h("div.row.mt-8", { style: "gap:8px" },
          h("span.weight-pill", sl.pill != null ? sl.pill : "weight " + sl.weight + "%")),
        h("div.sleeve-body", sl.body),
        h("div.sleeve-note." + sl.note.kind, h("span", noteIcon), h("span", sl.note.text))
      );
    });
    const govConflicts = LIVE.governance?.conflicts || [];
    const governanceNote = govConflicts.length ? h("div.callout.warn.mt-16",
      h("span.ci", "⚠"),
      h("span",
        h("b", "Governance resolved conflicting reports safely. "),
        govConflicts.map(c => `${({crypto: "Crypto", pead: "PEAD", regime: "QLD regime"})[c.sleeve] || c.sleeve} → ${c.enforced}`).join(" · "),
        ". The enforced verdict above is the one allowed to control capital.")) : null;

    /* ---- portfolio value chart (or "still building" note) ---- */
    let pvCard, ddCard, pvCanvas, ddCanvas;
    if (LIVE.pvEmpty) {
      pvCard = h("div.card.card-pad",
        sectionHead("Portfolio value", "Account total over time. The dotted line marks your $100k starting point."),
        h("div.callout.info.mt-16", h("span.ci", "⏳"),
          h("span", "Portfolio history is still building — the chart fills in as more daily bars arrive from Alpaca.")));
      ddCard = h("div.card.card-pad",
        sectionHead("Distance from high", "How far below the recent peak you are right now — a tiny drawdown."),
        h("div.callout.info.mt-16", h("span.ci", "⏳"), h("span", "Waiting on more bars.")));
    } else {
      pvCanvas = h("canvas");
      pvCard = h("div.card.chart-card",
        sectionHead("Portfolio value at completed sessions", "Broker daily history. Today's intraday value appears at the top; the dotted line marks the $100k funding baseline."),
        h("div.chart-box", { style: "height:240px" }, pvCanvas));
      ddCanvas = h("canvas");
      const lastDd = LIVE.dd[LIVE.dd.length - 1] || 0;
      // Strict: only claim "at all-time high" when the drawdown is actually 0.
      // The old -0.05% band showed "✓ At all-time high" while the account was
      // measurably below its peak — an honesty bug, not a rounding nicety.
      const atATH = lastDd >= 0;
      ddCard = h("div.card.chart-card",
        sectionHead("Drawdown at completed sessions",
          atATH
            ? h("p.caption",
                h("span.pos", "✓ At all-time high. "),
                "This chart tracks how far below the peak we've been over time — at 0% the bot has never been worth more than it is right now.")
            : h("p.caption",
                "Decline from the historical closing peak: ", term("drawdown", "drawdown"),
                ` ${lastDd.toFixed(2)}% as of ${LIVE.pvDates[LIVE.pvDates.length - 1]}. Today's mark is separate.`)),
        h("div.chart-box", { style: "height:170px" }, ddCanvas));
    }

    /* ---- deployment + ROI trajectory vs passive benchmarks ---- */
    const vs = LIVE.vsIndices || { dates: [], bot: [], spy: [], qqq: [] };
    let vsCard;
    if (vs.dates.length < 2) {
      vsCard = h("div.card.card-pad",
        sectionHead("Deployment & ROI trajectory",
          "Actual cumulative return from the paper deployment date, compared with passive benchmarks."),
        h("div.callout.info.mt-16", h("span.ci", "⏳"),
          h("span", "Tracking starts on the account's first live trading day. The bot and both indices will begin together at 0% ROI.")));
    } else {
      const vsCanvas = h("canvas");
      const last = (arr) => { for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return arr[i]; return null; };
      const lb = last(vs.bot), ls = last(vs.spy), lq = last(vs.qqq);
      const pct = (a, b) => (a != null && b != null) ? (a - b).toFixed(2) : null;
      const vsSpy = pct(lb, ls), vsQqq = pct(lb, lq);
      const botRoi = vs.bot.map(v => v == null ? null : v - 100);
      const spyRoi = vs.spy.map(v => v == null ? null : v - 100);
      const qqqRoi = vs.qqq.map(v => v == null ? null : v - 100);

      const launchIso = vs.launch_date || vs.dates[0];
      const launchDate = new Date(`${launchIso}T12:00:00`);
      const lastDate = new Date(`${vs.dates[vs.dates.length - 1]}T12:00:00`);
      const calendarDays = Math.max(1, Math.round((lastDate - launchDate) / 86400000));
      const sessions = botRoi.filter(v => v != null).length;
      const currentRoi = lb == null ? null : lb - 100;
      const annualized = lb != null && lb > 0
        ? (Math.pow(lb / 100, 365 / calendarDays) - 1) * 100 : null;

      // Fit only the latest 20 observed sessions and draw the fitted segment
      // only over those dates. It is descriptive, never extrapolated beyond
      // today, and therefore cannot masquerade as an expected-return forecast.
      const observed = botRoi.map((value, index) => ({ value, index }))
        .filter(row => row.value != null).slice(-20);
      let trajectoryPerWeek = null;
      const trajectory = Array(botRoi.length).fill(null);
      if (observed.length >= 2) {
        const n = observed.length;
        const meanX = (n - 1) / 2;
        const meanY = observed.reduce((sum, row) => sum + row.value, 0) / n;
        const numerator = observed.reduce(
          (sum, row, i) => sum + (i - meanX) * (row.value - meanY), 0);
        const denominator = observed.reduce(
          (sum, _row, i) => sum + Math.pow(i - meanX, 2), 0);
        const slope = denominator ? numerator / denominator : 0;
        const intercept = meanY - slope * meanX;
        observed.forEach((row, i) => { trajectory[row.index] = intercept + slope * i; });
        trajectoryPerWeek = slope * 5;
      }

      const deployedLabel = launchDate.toLocaleDateString("en-US", {
        month: "short", day: "numeric", year: "numeric",
      });
      const trajectoryLabel = trajectoryPerWeek == null ? "building"
        : `${trajectoryPerWeek >= 0 ? "+" : ""}${trajectoryPerWeek.toFixed(2)} pts/week`;
      const cap = (lb != null && ls != null && lq != null)
        ? h("p.caption",
            `Matched closing observations through ${vs.dates[vs.dates.length - 1]}. Since the first chart observation: `,
            h("b", (lb - 100).toFixed(2) + "% ROI"),
            " vs S&P ",
            h("span", { class: (vsSpy >= 0 ? "pos" : "neg") }, (vsSpy >= 0 ? "+" : "") + vsSpy + "% pts"),
            ", vs NASDAQ ",
            h("span", { class: (vsQqq >= 0 ? "pos" : "neg") }, (vsQqq >= 0 ? "+" : "") + vsQqq + "% pts"),
            ". The funded-account return at the top also includes today's intraday move.")
        : h("p.caption", "Cumulative ROI since deployment, with every series starting at 0%.");
      vsCard = h("div.card.chart-card",
        sectionHead("Deployment & ROI trajectory", cap),
        h("div.kv-strip.mt-16",
          h("div.kv", h("div.kv-k", "Deployed"),
            h("div.kv-v", deployedLabel),
            h("div.kv-cmp", `${calendarDays} calendar days · ${sessions} market sessions`)),
          h("div.kv", h("div.kv-k", "Completed-session ROI"),
            h("div.kv-v", { class: currentRoi >= 0 ? "pos" : "neg" },
              currentRoi == null ? "—" : `${currentRoi >= 0 ? "+" : ""}${currentRoi.toFixed(2)}%`),
            h("div.kv-cmp", `from first chart observation · ${vs.dates[vs.dates.length - 1]}`)),
          h("div.kv", h("div.kv-k", "Annualized pace"),
            h("div.kv-v", { class: annualized >= 0 ? "pos" : "neg" },
              annualized == null ? "—" : `${annualized >= 0 ? "+" : ""}${annualized.toFixed(1)}%`),
            h("div.kv-cmp", "compounded math · not a forecast")),
          h("div.kv", h("div.kv-k", "Recent trajectory"),
            h("div.kv-v", { class: trajectoryPerWeek >= 0 ? "pos" : "neg" }, trajectoryLabel),
            h("div.kv-cmp", "linear fit over latest 20 sessions"))),
        h("div.chart-box", { style: "height:280px" }, vsCanvas),
        legend([
          { label: "This bot",                color: cssColor("var(--pos-line)") },
          { label: "Just hold QQQ (NASDAQ-100)", color: cssColor("var(--c-qqq)"),    dash: true },
          { label: "Just hold SPY (S&P 500)",    color: cssColor("var(--c-regime)"), dash: true },
          { label: "Observed 20-session trend (not forecast)", color: cssColor("var(--accent)"), dash: true },
        ]));
      // Defer chart-draw until card is in DOM
      setTimeout(() => {
        const labels = vs.dates.map(d => new Date(d));
        const series = [
          { label: "This bot",                       data: botRoi, color: "var(--pos-line)", width: 2.5 },
          { label: "Just hold QQQ (NASDAQ-100)",     data: qqqRoi, color: "var(--c-qqq)",    width: 1.8, dash: [6, 5] },
          { label: "Just hold SPY (S&P 500)",        data: spyRoi, color: "var(--c-regime)", width: 1.8, dash: [6, 5] },
          { label: "Observed 20-session trend",      data: trajectory, color: "var(--accent)", width: 2.0, dash: [3, 4] },
        ];
        makeLineChart(vsCanvas, labels, series, {
          yfmt: v => `${v >= 0 ? "+" : ""}${v.toFixed(0)}%`,
          tfmt: v => v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`,
          titleFmt: d => d.toLocaleDateString("en-US", { month: "short", day: "numeric" }),
        });
      }, 0);
    }

    /* ---- dividend / interest income panel ---- */
    const inc = LIVE.income || {};
    const incCount = inc.count || 0;
    const incTotal = inc.total || 0;
    const incRecent = inc.recent || [];
    const bySymbol = inc.by_symbol || {};
    let incBody;
    if (incCount === 0) {
      incBody = h("div.callout.info", h("span", "No broker-credited income in the ledger yet. Headline equity and returns use broker values only."));
    } else {
      const symKvs = Object.entries(bySymbol).slice(0, 3).map(([sym, amt]) =>
        h("div.kv",
          h("div.kv-k", sym + " income"),
          h("div.kv-v.pos", "$" + amt.toFixed(2)),
          h("div.kv-cmp", "")));
      const recentRows = incRecent.map(r =>
        h("tr",
          h("td", { "data-label": "Date" }, h("span.t-num.t-muted", r.date)),
          h("td", { "data-label": "Type" }, h("span.t-sym", r.type)),
          h("td", { "data-label": "Symbol" }, h("span.t-sym", r.symbol || "—")),
          h("td", { "data-label": "Per share" }, h("span.t-num", r.per_share ? "$" + r.per_share.toFixed(4) : "—")),
          h("td", { "data-label": "Amount", class: "t-num pos" }, "$" + r.amount.toFixed(2))));
      incBody = h("div",
        h("div.kv-strip",
          h("div.kv",
            h("div.kv-k", "Total dividends + interest"),
            h("div.kv-v.pos", "$" + incTotal.toFixed(2)),
            h("div.kv-cmp", incCount + " credit" + (incCount === 1 ? "" : "s"))),
          ...symKvs),
        h("div.mt-16",
          h("table.tbl",
            h("thead", h("tr",
              h("th", "Date"), h("th", "Type"), h("th", "Symbol"),
              h("th", "Per share"), h("th", "Amount"))),
            h("tbody", ...recentRows))));
    }
    const incomeCard = h("div.card.card-pad",
      sectionHead("💰 Dividend & interest income",
        "Actual dividend and interest credits reported by the broker. These amounts are already included in account equity; no estimated yield is added."),
      h("div.mt-16", incBody)
    );

    /* ---- regime sleeve readout ---- */
    let regimeCard;
    if (!LIVE.regime) {
      regimeCard = h("div.card.card-pad",
        sectionHead("Regime engine", "Snapshot unavailable."),
        h("p.dim", "Could not fetch QQQ price."));
    } else {
      const rg = LIVE.regime;
      regimeCard = h("div.card.card-pad",
        sectionHead(h("span", "Regime engine — ", term("QLD", "qld"), " via ", term("QQQ", "qqq"), " 200-day average"),
          h("p.caption", "The bot holds the 2× Nasdaq fund only while the market trends up. Right now the trend is ",
            h("span", { class: "b " + (rg.on ? "pos" : "neg") }, rg.on ? "ON" : "OFF"), ".")),
        h("div.readout.mt-16",
          h("div.readout-cell",
            h("div.readout-k", "Trend status"),
            h("div.readout-v", h("span", { class: rg.on ? "pos" : "neg" }, "● "), rg.state),
            h("div.readout-k", { style: "margin-top:4px" }, rg.streak)),
          h("div.readout-cell",
            h("div.readout-k", "QQQ close"),
            h("div.readout-v", rg.qqqClose),
            h("div.readout-k", { style: "margin-top:4px" }, rg.qqqDate)),
          h("div.readout-cell",
            h("div.readout-k", term("200-day average", "sma200")),
            h("div.readout-v", rg.sma200),
            h("div.readout-k", { class: rg.on ? "pos" : "neg", style: "margin-top:4px" },
              rg.vsFilter + (rg.on ? " above" : " below") + " the line")),
          h("div.readout-cell",
            h("div.readout-k", "QLD target"),
            h("div.readout-v", rg.qldTarget, h("small", " (" + rg.qldTargetPct + ")")),
            h("div.readout-k", { style: "margin-top:4px" }, "currently holding " + rg.qldPos))
        ),
        rg.needsBuy ? h("div.callout.warn.mt-16",
          h("span.ci", "⏱"),
          h("span", "Trend is ON but QLD isn't bought yet — the next scheduled run (14:15 UTC, weekdays) will place the buy.")) : null
      );
    }

    /* ---- allocation bars ---- */
    const maxUsd = Math.max(...LIVE.alloc.map(a => a.usd), 1);
    // Bars under ~28% of the track width can't fit the "$xx,xxx · NN%" label
    // inside without truncating. For those rows we render the label OUTSIDE
    // the bar (in the empty track to its right) using --text-2 color.
    const LABEL_FIT_PCT = 28;
    const allocRows = LIVE.alloc.map(a => {
      const pct = a.usd === 0 ? 0 : (a.usd / maxUsd) * 100;
      const termKey = { BIL: "bil", QQQ: "qqq", DOGEUSD: "doge", SPY: "spy", QLD: "qld" }[a.name];
      const labelText = fmtUSD(a.usd) + "  ·  " + a.pct + "%";
      const fitsInside = pct >= LABEL_FIT_PCT && a.kind !== "empty";
      const barWidth = Math.max(pct, a.kind === "empty" ? 0 : 8);
      return h("div.allocbar-row",
        h("div.allocbar-name", termKey ? term(a.name, termKey) : a.name),
        h("div.allocbar-track",
          h("div.allocbar-fill." + a.kind, { style: `width:${barWidth}%` },
            a.usd > 0 && fitsInside ? h("span.lab", labelText) : null),
          // External label placed to the right of the bar fill
          a.usd > 0 && !fitsInside
            ? h("div.allocbar-lab-out", { style: `left:${barWidth}%` }, labelText)
            : null)
      );
    });
    const allocCard = h("div.card.card-pad",
      sectionHead("Where the money is", "How today's portfolio splits across holdings. BIL is the cash-like Treasury parking spot."),
      h("div.mt-16", allocRows)
    );

    /* ---- positions + orders ---- */
    const posTable = buildTable([
      { h: "Symbol", cell: r => h("span.t-sym", r.sym) },
      { h: "Quantity", cell: r => h("span.t-num.t-muted", r.qty) },
      { h: "Avg entry", cell: r => h("span.t-num", r.entry) },
      { h: "Price", cell: r => h("span.t-num", r.price) },
      { h: "Market value", cell: r => h("span.t-num", r.mv) },
      { h: "Unrealized P&L", cell: r => h("span.t-num." + r.plc, r.pl) },
      { h: "Income", cell: r => r.income == null
          ? h("span.t-num.t-muted", "—")
          : h("span.t-num.pos", "+$" + r.income.toFixed(2)) },
      { h: "Net P&L", cell: r => h("span.t-num." + r.netCls, r.netStr + " (" + r.netPct + ")") },
    ], LIVE.positions);
    const posCard = h("div.card.card-pad",
      sectionHead("Open positions", "What you currently hold and whether each is up or down versus where you bought."),
      h("div.mt-16", posTable.length === 0 || !LIVE.positions.length
        ? h("p.dim", "No open positions.") : posTable)
    );

    const orderTable = buildTable([
      { h: "When",     cell: r => h("span.t-num.t-muted", r.t) },
      { h: "Symbol",   cell: r => h("span.t-sym", r.sym) },
      { h: "Side",     cell: r => h("span", { class: r.side === "BUY" ? "side-buy" : "side-sell" }, r.side) },
      { h: "Quantity", cell: r => h("span.t-num", r.qty) },
      { h: "Price",    cell: r => h("span.t-num.t-muted", r.px) },
      { h: "P/L",      cell: r => r.pnl == null
                                    ? h("span.t-num.t-muted", "—")
                                    : h("span.t-num", { class: r.pnlClass },
                                        r.pnl + " ",
                                        h("span.t-muted", { style: "font-size:11.5px" }, "(" + r.pnlPct + ")")) },
      { h: "Status",   cell: r => h("span.t-status." + r.st, r.st) },
    ], LIVE.orders);
    const orderCard = h("div.card.card-pad",
      sectionHead("Recent orders", "Broker order history. A cancellation does not establish that a replacement filled; partial fills remain in the reconciled ledger."),
      h("div.mt-16", LIVE.orders.length ? orderTable : h("p.dim", "No orders yet."))
    );

    /* ---- sleeve P&L + component risk attribution ---- */
    const sleeveName = n => ({
      etf: "ETF mean-reversion", regime: "QLD regime",
      managed_futures: "Managed futures", cash_park: "BIL cash park",
      nasdaq_router: "QQQ beta from routing",
      crypto: "Crypto trend", pead: "PEAD", unowned: "Unowned",
    })[n] || n;
    const moneyCell = v => {
      const n = Number(v || 0);
      return h("span.t-num." + (n > 0 ? "pos" : n < 0 ? "neg" : "flat"),
        (n >= 0 ? "+" : "−") + "$" + Math.abs(n).toFixed(0));
    };
    const attrRows = (LIVE.sleeveAttribution?.rows || []).map(r => ({ ...r, display: sleeveName(r.sleeve) }));
    const attrTable = buildTable([
      { h: "Sleeve", cell: r => h("span.t-sym", r.display) },
      { h: "Capital", cell: r => h("span.t-num", (Number(r.weight) * 100).toFixed(1) + "%") },
      { h: "Realized", cell: r => moneyCell(r.realized_pnl) },
      { h: "Open P&L", cell: r => moneyCell(r.unrealized_pnl) },
      { h: "Income", cell: r => moneyCell(r.income) },
      { h: "Total", cell: r => moneyCell(r.total_pnl) },
      { h: "Risk contribution", cell: r => r.risk_contribution == null
          ? h("span.t-muted", "building…")
          : h("span.t-num", (Number(r.risk_contribution) * 100).toFixed(1) + "%") },
    ], attrRows);
    const attributionCard = h("div.card.card-pad",
      sectionHead("What is driving the result",
        "P&L is grouped by the sleeve that owns each position. Risk contribution uses recent covariance, so diversifiers can show a negative contribution."),
      h("div.mt-16", attrRows.length ? attrTable : h("p.dim", "No attributable activity yet.")));

    /* ---- AI counterfactual + execution quality + shadow QLD challengers ---- */
    const ai = LIVE.aiAttribution || {};
    const aiValue = Number(ai.estimated_value_add || 0);
    const aiAuthority = ai.authority || { mode: "shadow_advisory", reasons: [] };
    const aiCard = h("div.card.card-pad",
      sectionHead("AI overlay · measured, not assumed",
        "Compares the proposed AI target with the same quant decision without AI, marked at the next decision price and net of estimated turnover costs. Quant retains order authority until the evidence gate passes."),
      ai.decisions
        ? h("div.kv-strip.mt-16",
            h("div.kv", h("div.kv-k", "Decisions observed"), h("div.kv-v", String(ai.decisions)),
              h("div.kv-cmp", ai.altered_decisions + " targets changed")),
            h("div.kv", h("div.kv-k", "Marked intervals"), h("div.kv-v", String(ai.marked_intervals)),
              h("div.kv-cmp", "next-decision marks")),
            h("div.kv", h("div.kv-k", "Estimated AI value add"),
              h("div.kv-v", { class: aiValue >= 0 ? "pos" : "neg" },
                (aiValue >= 0 ? "+" : "−") + "$" + Math.abs(aiValue).toFixed(2)),
              h("div.kv-cmp", `net of ${Number(ai.assumed_cost_bps || 0).toFixed(1)} bp costs`)),
            h("div.kv", h("div.kv-k", "Order authority"),
              h("div.kv-v", aiAuthority.eligible ? "PROMOTED" : "SHADOW"),
              h("div.kv-cmp", aiAuthority.eligible
                ? "evidence gate passed"
                : (aiAuthority.reasons || []).join(" · "))))
        : h("div.callout.info.mt-16", h("span.ci", "⏳"),
            h("span", "Evidence collection begins with the next live open cycle.")));

    const execution = LIVE.executionQuality || {};
    const executionCost = Number(execution.estimated_slippage_usd || 0);
    const executionCard = h("div.card.card-pad",
      sectionHead("Execution quality",
        "Compares each future broker fill with the strategy price that triggered it. Positive slippage is a cost; negative is price improvement."),
      execution.measured_orders
        ? h("div.kv-strip.mt-16",
            h("div.kv", h("div.kv-k", "Measured fills"),
              h("div.kv-v", String(execution.measured_orders)),
              h("div.kv-cmp", `${execution.covered_filled_orders || 0} / ${execution.total_filled_orders || 0} fills covered; ${execution.uncovered_filled_orders || 0} missing references`)),
            h("div.kv", h("div.kv-k", "Reference turnover"),
              h("div.kv-v", fmtUSD(Number(execution.reference_notional || 0))),
              h("div.kv-cmp", `${execution.quote_covered_orders || 0} with venue quotes; diagnostic only`)),
            h("div.kv", h("div.kv-k", "Implementation shortfall"),
              h("div.kv-v", { class: executionCost <= 0 ? "pos" : "neg" },
                (executionCost >= 0 ? "+" : "−") + "$" + Math.abs(executionCost).toFixed(2)),
              h("div.kv-cmp", execution.slippage_bps == null
                ? "collecting"
                : `${Number(execution.slippage_bps).toFixed(2)} bps`)))
        : h("div.callout.info.mt-16", h("span.ci", "⏳"),
            h("span", "Capture begins with the next order submitted by the bot.")));

    const shadowVariants = Object.entries(LIVE.shadowRegime?.variants || {}).map(([name, row]) => ({
      name: ({ production_70: "Production 70%", cap_60: "60% cap", cap_50: "50% cap",
               stress_70: "Stress-aware 70%", risk_cap_25: "Risk-capped 25%" })[name] || name,
      ...row,
    }));
    const shadowTable = buildTable([
      { h: "Virtual portfolio", cell: r => h("span.t-sym", r.name) },
      { h: "Target", cell: r => h("span.t-num", (Number(r.target_weight) * 100).toFixed(1) + "%") },
      { h: "Virtual equity", cell: r => h("span.t-num", fmtUSD(Number(r.equity || 0))) },
      { h: "Total P/L", cell: r => moneyCell(r.total_pnl) },
      { h: "Drawdown", cell: r => h("span.t-num", (Number(r.drawdown_fraction || 0) * 100).toFixed(2) + "%") },
      { h: "Est. cost", cell: r => h("span.t-num.t-muted", "$" + Number(r.estimated_cost || 0).toFixed(2)) },
    ], shadowVariants);
    const shadowCard = h("div.card.card-pad",
      sectionHead("QLD allocation laboratory",
        "An isolated QLD sleeve diagnostic with modeled costs. It excludes the other sleeves and cannot establish whole-portfolio improvement. The frozen portfolio experiments above perform that comparison."),
      shadowVariants.length ? h("div.mt-16", shadowTable,
        h("p.dim", { style: "margin-top:10px" },
          `${LIVE.shadowRegime.measured_observations || 0} measured sessions · close-to-close marking`))
        : h("div.callout.info.mt-16", h("span.ci", "⏳"),
            h("span", "The first observation will be recorded by the next regime cycle.")));


    const lab = LIVE.portfolioLab || {};
    const rel = LIVE.release || {};
    const reconciliation = LIVE.reconciliation || {};
    const labRows = (lab.candidates || []).map(row => ({
      name: row.name.replaceAll("_", " "),
      sessions: row.forward?.observations || 0,
      forward: row.forward?.return,
      historical: row.historical?.return,
      drawdown: row.historical?.max_drawdown,
      capture: row.historical?.upside_capture,
      status: row.promotion?.status || row.status,
    }));
    const labTable = buildTable([
      {h:"Experiment", cell:r=>h("span.t-sym",r.name)},
      {h:"Forward sessions", cell:r=>String(r.sessions)},
      {h:"Forward return", cell:r=>r.sessions ? (r.forward*100).toFixed(2)+"%" : "Collecting"},
      {h:"Historical proxy", cell:r=>r.historical == null ? "Data needed" : (r.historical*100).toFixed(1)+"%"},
      {h:"Proxy drawdown", cell:r=>r.drawdown == null ? "—" : (r.drawdown*100).toFixed(1)+"%"},
      {h:"Upside capture", cell:r=>r.capture == null ? "—" : (r.capture*100).toFixed(0)+"%"},
      {h:"Status", cell:r=>r.status || "Awaiting data"},
    ],labRows);
    const labCard = h("div.card.card-pad",
      sectionHead("Whole-portfolio experiments",
        "The QLD alternatives include ETF positions, the idle router, Treasury reserves and modeled execution costs. Live allocations stay unchanged."),
      h("p.dim", lab.method || "Preparing the first frozen comparison."),
      h("div.callout.info", h("span",
        "Historical values are conditional research. ",
        (lab.parity?.blockers || []).join(" · "))),
      labRows.length ? h("div.mt-16",labTable) : h("p","Collecting initial observations."),
      h("p.dim", "Frozen " + (lab.frozen_at ? new Date(lab.frozen_at).toLocaleString() : "pending") +
        " · cohort " + (lab.cohort || "pending") + " · no candidate has order authority."),
      h("p", "Next: " + (lab.next_action || "Complete first research run.")));
    const operationalNote = h("div.card.card-pad",
      sectionHead("Deployment and protection",
        "Health comes from completed cycles. Broker quantities are reconciled against the full fill history."),
      h("p", "Release " + (rel.release_id || "unversioned") + " · deployed " +
        (rel.deployed_at ? new Date(rel.deployed_at).toLocaleString() : "not recorded")),
      h("p", "Holdings reconciliation: " + (reconciliation.status || "unknown") +
        " · " + (reconciliation.filled_orders || 0) + " filled orders checked"),
      h("p", (LIVE.automation?.issues || []).join(" · ") || "No overdue protection or scheduled cycles detected."),
      h("p.dim","Next cycle: " + (LIVE.automation?.next_cycle?.phase || "unknown") +
        " · " + (LIVE.automation?.next_cycle?.at ? new Date(LIVE.automation.next_cycle.at).toLocaleString() : "unknown")));

    const ev = LIVE.eventOptionsShadow || {};
    const evPlan = ev.plan || {};
    const evRisk = evPlan.risk || {};
    const evPnl = Number(ev.realized_pnl || 0);
    const evExec = ev.execution_quality || {};
    const evPromotion = ev.promotion_evidence || {};
    const evSurface = ev.surface_quality || {};
    const evCalendar = evPlan.calendar_health || {};
    const evProviders = Object.entries(evCalendar.provider_fact_counts || {})
      .map(([name, count]) => `${name} ${Number(count)}`).join(" · ");
    const evSourceIssues = [
      ...(evCalendar.source_errors || []),
      ...(evCalendar.source_warnings || []),
    ];
    const funnel = ev.rejection_funnel || {};
    const rejectionRows = Object.entries(funnel.rejection_classes || {}).map(([name,count]) => ({name,count}));
    const eventShadowCard = h("div.card.card-pad",
      sectionHead("Event-options discovery",
        "Observation only. It scans scheduled catalysts, asks a Featherless committee to classify evidence, replays prior events, checks the live options surface, and books virtual fills. It has no broker order authority."),
      ev.plan ? h("div.kv-strip.mt-16",
        h("div.kv", h("div.kv-k", "Current state"),
          h("div.kv-v", String(ev.status).replaceAll("_", " ").toLowerCase()),
          h("div.kv-cmp", `${evPlan.prequalified} of ${evPlan.candidates} candidates prequalified`)),
        h("div.kv", h("div.kv-k", "Virtual risk open"),
          h("div.kv-v", fmtUSD(Number(ev.capital_at_risk || 0))),
          h("div.kv-cmp", `${((Number(evRisk.per_event_risk_pct || 0)) * 100).toFixed(0)}% per event · ${((Number(evRisk.aggregate_risk_pct || 0)) * 100).toFixed(0)}% total cap`)),
        h("div.kv", h("div.kv-k", "Closed virtual P/L"),
          h("div.kv-v", { class: evPnl >= 0 ? "pos" : "neg" },
            (evPnl >= 0 ? "+" : "−") + "$" + Math.abs(evPnl).toFixed(2)),
          h("div.kv-cmp", `${ev.evidence_rows} audited rows · ${String(ev.evidence_chain).toLowerCase()}`)),
        h("div.kv", h("div.kv-k", "Plan history"),
          h("div.kv-v", String(Number(ev.plan_archive_count || 0))),
          h("div.kv-cmp", "sealed candidate-level snapshots")),
        h("div.kv", h("div.kv-k", "Execution capture"),
          h("div.kv-v", evExec.shrunk_capture == null ? "collecting" : `${(Number(evExec.shrunk_capture) * 100).toFixed(1)}%`),
          h("div.kv-cmp", `${Number(evExec.observations || 0)} fills · shrunk toward prior`)),
        h("div.kv", h("div.kv-k", "Promotion evidence"),
          h("div.kv-v", `${Number(evPromotion.independent_outcomes || 0)}/${Number(evPromotion.minimum_outcomes || 30)}`),
          h("div.kv-cmp", "independent, cost-adjusted outcomes")),
        h("div.kv", h("div.kv-k", "Surface integrity"),
          h("div.kv-v", String(evSurface.status || "awaiting").toLowerCase()),
          h("div.kv-cmp", `${Number(evSurface.crossable_violations || 0)} crossable violations`)),
        h("div.kv", h("div.kv-k", "Calendar data health"),
          h("div.kv-v", evSourceIssues.length ? "DEGRADED" : "HEALTHY"),
          h("div.kv-cmp", evSourceIssues.length
            ? evSourceIssues.join(" · ")
            : `${evProviders || "no candidates"} · ${Number(evCalendar.enriched_schedule_fact_count || 0)} news confirmations`)))
        : h("div.callout.info.mt-16", h("span.ci", "⏳"),
            h("span", "The first weekly discovery plan has not run yet.")),
      h("div.mt-16", sectionHead("Why candidates did not trade", funnel.interpretation || "Awaiting candidate evidence."),
        h("p", `Plan: ${ev.plan_freshness?.status || "unknown"} · ${funnel.unique_rejected_events || 0} unique rejected events · ${funnel.valid_entries || 0} valid virtual entries · ${funnel.completed_outcomes || 0} completed outcomes`),
        buildTable([
          {h:"Reason class",cell:r=>r.name.replaceAll("_"," ")},
          {h:"Events",cell:r=>String(r.count)}
        ],rejectionRows)));

    const pr = LIVE.portfolioRisk || {};
    const riskReady = pr.status === "MEASURING";
    const riskCard = h("div.card.card-pad",
      sectionHead("Portfolio tail risk",
        "Historical simulation applies today’s holdings to recent daily returns. This is a warning instrument, not an automatic hedge or sizing rule."),
      riskReady ? h("div.kv-strip.mt-16",
        h("div.kv", h("div.kv-k", "99% one-day VaR"),
          h("div.kv-v", fmtUSD(Number(pr.var_usd || 0))),
          h("div.kv-cmp", `${(Number(pr.var_fraction || 0) * 100).toFixed(2)}% of equity`)),
        h("div.kv", h("div.kv-k", "Expected shortfall"),
          h("div.kv-v", fmtUSD(Number(pr.expected_shortfall_usd || 0))),
          h("div.kv-cmp", "average beyond the 99% cutoff")),
        h("div.kv", h("div.kv-k", "Worst observed day"),
          h("div.kv-v", fmtUSD(Number(pr.worst_observed_usd || 0))),
          h("div.kv-cmp", `${(Number(pr.worst_observed_fraction || 0) * 100).toFixed(2)}% replayed loss`)),
        h("div.kv", h("div.kv-k", "Evidence window"),
          h("div.kv-v", String(pr.observations || 0)),
          h("div.kv-cmp", `${pr.tail_observations || 0} tail observations · ${pr.tail_reliability || "unverified"}`)))
        : h("div.callout.info.mt-16", h("span.ci", "⏳"),
            h("span", pr.reason || "Building enough aligned price history for the current holdings.")));

    /* ---- assemble ---- */
    body.appendChild(strip);
    if (valuationNote) body.appendChild(valuationNote);
    body.appendChild(h("div.section",operationalNote));
    body.appendChild(h("div.section",labCard));
    body.appendChild(h("div.section",
      sectionHead("Strategy control tower",
        "One view of every capital route, disabled strategy, AI measurement, and shadow challenger — including what each sees now and what it will do next.",
        "Bird’s-eye view"),
      towerCard));
    body.appendChild(h("div.section",
      sectionHead(h("span", "Strategy health"),
        h("p.caption", "The bot runs three ", term("sleeves", "sleeve"),
          " (ETF mean-reversion, QLD-regime, and the managed-futures idle router), plus a cash park. Crypto and PEAD are disabled pending validation. Each is graded and automatically dialed up or down, with idle capital routed by the regime-aware split router. Hover any underlined term for a plain-English definition."),
        "Self-checking"),
      governanceNote,
      h("div.grid.cols-3", sleeveCards)));
    body.appendChild(h("div.section", h("div.grid.cols-2", pvCard, ddCard)));
    body.appendChild(h("div.section", vsCard));
    body.appendChild(h("div.section", h("div.grid.cols-2-bias", regimeCard, allocCard)));
    body.appendChild(h("div.section", incomeCard));
    body.appendChild(h("div.section", attributionCard));
    body.appendChild(h("div.section", riskCard));
    body.appendChild(h("div.section", h("div.grid.cols-2", aiCard, executionCard)));
    body.appendChild(h("div.section", shadowCard));
    body.appendChild(h("div.section", eventShadowCard));
    body.appendChild(h("div.section", posCard));
    body.appendChild(h("div.section", orderCard));

    // build charts now that nodes are in the DOM
    if (pvCanvas) {
      makeAreaChart(pvCanvas, LIVE.pvDates, LIVE.pv, {
        color: cssColor("var(--pos-line)"), baseline: 100000,
        yfmt: v => (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + "k",
        tfmt: v => fmtUSD(v),
      });
    }
    if (ddCanvas) {
      const lastDdVal = LIVE.dd[LIVE.dd.length - 1] || 0;
      const atATHnow = lastDdVal >= 0;   // strict — match the caption's rule
      makeAreaChart(ddCanvas, LIVE.ddDates, LIVE.dd, {
        // Green when at ATH (drawdown effectively zero); red when meaningfully below peak.
        color: cssColor(atATHnow ? "var(--pos-line)" : "var(--neg)"),
        negative: true,
        yfmt: v => v.toFixed(2) + "%", tfmt: v => v.toFixed(3) + "%",
      });
    }
  })();

  return root;
}
