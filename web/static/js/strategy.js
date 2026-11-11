async function renderStrategy() {
  const body = h("div", h("p.dim", "Loading frozen experiments…"));
  const root = h("section.tab", {id:"tab-strategy",role:"tabpanel"},
    h("div.section", sectionHead("Which changes earn a place in the portfolio?",
      "Fixed experiments compare risk budgets, recovery rules and diversification. Broker performance is on the Live tab."),body));
  root._charts=()=>{};
  root._asyncReady=(async()=>{
    try {
      const lab=await getJSON("/api/portfolio_lab");
      body.replaceChildren();
      const pct=v=>v==null?"—":(v*100).toFixed(2)+"%";
      const candidates=lab.candidates||[];
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
        h("p",lab.next_action||"Awaiting first research run."))));
      const baseline=candidates.find(r=>r.name==="baseline_70");
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
