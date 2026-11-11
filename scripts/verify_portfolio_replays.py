"""Verify captured production cycles offline. Never connects to a broker."""
from pathlib import Path
import json
from trading_bot.broker.replay import verify_recorded_cycle
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
        output=data/"research/verified_replays"/(folder.name+"-"+engine[:12]+".json")
        if output.exists():
            result=json.loads(output.read_text())
        else:
            try:result=verify_recorded_cycle(folder)
            except Exception as exc:
                result={"status":"INCOMPLETE_CAPTURE" if not (folder/"integrity.json").exists() else "FAILED",
                        "cycle":folder.name,"error_type":type(exc).__name__}
            if result["status"]!="INCOMPLETE_CAPTURE":atomic_json(output,result)
        results.append(result)
    matching=[r for r in results if r.get("status")=="MATCHED" and r.get("engine_hash")==engine]
    sessions=sorted({r["session"] for r in matching})
    orders=sum(r.get("order_requests_verified",0) for r in matching)
    failures=[r for r in results if r.get("status") in {"FAILED","MISMATCH"}
              and r.get("engine_hash",engine)==engine]
    out={"at":iso_now(),"status":"MISMATCH" if failures else "VERIFIED" if len(sessions)>=5 and orders else "COLLECTING" if results else "AWAITING_CAPTURE",
         "engine_hash":engine,"matching_sessions":sessions,"required_sessions":5,
         "verified_order_requests":orders,"mismatches":len(failures),"cycles":results,
         "network_access":False,"order_authority":False,
         "scope":"Exact production request/response and risk-state replay. Daily research proxy and strategy profitability need separate evidence."}
    atomic_json(data/"research/production_verification.json",out)
    return out


if __name__=="__main__":
    report=verify_all()
    print(json.dumps({k:report[k] for k in ("status","matching_sessions","verified_order_requests","mismatches")}))
