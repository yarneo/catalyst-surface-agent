/* Presentation-only calculations. No trading decisions or private snapshots. */
const Dashboard = {
  number(value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  },
  money(value, signed = false) {
    const n = this.number(value);
    if (n == null) return "—";
    return (n < 0 ? "−" : signed && n > 0 ? "+" : "") +
      "$" + Math.abs(n).toLocaleString("en-US", {maximumFractionDigits: 0});
  },
  percent(value) {
    const n = this.number(value);
    return n == null ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + (Math.abs(n) * 100).toFixed(2) + "%";
  },
  tone(value) { return value == null || value === 0 ? "flat" : value < 0 ? "neg" : "pos"; },
  date(value) {
    if (!value) return "Date unavailable";
    const d = new Date(value.length === 10 ? value + "T12:00:00Z" : value);
    return isNaN(d) ? "Date unavailable" : d.toLocaleDateString("en-US", {
      month: "short", day: "numeric", ...(value.length === 10 ? {timeZone: "UTC"} : {})});
  },
  time(value) {
    if (!value) return "Schedule unavailable";
    const d = new Date(value);
    return isNaN(d) ? "Schedule unavailable" : d.toLocaleString("en-US", {
      weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"});
  },
  performance(account = {}, rows = [], startingCapital = 100000) {
    const curve = [...new Map(rows.filter(r => r.date && this.number(r.equity) != null && Number(r.equity) > 0)
      .map(r => [r.date, {date: r.date, equity: Number(r.equity)}])).values()].sort((a,b) => a.date.localeCompare(b.date));
    const change = (from, to, start, end) => from > 0 && to != null
      ? {amount: to - from, fraction: to / from - 1, start, end} : null;
    const equity = this.number(account.equity);
    const last = curve.at(-1), previous = curve.at(-2), fiveAgo = curve.at(-6);
    const intraday = account.valuation?.market_open === true;
    return {equity, curve, intraday,
      total: change(startingCapital, equity),
      day: intraday ? change(this.number(account.last_equity), equity)
        : last && previous ? change(previous.equity, last.equity, previous.date, last.date) : null,
      fiveDays: last && fiveAgo ? change(fiveAgo.equity, last.equity, fiveAgo.date, last.date) : null};
  },
  health(automation = {}) {
    const state = automation.status;
    if (automation.killed || state === "KILLED") return {label: "Trading stopped", tone: "neg", text: "The portfolio stop is active. New trading is blocked."};
    if (state === "HALTED_TODAY") return {label: "Trading paused today", tone: "amber", text: "The daily risk limit has paused trading."};
    if (state === "HEALTHY" && !(automation.issues || []).length)
      return {label: "Bot running normally", tone: "pos", text: "Scheduled checks are up to date."};
    if (["DEGRADED", "STATE_ERROR"].includes(state) || (automation.issues || []).length)
      return {label: "Bot needs attention", tone: "neg", text: "A scheduled check failed, is overdue, or could not be verified."};
    return {label: "Bot status unavailable", tone: "amber", text: "We could not verify the latest scheduled checks."};
  },
  research(lab) {
    if (!lab || lab.error) return {label: "Research status unavailable", tone: "amber", text: "The research report could not be loaded. Account performance is shown separately."};
    const market = lab.market_collection || {}, access = lab.market_feed_access || market.feed_access || {};
    const replay = lab.production_verification || {};
    if (replay.mismatches > 0 || ["SIMULATOR_MISMATCH","MISMATCH"].includes(replay.simulator_comparison?.status))
      return {label: "Research checks need attention", tone: "amber", text: "Some simulated decisions or accounting checks do not match. Results cannot qualify until these checks pass."};
    if (lab.pipeline?.status === "ACTION_REQUIRED") {
      const feedLimited = /SIP|consolidated/i.test(access.limitation || "") ||
        (lab.pipeline.issues || []).some(s => /SIP.*unavailable/i.test(s));
      return {label: feedLimited ? "Research needs better price data" : "Research data needs attention", tone: "amber",
        text: feedLimited ? "Some test prices are stale or missing. Full-market SIP access is the next data upgrade; completed checks must still pass."
          : "Some observations or checks are incomplete. Those days remain in the results, but do not count as reliable evidence."};
    }
    if (lab.report_stale || market.stale)
      return {label: "Research report is behind", tone: "amber", text: "The latest expected observations have not arrived. Earlier results remain available."};
    if (!lab.pipeline || !Array.isArray(lab.candidates))
      return {label: "Research is starting", tone: "flat", text: "Waiting for the first complete research report."};
    return {label: "Research is gathering evidence", tone: "flat", text: "Experiments use virtual portfolios. They need more complete trading days before we can judge them."};
  },
  strategyState(verdict, held) {
    if (["FAST_TRIPPED", "TRIPPED"].includes(verdict)) return {label: "Risk pause", tone: "amber"};
    if (verdict === "DISABLED") return {label: "Switched off", tone: "flat"};
    if (verdict === "DECAYING") return {label: "Under review", tone: "amber"};
    if (verdict === "HEALTHY" && held == null) return {label: "Holdings unavailable", tone: "amber"};
    if (verdict === "HEALTHY") return {label: held ? "Holding investments" : "Waiting for a signal", tone: held ? "pos" : "flat"};
    return {label: "Status unavailable", tone: "amber"};
  },
  experimentName(name) {
    const names = {baseline_70: "Current rules (reference)", pead_10pct: "Add earnings trades (10%)",
      without_etf: "Remove ETF rotation", no_etf: "Remove ETF rotation"};
    return names[name] || String(name || "Unnamed experiment").replaceAll("_", " ").replace(/\bqld\b/gi,"QLD");
  }
};
