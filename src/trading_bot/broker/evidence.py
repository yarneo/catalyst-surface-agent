"""Private broker-response tape for deterministic, network-free verification."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from ..runtime_state import DATA, append_record, atomic_json
from ..sessions import iso_now

_RECORDER = ContextVar("portfolio_recorder", default=None)
CLIENT_METHODS = frozenset({"get_account", "get_clock", "get_all_positions", "get_open_position",
    "get_orders", "get_order_by_client_id", "get_order_by_id", "submit_order", "cancel_order_by_id", "close_position"})


def encode(value):
    if isinstance(value, Enum):
        return encode(value.value)
    if value is None or isinstance(value,(str,int,float,bool)):
        return value
    if isinstance(value,datetime):
        return {"__datetime__":value.isoformat()}
    if isinstance(value,(Decimal,UUID,date)):
        return str(value)
    if isinstance(value,dict):
        return {str(k):encode(v) for k,v in value.items()}
    if isinstance(value,(list,tuple,set)):
        return [encode(v) for v in value]
    if hasattr(value,"model_dump"):
        fields=value.model_dump()
    elif is_dataclass(value):
        fields=asdict(value)
    elif isinstance(value,SimpleNamespace):
        fields=vars(value)
    else:
        raise TypeError("unsupported private evidence type: "+type(value).__name__)
    # Account credentials/identifiers are irrelevant to decision replay.
    fields={k:v for k,v in fields.items() if k not in {"account_number","account_id","api_key","secret_key"}}
    return {"__record__":encode(fields)}


def decode(value):
    if isinstance(value,list):return [decode(v) for v in value]
    if isinstance(value,dict):
        if set(value)=={"__datetime__"}:return datetime.fromisoformat(value["__datetime__"])
        if set(value)=={"__record__"}:return SimpleNamespace(**decode(value["__record__"]))
        return {k:decode(v) for k,v in value.items()}
    return value


def engine_fingerprint(root):
    paths=[Path(root)/"scripts/run_portfolio.py"]
    paths += [p for p in (Path(root)/"src/trading_bot").rglob("*.py") if
              p.parts[-2] in {"broker","live","risk","strategies","learning","data","signals"}
              or p.name in {"allocations.py","cycle.py","config.py","ownership.py","marketdata.py","sessions.py","portfolio_policy.py"}]
    return hashlib.sha256("".join(p.relative_to(root).as_posix()+hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(set(paths))).encode()).hexdigest()


class Recorder:
    def __init__(self,folder,metadata=None):
        self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        (self.folder/"broker.jsonl").touch(mode=0o600)
        self.metadata=metadata or {}
        self.sequence=0
        atomic_json(self.folder/"metadata.json",self.metadata)

    def call(self,name,fn,*args,**kwargs):
        row={"sequence":self.sequence,"method":name,"args":encode(args),"kwargs":encode(kwargs)}
        self.sequence+=1
        try:
            result=fn(*args,**kwargs)
        except BaseException as exc:
            row["error"]={"type":type(exc).__name__,"status_code":getattr(exc,"status_code",None)}
            append_record(self.folder/"broker.jsonl",row)
            raise
        row["result"]=encode(result)
        append_record(self.folder/"broker.jsonl",row)
        return result


class RecordingClient:
    def __init__(self,client,recorder):
        self.client,self.recorder=client,recorder
    def __getattr__(self,name):
        if name not in CLIENT_METHODS:
            raise AttributeError("broker method is outside recorded interface: "+name)
        return lambda *a,**k:self.recorder.call(name,getattr(self.client,name),*a,**k)


def capture_broker(broker):
    recorder=_RECORDER.get()
    if recorder is None:return broker
    recorder.metadata["initial_journal"]=broker.orders._load()
    recorder.broker = broker
    broker._client=RecordingClient(broker._client,recorder)
    broker.orders.client=broker._client
    quote=broker.quote
    broker.quote=lambda symbol:recorder.call("quote",quote,symbol)
    return broker


def capture_inputs(factory):
    recorder=_RECORDER.get()
    if recorder is not None:
        recorder.metadata.update(factory())
        atomic_json(recorder.folder/"metadata.json",recorder.metadata)


def finish_capture():
    """Archive final broker truth without changing the trading outcome."""
    recorder=_RECORDER.get()
    if recorder is None:return
    try:
        account=recorder.broker.get_account()
        positions=recorder.broker.get_positions()
        recorder.metadata["final_account"]={k:float(getattr(account,k)) for k in ("cash","portfolio_value","last_equity")}
        recorder.metadata["final_positions"]={p.symbol:{"qty":float(p.qty),"avg_entry":float(p.avg_entry_price),
            "price":float(p.current_price)} for p in positions}
    except Exception as exc:
        recorder.metadata["final_capture_error"]=type(exc).__name__


def external(name,fn,*args,**kwargs):
    recorder=_RECORDER.get()
    return recorder.call(name,fn,*args,**kwargs) if recorder else fn(*args,**kwargs)


@contextmanager
def capture_cycle(enabled,root):
    if not enabled:
        yield
        return
    from ..marketdata import captured_signals
    folder=DATA/"production_cycles"/(iso_now().replace(":","")+"-"+uuid4().hex[:8])
    recorder=Recorder(folder,{"started_at":iso_now(),"engine_hash":engine_fingerprint(root),"status":"RUNNING"})
    token=_RECORDER.set(recorder)
    try:
        yield
    except BaseException as exc:
        recorder.metadata.update(status="FAILED",error_type=type(exc).__name__)
        raise
    else:
        recorder.metadata["status"]="COMPLETED"
    finally:
        _RECORDER.reset(token)
        atomic_json(folder/"signals.json",captured_signals())
        recorder.metadata.update(completed_at=iso_now(),recorded_calls=recorder.sequence)
        atomic_json(folder/"metadata.json",recorder.metadata)
        files=("metadata.json","signals.json","broker.jsonl")
        atomic_json(folder/"integrity.json",{name:hashlib.sha256((folder/name).read_bytes()).hexdigest()
                                           for name in files if (folder/name).exists()})


class ReplayMismatch(RuntimeError):
    pass


class RecordedRemoteError(RuntimeError):
    def __init__(self,row):
        self.status_code=row.get("status_code")
        super().__init__("recorded "+row["type"])


class PlaybackClient:
    def __init__(self,rows):
        self.rows,self.index=rows,0
    def consume(self,name,*args,**kwargs):
        if self.index >= len(self.rows):
            raise ReplayMismatch("extra call: "+name)
        row=self.rows[self.index]
        if (row["method"],row["args"],row["kwargs"]) != (name,encode(args),encode(kwargs)):
            raise ReplayMismatch(f"call {self.index}: expected {row['method']}, observed {name}; decision inputs or order request differ")
        self.index+=1
        if row.get("error"):raise RecordedRemoteError(row["error"])
        return decode(row.get("result"))
    def __getattr__(self,name):
        if name not in CLIENT_METHODS:raise AttributeError(name)
        return lambda *a,**k:self.consume(name,*a,**k)
    def assert_finished(self):
        if self.index != len(self.rows):raise ReplayMismatch("recorded broker calls were omitted")
