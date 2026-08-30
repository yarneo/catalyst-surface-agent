"""Read-only Catalyst Surface Agent result and evidence dashboard."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.options.book import Book, BookCorrupt  # noqa: E402
from trading_bot.options.clock import ET, now_et  # noqa: E402
from trading_bot.tournament.audit import AuditCorrupt, AuditLedger  # noqa: E402
from trading_bot.tournament.scheduled import ScheduledEventPolicy  # noqa: E402


START = dt.datetime(2026, 8, 31, 9, 30, tzinfo=ET)
DEADLINE = dt.datetime(2026, 9, 4, 9, 30, tzinfo=ET)
POLICY = ScheduledEventPolicy()

DECISIONS = [
    {
        "Idea": "Large short-volatility condor portfolio",
        "Evidence": "$67,728 max risk; early equity near $96.5k; correlated losses",
        "Decision": "Retired",
    },
    {
        "Idea": "Broad direct-news continuation",
        "Evidence": "3 events; +0.461% mean at 60m, +0.018% at 120m",
        "Decision": "Shadow only",
    },
    {
        "Idea": "NFP miss → BTC bridge",
        "Evidence": "3 signals after 30 bp cost; -0.086% compounded",
        "Decision": "Rejected",
    },
    {
        "Idea": "AVGO direction / peer spillover",
        "Evidence": "Direct +2h -0.594%; selected peers +2h -0.267%",
        "Decision": "Rejected",
    },
    {
        "Idea": "ISM continuation",
        "Evidence": "14 events; -0.088% to 10:30, +0.055% to 11:00",
        "Decision": "Rejected",
    },
    {
        "Idea": "AVGO earnings ATM straddle",
        "Evidence": "8 historical option-bar events: +44.29% mean, +28.49% median",
        "Decision": "Final conditional strategy",
    },
]

STRADDLES = [
    ("2024-09-05", 6.20, 30.15, -6.36),
    ("2024-12-12", 7.60, 216.69, 154.52),
    ("2025-03-06", 9.31, -9.82, -35.69),
    ("2025-06-05", 6.63, -66.86, -75.57),
    ("2025-09-04", 5.74, 111.92, 100.06),
    ("2025-12-11", 6.65, 26.83, 5.73),
    ("2026-03-04", 7.74, -28.21, -50.60),
    ("2026-06-03", 8.63, 73.62, 52.40),
]


st.set_page_config(page_title="Catalyst Surface Agent", page_icon="⚡",
                   layout="wide", initial_sidebar_state="expanded")


def _stretch_kwarg() -> dict[str, object]:
    """Return the full-width keyword this Streamlit build understands.

    Streamlit renamed ``use_container_width`` to ``width`` in 1.49. Passing the
    wrong one raises TypeError mid-script, which leaves the sidebar rendered and
    the whole page below it blank. The deployed environment does not always
    match the pin, so resolve the name once rather than trusting the version.
    """
    try:
        major, minor = (int(part) for part in st.__version__.split(".")[:2])
    except ValueError:
        return {"width": "stretch"}
    if (major, minor) >= (1, 49):
        return {"width": "stretch"}
    return {"use_container_width": True}


STRETCH = _stretch_kwarg()


def _credentials() -> tuple[str | None, str | None]:
    try:
        return st.secrets["ALPACA_API_KEY"], st.secrets["ALPACA_SECRET_KEY"]
    except Exception:  # noqa: BLE001
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if key and secret:
            return key, secret
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env.local")
        return values.get("ALPACA_API_KEY"), values.get("ALPACA_SECRET_KEY")


@st.cache_data(ttl=60, show_spinner=False)
def _account() -> dict | None:
    import requests
    key, secret = _credentials()
    if not key or not secret:
        return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    base = "https://paper-api.alpaca.markets"
    account = requests.get(f"{base}/v2/account", headers=headers, timeout=10)
    positions = requests.get(f"{base}/v2/positions", headers=headers, timeout=10)
    account.raise_for_status()
    positions.raise_for_status()
    return {"account": account.json(), "positions": positions.json()}


def _audit_rows():
    return AuditLedger(ROOT / "data" / "event_evidence.jsonl").read()


def _preflight_rows():
    live_path = ROOT / "data" / "preflight_evidence.jsonl"
    published_path = ROOT / "evidence" / "preflight_evidence.jsonl"
    return AuditLedger(live_path if live_path.exists() else published_path).read()


def _latest(rows, event_type):
    return next((row for row in reversed(rows)
                 if row.event_type == event_type), None)


def _display_safe(value, *, key=""):
    """Keep dashboard inspection useful without exposing account identifiers."""
    blocked = ("secret", "password", "api_key", "authorization",
               "credential", "account_number", "access_token")
    if any(part in key.lower() for part in blocked):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _display_safe(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_display_safe(item) for item in value]
    return value


@st.cache_data(ttl=300, show_spinner=False)
def _funnel() -> dict | None:
    """Load the credential-free artifact behind this week's sealed plan."""
    path = ROOT / "evidence" / "event_premium_funnel.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _book():
    return Book(ROOT / "data" / "event_book.json")


def _remaining(value: dt.timedelta) -> str:
    seconds = max(0, int(value.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m"


def _runtime_mode() -> str:
    value = os.getenv("TOURNAMENT_ENABLE_ORDERS")
    if value is None:
        try:
            value = st.secrets.get("TOURNAMENT_ENABLE_ORDERS")
        except Exception:  # noqa: BLE001
            value = None
    if value is None:
        from dotenv import dotenv_values
        value = dotenv_values(ROOT / ".env.local").get(
            "TOURNAMENT_ENABLE_ORDERS")
    if value == "YES":
        return "ORDER-ENABLED"
    return "SHADOW" if value == "NO" else "PUBLIC DEMO"


def _phase(now: dt.datetime, *, has_position: bool) -> tuple[str, str, str]:
    if now < START:
        return (
            "PREFLIGHT", "WAIT",
            f"No trading yet. Measurement begins {START:%a %b %d at %H:%M ET}.",
        )
    if has_position:
        if now >= POLICY.exit_at:
            return (
                "EXITING", "EXIT",
                "The fixed exit time has arrived; the agent keeps trying until flat.",
            )
        return (
            "POSITION OPEN", "HOLD",
            f"Hold through the event, then exit {POLICY.exit_at:%a at %H:%M ET}.",
        )
    if now < POLICY.entry_start:
        return (
            "OBSERVING", "WAIT",
            f"Watching only. Entry evaluation starts {POLICY.entry_start:%a at %H:%M ET}.",
        )
    if now <= POLICY.entry_end:
        return (
            "ENTRY WINDOW", "EVALUATE",
            "Fresh quotes and every frozen gate must pass; otherwise the action is no trade.",
        )
    return (
        "COMPLETE", "NO NEW TRADE",
        "The one permitted entry window has passed.",
    )


now = now_et()
try:
    account_payload = _account()
except Exception as exc:  # noqa: BLE001
    account_payload = None
    account_error = f"{type(exc).__name__}: {exc}"
else:
    account_error = None

try:
    audit = _audit_rows()
    audit_ok = True
except AuditCorrupt as exc:
    audit, audit_ok = [], False
    st.error(f"Evidence chain failed verification: {exc}")

try:
    preflight = _preflight_rows()
    preflight_ok = True
except AuditCorrupt as exc:
    preflight, preflight_ok = [], False
    st.error(f"Preflight evidence chain failed verification: {exc}")

try:
    book = _book()
except BookCorrupt as exc:
    book = None
    st.error(f"Position registry failed verification: {exc}")

equity = None
positions = []
if account_payload:
    equity = float(account_payload["account"]["equity"])
    positions = account_payload["positions"]

mode = _runtime_mode()
phase, action, explanation = _phase(now, has_position=bool(positions))
rehearsal = _latest(preflight, "rehearsal_summary")
surface_row = _latest(preflight, "surface_diagnostic")
semantic_rows = [row for row in preflight
                 if row.event_type == "featherless_preflight"]
semantic_row = semantic_rows[-1] if semantic_rows else None

st.sidebar.title("Catalyst Surface Agent")
st.sidebar.caption("Yar + Starboi")
st.sidebar.markdown("**How to read this**")
st.sidebar.markdown(
    "1. Start with **Current action**.  \n"
    "2. Check whether the gates pass.  \n"
    "3. Open details only if you want proof."
)
st.sidebar.divider()
st.sidebar.markdown(
    f"**Entry check:** {POLICY.entry_start:%a %b %d, %H:%M}–"
    f"{POLICY.entry_end:%H:%M} ET  \n"
    f"**Planned exit:** {POLICY.exit_at:%a %b %d, %H:%M} ET  \n"
    f"**Final cutoff:** {DEADLINE:%a %b %d, %H:%M} ET"
)
st.sidebar.metric("Time to cutoff", _remaining(DEADLINE - now))
if st.sidebar.button("Refresh", **STRETCH):
    st.cache_data.clear()
    st.rerun()

st.title("Catalyst Surface Agent")
st.caption(
    "One autonomous weekly pipeline: discover → validate → seal → execute. "
    "This week's sealed plan contains one conditional, direction-neutral AVGO trade."
)

st.info(
    f"### Current action: {action}\n"
    f"**{phase} · {mode}** — {explanation}"
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Mode", mode)
m2.metric("Position", "FLAT" if not positions else f"{len(positions)} leg(s)")
m3.metric(
    "Measured P&L",
    "Not started" if now < START else
    f"${equity - 100_000:+,.2f}" if equity is not None else "Not published",
)
m4.metric(
    "Evidence health",
    "VERIFIED" if audit_ok and preflight_ok else "FAILED",
    f"{len(preflight)} preflight · {len(audit)} runtime",
    delta_color="off",
)

tabs = st.tabs(["Overview", "Decision evidence", "Operations & audit"])

with tabs[0]:
    st.subheader("How the weekly agent reached today's action")
    phase1, phase2, phase3, phase4 = st.columns(4)
    with phase1:
        st.markdown("#### 1 · Discover")
        st.write("Scan 64 liquid names and require two calendar sources to agree.")
    with phase2:
        st.markdown("#### 2 · Validate")
        st.write("Use Alpaca MCP, Featherless quorum, and historical option replays.")
    with phase3:
        st.markdown("#### 3 · Seal")
        st.write("Freeze the promoted weekly plan before measured P&L begins: AVGO.")
    with phase4:
        st.markdown("#### 4 · Execute")
        st.write("Recheck live gates, size exact loss, trade, reconcile, and exit autonomously.")
    st.caption(
        "The plan is frozen to prevent hindsight and strategy drift. AVGO is "
        "this week's selected plan, not a separately hard-coded product."
    )

    st.divider()
    st.subheader("What happens next")
    step1, step2, step3 = st.columns(3)
    with step1:
        st.markdown("#### 1 · Observe")
        st.write("Now through Wednesday: stay flat and keep checking account, data, and event integrity.")
    with step2:
        st.markdown("#### 2 · Decide")
        st.write("Wednesday 15:20–15:40 ET: buy the straddle only if every frozen gate passes.")
    with step3:
        st.markdown("#### 3 · Exit")
        st.write("Thursday 09:45 ET: close. At 15:30 ET, any unresolved close is an emergency.")

    st.divider()
    st.subheader("Latest surface check")
    if surface_row:
        diagnostic = surface_row.payload.get("diagnostic", {})
        premium = diagnostic.get("executable_premium_to_spot")
        width = diagnostic.get("total_spread_pct")
        fresh = bool(surface_row.payload.get("fresh_for_entry"))
        gate_rows = [
            {
                "Gate": "Premium / spot",
                "Observed": f"{premium:.2%}" if premium is not None else "—",
                "Required": "≤ 8.50%",
                "Result": "✅ Under limit" if premium is not None and premium <= 0.085
                else "❌ Over limit",
            },
            {
                "Gate": "Combined bid/ask width",
                "Observed": f"{width:.2%}" if width is not None else "—",
                "Required": "≤ 5.00%",
                "Result": "✅ Pass" if width is not None and width <= 0.05
                else "❌ Too wide",
            },
            {
                "Gate": "Fresh, live quotes",
                "Observed": "Fresh" if fresh else "Closed-market snapshot",
                "Required": "Fresh during entry",
                "Result": "✅ Pass" if fresh else "❌ Not tradable",
            },
        ]
        st.dataframe(gate_rows, hide_index=True, **STRETCH)
        st.warning(
            "Bottom line: this Saturday snapshot is useful diagnostics, but it "
            "would not permit a trade. Wednesday's fresh snapshot makes the real decision."
        )
    else:
        st.info("No surface snapshot has been captured yet.")

    st.subheader("Why this trade is still only conditional")
    h1, h2, h3 = st.columns(3)
    h1.metric("Historical gated sample", "6 events")
    h2.metric("Last-trade proxy", "+48.42% mean", "4 of 6 positive",
              delta_color="off")
    h3.metric("Worst adverse proxy", "-75.57% premium",
              "≈ -30.2% account at 40% risk", delta_color="off")
    st.caption(
        "These are historical option-trade-bar proxies, not guaranteed returns "
        "or historical marketable fills. The live gate decides whether to trade."
    )

    with st.expander("Exact frozen rules"):
        st.markdown(
            """
- Evaluate only **Wed Sep 2, 15:20–15:40 ET**.
- Buy the closest common-strike Sep 4 call and put.
- Require premium/spot ≤ **8.5%**, total width ≤ **5%**, each leg ≤ **15%**,
  fresh synchronized quotes with displayed size, and a valid Featherless quorum.
- Make one entry attempt; maximum loss is capped at **40% of equity**.
- Exit Thu at **09:45 ET**; emergency flat-by is **15:30 ET**.
            """
        )

with tabs[1]:
    funnel = _funnel()
    if funnel:
        st.subheader("One trade is what survived, not what we examined")
        st.write(
            "The agent scanned its fixed liquid universe, measured event-like "
            "option surfaces, confirmed scheduled events, and replayed each "
            "candidate's historical options. Exactly one plan survived this week."
        )
        stages = funnel["stages"]
        columns = st.columns(len(stages))
        for column, stage in zip(columns, stages):
            column.metric(stage["stage"].title(), stage["count"])
            names = stage.get("names")
            if names:
                column.caption(", ".join(names))
        st.caption(
            f"Scanned {funnel['scan_generated_at'][:10]} against the "
            f"{funnel['front_expiry']} / {funnel['back_expiry']} expiries. "
            "Research artifacts carry no order authority."
        )

        with st.expander("Every candidate and why it did not get the trade"):
            st.dataframe([{
                "Ticker": row["ticker"],
                "Term ratio": f"{row['term_ratio']:.2f}",
                "Implied move": f"{row['implied_event_move']:.1%}",
                "Event": row["event_type"],
                "Dated": "yes" if row.get("datetime_quorum") else "no",
                "Straddle mean": (f"{row['replay']['mean']:+.1%}"
                                  if row.get("replay") else "not replayed"),
                "Adverse median": (f"{row['replay']['adverse_median']:+.1%}"
                                   if row.get("replay") else "—"),
                "Outcome": (row["replay"]["disposition"]
                            if row.get("replay") else "NOT CONFIRMED"),
            } for row in funnel["candidates"]], hide_index=True, **STRETCH)
            for row in funnel["candidates"]:
                if row.get("replay"):
                    st.markdown(
                        f"**{row['ticker']} — {row['replay']['disposition']}.** "
                        f"{row['replay']['reason']}"
                    )
            st.caption(f"Replay rows quoted from `{funnel['replay_source']}`. "
                       "Regenerate this funnel with "
                       "`python scripts/build_event_premium_funnel.py`.")

        with st.expander("Why some names could not be measured"):
            st.dataframe(funnel["skip_reasons"], hide_index=True, **STRETCH)
            st.caption(
                "A name is skipped when its surface cannot support an "
                "auditable measurement, never because the answer was unwanted."
            )

    st.subheader("Why the weekly agent selected AVGO")
    st.write(
        "We are not predicting whether earnings are good or bad. We are buying "
        "movement only when the option price and liquidity remain acceptable."
    )
    st.dataframe(DECISIONS, hide_index=True, **STRETCH)

    st.subheader("What Featherless contributes")
    if semantic_row:
        vector = semantic_row.payload.get("surprise_vector") or {}
        committee = semantic_row.payload.get("committee") or {}
        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Surprise", f"{vector.get('surprise', 0):.2f}")
        f2.metric("Novelty", f"{vector.get('novelty', 0):.2f}")
        f3.metric("Confidence", f"{vector.get('confidence', 0):.2f}")
        f4.metric("Interpretation", vector.get("direction", "no quorum"))
        st.info(
            "Featherless can veto entry if the scheduled uncertainty appears to "
            "have resolved early. It cannot create a trade, change size, relax a "
            "market gate, or delay the exit."
        )
        st.write(
            "Reliability trail: the first live committee failed closed at 0/3; "
            f"the latest result was **{committee.get('reason', 'unavailable')}**."
        )
        with st.expander("Model-by-model results"):
            st.dataframe(committee.get("attempts") or [],
                         hide_index=True, **STRETCH)
            st.dataframe([{
                "Audit": f"#{row.sequence}",
                "Outcome": row.payload.get("committee", {}).get("reason"),
                "Integrity clear": row.payload.get("event_integrity", {}).get("clear"),
                "Hash": row.hash[:12],
            } for row in reversed(semantic_rows)], hide_index=True, **STRETCH)
    else:
        st.info("No semantic preflight is available yet.")

    with st.expander("Historical event chart"):
        import plotly.graph_objects as go
        dates = [row[0] for row in STRADDLES]
        proxy = [row[2] for row in STRADDLES]
        adverse = [row[3] for row in STRADDLES]
        colors = ["#7ec88c" if value > 0 else "#ef6b73" for value in proxy]
        fig = go.Figure()
        fig.add_trace(go.Bar(x=dates, y=proxy, name="last-trade proxy",
                             marker_color=colors))
        fig.add_trace(go.Scatter(x=dates, y=adverse, name="adverse envelope",
                                 mode="lines+markers", line=dict(color="#f5a97c")))
        fig.add_hline(y=0, line_color="#888")
        fig.update_layout(height=380, yaxis_title="premium return (%)",
                          margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, **STRETCH)

    if surface_row:
        diagnostic = surface_row.payload.get("diagnostic", {})
        points = diagnostic.get("points") or []
        with st.expander("Multi-strike smile diagnostic"):
            if points:
                import plotly.graph_objects as go
                smile = go.Figure(go.Scatter(
                    x=[point["strike"] for point in points],
                    y=[100 * point["mean_iv"] for point in points],
                    mode="lines+markers", name="paired call/put mean IV"))
                smile.add_vline(x=diagnostic.get("spot"), line_dash="dash",
                                annotation_text="spot")
                smile.update_layout(height=330, xaxis_title="strike",
                                    yaxis_title="implied volatility (%)")
                st.plotly_chart(smile, **STRETCH)
            st.caption(
                f"{diagnostic.get('point_count', '—')} paired strikes · "
                f"{diagnostic.get('shape', '—')} · diagnostic only · gate unchanged"
            )

with tabs[2]:
    st.subheader("Operational health")
    o1, o2, o3 = st.columns(3)
    o1.metric("Rehearsal", (
        f"{rehearsal.payload.get('passed_count')}/"
        f"{rehearsal.payload.get('total_count')} passed"
        if rehearsal else "Not recorded"))
    o2.metric("Broker view", "Connected" if account_payload else "Private runtime")
    o3.metric("Open broker legs", len(positions))

    if account_error:
        st.warning(f"Broker read unavailable: {account_error}")
    elif account_payload is None:
        st.info(
            "The public dashboard intentionally has no broker credentials. "
            "Live account state stays in the private runtime."
        )

    if positions:
        st.dataframe([{
            "Symbol": row.get("symbol"), "Qty": row.get("qty"),
            "Market value": row.get("market_value"),
            "Unrealized P&L": row.get("unrealized_pl"),
        } for row in positions], hide_index=True, **STRETCH)
    if book is not None and book.entries:
        st.dataframe([{
            "ID": row.id, "Structure": row.structure, "Qty": row.qty,
            "Entry": row.entry, "Max loss": row.max_loss * 100 * row.qty,
            "Opened": row.opened_at, "Closed": row.closed_at,
        } for row in reversed(book.entries)], hide_index=True, **STRETCH)

    st.subheader("Autonomous loop")
    st.code(
        """Every minute
  Alpaca MCP reads account, clock, news, stock and option data
  → Featherless returns a typed, grounded integrity assessment
  → deterministic code applies price, liquidity, risk and time gates
  → Alpaca MCP executes/reconciles when—and only when—eligible
  → results enter the hash-chained evidence ledger""",
        language="text",
    )

    with st.expander("Measured-window audit rows"):
        if not audit:
            st.info("No measured-window cycles are published yet.")
        else:
            latest = list(reversed(audit[-100:]))
            st.dataframe([{
                "Sequence": row.sequence,
                "Recorded ET": row.recorded_at,
                "Event": row.event_type,
                "Hash": row.hash[:12],
            } for row in latest], hide_index=True, **STRETCH)
            selected = st.selectbox(
                "Inspect evidence row", options=latest,
                format_func=lambda row: f"#{row.sequence} · {row.event_type}")
            st.json(_display_safe(selected.payload))

    with st.expander("Technical safeguards"):
        st.markdown(
            """
- The MCP client is read-only unless both order interlocks are enabled.
- Stable client order IDs recover lost replies without duplicate exposure.
- Reconciliation disagreement blocks entry but never blocks exit.
- Model/data failure removes trades; it cannot remove the deadline.
- Every evidence row links to the previous SHA-256 hash.
            """
        )

st.caption(
    "Catalyst Surface Agent · weekly intelligence + sealed autonomous execution · "
    "research software, not investment advice"
)
