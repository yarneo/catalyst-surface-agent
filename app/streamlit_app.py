"""Read-only Catalyst Surface Agent result and evidence dashboard."""

from __future__ import annotations

import datetime as dt
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


def _book():
    return Book(ROOT / "data" / "event_book.json")


def _remaining(value: dt.timedelta) -> str:
    seconds = max(0, int(value.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m"


now = now_et()
st.sidebar.title("Catalyst Surface Agent")
st.sidebar.caption("Yar + Starboi · autonomous paper-trading research")
st.sidebar.metric("Measured window", _remaining(DEADLINE - now),
                  "remaining" if now < DEADLINE else "complete",
                  delta_color="off")
st.sidebar.markdown(
    f"**Start:** {START:%a %b %d, %H:%M} ET  \n"
    f"**Entry:** {POLICY.entry_start:%a %b %d, %H:%M}–"
    f"{POLICY.entry_end:%H:%M} ET  \n"
    f"**Exit:** {POLICY.exit_at:%a %b %d, %H:%M} ET  \n"
    f"**Cutoff:** {DEADLINE:%a %b %d, %H:%M} ET"
)
if st.sidebar.button("Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.title("Semantic event convexity, with every decision auditable")
st.caption(
    "A fully autonomous, direction-neutral AVGO earnings strategy. Featherless "
    "can veto stale event uncertainty and explain the release; deterministic "
    "Alpaca MCP data owns surface, risk, orders, reconciliation, and P&L."
)

try:
    account_payload = _account()
except Exception as exc:  # noqa: BLE001
    account_payload = None
    st.warning(f"Replacement account unavailable: {type(exc).__name__}: {exc}")

try:
    audit = _audit_rows()
    audit_ok = True
except AuditCorrupt as exc:
    audit, audit_ok = [], False
    st.error(f"Evidence chain failed verification: {exc}")

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

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Equity", f"${equity:,.2f}" if equity is not None else "Awaiting account",
          f"${equity - 100_000:+,.2f}" if equity is not None else None)
c2.metric("Measured P&L",
          f"{(equity / 100_000 - 1):+.2%}" if equity is not None else "—")
c3.metric("Open broker legs", len(positions))
c4.metric("Max-loss ceiling", "25%", "$25,000 at start", delta_color="off")
c5.metric("Evidence chain", "verified" if audit_ok else "failed",
          f"{len(audit)} rows", delta_color="off")

tabs = st.tabs(["Strategy", "Research ledger", "Autonomous evidence",
                "Positions & P&L", "Technical method"])

with tabs[0]:
    st.subheader("Final policy")
    left, right = st.columns([1.35, 1])
    with left:
        st.markdown(
            """
1. Observe only the predeclared Broadcom Q3 FY2026 earnings event.
2. During **Wed Sep 2, 15:20–15:40 ET**, select the closest common-strike Sep 4
   call and put.
3. Require marketable premium ≤ **8.5% of spot**, total width ≤ **5%**, each-leg
   width ≤ **15%**, fresh synchronized quotes, displayed size, and a valid
   Featherless integrity quorum.
4. Buy once, with exact maximum loss capped at **25% of equity**. Never chase a
   confirmed non-fill.
5. Hold through the after-close release and begin the close **Thu at 09:45 ET**.
   Emergency flat-by is 15:30 ET; nothing is intentionally carried to Friday.
            """
        )
    with right:
        st.info(
            "Featherless has asymmetric authority: a grounded indication that "
            "results/guidance arrived early can veto entry. It cannot add a "
            "trade, choose size, relax a surface gate, or delay the exit."
        )
        st.metric("Gated historical proxy", "+48.42% mean on premium")
        st.metric("Gated win rate", "66.7%", "6 post-split events",
                  delta_color="off")
        st.metric("Worst gated adverse envelope", "-75.57% on premium",
                  "≈ -18.9% account at 25% allocation", delta_color="off")

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
    fig.update_layout(height=400, yaxis_title="premium return (%)",
                      margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Actual Alpaca MCP historical option trade bars, not a historical NBBO "
        "fill backtest. The adverse envelope buys each leg at its highest entry-"
        "window trade and sells at its lowest exit-window trade."
    )

with tabs[1]:
    st.subheader("What failed—and how it changed the strategy")
    st.dataframe(DECISIONS, use_container_width=True, hide_index=True)
    st.markdown(
        "The final policy is intentionally narrow because the direct-news, "
        "NFP-to-BTC, AVGO continuation, peer-spillover, and ISM variants did "
        "not survive their first timestamped falsification. The executable "
        "research programs and event-level results remain under "
        "`research/strategy-evidence/`; rejected ideas are not deleted from the story."
    )
    st.warning(
        "Evidence classes stay separate: paper fills, stock/crypto event studies, "
        "historical option-trade-bar proxies, current-surface calculations, and "
        "actual measured-window P&L are never relabeled as one another."
    )

with tabs[2]:
    st.subheader("Hash-chained runtime evidence")
    if not audit:
        st.info("No tournament cycles have been recorded yet.")
    else:
        latest = list(reversed(audit[-100:]))
        st.dataframe([
            {
                "Sequence": row.sequence,
                "Recorded ET": row.recorded_at,
                "Event": row.event_type,
                "Hash": row.hash[:12],
                "Previous": row.previous_hash[:12],
            }
            for row in latest
        ], use_container_width=True, hide_index=True)
        selected = st.selectbox(
            "Inspect evidence row",
            options=latest,
            format_func=lambda row: f"#{row.sequence} · {row.event_type} · {row.recorded_at}")
        st.json(selected.payload)
    st.markdown(
        "Each row includes the previous row's SHA-256 digest. Editing, removing, "
        "or reordering a past decision breaks verification. Sensitive config "
        "keys are redacted before persistence."
    )

with tabs[3]:
    st.subheader("Broker truth and local structure registry")
    if not positions:
        st.info("No broker positions are visible yet.")
    else:
        st.dataframe([
            {
                "Symbol": row.get("symbol"), "Qty": row.get("qty"),
                "Market value": row.get("market_value"),
                "Unrealized P&L": row.get("unrealized_pl"),
            }
            for row in positions
        ], use_container_width=True, hide_index=True)
    if book is not None:
        st.markdown(f"**Local open structures:** {len(book.open_entries)}")
        if book.entries:
            st.dataframe([
                {
                    "ID": row.id, "Structure": row.structure,
                    "Qty": row.qty, "Entry": row.entry,
                    "Max loss": row.max_loss * 100 * row.qty,
                    "Opened": row.opened_at, "Closed": row.closed_at,
                    "Reason": row.close_reason,
                }
                for row in reversed(book.entries)
            ], use_container_width=True, hide_index=True)
            st.metric("Registry realized P&L", f"${book.realised_pnl():+,.2f}")

with tabs[4]:
    st.subheader("One sponsor-native autonomous loop")
    st.code(
        """launchd (60s, non-overlapping)
  → Alpaca MCP account + clock + news + stock snapshot
  → Alpaca MCP option chain + historical evidence
  → Featherless typed quorum / event-integrity veto
  → deterministic surface gates + exact max-loss sizing
  → Alpaca MCP idempotent multi-leg order + reconciliation
  → Alpaca MCP activities + portfolio history
  → hash-chained evidence ledger + this read-only dashboard""",
        language="text",
    )
    st.markdown(
        """
The installed Alpaca MCP server exposes 72 tools. Raw movers are not treated as
a universe because observed results contained penny stocks, warrants, and names
without usable options. Historical bar pagination is chunked and deduplicated;
paper fills are described honestly because paper trading does not model queue
position, market impact, latency slippage, or displayed-size constraints.

Featherless requests run concurrently under a true killable wall-clock deadline.
Outputs must match an exact schema, cite supplied fact IDs, and stay inside the
eligible ticker universe. Narrow non-expansive repairs—such as deleting a causal
self-link—are recorded. No valid quorum means no entry.

The stable client order ID is written before submission. A lost response is
recovered by that ID; an unknown state blocks later actions. The registry groups
broker legs into the structure across restarts. Reconciliation disagreements
block entry but never block the evidence-matched exit.
        """
    )
    st.caption(
        "Final strategy v0.4 · exact measured window Mon Aug 31 09:30 ET to "
        "Fri Sep 4 09:30 ET · all normal decisions autonomous"
    )
