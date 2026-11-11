"""Complete broker history with explicit failure on ambiguous pagination."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def all_orders(client, *, page_size=500):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.common.enums import Sort
    rows, seen, until = [], set(), None
    for _ in range(10000):
        page = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=page_size, direction=Sort.DESC, until=until))
        for row in page:
            if str(row.id) not in seen:
                rows.append(row)
                seen.add(str(row.id))
        if len(page) < page_size:
            return rows
        # Overlap the boundary; deduplication avoids double counting. A full
        # page at one timestamp is ambiguous, so never claim it is complete.
        cursor = min(row.submitted_at for row in page) + timedelta(microseconds=1)
        if until is not None and cursor >= until:
            raise RuntimeError("order pagination stalled; history is incomplete")
        until = cursor
    raise RuntimeError("order history exceeded pagination bound")


def all_activities(config, kinds, *, get=None):
    import requests
    get = get or requests.get
    if not config.paper:
        raise RuntimeError("paper account required")
    headers = {"APCA-API-KEY-ID": config.api_key, "APCA-API-SECRET-KEY": config.secret_key}
    output, seen, token = [], set(), None
    for _ in range(10000):
        params = {"activity_types": ",".join(kinds), "page_size": 100, "direction": "desc"}
        if token:
            params["page_token"] = token
        response = get("https://paper-api.alpaca.markets/v2/account/activities",
                       headers=headers, params=params, timeout=15)
        response.raise_for_status()
        page = response.json()
        for row in page:
            if row["id"] not in seen:
                output.append(row)
                seen.add(row["id"])
        if len(page) < 100:
            return output
        cursor = page[-1]["id"]
        if cursor == token:
            raise RuntimeError("activity pagination stalled")
        token = cursor
    raise RuntimeError("activity history exceeded pagination bound")


def holdings_reconciliation(fills, positions):
    ledger = {}
    for row in fills:
        symbol = row["symbol"].replace("/", "")
        qty = Decimal(str(row["qty"]))
        ledger[symbol] = ledger.get(symbol, Decimal(0)) + qty * (1 if row["side"] == "buy" else -1)
    actual = {p.symbol.replace("/", ""): Decimal(str(p.qty)) for p in positions}
    differences = [{"symbol": s, "ledger_qty": float(ledger.get(s, 0)),
                    "broker_qty": float(actual.get(s, 0)),
                    "difference": float(actual.get(s, 0) - ledger.get(s, 0))}
                   for s in sorted(ledger.keys() | actual.keys())
                   if abs(actual.get(s, 0) - ledger.get(s, 0)) > Decimal("0.00001")]
    return {"status": "MATCHED" if not differences else "MISMATCH",
            "differences": differences, "filled_orders": len(fills),
            "note": "Splits, transfers and other corporate actions require activity reconciliation."}


def fill_ledger(activities, orders):
    """Aggregate actual fill activities, including partially filled cancellations."""
    statuses = {str(o.id): str(getattr(o.status,"value",o.status)) for o in orders}
    grouped = {}
    for row in activities:
        if row.get("activity_type") != "FILL":
            continue
        key = str(row["order_id"])
        qty, price = Decimal(str(row["qty"])), Decimal(str(row["price"]))
        if not qty.is_finite() or not price.is_finite() or qty <= 0 or price <= 0:
            raise ValueError("invalid fill activity; ledger cannot be reconciled")
        bucket = grouped.setdefault(key, {"order_id": key, "symbol": row["symbol"],
            "side": row["side"], "quantity": Decimal(0), "notional": Decimal(0),
            "filled": row["transaction_time"], "submitted": None,
            "status": statuses.get(key,"activity_only")})
        if bucket["symbol"] != row["symbol"] or bucket["side"] != row["side"]:
            raise ValueError("order contains multiple symbols; leg-level accounting required")
        bucket["quantity"] += qty
        bucket["notional"] += qty*price
        bucket["filled"] = max(bucket["filled"], row["transaction_time"])
    result=[]
    for row in grouped.values():
        qty, notional=row.pop("quantity"), row.pop("notional")
        result.append({**row, "qty":float(qty), "price":float(notional/qty),
                       "basis":"broker_fill_activities"})
    return sorted(result,key=lambda r:r["filled"])
