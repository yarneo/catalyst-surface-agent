async function renderStrategy() {
  const body = h("div",h("p.dim","Loading research progress…"));
  const root = h("section.tab",{id:"tab-strategy",role:"tabpanel","aria-labelledby":"nav-strategy"},
    h("div.overview-heading",h("div",h("p.eyebrow","Virtual portfolios · no account trading"),
      h("h1.h-lg","Can we make the bot better?"),h("p.lead","New ideas are tested here before they can earn a place in the account."))),body);
  root._asyncReady = (async()=>{
    try {
      const results = await Promise.allSettled(["portfolio_lab","ai_attribution","event_options_shadow"].map(n=>dashboardFetch("/api/"+n)));
      const [lab,ai,options] = results.map(r=>r.status==="fulfilled"?r.value:null);
      if (!lab) throw new Error("Research report unavailable. Refresh the page to retry.");
      const state = Dashboard.research(lab), candidates = lab.candidates || [];
      const baseline = candidates.find(c=>c.name==="baseline_70");
      const earnings = candidates.find(c=>c.name==="pead_10pct");
      const replay = lab.production_verification || {};
      const verified = (replay.matching_sessions || []).filter(day=>(replay.simulator_comparison?.matching_sessions || []).includes(day)).length;
      const old = lab.prior_forward?.candidates || [];
      const oldDays = Math.max(0,...old.map(c=>c.forward?.observations||0));
      const newDays = baseline?.forward?.observations || 0;
      const progress = (title,value,text,complete,total)=>h("div.card.card-pad.research-progress",
        h("h2.h-md",title),h("strong.research-count",value),
        h("progress",{max:total,value:Math.min(complete,total),"aria-label":title}),h("p.caption",text));
      body.replaceChildren(h("div.research-summary",h("strong."+state.tone,state.label),h("p",state.text),
        h("p.caption","These checks apply to experiments. The account's trading status is on Overview.")),
        h("div.section.grid.cols-3",
          progress("Check the simulator",verified+" / 5 days",
            "Reproduce actual decisions and independently verify cash, holdings and costs. At least one order is also required.",
            verified,5),
          progress("Compare portfolio changes",(baseline?.promotion?.complete_sessions||0)+" / 60 days",
            newDays+" days observed in this validation run. Only complete days count; 12 weekly blocks, costs and other checks must also pass.",
            baseline?.promotion?.complete_sessions||0,60),
          h("div.card.card-pad.research-progress",h("h2.h-md","Test earnings trades"),
            h("strong.research-count",(earnings?.pead_open_positions||0)+" open · "+(earnings?.pead_completed_outcomes||0)+" closed"),
            h("p.caption","Virtual trades after earnings surprises, using up to 10% of the test portfolio. We need completed trades to judge results."))),
        h("p.overview-footnote","Reaching these counts allows a review. It does not prove profitability or switch a strategy on."));

      if (oldDays) body.appendChild(h("div.research-context",
        h("strong",oldDays+" earlier trading days are still saved"),
        h("p","The validation repair started a new evidence count. Existing virtual cash, holdings, losses and risk limits carried forward; earlier results remain below.")));

      const usePrior = !newDays && old.length>0;
      const rows = usePrior ? old : candidates;
      const outcomes = h("div.card.card-pad",sectionHead("What the experiments show so far",
        usePrior ? "Earlier run through "+Dashboard.date(lab.prior_forward.as_of)+". These incomplete observations are informative, but do not qualify as validated results."
          : "Virtual whole-portfolio returns after modeled costs. These are not changes in the broker account."));
      const displayRows = rows.filter(r=>["baseline_70","pead_10pct","without_etf","no_etf"].includes(r.name));
      outcomes.appendChild(buildTable([
        {h:"Portfolio",cell:r=>Dashboard.experimentName(r.name)},
        {h:"Observed days",cell:r=>String(r.forward?.observations||0)},
        {h:"Return",cell:r=>h("span."+Dashboard.tone(r.forward?.return),r.forward?.observations?Dashboard.percent(r.forward.return):"Waiting for data")},
        {h:"With higher costs",cell:r=>r.forward_high_cost?.observations?Dashboard.percent(r.forward_high_cost.return):"Waiting for data"}
      ],displayRows));
      outcomes.appendChild(h("p.caption","Higher costs assume 0.25% price impact per trade beyond the bid/ask, versus 0.05% in the main case. The earnings row includes the entire portfolio, not just its earnings trades."));
      body.appendChild(h("div.section",outcomes));

      const inputs = ai?.inputs || {}, sentiment = Object.entries(inputs.sentiment || {});
      const inputCard = h("div.card.card-pad", sectionHead("Are the research inputs working?",
        "Working inputs let us test an idea. Only completed comparisons can show whether it helps."));
      if (sentiment.length) inputCard.appendChild(buildTable([
        {h:"Symbol",cell:r=>r[0]},
        {h:"News → AI",cell:r=>r[1].stale?"Needs a fresh check":r[1].input_status==="READY"?"Cited assessment available":r[1].input_status==="NO_RECENT_NEWS"?"No recent coverage":"Input unavailable"},
        {h:"Articles used",cell:r=>(r[1].direct_articles||0)+" direct · "+(r[1].context_articles||0)+" sector context"},
        {h:"Last checked",cell:r=>r[1].observed_at?new Date(r[1].observed_at).toLocaleString():"Unknown"}
      ],sentiment));
      else inputCard.appendChild(h("p","News and AI have not produced a health check yet."));
      const access = inputs.quote_access || {}, eventInput = inputs.events || {};
      inputCard.appendChild(h("p",access.feed==="sip"?"Prices: consolidated SIP selected. Each quote must still pass freshness and spread checks.":
        access.feed==="iex"?"Prices: IEX only. Quiet symbols can have quote gaps; real-time SIP access is "+
        (access.capabilities?.sip==="SUBSCRIPTION_REQUIRED"?"blocked by the account subscription.":"unavailable or not selected."):"Price-feed access has not been checked."),
        h("p","Options: "+(eventInput.classified||0)+" / "+(eventInput.candidates||0)+" candidate events classified by the model committee. Classification alone does not approve a trade."),
        h("p.caption","AI runs for nonzero trade targets. Missing news or a failed model is recorded as unavailable, not as agreement with the trading rules. Sector context comes from fixed proxy companies, not direct ETF announcements."));
      body.appendChild(h("div.section",inputCard));

      body.appendChild(h("div.section",sectionHead("What each test is trying to improve"),
        h("div.grid.cols-3.research-ideas",
          h("div.card.card-pad",h("h3","Risk and recovery"),h("p","Compare QLD position limits and different rules for pausing or resuming. Position limits only matter when the strategy is allowed to trade.")),
          h("div.card.card-pad",h("h3","Diversification"),h("p","Check whether keeping ETF rotation improves the whole portfolio compared with removing it, after trading costs.")),
          h("div.card.card-pad",h("h3","Earnings opportunities"),h("p","Test whether price moves after earnings can add value without ignoring the portfolio's cash and risk limits.")))));

      body.appendChild(dashboardDisclosure("AI and options research","Separate tests of whether AI changes help and whether earnings options are tradable.",
        h("div.grid.cols-2",
          h("div",h("h3","Does AI improve decisions?"),h("p",ai?(ai.independent_changed_sessions||0)+" / 30 distinct days where AI changed the target.":"AI report unavailable."),
            h("p.caption","Repeating an AI call is not new evidence. We compare changed decisions with what the rules alone would have done.")),
          h("div",h("h3","Are earnings options worth their price?"),h("p",options?(options.promotion_evidence?.independent_outcomes||0)+" / 30 completed event outcomes.":"Options report unavailable."),
            h("p.caption","Needs an eligible event, tradable quotes and a completed exit. No experimental options orders are placed in this account.")))));

      const records = dashboardDisclosure("All experiments and technical evidence","Full comparisons, earlier results, price coverage and the reasons a day did not qualify.");
      let mounted=false;
      records.addEventListener("toggle",async()=>{
        if(!records.open||mounted)return;
        mounted=true;
        const node=await renderResearchEvidence(lab,ai||{},options||{});
        records.querySelector(".disclosure-body").appendChild(node);
      });
      body.appendChild(records);
    } catch (error) {body.replaceChildren(h("div.research-summary",h("strong.amber","Research could not be loaded"),h("p",error.message)));}
  })();
  return root;
}
