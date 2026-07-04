"""
market_calendar.py — single source of truth for the NYSE trading calendar.

The holiday set + weekend/holiday logic used to live only in ``preflight_gate.py``.
Phase 3 adds a second consumer — the heartbeat dead-man's switch (§15.4) must know
which days are trading days so it does NOT alert on a legitimately-closed market
(a Saturday with no fresh snapshot is correct, not a failure). Two copies of the
calendar would drift, so it lives here and ``preflight_gate`` imports it.

Dates are OBSERVED closure dates (an observed holiday on a weekend shifts to the
adjacent weekday). Does NOT model early-close (half) days — those still trade.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Observed NYSE full-closure dates. Keep in sync as new years are added; the
# preflight gate + heartbeat both read this one set. (Was preflight_gate.NYSE_HOLIDAYS.)
NYSE_HOLIDAYS: set[str] = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _to_date(d: "str | date | datetime") -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


def is_trading_day(d: "str | date | datetime") -> bool:
    """True if ``d`` is a normal NYSE trading day (not a weekend, not a full holiday).

    Accepts an ISO string, a ``date``, or a ``datetime`` (date component used).
    Mirror of ``preflight_gate._market_closed_today`` but parameterized on the date
    so the heartbeat can ask about an arbitrary day, not just 'today'.
    """
    dt = _to_date(d)
    if dt.weekday() >= 5:            # 5=Sat, 6=Sun
        return False
    return dt.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS


def today_et() -> date:
    """Today's date in US/Eastern — the market-day date the pipeline keys on."""
    return datetime.now(ET).date()


def iso_week_of(d: "str | date | datetime") -> str:
    """ISO week key ('2026-W28') for a date. The once-per-ISO-week rebalance lock
    (Phase 5, §6.5) keys on this — the gate, journal stamp, risk_watch interlock,
    and heartbeat missed-week check must all derive the week from ONE function or
    they can disagree at a year boundary (ISO week 1 can start in December)."""
    iso = _to_date(d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"
