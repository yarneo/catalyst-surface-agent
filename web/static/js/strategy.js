async function renderStrategy() {
  const body = h("div", h("p.dim", "Loading frozen experiments…"));
  const root = h("section.tab", {id:"tab-strategy",role:"tabpanel"},
    h("div.section", sectionHead("Which changes earn a place in the portfolio?",
      "Fixed experiments compare risk budgets, recovery rules and diversification. Broker performance is on the Live tab."),body));
  root._charts=()=>{};
  root._asyncReady=(async()=>{
    try {
      const [lab,ai,options]=await Promise.all([
        getJSON("/api/portfolio_lab"),getJSON("/api/ai_attribution"),getJSON("/api/event_options_shadow")]);
      body.replaceChildren();
      const pct=v=>v==null?"—":(v*100).toFixed(2)+"%";
      const candidates=lab.candidates||[];
      const replay=lab.production_verification||{},pead=lab.pead_collection||{};
      const baseline=candidates.find(r=>r.name==="baseline_70");
      body.appendChild(h("div.section",h("div.card.card-pad",
        sectionHead("What is ready, and what needs time?","Collectors run automatically. Sample thresholds allow review; they do not establish a profitable strategy."),
        buildTable([
          {h:"Evidence",cell:r=>r[0]},{h:"Progress",cell:r=>r[1]},{h:"What happens next",cell:r=>r[2]},
        ],[
          ["Production replay",(replay.matching_sessions||[]).length+" / 5 open sessions",
            replay.status+" · "+(replay.verified_order_requests||0)+" order requests verified; requires at least one order"],
          ["Portfolio experiments",(baseline?.forward?.observations||0)+" / 60 sessions",
            (lab.forward_collection?.status||"Awaiting collector")+" · 12 weekly blocks, costs and parity also required"],
          ["AI contribution",(ai.independent_changed_sessions||0)+" / 30 changed sessions",
            "Only sessions where AI changes the target count; repeated calls add no evidence"],
          ["Options",(options.promotion_evidence?.independent_outcomes||0)+" / 30 completed events",
            "Needs an eligible event, executable quotes and a completed exit"],
          ["Earnings drift (PEAD)",(pead.forecast_events||0)+" archived forecasts · "+(pead.open_positions||0)+" virtual positions",
            (pead.stale?"STALE":pead.status||"Awaiting collector")+" · "+(pead.completed_outcomes||0)+" completed outcomes; each hold lasts 60 sessions"],
        ]),
        h("p.dim","PEAD is a separate virtual book until its allocation comparison is validated. Missing historical health decisions and venue quotes cannot be recreated by waiting."),
        h("p",lab.report_stale?"Research report is behind the latest completed session.":"Research report covers the latest completed session."),
        h("p.dim","PEAD last observation: "+(pead.at||"pending")+" · feed problems: "+Object.keys(pead.feed_errors||{}).join(", ")))));
      body.appendChild(h("div.callout.info",h("span",
        "Promotion gate: "+(lab.parity?.passed?"parity verified":"parity not yet verified")+". "+
        (lab.parity?.blockers||[]).join(" · "))));
      body.appendChild(h("div.section",h("div.card.card-pad",
        sectionHead("Frozen forward comparison","New observations count only after the cohort was frozen. Prior research returns cannot be recycled into this sample."),
        buildTable([
          {h:"Experiment",cell:r=>r.name.replaceAll("_"," ")},
          {h:"Sessions",cell:r=>String(r.forward?.observations||0)},
          {h:"Return",cell:r=>r.forward?.observations?pct(r.forward.return):"Collecting"},
          {h:"Drawdown",cell:r=>pct(r.forward?.max_drawdown)},
          {h:"State",cell:r=>r.promotion?.status||r.status},
        ],candidates),
        h("p.dim","Frozen "+(lab.frozen_at||"pending")+" · "+(lab.cohort||"pending")),
        h("p.dim","Forward sessions begin after "+(lab.forward_start_after||"the freeze date")+". An earlier opening price on the freeze date cannot count as a forward fill."),
        h("p",lab.next_action||"Awaiting first research run."))));
      if((baseline?.curve||[]).length>1) {
        const canvas=h("canvas");
        body.appendChild(h("div.section",h("div.card.chart-card",
          sectionHead("Forward virtual equity","Equal starting capital, modeled trading costs; these are not broker balances."),
          h("div.chart-box",{style:"height:320px"},canvas))));
        const palette=["var(--pos-line)","var(--c-qqq)","var(--c-regime)","var(--amber)","var(--text-3)"];
        makeLineChart(canvas,baseline.curve.map(r=>new Date(r.date+"T12:00:00Z")),
          candidates.filter(r=>r.curve?.length===baseline.curve.length).slice(0,5).map((r,i)=>({
            label:r.name.replaceAll("_"," "),data:r.curve.map(x=>x.equity),color:palette[i]
          })),{yfmt:v=>"$"+Math.round(v).toLocaleString()});
      }
      body.appendChild(h("div.section",h("div.card.card-pad",
        sectionHead("Conditional historical research","Whole-portfolio daily-bar proxies. Today's ETF selection is used retrospectively, so these are not validated live-strategy returns."),
        buildTable([
          {h:"Experiment",cell:r=>r.name.replaceAll("_"," ")},
          {h:"CAGR",cell:r=>pct(r.historical?.cagr)},
          {h:"Drawdown",cell:r=>pct(r.historical?.max_drawdown)},
          {h:"Upside capture",cell:r=>pct(r.historical?.upside_capture)},
          {h:"CAGR at 25bp costs",cell:r=>pct(r.high_cost?.cagr)},
          {h:"Evidence",cell:r=>r.reason||r.status},
        ],candidates))));
    } catch(e) { body.replaceChildren(h("p","Research data unavailable: "+e.message)); }
  })();
  return root;
}
