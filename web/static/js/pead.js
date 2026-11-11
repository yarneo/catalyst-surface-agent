/* ============================================================
   pead.js — PEAD tab (open positions + recent earnings + upcoming)
   ============================================================ */
async function renderPead() {
  const root = h("section.tab", { id: "tab-pead", role: "tabpanel" },
    h("div.section",
      h("div.section-head",
        h("p.eyebrow", "📰  Post-earnings drift"),
        h("h1.h-lg", "PEAD universe — earnings calendar"),
        h("p.lead",
          "The PEAD sleeve scans ~44 large/mid-cap US stocks and buys those with a ",
          term("concordant earnings beat", "pead"),
          " (EPS beat AND price reacted up), holding ~60 trading days. Shows current open positions, recent earnings the strategy evaluated, and upcoming earnings that could trigger an entry.")),
      h("div", { id: "pead-body" }, h("p.dim", { style: "padding:24px 0" }, "Loading PEAD data (~5-30s for first load — scanning 44 symbols)…")))
  );

  root._charts = () => {};
  root._asyncReady = (async () => {
    let P;
    try { P = await loadPead(); }
    catch (e) {
      $("#pead-body", root).innerHTML = "";
      $("#pead-body", root).appendChild(h("div.callout.warn",
        h("span.ci", "⚠"), h("span", "Failed to load PEAD data: " + e.message)));
      return;
    }
    if (P.error) {
      $("#pead-body", root).innerHTML = "";
      $("#pead-body", root).appendChild(h("div.callout.warn",
        h("span.ci", "⚠"), h("span", "PEAD endpoint returned: " + P.error)));
      return;
    }
    const body = $("#pead-body", root);
    body.innerHTML = "";

    /* ---- summary KVs ---- */
    body.appendChild(h("div.kv-strip",
      h("div.kv",
        h("div.kv-k", "Universe"),
        h("div.kv-v", String(P.universeSize)),
        h("div.kv-cmp", "stocks scanned")),
      h("div.kv",
        h("div.kv-k", "Open positions"),
        h("div.kv-v", String(P.positions.length)),
        h("div.kv-cmp", `of 8 slots used`)),
      h("div.kv",
        h("div.kv-k", "Recent earnings (21d)"),
        h("div.kv-v", String(P.recent.length)),
        h("div.kv-cmp", P.recent.filter(r => r.concordant).length + " concordant")),
      h("div.kv",
        h("div.kv-k", "Upcoming (21d)"),
        h("div.kv-v", String(P.upcoming.length)),
        h("div.kv-cmp", "stocks with earnings coming"))
    ));

    /* ---- open positions ---- */
    const posCard = h("div.card.card-pad",
      sectionHead("Open PEAD positions",
        h("p.caption", "Stocks the bot is currently holding from prior concordant signals. Each holds ~60 trading days from entry, then exits automatically.")),
      h("div.mt-16",
        P.positions.length === 0
          ? h("div.callout.info", h("span.ci", "💤"),
              h("span", "No positions open right now. PEAD enters only on concordant earnings beats — when one fires, you'll see it appear here within 24h."))
          : buildTable([
              { h: "Symbol",      cell: r => h("span.t-sym", r.symbol) },
              { h: "Entry date",  cell: r => h("span.t-num.t-muted", r.entry_date) },
              { h: "Days held",   cell: r => h("span.t-num", r.days_held + "d") },
              { h: "Days to exit", cell: r => h("span.t-num", r.days_to_exit > 0 ? r.days_to_exit + "d" : "exit due") },
              { h: "Avg entry",   cell: r => h("span.t-num", "$" + r.avg_entry.toFixed(2)) },
              { h: "Price",       cell: r => h("span.t-num", "$" + r.price.toFixed(2)) },
              { h: "Market value", cell: r => h("span.t-num", "$" + Math.round(r.market_value).toLocaleString()) },
              { h: "Unrealized P&L", cell: r =>
                  h("span", { class: "t-num " + (r.unrealized_pl >= 0 ? "pos" : "neg") },
                    (r.unrealized_pl >= 0 ? "+$" : "−$") + Math.abs(r.unrealized_pl).toFixed(0)) },
            ], P.positions)
      )
    );
    body.appendChild(h("div.section", posCard));

    /* ---- recent earnings (with concordant flag) ---- */
    const recRows = P.recent.map(r => ({
      ...r,
      surpriseStr: (r.surprise >= 0 ? "+" : "") + r.surprise.toFixed(1) + "%",
      reactionStr: r.reaction == null ? "—" :
                    (r.reaction >= 0 ? "+" : "") + r.reaction.toFixed(2) + "%",
      timing: r.amc ? "after close" : "before open",
    }));
    const recCard = h("div.card.card-pad",
      sectionHead("Recent earnings the strategy evaluated",
        h("p.caption",
          "Every earnings event in the universe in the past 21 days. ",
          h("b.pos", "✓ concordant"),
          " means EPS beat AND price reacted up → the bot would enter. ",
          h("b.neg", "✗ "),
          " means non-concordant (beat-but-sold, missed, or no reaction) → no trade.")),
      h("div.mt-16",
        P.recent.length === 0
          ? h("div.callout.info", h("span.ci", "🤷"), h("span", "No earnings in the universe in the last 21 days — quiet between earnings seasons."))
          : buildTable([
              { h: "Symbol",   cell: r => h("span.t-sym", r.symbol) },
              { h: "Date",     cell: r => h("span.t-num.t-muted", r.date) },
              { h: "Timing",   cell: r => h("span.t-muted", { style: "font-size:11.5px" }, r.timing) },
              { h: "Surprise", cell: r => h("span.t-num", { class: r.miss ? "neg" : (r.surprise > 0 ? "pos" : "") }, r.surpriseStr) },
              { h: "Reaction", cell: r => h("span.t-num", { class: r.reaction == null ? "t-muted" : (r.reaction >= 0 ? "pos" : "neg") }, r.reactionStr) },
              { h: "Signal",   cell: r => r.concordant
                  ? h("span.badge.healthy", h("span.b-dot"), "concordant → buy")
                  : h("span.badge.neutral", h("span.b-dot"), "no trade") },
            ], recRows)
      )
    );
    body.appendChild(h("div.section", recCard));

    /* ---- upcoming earnings ---- */
    const upRows = P.upcoming.map(r => ({
      ...r,
      estStr: r.estimate == null ? "—" : "$" + r.estimate.toFixed(2),
    }));
    const upCard = h("div.card.card-pad",
      sectionHead("Upcoming earnings",
        h("p.caption", "Stocks in the universe reporting earnings in the next 21 days. After each report, the bot checks for concordant beats and enters within 1-2 days if it fires.")),
      h("div.mt-16",
        P.upcoming.length === 0
          ? h("div.callout.info", h("span.ci", "📅"), h("span", "No upcoming earnings in the universe in the next 21 days."))
          : buildTable([
              { h: "Symbol", cell: r => h("span.t-sym", r.symbol) },
              { h: "Expected date", cell: r => h("span.t-num", r.date) },
              { h: "EPS estimate", cell: r => h("span.t-num.t-muted", r.estStr) },
            ], upRows)
      )
    );
    body.appendChild(h("div.section", upCard));
  })();

  return root;
}
