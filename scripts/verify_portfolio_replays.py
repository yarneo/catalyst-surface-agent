"""Verify captured production cycles offline. Never connects to a broker."""
from pathlib import Path
import hashlib
import json
from trading_bot.broker.replay import verify_recorded_cycle
from trading_bot.broker.comparison import compare_cycle
from trading_bot.broker.evidence import engine_fingerprint
from trading_bot.runtime_state import DATA, atomic_json
from trading_bot.sessions import iso_now


def verify_all(data=DATA):
    data=Path(data)
    root=Path(__file__).resolve().parents[1]
    engine=engine_fingerprint(root)
    results=[]
    for folder in sorted((data/"production_cycles").glob("*")):
        if not folder.is_dir():continue
        capture_hash=hashlib.sha256(b"".join((folder/name).read_bytes() if (folder/name).exists() else b"MISSING"
            for name in ("metadata.json","broker.jsonl","signals.json","integrity.json"))).hexdigest()
        output=data/"research/verified_replays"/(folder.name+"-"+engine[:12]+".json")
        result=json.loads(output.read_text()) if output.exists() else {}
        if result.get("capture_hash")!=capture_hash:
            try:
                result=verify_recorded_cycle(folder)
                result["simulator_comparison"]=compare_cycle(folder)
            except Exception as exc:
                result={"status":"INCOMPLETE_CAPTURE" if not (folder/"integrity.json").exists() else "FAILED",
                        "cycle":folder.name,"error_type":type(exc).__name__}
            result["capture_hash"]=capture_hash
            if result["status"]!="INCOMPLETE_CAPTURE":atomic_json(output,result)
        results.append(result)
    matching=[r for r in results if r.get("status")=="MATCHED" and r.get("engine_hash")==engine]
    sessions=sorted({r["session"] for r in matching})
    orders=sum(r.get("order_requests_verified",0) for r in matching)
    failures=[r for r in results if r.get("status") in {"FAILED","MISMATCH"}
              and r.get("engine_hash",engine)==engine]
    comparisons=[r["simulator_comparison"] for r in matching if "simulator_comparison" in r]
    compared=sorted({r["session"] for r in comparisons if r.get("passed")})
    comparison_failures=[r for r in comparisons if not r.get("passed") and r["status"]!="AWAITING_COMPLETE_CAPTURE"]
    compared_orders=sum(r.get("orders",0) for r in comparisons if r.get("passed"))
    out={"at":iso_now(),"status":"MISMATCH" if failures else "VERIFIED" if len(sessions)>=5 and orders else "COLLECTING" if results else "AWAITING_CAPTURE",
         "engine_hash":engine,"matching_sessions":sessions,"required_sessions":5,
         "verified_order_requests":orders,"mismatches":len(failures),"cycles":results,
         "network_access":False,"order_authority":False,
         "simulator_comparison":{"status":"MISMATCH" if comparison_failures else "VERIFIED" if len(compared)>=5 and compared_orders else "COLLECTING" if comparisons else "AWAITING_CAPTURE",
             "matching_sessions":compared,"required_sessions":5,"verified_order_requests":compared_orders,
             "failures":comparison_failures,"engineering_ready":True},
         "scope":"Exact production requests plus independent cash/holdings/cost-basis and risk-state comparison. Five successful opens including an order are required; historical proxies cannot qualify."}
    atomic_json(data/"research/production_verification.json",out)
    return out


if __name__=="__main__":
    report=verify_all()
    print(json.dumps({k:report[k] for k in ("status","matching_sessions","verified_order_requests","mismatches")}))
