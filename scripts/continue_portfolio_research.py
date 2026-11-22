"""Continue every virtual book after a validation repair; never place orders."""
import argparse
import json

from trading_bot.forward_lab import continue_books
from trading_bot.runtime_state import atomic_json, execution_lock, DATA
from trading_bot.sessions import iso_now


def run(predecessor):
    from portfolio_lab import context
    with execution_lock(DATA/"portfolio_lab.lock"):
        active=DATA/"research/active_cohort.json"
        previous=json.loads(active.read_text()) if active.exists() else None
        try:
            folder,_,_,_,cohort,manifest=context()
            report=folder/"portfolio_lab.json"
            if report.exists():
                old=json.loads(report.read_text())
                if old.get("cohort")==predecessor:
                    archive=folder/"cohorts"/predecessor/"report.json"
                    if not archive.exists():atomic_json(archive,old)
            result=continue_books(folder,manifest,predecessor,at=iso_now())
        except BaseException:
            if previous is not None:atomic_json(active,previous)
            raise
        print(json.dumps({"status":"BOOKS_CONTINUED","cohort":cohort,
            "predecessor":predecessor,"books":sum(len(v) for v in result["opening_books"].values()),
            "last_session":result["continuation"]["last_session"],"order_authority":False}))
        return result


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--from-cohort",required=True)
    run(p.parse_args().from_cohort)
