/* ============================================================
   data.js — fetches real data from our FastAPI /api endpoints
   and shapes it into LIVE / STRAT / CRASH consts the tab
   renderers consume. Also: the GLOSSARY for plain-English
   tooltip definitions.
   ============================================================ */

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok && r.status !== 503) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

const fmtNum = (v, d = 0) => Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const fmtUSDc = v => "$" + Math.round(v).toLocaleString("en-US");
const fmtPct  = (v, d = 1) => (v * 100).toFixed(d) + "%";
const signed  = s => (s && s[0] !== "-" && s !== "0" ? "+" + s : s);

/* ---------------- LIVE tab ---------------- */
async function loadLive() {
  const [acct, pos, ord, eq, reg, alloc, rev, act, inc, vsidx, champ,
         sleeveAttr, aiAttr, executionQuality, shadowRegime, eventShadow, portfolioRisk,
         researchAutopilot, automation, portfolioLab, release, reconciliation] = await Promise.all([
    getJSON("/api/account"),
    getJSON("/api/positions"),
    getJSON("/api/orders"),
    getJSON("/api/equity_curve"),
    getJSON("/api/regime"),
    getJSON("/api/allocations"),
    getJSON("/api/revalidation"),
    getJSON("/api/sleeve_activity"),
    getJSON("/api/income"),
    getJSON("/api/equity_vs_indices"),
    getJSON("/api/champions"),
    getJSON("/api/sleeve_attribution"),
    getJSON("/api/ai_attribution"),
    getJSON("/api/execution_quality"),
    getJSON("/api/shadow_regime"),
    getJSON("/api/event_options_shadow"),
    getJSON("/api/portfolio_risk"),
    getJSON("/api/research_autopilot"),
    getJSON("/api/automation"),
    getJSON("/api/portfolio_lab"),
    getJSON("/api/release"),
    getJSON("/api/reconciliation"),
  ]);

  const equity = acct.equity;
  const last_eq = acct.last_equity;
  const cash = acct.cash;
  // "Invested" must be computed from price-only equity (equity_raw): `equity`
  // includes simulated BIL income (cash-like), which otherwise shows up as
  // phantom "invested" capital and grows forever (~$2k/yr at ~$40k BIL).
  const invested = (acct.equity_raw != null ? acct.equity_raw : equity) - cash;
  const total_pl = equity - 100000;
  const day_pl = equity - last_eq;
  const allocW = (alloc && alloc.weights) || {};
  const verdicts = (alloc && alloc.verdicts) || {};
  const streaks = (alloc && alloc.streaks) || {};

  // sleeves: in design order. ETF mean-reversion first — it holds the gated
  // champions and does most of the trading, and was missing from this panel
  // entirely until 2026-08-04. PEAD retired 2026-06-26.
  const sleeveOrder = [
    { key: "etf", name: "ETF mean-reversion",
      bodyHealthy: "Trades short-term mean-reversion across the liquid ETF universe, but only on symbols whose strategy cleared the Deflated-Sharpe gate — the discount applied for how many configurations were searched. Symbols that fail the gate are not traded at all, and a champion that stops clearing it is retired and its position closed.",
      bodyDecaying: "Same rule. Fewer symbols are clearing the Deflated-Sharpe gate than before — the sleeve trades less as the evidence thins.",
      bodyTripped:  "No symbol currently clears the Deflated-Sharpe gate, so the sleeve is holding nothing. That is the design working, not a failure." },
    { key: "mf", name: "Idle router / managed futures",
      bodyHealthy: "Puts capital the other sleeves aren't using to work, based on the regime: in a bull (QQQ above its 200-day average) it splits idle cash 50/50 between QQQ and a DBMF+KMLM managed-futures blend; in a bear it moves all of it into managed futures as a hedge.",
      bodyDecaying: "Same rule. Managed-futures leg drifting — the blend is trend-following, so drawdowns are expected rather than a fault.",
      bodyTripped:  "Auto-paused — idle capital is sitting in the cash/BIL reserve." },
    { key: "regime", name: "QLD-regime",
      bodyHealthy: "Holds the 2× Nasdaq ETF when QQQ trades above its 200-day average; sits in cash otherwise. " +
                   "If the sleeve itself underperforms for 6 weeks straight, the adaptive policy pauses it and parks its share in the cash/BIL reserve.",
      bodyDecaying: "Same rule (QLD when trending, cash when not). Recent Sharpe drifting toward zero — if it stays negative for 6 weeks the slot is paused and parked in the cash/BIL reserve.",
      bodyTripped:  "Auto-paused — its share moved to the cash/BIL reserve until the sleeve's Sharpe recovers." },
    { key: "crypto", name: "Crypto trend",
      bodyHealthy: "Trend-follows the major coins via a Keltner+Donchian+MACD ensemble, vol-targeted at 20%. " +
                   "If the sleeve underperforms for 6 weeks straight, the adaptive policy pauses it — its share goes to the regime-aware idle router (QQQ/managed-futures split in a bull, all managed futures in a bear).",
      bodyDecaying: "Same rule. Trend signal weakening — if it stays negative for 6 weeks the slot is paused (share → idle router + cash reserve).",
      bodyTripped:  "Auto-paused — share routed through the regime-aware idle router until the trend signal returns and the streak resets." },
    { key: "pead", name: "PEAD equity",
      bodyHealthy: "Looks for post-earnings drift after a company reports a sufficiently strong surprise, then holds for a short, predefined window.",
      bodyDecaying: "The historical edge is weakening, so its allocation is reduced while evidence is rechecked.",
      bodyTripped:  "Retired from production. It remains visible so a disabled strategy can never disappear from operational oversight." },
    { key: "cash_park", name: "BIL cash park",
      bodyHealthy: "Sweeps the deliberate reserve into short-duration Treasury exposure after the risk-taking sleeves finish sizing.",
      bodyDecaying: "The reserve remains available, but its implementation is under review.",
      bodyTripped:  "Cash sweep is paused; unallocated funds remain as broker cash." },
  ];
  const liveTrades = (act && act.counts) || {};
  const lastFill   = (act && act.last_fill) || {};
  const currentSleeveWeights = Object.fromEntries(
    (((sleeveAttr && sleeveAttr.rows) || []).map(row => [row.sleeve, Number(row.weight || 0)])));
  // Hybrid policy metrics (if present): 60d cumret per sleeve and slow trip threshold.
  const hybrid = alloc && alloc.hybrid || null;
  const slowCumret = hybrid?.slow?.cumret || {};
  const fastCumret = hybrid?.fast?.cumret || {};
  const slowThresh = hybrid?.slow?.threshold ?? -0.03;
  const fastThresh = hybrid?.fast?.threshold ?? -0.12;
  // The ETF sleeve is NOT part of the adaptive weight policy — it has no entry
  // in sleeve_allocations.json. Its exposure comes from per-symbol vol-targeting
  // on whatever cleared the Deflated-Sharpe gate, so a "weight %" would be
  // invented. Show the gate result instead, which is the number that matters.
  const champList = (champ && champ.champions) || [];
  const champUniverse = (champ && champ.universe) || 0;
  const sleeves = sleeveOrder.map(s => {
    const verdictKey = s.key === "mf" ? "managed_futures" : s.key;
    let verdict = verdicts[verdictKey] || "HEALTHY";
    if (s.key === "etf") {
      verdict = !champ || !champ.available ? "TRIPPED"
              : champList.length === 0 ? "TRIPPED" : "HEALTHY";
    }
    const streak = streaks[s.key] || 0;
    const actualKey = s.key === "mf" ? "managed_futures" : s.key;
    const effectiveWeight = ["mf", "cash_park"].includes(s.key)
      ? (currentSleeveWeights[actualKey] ?? 0)
      : (allocW[s.key] ?? 0);
    const w = Math.round(effectiveWeight * 100);
    const pill = s.key === "etf"
      ? `${champList.length} of ${champUniverse} symbols gated in`
      : (["mf", "cash_park"].includes(s.key) ? "deployed " : "weight ") + w + "%";
    const trades = liveTrades[s.key] || 0;
    const body = verdict === "TRIPPED" ? s.bodyTripped :
                 verdict === "DECAYING" ? s.bodyDecaying : s.bodyHealthy;
    // Build a "distance to trip" indicator for the sleeve card. Honest about
    // sleeves that pass the trip rule but are still nearby (e.g. crypto -1.9%
    // — still HEALTHY but only 1.1pp from the trip threshold).
    const cr = slowCumret[s.key];
    const fr = fastCumret[s.key];
    let distance = "";
    if (cr !== undefined) {
      const gapSlow = (cr - slowThresh) * 100;   // positive = above threshold
      const crPct = (cr * 100).toFixed(1) + "%";
      if (gapSlow > 2.0) {
        distance = `60d return ${cr >= 0 ? "+" : ""}${crPct} · well clear of trip threshold (-3%)`;
      } else if (gapSlow > 0) {
        distance = `⚠ 60d return ${cr >= 0 ? "+" : ""}${crPct} · only ${gapSlow.toFixed(1)}pp from trip threshold (-3%)`;
      } else {
        distance = `60d return ${cr >= 0 ? "+" : ""}${crPct} · below trip threshold of -3%`;
      }
    }
    let note;
    if (verdict === "DISABLED") {
      note = { kind: "bad", text: "Disabled — this sleeve is switched off and holds nothing." };
    } else if (s.key === "etf" && verdict === "TRIPPED") {
      note = { kind: "bad", text: champ && champ.available
        ? `No symbol clears the Deflated-Sharpe gate right now — sleeve holds nothing.`
        : `Champion store unavailable — sleeve is idle.` };
    } else if (s.key === "etf") {
      const names = champList.map(c => c.symbol).join(", ");
      const liveBit = trades === 0 ? "0 trades since deploy" : `${trades} live trades to date`;
      note = { kind: "good", text: `Trading ${names} · ${liveBit}` };
    } else if (verdict === "FAST_TRIPPED") {
      const fastTxt = fr !== undefined ? `20d return ${(fr*100).toFixed(1)}% (< ${fastThresh*100}% trigger)` : "";
      note = { kind: "bad", text: `🚨 Fast trigger fired — moved to cash. ${fastTxt}` };
    } else if (verdict === "TRIPPED") {
      note = { kind: "bad", text: `Auto-paused — ${distance}, streak ${streak}/6 weeks` };
    } else if (verdict === "DECAYING") {
      note = { kind: "warn", text: `Streak ${streak}/6 weeks of losses; ${distance}` };
    } else {
      const liveBit = trades === 0
        ? "live bot still warming up (0 trades since deploy)"
        : `${trades} live trades to date`;
      const distSuffix = distance ? "  ·  " + distance : "";
      note = { kind: "good", text: `Running normally · ${liveBit}${distSuffix}` };
    }
    return {
      key: s.key, name: s.name, status: verdict, weight: w, pill, body, note,
      trades, lastFill: lastFill[s.key] || null,
    };
  });

  // equity curve: prefer real Alpaca data; show note if empty
  const pvDates = eq.curve.map(d => d.date.slice(5).replace("-", "/"));
  const pv      = eq.curve.map(d => d.equity);
  // drawdown
  let peak = pv[0] || 100000;
  const dd = pv.map(v => { peak = Math.max(peak, v); return ((v / peak - 1) * 100); });
  const ddDates = pvDates;

  // regime readout
  const qldPos = (pos.positions.find(p => p.symbol === "QLD") || {}).market_value || 0;
  // Guards: server-side returns null if upstream data was NaN (yfinance returning
  // partial bars for an in-progress session). Don't crash on .toFixed() / Math.round.
  const _num = (v, fb = "—") => (v == null || !isFinite(v)) ? fb : v;
  const regimeBlock = reg && reg.regime_on !== undefined ? {
    state:        reg.regime_on ? "ON (hold QLD)" : "OFF (hold cash)",
    streak:       `${reg.streak_days}-day streak`,
    qqqClose:     reg.close == null ? "—" : "$" + Math.round(reg.close).toLocaleString(),
    qqqDate:      reg.as_of,
    sma200:       reg.sma == null ? "—" : "$" + Math.round(reg.sma).toLocaleString(),
    vsFilter:     reg.distance_pct == null ? "—" : (reg.distance_pct >= 0 ? "+" : "") + reg.distance_pct.toFixed(1) + "%",
    qldPos:       fmtUSDc(qldPos),
    qldTarget:    fmtUSDc(equity * (allocW.regime || 0)),
    qldTargetPct: Math.round((allocW.regime || 0) * 100) + "%",
    on: !!reg.regime_on,
    needsBuy: reg.regime_on && qldPos < 1 && (allocW.regime || 0) > 0,
  } : null;

  // allocation rows: largest first, plus a "Cash" row inferred from cash.
  // Filter dust (market_value < $1) — these are sub-cent fractional remnants
  // Alpaca can leave after an imperfect liquidation (e.g. crypto qty 4e-7).
  const DUST_THRESHOLD = 1.0;
  const allocRows = [];
  const allRows = pos.positions
    .filter(p => Math.abs(p.market_value) >= DUST_THRESHOLD)
    .slice().sort((a, b) => b.market_value - a.market_value);
  allRows.push({ symbol: "Cash", market_value: cash, _isCash: true });
  const maxVal = Math.max(...allRows.map(r => r.market_value), 1);
  for (const r of allRows) {
    const pct = equity ? Math.round((r.market_value / equity) * 100) : 0;
    let kind = "equity";
    if (r._isCash) kind = "cash";
    else if (r.market_value <= 1) kind = "empty";
    allocRows.push({
      name: r.symbol.replace("/", ""),
      usd:  r.market_value,
      pct,
      kind,
      pctOfMax: r.market_value / maxVal,
    });
  }
  // dedupe: the loop above pushed twice for cash; rebuild cleanly
  const allocClean = [];
  const seen = new Set();
  for (const a of allocRows) {
    if (seen.has(a.name)) continue;
    seen.add(a.name);
    allocClean.push(a);
  }

  // positions table — same dust filter as the allocation bar
  const positions = pos.positions.filter(p => Math.abs(p.market_value) >= DUST_THRESHOLD).map(p => ({
    sym:   p.symbol.replace("/", ""),
    qty:   Number(p.qty).toFixed(4),
    entry: "$" + Number(p.avg_entry).toFixed(p.avg_entry > 100 ? 0 : 2),
    price: "$" + Number(p.price).toFixed(p.price > 100 ? 0 : 2),
    mv:    fmtUSDc(p.market_value),
    mvRaw: Number(p.market_value),
    pl:    (p.unrealized_pl >= 0 ? "+" : "−") + "$" + Math.abs(p.unrealized_pl).toFixed(0),
    income: p.income || null,
    plc:   p.unrealized_pl > 0 ? "pos" : (p.unrealized_pl < 0 ? "neg" : "flat"),
    // Net = price P&L + accrued income (income only exists for BIL). The last
    // column is the bottom line; price-only % next to green income read as a
    // contradiction.
    net:      p.unrealized_pl + (p.income || 0),
    netStr:   ((p.unrealized_pl + (p.income || 0)) >= 0 ? "+" : "−") + "$" + Math.abs(p.unrealized_pl + (p.income || 0)).toFixed(0),
    netPct:   (() => { const cost = Math.abs(p.market_value - p.unrealized_pl); const n = p.unrealized_pl + (p.income || 0); const pc = cost > 0 ? (n / cost) * 100 : 0; return (pc >= 0 ? "+" : "") + pc.toFixed(2) + "%"; })(),
    netCls:   (p.unrealized_pl + (p.income || 0)) > 0 ? "pos" : ((p.unrealized_pl + (p.income || 0)) < 0 ? "neg" : "flat"),
  }));

  // orders — SELL rows carry realized pnl when paired (avg-cost) with prior buys
  const orders = ord.orders.slice(0, 15).map(o => {
    const row = {
      t:    (o.filled || o.submitted || "").slice(0, 19).replace("T", " "),
      sym:  o.symbol,
      side: o.side.toUpperCase(),
      qty:  Number(o.qty).toFixed(4),
      px:   o.price ? "$" + Number(o.price).toFixed(o.price > 100 ? 0 : 2) : "—",
      st:   o.status,
      pnl:  null, pnlPct: null, pnlClass: "t-muted",
    };
    if (o.side === "sell" && o.pnl !== undefined && o.pnl !== null) {
      row.pnl = (o.pnl >= 0 ? "+" : "−") + "$" + Math.abs(o.pnl).toFixed(2);
      row.pnlPct = (o.pnl_pct >= 0 ? "+" : "") + Number(o.pnl_pct).toFixed(2) + "%";
      row.pnlClass = o.pnl > 0 ? "pos" : (o.pnl < 0 ? "neg" : "flat");
    }
    return row;
  });

  /* One normalized model for the control-tower view. It deliberately keeps
     live capital, disabled ideas, and shadow experiments in separate lanes. */
  const attrBySleeve = Object.fromEntries(
    ((sleeveAttr && sleeveAttr.rows) || []).map(r => [r.sleeve, r]));
  const towerAttr = key => attrBySleeve[
    ({ mf: "managed_futures" })[key] || key
  ] || { market_value: 0, total_pnl: 0, weight: 0, risk_contribution: null };
  const sleeveByKey = Object.fromEntries(sleeves.map(row => [row.key, row]));
  const nextCycle = automation && automation.next_rebalance;
  const scheduleLabel = value => {
    const parsed = new Date(value);
    return isNaN(parsed) ? value : parsed.toLocaleString("en-US", {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  };
  const nextProduction = nextCycle
    ? `${nextCycle.phase} cycle · ${scheduleLabel(nextCycle.at)}`
    : "Next scheduled portfolio cycle";
  const productionSpec = [
    { key: "etf", name: "ETF mean-reversion",
      signal: champList.length ? `Eligible now: ${champList.map(c => c.symbol).join(", ")}` : "No symbol currently clears the research gate" },
    { key: "regime", name: "QLD regime",
      signal: regimeBlock ? (regimeBlock.on
        ? `Risk-on: hold QLD · QQQ ${regimeBlock.vsFilter} above its 200-day average`
        : "Risk-off: hold cash · QQQ is below its 200-day average") : "Regime data unavailable" },
    { key: "nasdaq_router", name: "QQQ beta from idle routing",
      signal: "Nasdaq index exposure, accounted separately from managed futures" },
    { key: "mf", name: "Managed-futures funds",
      signal: regimeBlock && regimeBlock.on
        ? "Bull routing: QQQ plus DBMF/KMLM diversification"
        : "Defensive routing: DBMF/KMLM diversification" },
    { key: "cash_park", name: "BIL cash park",
      signal: "Short-duration Treasury reserve after all other sleeves size" },
  ];
  const production = productionSpec.map(spec => {
    const sleeve = sleeveByKey[spec.key === "nasdaq_router" ? "mf" : spec.key] || {};
    const attr = towerAttr(spec.key);
    return {
      ...spec, lane: "LIVE CAPITAL", status: sleeve.status || "UNKNOWN",
      capital: Number(attr.market_value || 0), capitalPct: Number(attr.weight || 0),
      pnl: Number(attr.total_pnl || 0), riskContribution: attr.risk_contribution,
      activity: liveTrades[spec.key] ? `${liveTrades[spec.key]} fills` : "No fills yet",
      lastActivity: lastFill[spec.key] || null, next: nextProduction,
    };
  });
  const parked = ["crypto", "pead"].map(key => {
    const sleeve = sleeveByKey[key] || {};
    const attr = towerAttr(key);
    return {
      key, name: key === "crypto" ? "Crypto trend" : "PEAD equity",
      lane: "PARKED", status: sleeve.status || "DISABLED",
      capital: Number(attr.market_value || 0), capitalPct: Number(attr.weight || 0),
      pnl: Number(attr.total_pnl || 0), riskContribution: attr.risk_contribution,
      signal: key === "crypto"
        ? "Disabled pending stronger out-of-sample evidence"
        : "Retired; weekly research is monitored but cannot allocate capital",
      activity: sleeve.trades ? `${sleeve.trades} historical fills` : "No live fills",
      lastActivity: sleeve.lastFill || null,
      next: "Weekly revalidation; no automatic re-enable",
    };
  });
  const shadowLatest = shadowRegime && shadowRegime.latest;
  const shadowVariants = (shadowRegime && shadowRegime.variants) || {};
  const shadowProduction = shadowVariants.production_70;
  const shadowLeader = Object.entries(shadowVariants).sort(
    (a, b) => Number(b[1].total_pnl || 0) - Number(a[1].total_pnl || 0))[0];
  const shadowLeaderPct = shadowLeader
    ? Number(shadowLeader[1].return_fraction || 0) * 100 : 0;
  const eventPlan = eventShadow && eventShadow.plan;
  const eventDetails = (eventPlan && eventPlan.candidate_details) || [];
  const nextEventEntry = eventDetails
    .filter(row => row.promoted && row.schedule && row.schedule.entry_start)
    .map(row => row.schedule.entry_start).sort()[0];
  const shadows = [{
    key: "qld_shadow", name: "QLD allocation laboratory", lane: "SHADOW",
    status: shadowProduction ? "MEASURING" : "STARTING",
    capital: shadowProduction
      ? Number(shadowLatest.observed_account_equity || 0) * Number(shadowProduction.target_weight || 0)
      : 0,
    capitalPct: Number((shadowProduction && shadowProduction.target_weight) || 0),
    pnl: shadowProduction ? Number(shadowProduction.total_pnl || 0) : null,
    riskContribution: null,
    signal: shadowLeader
      ? `${shadowLeader[0].replaceAll("_", " ")} currently leads at ${shadowLeaderPct >= 0 ? "+" : ""}${shadowLeaderPct.toFixed(2)}%`
      : "Awaiting the first cost-adjusted mark across 70%, 60%, 50% and stress-aware sizing",
    activity: `${Number(shadowRegime && shadowRegime.measured_observations || 0)} measured sessions`,
    lastActivity: shadowLatest && shadowLatest.ts,
    next: "Next regime cycle; cannot submit orders",
  }, {
    key: "event_options", name: "Event-options discovery", lane: "SHADOW",
    status: (eventShadow && eventShadow.status) || "STARTING",
    capital: Number((eventShadow && eventShadow.capital_at_risk) || 0),
    capitalPct: 0, pnl: Number((eventShadow && eventShadow.realized_pnl) || 0),
    riskContribution: null,
    signal: eventPlan
      ? `${eventPlan.candidates} scanned · ${eventPlan.prequalified} eligible${eventDetails.length ? ` · ${eventDetails.map(r => r.symbol).join(", ")}` : ""}`
      : "Awaiting first sealed weekly plan",
    activity: `${Number(eventShadow && eventShadow.evidence_rows || 0)} audited decisions`,
    lastActivity: eventShadow && eventShadow.latest_evidence && eventShadow.latest_evidence.recorded_at,
    next: nextEventEntry ? `Virtual entry window · ${scheduleLabel(nextEventEntry)}`
                         : "Next discovery scan Sunday 1:15 PM ET; cannot submit orders",
  }];
  const aiValue = Number(aiAttr && aiAttr.estimated_value_add || 0);
  const tailMeasuring = portfolioRisk && portfolioRisk.status === "MEASURING";
  const autoShadow = null; // legacy model was quarantined after parity audit
  const autoDeployment = null;
  const latestResearch = null;
  const intelligence = [{
    key: "ai_overlay", name: "Portfolio AI overlay", lane: "MEASUREMENT",
    status: aiAttr && aiAttr.decisions ? "MEASURING" : "STARTING",
    capital: null, capitalPct: null,
    pnl: aiAttr && aiAttr.marked_intervals ? aiValue : null,
    riskContribution: null,
    signal: aiAttr && aiAttr.decisions
      ? `${aiAttr.altered_decisions} of ${aiAttr.decisions} quant targets changed`
      : "Waiting for the next live ETF decision to create the first counterfactual",
    activity: `${Number(aiAttr && aiAttr.marked_intervals || 0)} marked intervals`,
    lastActivity: null,
    next: "Next live ETF decision records a proposal; quant executes until the evidence gate passes",
  }, {
    key: "tail_risk", name: "Portfolio tail-risk monitor", lane: "MEASUREMENT",
    status: tailMeasuring ? "MEASURING" : "STARTING",
    capital: null, capitalPct: null, pnl: null, riskContribution: null,
    signal: tailMeasuring
      ? `99% one-day VaR ${fmtUSDc(portfolioRisk.var_usd)} · expected shortfall ${fmtUSDc(portfolioRisk.expected_shortfall_usd)}`
      : `Tail-risk estimate ${String(portfolioRisk && portfolioRisk.status || "building").toLowerCase()}`,
    activity: `${Number(portfolioRisk && portfolioRisk.observations || 0)} aligned sessions`,
    lastActivity: null,
    next: "Refreshes from current holdings and 180 days of returns; advisory only",
  }, {
    key: "research_autopilot", name: "Frozen portfolio experiments", lane: "SHADOW",
    status: autoDeployment ? "MONITORING" : autoShadow ? "OBSERVING" : latestResearch ? "MEASURING" : "STARTING",
    capital: null, capitalPct: null, pnl: null, riskContribution: null,
    signal: autoDeployment
      ? `${autoDeployment.name} is live in paper with automatic rollback monitoring`
      : autoShadow
        ? `${autoShadow.name} · ${Number(autoShadow.metrics && autoShadow.metrics.weeks || 0)}/8 forward weeks`
        : latestResearch
          ? `${latestResearch.evaluated} tested · ${latestResearch.survivors.length} survivors · ${latestResearch.reviews.length} review-only (${latestResearch.reviews.join(", ") || "none"})`
          : `${(portfolioLab.candidates || []).length} fixed experiments · parity ${portfolioLab.parity?.passed ? "verified" : "not yet verified"}`,
    activity: latestResearch
      ? `${latestResearch.evaluated} variants tested ${String(latestResearch.ts || "").slice(0, 10)}`
      : `${Number(researchAutopilot && researchAutopilot.history_count || 0)} completed candidates`,
    lastActivity: (autoDeployment && autoDeployment.last_checked_at)
      || (autoShadow && (autoShadow.last_checked_at || autoShadow.started_at))
      || (latestResearch && latestResearch.ts) || null,
    next: autoDeployment
      ? "Sunday rollback check against the prior champion"
      : "Forward results and production parity must pass before a candidate can trade",
  }];
  const riskRows = production.filter(r => r.riskContribution != null);
  const topRisk = riskRows.slice().sort(
    (a, b) => Math.abs(b.riskContribution) - Math.abs(a.riskContribution))[0] || null;
  const enabledProduction = production.filter(
    r => !["DISABLED", "TRIPPED", "FAST_TRIPPED"].includes(r.status)).length;
  const controlTower = {
    production, parked, shadows, intelligence,
    summary: {
      enabledProduction, parked: parked.length, shadows: shadows.length,
      governance: (alloc && alloc.governance && alloc.governance.status) || "UNKNOWN",
      automation: (automation && automation.status) || "UNKNOWN",
      nextCycle: nextCycle || null,
      topRisk,
    },
  };

  return {
    stats: {
      portfolio: equity,
      portfolioSub: (total_pl >= 0 ? "+" : "−") + "$" + Math.abs(total_pl).toFixed(0) + " all-time",
      portfolioPos: total_pl >= 0,
      totalReturn: ((total_pl / 100000) * 100).toFixed(2),
      todayUsd:    day_pl,
      todayPct:    ((day_pl / last_eq) * 100).toFixed(2),
      todayPos:    day_pl >= 0,
      cash, cashPct: Math.round((cash / equity) * 100),
      invested, investedPct: Math.round((invested / equity) * 100),
      valuation: acct.valuation || null,
    },
    sleeves,
    governance: (alloc && alloc.governance) || { status: "UNKNOWN", conflicts: [] },
    pvDates, pv, ddDates, dd,
    pvEmpty: pv.length < 2,
    regime: regimeBlock,
    alloc: allocClean,
    positions,
    orders,
    income: inc || {},
    vsIndices: vsidx || {dates: [], bot: [], spy: [], qqq: []},
    simDivToDate: acct.sim_div_to_date || 0,
    sleeveAttribution: sleeveAttr || { rows: [] },
    aiAttribution: aiAttr || {},
    executionQuality: executionQuality || {},
    shadowRegime: shadowRegime || {},
    eventOptionsShadow: eventShadow || {},
    portfolioRisk: portfolioRisk || {},
    researchAutopilot: researchAutopilot || {},
    automation: automation || {},
    portfolioLab: portfolioLab || {},
    release: release || {},
    reconciliation: reconciliation || {},
    controlTower,
  };
}

/* ---------------- STRATEGY tab ---------------- */
async function loadStrategy() {
  const [snap, alloc, vsIdx] = await Promise.all([
    getJSON("/api/snapshot/team_vs_hold"),
    getJSON("/api/allocations"),
    getJSON("/api/equity_vs_indices"),
    getJSON("/api/champions"),
  ]);
  if (!snap || snap.error) {
    return { error: snap?.error || "missing snapshot" };
  }

  // LIVE since-launch return (equity_vs_indices is normalized to 100 at launch),
  // kept DISTINCT from the full-cycle backtest metrics so the two aren't conflated.
  let sinceLaunch = null;
  if (vsIdx && Array.isArray(vsIdx.bot) && vsIdx.bot.length) {
    const last = a => (Array.isArray(a) && a.length ? a[a.length - 1] : null);
    const b = last(vsIdx.bot), q = last(vsIdx.qqq), s = last(vsIdx.spy);
    sinceLaunch = {
      bot: b != null ? b - 100 : null,
      qqq: q != null ? q - 100 : null,
      spy: s != null ? s - 100 : null,
      start: vsIdx.dates[0],
      days: vsIdx.dates.length,
    };
  }

  // policy banner
  const w = (alloc && alloc.weights) || {};
  const verdicts = (alloc && alloc.verdicts) || {};
  const streaks = (alloc && alloc.streaks) || {};
  const mixOrder = [
    { key: "regime", name: "QLD-regime",   color: "var(--c-regime)" },
    { key: "crypto", name: "Crypto trend", color: "var(--c-crypto)" },
    { key: "qqq",    name: "QQQ",          color: "var(--c-qqq)" },
  ];
  const mix = mixOrder.map(m => ({
    name:   m.name,
    w:      Math.round(((w[m.key] ?? 0) * 100)),
    status: verdicts[m.key] || (m.key === "qqq" ? "fallback" : "HEALTHY"),
    color:  m.color,
  }));
  const tripBits = [];
  for (const k of ["crypto", "regime"]) {
    if ((streaks[k] || 0) > 0)
      tripBits.push(`${mixOrder.find(m => m.key === k).name} on a ${streaks[k]}-week negative streak`);
  }
  // ACTUAL live deployment (the chips above are policy TARGETS). Sourced from
  // real positions, so it shows where capital genuinely sits — including the
  // managed-futures sleeve and the idle BIL that feeds it.
  const liveW = alloc && alloc.live && alloc.live.weights;
  const liveDeploy = liveW ? [
    { key: "regime", name: "QLD-regime",      color: "var(--c-regime)" },
    { key: "crypto", name: "Crypto trend",    color: "var(--c-crypto)" },
    { key: "qqq",    name: "QQQ (idle)",      color: "var(--c-qqq)" },
    { key: "mf",     name: "Managed futures", color: "var(--amber)" },
    { key: "bil",    name: "BIL (idle)",      color: "var(--text-3)" },
  ].map(m => ({ name: m.name, color: m.color, w: Math.round((liveW[m.key] || 0) * 100) }))
   : null;

  const policy = {
    name:    (alloc && alloc.policy) || "ADAPTIVE_HARD",
    updated: alloc && alloc.ts ? alloc.ts.replace("T", " ").slice(0, 19) : "—",
    mix,
    live: liveDeploy,
    mfPending: !!(alloc && alloc.live && alloc.live.mf_pending),
    tripwire: tripBits.length ? tripBits.join(" · ") : "All sleeves healthy",
  };

  // metrics: adaptive4 (live) vs QQQ
  const live = snap.metrics.adaptive4, qqq = snap.metrics.qqq;
  const fmtSh = v => v.toFixed(2);
  const fmtP  = v => (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%";
  const metrics = [
    { k: "Sharpe ratio",  v: fmtSh(live.sharpe),  cmp: `Just-QQQ ${fmtSh(qqq.sharpe)}`,    good: live.sharpe  > qqq.sharpe,  term: "sharpe" },
    { k: "Annual return", v: fmtP(live.cagr),     cmp: `Just-QQQ ${fmtP(qqq.cagr)}`,        good: live.cagr    > qqq.cagr,    term: "cagr" },
    { k: "Worst drop",    v: fmtP(live.max_dd),   cmp: `Just-QQQ ${fmtP(qqq.max_dd)}`,      good: live.max_dd  > qqq.max_dd,  term: "drawdown" },
    { k: "Volatility",    v: Math.round(live.vol * 100) + "%", cmp: `Just-QQQ ${Math.round(qqq.vol * 100)}%`, good: live.vol < qqq.vol, term: "vol" },
  ];

  // chart series — we DROPPED regime_standalone earlier per user request
  const series = [
    { key: "adaptive4",  label: "This bot",     color: "var(--pos-line)", width: 3,   live: true },
    { key: "qqq",        label: "Just hold QQQ (NASDAQ-100)",                    color: "var(--c-qqq)",    width: 1.5, dash: [6,5] },
    { key: "spy",        label: "Just hold SPY (S&P 500)",                    color: "var(--c-regime)", width: 1.5, dash: [6,5] },
    { key: "bogle",      label: "60/40 Bogleheads",                 color: "var(--text-4)",   width: 1.5, dash: [2,4] },
  ];
  // labels = the union of dates (from longest series; adaptive4 spans the full window)
  const fullSeries = snap.series.adaptive4 || [];
  const labels = fullSeries.map(d => new Date(d.date));
  // align each series by mapping date → value (snapshot writer uses common index already)
  const seriesData = {};
  for (const s of series) {
    const data = snap.series[s.key] || [];
    const byDate = Object.fromEntries(data.map(d => [d.date, d.value]));
    seriesData[s.key] = labels.map(dt => byDate[dt.toISOString().slice(0, 10)] ?? null);
  }

  // metrics table
  const table = [
    { s: "This bot",        sh: fmtSh(snap.metrics.adaptive4.sharpe),  c: fmtP(snap.metrics.adaptive4.cagr),  dd: fmtP(snap.metrics.adaptive4.max_dd),  v: Math.round(snap.metrics.adaptive4.vol  * 100) + "%", live: true },
    { s: "Same sleeves, fixed ⅓ each",       sh: fmtSh(snap.metrics.mix_regime.sharpe), c: fmtP(snap.metrics.mix_regime.cagr), dd: fmtP(snap.metrics.mix_regime.max_dd), v: Math.round(snap.metrics.mix_regime.vol * 100) + "%" },
    { s: "Old mix (QQQ instead of QLD)",      sh: fmtSh(snap.metrics.mix_old.sharpe),    c: fmtP(snap.metrics.mix_old.cagr),    dd: fmtP(snap.metrics.mix_old.max_dd),    v: Math.round(snap.metrics.mix_old.vol    * 100) + "%" },
    { s: "Just hold QQQ (NASDAQ-100)",                      sh: fmtSh(snap.metrics.qqq.sharpe),        c: fmtP(snap.metrics.qqq.cagr),        dd: fmtP(snap.metrics.qqq.max_dd),        v: Math.round(snap.metrics.qqq.vol        * 100) + "%" },
    { s: "Just hold SPY (S&P 500)",                      sh: fmtSh(snap.metrics.spy.sharpe),        c: fmtP(snap.metrics.spy.cagr),        dd: fmtP(snap.metrics.spy.max_dd),        v: Math.round(snap.metrics.spy.vol        * 100) + "%" },
    { s: "60/40 Bogleheads",                   sh: fmtSh(snap.metrics.bogle.sharpe),      c: fmtP(snap.metrics.bogle.cagr),      dd: fmtP(snap.metrics.bogle.max_dd),      v: Math.round(snap.metrics.bogle.vol      * 100) + "%" },
  ];

  // weights stack — snapshot has monthly dates + per-sleeve arrays in 0-1 form
  const wDates = snap.weights.dates.map(d => new Date(d));
  // reindex to weekly chart labels via nearest-date map
  const reindexWeights = arr => {
    const byMonth = Object.fromEntries(snap.weights.dates.map((d, i) => [d.slice(0, 7), arr[i]]));
    let last = 100;
    return labels.map(dt => {
      const k = dt.toISOString().slice(0, 7);
      if (byMonth[k] !== undefined) last = Math.round(byMonth[k] * 100);
      return last;
    });
  };
  const wRegime = reindexWeights(snap.weights.regime);
  const wPead   = reindexWeights(snap.weights.pead);
  const wCrypto = reindexWeights(snap.weights.crypto);
  const wQqq    = reindexWeights(snap.weights.qqq);
  const wMf     = snap.weights.mf  ? reindexWeights(snap.weights.mf)  : labels.map(() => 0);
  const wBil    = snap.weights.bil ? reindexWeights(snap.weights.bil) : labels.map(() => 0);

  // rolling vol — snapshot is weekly already
  const volReindex = obj => {
    const byDate = Object.fromEntries(obj.dates.map((d, i) => [d, obj.values[i]]));
    let last = null;
    return labels.map(dt => {
      const k = dt.toISOString().slice(0, 10);
      if (byDate[k] !== undefined) last = byDate[k];
      return last;
    });
  };

  return {
    policy, metrics, series, seriesData, table, sinceLaunch,
    labels,
    weights: { crypto: wCrypto, pead: wPead, regime: wRegime, qqq: wQqq, mf: wMf, bil: wBil },
    vol: {
      live:  volReindex(snap.vol.adaptive4),
      qqq:   snap.vol.qqq   ? volReindex(snap.vol.qqq)   : null,
      spy:   snap.vol.spy   ? volReindex(snap.vol.spy)   : null,
      bogle: snap.vol.bogle ? volReindex(snap.vol.bogle) : null,
    },
  };
}

/* ---------------- CRASH tab ---------------- */
async function loadCrash() {
  const snap = await getJSON("/api/snapshot/crash_perf");
  if (!snap || snap.error) return { error: snap?.error || "missing snapshot" };

  // real crashes table — drop the Old-static column per user; surface days-to-recover
  const fmtDays = d => (d == null ? "—" : d + "d");
  const realCrashes = (snap.real.crashes || []).map(c => ({
    peak:   c.start, trough: c.trough,
    qqq:    (c.qqq_dd  * 100).toFixed(1) + "%",
    live:   (c.live_dd * 100).toFixed(1) + "%",
    saved:  ((c.qqq_dd - c.live_dd) * 100).toFixed(1) + "pp",
    qqqRec:  fmtDays(c.qqq_days_to_recover),
    liveRec: fmtDays(c.live_days_to_recover),
  }));

  // overlay (weekly) — already in arrays
  const ov = snap.real.overlay;
  const overlayLabels = ov.qqq.dates.map(d => new Date(d));
  const overlay = {
    qqq:  ov.qqq.values,
    live: alignByDate(ov.live.dates, ov.live.values, ov.qqq.dates),
    old:  alignByDate(ov.static.dates, ov.static.values, ov.qqq.dates),
  };

  // historical proxy
  const hist = snap.historical_2004;
  const proxyStats = [
    { k: "QQQ Sharpe (2006→now)",       v: hist.metrics.qqq.sharpe.toFixed(2),   tone: "" },
    { k: "Regime defense Sharpe",       v: hist.metrics.proxy.sharpe.toFixed(2), tone: "pos" },
    { k: "QQQ worst drop",              v: (hist.metrics.qqq.max_dd * 100).toFixed(0) + "%", tone: "neg" },
    { k: "Regime defense worst drop",   v: (hist.metrics.proxy.max_dd * 100).toFixed(0) + "%", tone: "pos" },
  ];
  const pl = hist.overlay.qqq.dates.map(d => new Date(d));
  const proxy = {
    labels: pl,
    qqq:    hist.overlay.qqq.values,
    proxy:  alignByDate(hist.overlay.proxy.dates, hist.overlay.proxy.values, hist.overlay.qqq.dates),
    // Full diversified bot drawdown, overlaid for 2018+ (null before — crypto/MF
    // didn't exist). Shows how much shallower the real bot is than the leveraged
    // gate alone, so the gate-only whipsaw isn't mistaken for the bot.
    bot:    (snap.real && snap.real.overlay && snap.real.overlay.live)
            ? alignByDate(snap.real.overlay.live.dates, snap.real.overlay.live.values, hist.overlay.qqq.dates)
            : null,
  };
  // 2008 band: find indices roughly Oct 2007 → Jul 2009
  const findIdx = (dates, target) => {
    const t = new Date(target).getTime();
    let best = 0, bestDiff = Infinity;
    dates.forEach((d, i) => {
      const diff = Math.abs(new Date(d).getTime() - t);
      if (diff < bestDiff) { best = i; bestDiff = diff; }
    });
    return best;
  };
  const gfcFrom = findIdx(hist.overlay.qqq.dates, "2007-10-01");
  const gfcTo   = findIdx(hist.overlay.qqq.dates, "2009-07-01");

  const majorDD = (hist.crashes || []).map(c => ({
    peak: c.start, trough: c.trough,
    qqq:   (c.qqq_dd * 100).toFixed(1) + "%",
    proxy: (c.proxy_dd * 100).toFixed(1) + "%",
    saved: ((c.qqq_dd - c.proxy_dd) * 100).toFixed(1) + "pp",
    savedNum: (c.qqq_dd - c.proxy_dd),  // sign for coloring
    coverage: c.coverage || "regime_only",  // "full_bot" (2018+) | "regime_only" (pre-2018)
    qqqRec:   fmtDays(c.qqq_days_to_recover),
    proxyRec: fmtDays(c.proxy_days_to_recover),
  }));

  return {
    realCrashes,
    overlay, overlayLabels,
    proxy, proxyStats, majorDD,
    gfcBand: { from: gfcFrom, to: gfcTo },
  };
}

function alignByDate(dates, values, refDates) {
  const m = Object.fromEntries(dates.map((d, i) => [d, values[i]]));
  let last = null;
  return refDates.map(d => { if (m[d] !== undefined) last = m[d]; return last; });
}

/* ---------------- PEAD tab ---------------- */
async function loadPead() {
  const snap = await getJSON("/api/pead");
  if (!snap || snap.error) return { error: snap?.error || "missing data" };
  return {
    asOf:         snap.as_of,
    universeSize: snap.universe_size,
    positions:    snap.positions || [],
    recent:       snap.recent || [],
    upcoming:     snap.upcoming || [],
  };
}


/* ---------------- GLOSSARY ---------------- */
const GLOSSARY = {
  sharpe:    ["Sharpe ratio", "Return earned per unit of risk taken. Above 1 is good; higher means smoother gains for the same reward."],
  cagr:      ["Annual return (CAGR)", "The smoothed yearly growth rate — what the account would have compounded at each year on average."],
  drawdown:  ["Drawdown", "How far the account has fallen from its highest point. A smaller worst-drawdown means less painful dips."],
  vol:       ["Volatility", "How bumpy the ride is. Lower volatility means steadier day-to-day value."],
  pead:      ["PEAD", "Post-Earnings Announcement Drift — a strategy that buys companies right after they beat earnings, since they tend to keep drifting up."],
  qldregime: ["QLD-regime", "Holds QLD (a 2× Nasdaq fund) only when the market is trending up (QQQ above its 200-day average); otherwise moves to cash to dodge crashes."],
  sma200:    ["200-day average", "The average price over the last 200 trading days — a slow line used to tell an uptrend from a downtrend."],
  sleeve:    ["Sleeve", "One self-contained sub-strategy. The bot runs several and blends them together."],
  tripwire:  ["Tripwire", "A safety rule: if a sleeve underperforms for several weeks straight, its money is automatically pulled and parked in the safe fallback."],
  adaptive:  ["Adaptive policy", "The rule that splits money across sleeves and cuts any that goes cold. Idle capital is routed by a regime-aware router: a 50/50 split between QQQ and the managed-futures sleeve when QQQ is above its 200-day average, and entirely to managed futures when it's below — keeping a ~12% cash/BIL reserve."],
  mf:        ["Managed-futures sleeve", "DBMF+KMLM, a 50/50 blend of two managed-futures ETFs that go long/short across global futures markets. It absorbs idle capital when other sleeves are paused — uncorrelated to stocks, so it diversifies instead of just doubling down on the Nasdaq."],
  qqq:       ["QQQ", "An ETF tracking the 100 largest Nasdaq companies — used here as the safe, diversified fallback."],
  qld:       ["QLD", "A fund that aims for 2× the daily move of the Nasdaq-100 — more upside, but bigger swings."],
  bil:       ["BIL", "A short-term Treasury-bill ETF — effectively a cash-like parking spot that earns a little yield."],
  doge:      ["DOGE/USD", "Dogecoin priced in dollars — the asset the crypto-trend sleeve trades."],
  spy:       ["SPY", "An ETF tracking the S&P 500 — the classic 'just hold the market' benchmark."],
  fallback:  ["Fallback", "Where capital goes when a sleeve is paused — into a ~12% cash/BIL reserve plus the regime-aware idle router, which holds a 50/50 QQQ + managed-futures split when QQQ is in an uptrend and all managed futures when it's in a downtrend."],
};
