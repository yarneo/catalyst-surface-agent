"""Explain observed validation failures separately from implementation status."""
def apply_research_health(report):
    replay=report.get("production_verification",{})
    market=report.get("market_collection",{})
    coverage=market.get("session_coverage",{})
    issues=[];waiting=[]
    if replay.get("mismatches",0):
        issues.append(f"{replay['mismatches']} production replays failed; inspect the first recorded error")
    legacy=len(replay.get("legacy_precision_sessions",[]))
    if legacy and len(replay.get("matching_sessions",[]))<5:
        waiting.append(f"{legacy} older sessions used rounded inputs and remain diagnostic; five fresh lossless opens including an order are required")
    gaps=coverage.get("quote_gap_minutes_by_symbol",{})
    actionable_gaps={s:n for s,n in gaps.items() if s!="BIL"}
    if coverage.get("missing_tick_minutes",0):
        issues.append(f"{coverage['missing_tick_minutes']} scheduled quote observations are missing")
    if actionable_gaps:
        text=", ".join(f"{s}: {count}" for s,count in sorted(gaps.items()))
        issues.append("Latest observed session has quote gaps by symbol (minutes): "+text)
    elif report.get("forward_collection",{}).get("status") in {"BLOCKED","DATA_INCOMPLETE"}:
        issues.append(report["forward_collection"].get("reason","forward collection is incomplete"))
    comparison=replay.get("simulator_comparison",{})
    if comparison.get("status")=="MISMATCH":
        issues.append("Independent simulator comparison failed")
    access=market.get("feed_access",report.get("market_feed_access",{}))
    if actionable_gaps and access.get("capabilities",{}).get("sip")=="SUBSCRIPTION_REQUIRED":
        issues.append("Real-time consolidated SIP data is unavailable on this account; IEX retries cannot guarantee complete coverage")
    report["pipeline"]={"status":"ACTION_REQUIRED" if issues else "AWAITING_OBSERVATIONS",
        "issues":issues,"waiting_for":waiting,"implementation":"IMPLEMENTED"}
    if issues:
        report["status"]="DATA_BLOCKED"
        report["next_action"]=" · ".join(issues+waiting)
    elif waiting:
        report["next_action"]=" · ".join(waiting)
    return report
