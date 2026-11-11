"""Journaled order lifecycle. A submitted order is not a completed rebalance."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..runtime_state import atomic_json
from ..sessions import iso_now

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


class ExecutionFailure(RuntimeError):
    pass


def status_of(order) -> str:
    value = getattr(order, "status", "unknown")
    return str(getattr(value, "value", value)).lower()


def error_status(exc) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class OrderCoordinator:
    def __init__(self, client, path: Path, cycle_id: str, *, timeout: float = 120,
                 poll: float = 1, sleep=time.sleep):
        self.client, self.path, self.cycle_id = client, path, cycle_id
        self.timeout, self.poll, self.sleep = timeout, poll, sleep

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "orders": {}}
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 1 or not isinstance(data.get("orders"), dict):
            raise ExecutionFailure("invalid order journal; refusing new orders")
        return data

    def _record(self, coid: str, **changes) -> dict:
        state = self._load()
        row = state["orders"].setdefault(coid, {"client_order_id": coid})
        row.update(changes, updated_at=iso_now())
        atomic_json(self.path, state)
        return row

    def _mark(self, coid: str, order) -> str:
        status = status_of(order)
        self._record(coid, status=status, order_id=str(order.id),
                     filled_qty=str(getattr(order, "filled_qty", 0) or 0),
                     filled_avg_price=str(getattr(order, "filled_avg_price", "") or ""))
        return status

    def reconcile(self) -> list[dict]:
        """An unknown submission stays unresolved until the broker identifies it."""
        unresolved = []
        for coid, row in self._load()["orders"].items():
            if row.get("status") in TERMINAL:
                continue
            try:
                order = self.client.get_order_by_client_id(coid)
                if self._mark(coid, order) not in TERMINAL:
                    unresolved.append(row)
            except Exception:
                unresolved.append(row)
        return unresolved

    def execute(self, request, *, sleeve: str, reference: float | None,
                quote: dict | None = None):
        payload = f"{self.cycle_id}|{sleeve}|{request.symbol}|{request.side}|{request.qty}"
        coid = "pf-" + hashlib.sha256(payload.encode()).hexdigest()[:40]
        request.client_order_id = coid
        prior = self._load()["orders"].get(coid)
        if prior:
            try:
                order = self.client.get_order_by_client_id(coid)
            except Exception as exc:
                raise ExecutionFailure("prior order cannot be reconciled; no resubmission") from exc
        else:
            unresolved = self.reconcile()
            if unresolved:
                raise ExecutionFailure("unresolved prior submission; new orders blocked")
            self._record(coid, status="prepared", cycle_id=self.cycle_id,
                         symbol=request.symbol, side=str(getattr(request.side, "value", request.side)),
                         requested_qty=str(request.qty), sleeve=sleeve,
                         reference_price=reference, quote=quote, submitted_at=iso_now())
            try:
                order = self.client.submit_order(request)
            except Exception as exc:
                self._record(coid, status="rejected" if error_status(exc) in {400, 401, 403, 422}
                             else "unknown", error_type=type(exc).__name__)
                raise ExecutionFailure("order submission failed; broker reconciliation required") from exc
        waited = 0.0
        while True:
            status = self._mark(coid, order)
            if status == "filled":
                if abs(float(order.filled_qty) - float(request.qty)) > 1e-8:
                    raise ExecutionFailure("filled quantity does not match the intended quantity")
                return order
            if status in TERMINAL:
                raise ExecutionFailure(f"order ended {status}; dependent purchases blocked")
            if waited >= self.timeout:
                self._record(coid, status=status, timed_out=True)
                raise ExecutionFailure("order fill timed out; dependent purchases blocked")
            self.sleep(self.poll)
            waited += self.poll
            try:
                order = self.client.get_order_by_id(order.id)
            except Exception as exc:
                raise ExecutionFailure("cannot confirm order fill; dependent purchases blocked") from exc
