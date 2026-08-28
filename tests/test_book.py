"""The registry is what stops positions becoming orphaned — held by the account
and managed by nobody, which is exactly how 63% of the equity book went
unmanaged before it existed."""
import json

import pytest

from trading_bot.options.book import Book, BookEntry


def entry(eid="a", qty=2, entry_price=-1.50, **kw):
    return BookEntry(
        id=eid, underlying="SPY", structure="iron_condor",
        legs=[{"symbol": "SPY260904C00770000", "side": "sell", "ratio_qty": 1},
              {"symbol": "SPY260904C00780000", "side": "buy", "ratio_qty": 1}],
        qty=qty, entry=entry_price, max_profit=1.5, max_loss=8.5,
        opened_at="2026-08-28T12:00:00", **kw)


def test_round_trips_through_disk(tmp_path):
    b = Book(tmp_path / "book.json")
    b.add(entry())
    again = Book(tmp_path / "book.json")
    assert len(again.open_entries) == 1
    assert again.open_entries[0].qty == 2


def test_save_is_atomic(tmp_path):
    """A truncated registry read back after a crash orphans whatever it lost."""
    p = tmp_path / "book.json"
    b = Book(p)
    b.add(entry())
    assert not p.with_suffix(".tmp").exists()
    json.loads(p.read_text())


def test_leg_exposure_is_signed_and_scaled_by_qty():
    e = entry(qty=3)
    assert e.leg_exposure() == {"SPY260904C00770000": -3, "SPY260904C00780000": 3}


def test_reconcile_is_silent_when_the_account_agrees(tmp_path):
    b = Book(tmp_path / "b.json")
    b.add(entry(qty=2))
    assert b.reconcile({"SPY260904C00770000": -2, "SPY260904C00780000": 2}) == {}


def test_reconcile_reports_a_leg_the_account_does_not_have(tmp_path):
    """An assigned or expired leg leaves the registry claiming risk that is not
    there, and every size computed against it is wrong."""
    b = Book(tmp_path / "b.json")
    b.add(entry(qty=2))
    diff = b.reconcile({"SPY260904C00770000": -2})
    assert diff == {"SPY260904C00780000": (2, 0)}


def test_reconcile_reports_a_position_we_never_opened(tmp_path):
    b = Book(tmp_path / "b.json")
    diff = b.reconcile({"SPY260904P00700000": -5})
    assert diff == {"SPY260904P00700000": (0, -5)}


def test_closed_entries_stop_claiming_exposure(tmp_path):
    b = Book(tmp_path / "b.json")
    b.add(entry())
    b.close("a", exit_price=0.40, reason="profit target")
    assert b.expected_exposure() == {}
    assert b.reconcile({}) == {}


def test_realised_pnl_uses_the_broker_sign_convention(tmp_path):
    """Opened for 1.50 credit, bought back at 0.40, two contracts -> +$220."""
    b = Book(tmp_path / "b.json")
    b.add(entry(qty=2, entry_price=-1.50))
    b.close("a", exit_price=0.40, reason="profit target")
    assert b.realised_pnl() == pytest.approx(220.0)


def test_a_loser_books_a_loss(tmp_path):
    b = Book(tmp_path / "b.json")
    b.add(entry(qty=1, entry_price=-1.50))
    b.close("a", exit_price=5.00, reason="loss limit")
    assert b.realised_pnl() == pytest.approx(-350.0)


def test_closing_an_unknown_entry_raises(tmp_path):
    b = Book(tmp_path / "b.json")
    with pytest.raises(KeyError):
        b.close("nope", exit_price=0.1, reason="x")


def test_closing_twice_raises(tmp_path):
    b = Book(tmp_path / "b.json")
    b.add(entry())
    b.close("a", exit_price=0.4, reason="profit target")
    with pytest.raises(KeyError):
        b.close("a", exit_price=0.4, reason="again")


def test_two_processes_do_not_share_a_temp_path(tmp_path):
    """A fixed ".tmp" is shared by every instance: a second process renaming it
    away between our write and our replace raises FileNotFoundError from inside
    `book.add`, which sits outside the executor's try — so the cycle dies AFTER
    the order filled, with nothing recorded. Reproduced 8/8 in audit."""
    import os
    a, b = Book(tmp_path / "book.json"), Book(tmp_path / "book.json")
    a.add(entry("a"))
    b.load()
    b.add(entry("b"))
    assert a.path.with_suffix(f".tmp{os.getpid()}") == b.path.with_suffix(
        f".tmp{os.getpid()}")
    leftovers = list(tmp_path.glob("*.tmp*"))
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert len(Book(tmp_path / "book.json").entries) == 2


def test_a_truncated_registry_fails_with_an_explanation(tmp_path):
    """A crash mid-write leaves invalid JSON. The constructor used to raise
    JSONDecodeError from the runner's import-time setup, uncaught."""
    from trading_bot.options.book import BookCorrupt
    p = tmp_path / "b.json"
    p.write_text('[{"id": "a", "underly')
    with pytest.raises(BookCorrupt, match="not valid JSON"):
        Book(p)


def test_a_registry_with_unknown_fields_fails_with_an_explanation(tmp_path):
    from trading_bot.options.book import BookCorrupt
    p = tmp_path / "b.json"
    p.write_text('[{"id": "a", "surprise_field": 1}]')
    with pytest.raises(BookCorrupt, match="cannot read"):
        Book(p)


def test_an_empty_file_is_an_empty_book(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("   ")
    assert Book(p).entries == []


def test_two_offsetting_phantom_rows_do_not_cancel_out(tmp_path):
    """Net exposure can agree while the registry is wrong. Two rows holding
    opposite positions in the same contract sum to zero and vanish from
    expected_exposure, so reconcile reported nothing while the agent kept
    computing profit targets for two structures that did not exist."""
    b = Book(tmp_path / "b.json")
    b.add(entry("long", qty=1))
    flipped = entry("short", qty=1)
    flipped.legs = [{"symbol": l["symbol"],
                     "side": "buy" if l["side"] == "sell" else "sell",
                     "ratio_qty": 1} for l in flipped.legs]
    b.add(flipped)
    assert b.expected_exposure() == {}, "fixture does not net to zero"
    diff = b.reconcile({})
    assert diff != {}, "two phantom rows cancelled and reconcile stayed silent"
    assert sorted(b.phantom_rows({})) == ["long", "short"]


def test_phantom_detection_is_quiet_when_the_account_agrees(tmp_path):
    b = Book(tmp_path / "b.json")
    b.add(entry("a", qty=2))
    actual = {"SPY260904C00770000": -2, "SPY260904C00780000": 2}
    assert b.phantom_rows(actual) == []
    assert b.reconcile(actual) == {}
