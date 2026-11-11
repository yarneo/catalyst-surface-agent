async function renderCrash() {
  const body=h("div",h("p.dim","Loading risk evidence…"));
  const root=h("section.tab",{id:"tab-crash",role:"tabpanel"},
    h("div.section",sectionHead("How exposed are we to a bad surprise?",
      "Current holdings, historical uncertainty and explicit stress scenarios."),body));
  root._charts=()=>{};
  root._asyncReady=(async()=>{
    try {
      const [risk,lab]=await Promise.all([getJSON("/api/portfolio_risk"),getJSON("/api/portfolio_lab")]);
      body.replaceChildren();
      const pct=v=>v==null?"—":(v*100).toFixed(2)+"%";
      body.appendChild(h("div.card.card-pad",
        sectionHead("Current-position tail estimate","A historical estimate, not a guaranteed maximum loss."),
        h("p","Status: "+risk.status+" · "+(risk.observations||0)+" aligned sessions · "+(risk.tail_observations||0)+" tail observations"),
        h("p","99% daily VaR: "+pct(risk.var_fraction)+" · expected shortfall: "+pct(risk.expected_shortfall_fraction)),
        h("p","Tail reliability: "+(risk.tail_reliability||"not established")),
        h("p.dim",risk.uncertainty_note||risk.reason||""),
        h("p","90% resampled ES range: "+(risk.es_bootstrap_90pct_interval||[]).map(pct).join(" to "))));
      body.appendChild(h("div.section",h("div.card.card-pad",
        sectionHead("Hypothetical simultaneous shocks","Includes a falling hedge and a fast rebound while underinvested. These are scenarios, not historical predictions."),
        buildTable([
          {h:"Scenario",cell:r=>r.name.replaceAll("_"," ")},
          {h:"Portfolio move",cell:r=>pct(r.hypothetical_return)},
          {h:"Assumption",cell:r=>r.assumption}
        ],risk.stress_scenarios||[]))));
      body.appendChild(h("div.section",h("div.card.card-pad",
        sectionHead("Crisis coverage","Unavailable fund history is shown explicitly. Conditional proxy results cannot validate missing periods."),
        buildTable([
          {h:"Period",cell:r=>r.name},
          {h:"Coverage",cell:r=>r.status},
          {h:"Proxy return",cell:r=>pct(r.metrics?.return)},
          {h:"Proxy drawdown",cell:r=>pct(r.metrics?.max_drawdown)},
          {h:"QQQ actual period return",cell:r=>pct(r.qqq_return)}
        ],lab.historical_crises||[]))));
    } catch(e) {body.replaceChildren(h("p","Risk evidence unavailable: "+e.message));}
  })();
  return root;
}
