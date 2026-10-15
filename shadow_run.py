"""Replay one live rebalance through the Cursor LLM backend.

Stops after the 7 agents and the shared guard chain (``main.apply_guard_chain``).
Places no orders. Does not write ``pending_decisions.json``, ``trades.csv``,
``transactions.json``, ``decision_journal.json``, the calibration ledgers,
Supabase, or ``system_health.json``.

Frozen inputs for the week's live ``chore: daily cycle`` commit D (the one
whose envelope ``mode`` is ``rebalance``):

* ``mcp_portfolio.json`` from D — that is the book the routine sized against.
* Snapshot, dossier, ``trades.csv``, ``decision_journal.json``,
  ``last_rebalance.json``, ``transactions.json``, ``portfolio_peak.json``, and
  the data-quality report from the nearest ancestor of D whose journal does
  not yet contain D's ``run_id``. D's parent is the execution-claim commit,
  which already holds this run's journal rows, trades, and week lock. Using
  it would feed the run its own decisions as history.

``today`` is the envelope date, not the wall clock. Outputs go only under
``shadow/YYYY-MM-DD/`` plus one row in ``shadow/ledger.jsonl``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

INPUT_FILES = (
    "market_snapshot.json",
    "research_dossier.json",
    "trades.csv",
    "decision_journal.json",
    "last_rebalance.json",
    "transactions.json",
    "portfolio_peak.json",
    "data_quality_report.json",
)

SEAT_FILES = {
    "regime.json": "regime",
    "research.json": "research",
    "earnings.json": "earnings",
    "devils_advocate.json": "devils_advocate",
    "position_reviews.json": "position_reviews",
}


def git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False,
    )


def git_bytes(rev: str, path: str, cwd: str) -> bytes | None:
    proc = git(["show", f"{rev}:{path}"], cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout


def history_ref(cwd: str) -> str:
    """Prefer origin/main so a long-lived branch still sees new rebalances."""
    if git(["rev-parse", "--verify", "--quiet", "origin/main"], cwd).returncode == 0:
        return "origin/main"
    return "HEAD"


def _subjects(ref: str, cwd: str) -> list[tuple[str, str]]:
    proc = git(["log", ref, "--pretty=%H%x09%s"], cwd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[:300] or "git log failed")
    out = []
    for line in proc.stdout.decode().splitlines():
        sha, _, subject = line.partition("\t")
        if sha:
            out.append((sha, subject.strip()))
    return out


def find_rebalance(cwd: str, ref: str, date_wanted: str | None = None,
                   commit: str | None = None) -> tuple[str, dict] | None:
    """The ``chore: daily cycle`` commit whose envelope mode is rebalance.

    ``commit`` pins that commit. ``date_wanted`` pins the envelope date.
    With neither, take the latest rebalance in today's ISO week (ET), so a
    Thursday catch-up still finds Wednesday.
    """
    from market_calendar import iso_week_of
    from zoneinfo import ZoneInfo

    if commit:
        raw = git_bytes(commit, "pending_decisions.json", cwd)
        if not raw:
            raise RuntimeError(f"{commit} has no pending_decisions.json")
        pending = json.loads(raw)
        if pending.get("mode") != "rebalance":
            return None
        return commit, pending

    today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    want_week = iso_week_of(date_wanted or today_et)

    for sha, subject in _subjects(ref, cwd):
        if subject != "chore: daily cycle":
            continue
        raw = git_bytes(sha, "pending_decisions.json", cwd)
        if not raw:
            continue
        try:
            pending = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if pending.get("mode") != "rebalance":
            continue
        pdate = str(pending.get("date") or "")
        if date_wanted:
            if pdate == date_wanted:
                return sha, pending
            continue
        if pdate and iso_week_of(pdate) == want_week:
            return sha, pending
    return None


def input_commit(rebalance_sha: str, run_id: str, cwd: str) -> str:
    """Nearest ancestor whose journal does not already contain this run.

    The execution-claim commit (D's parent) is too late: it already has the
    run's journal rows, trades, and week lock.
    """
    if not run_id:
        raise RuntimeError(f"{rebalance_sha} pending envelope has no run_id")
    needle = run_id.encode()
    sha = rebalance_sha
    while sha:
        blob = git_bytes(sha, "decision_journal.json", cwd)
        if blob is None or needle not in blob:
            return sha
        parent = git(["rev-parse", f"{sha}^"], cwd)
        if parent.returncode != 0:
            break
        sha = parent.stdout.decode().strip()
    raise RuntimeError(
        f"no ancestor of {rebalance_sha} is free of run_id {run_id}")


def materialize(work: str, rebalance_sha: str, inputs_sha: str, cwd: str) -> dict:
    """Copy frozen blobs into ``work``. Returns sha256 manifest. Missing
    optional files are recorded as null and skipped."""
    manifest = {}
    for name in INPUT_FILES:
        blob = git_bytes(inputs_sha, name, cwd)
        if blob is None:
            manifest[name] = None
            continue
        dest = os.path.join(work, name)
        with open(dest, "wb") as fh:
            fh.write(blob)
        manifest[name] = {
            "commit": inputs_sha,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
        }
    mcp = git_bytes(rebalance_sha, "mcp_portfolio.json", cwd)
    if mcp is None:
        raise RuntimeError(f"{rebalance_sha} has no mcp_portfolio.json")
    with open(os.path.join(work, "mcp_portfolio.json"), "wb") as fh:
        fh.write(mcp)
    manifest["mcp_portfolio.json"] = {
        "commit": rebalance_sha,
        "sha256": hashlib.sha256(mcp).hexdigest(),
        "bytes": len(mcp),
    }
    return manifest


def recently_exited_as_of(within_days: int, today: str) -> dict:
    """``journal.recently_exited``, dated from the frozen rebalance day.

    The live helper calls ``date.today()`` with no argument. The shadow must
    not shift the wash-sale window onto the day the job happens to run.
    """
    from journal import JOURNAL_FILE, _load_list
    cutoff = date.fromisoformat(today) - timedelta(days=within_days)
    out: dict = {}
    for entry in _load_list(JOURNAL_FILE):
        if entry.get("status") != "closed":
            continue
        exits = entry.get("exits") or []
        if not exits:
            continue
        try:
            exited = date.fromisoformat(exits[-1]["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if exited >= cutoff:
            out[entry["ticker"]] = entry
    return out


def _portfolio_as_of(today: str) -> dict:
    """Load ``mcp_portfolio.json`` and require its ET date to be ``today``."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    with open("mcp_portfolio.json") as fh:
        data = json.load(fh)
    as_of = data.get("as_of")
    if not as_of:
        raise RuntimeError("mcp_portfolio.json has no as_of")
    stamp = _dt.fromisoformat(as_of)
    et = ZoneInfo("America/New_York")
    stamp = stamp.replace(tzinfo=et) if stamp.tzinfo is None else stamp.astimezone(et)
    if stamp.date().isoformat() != today:
        raise RuntimeError(
            f"mcp as_of {as_of} is ET {stamp.date().isoformat()}, not {today}")
    return data


class _FrozenDateTime:
    """Stand-in for ``datetime`` that reports one ET calendar day from ``now``."""

    def __init__(self, real, fixed: datetime):
        self._real = real
        self._fixed = fixed

    def now(self, tz=None):
        if tz is None:
            return self._fixed.replace(tzinfo=None)
        return self._fixed.astimezone(tz)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _freeze_guard_clock(today: str):
    """``validate_decisions`` reads the wall clock for the good-faith window.
    Point that clock at the rebalance date for the duration of the chain."""
    import guardrails
    y, m, d = (int(x) for x in today.split("-"))
    fixed = datetime(y, m, d, 16, 0, tzinfo=timezone.utc)
    real = guardrails.datetime
    guardrails.datetime = _FrozenDateTime(real, fixed)
    return real


def _orders(decisions: list) -> list[dict]:
    out = []
    for d in decisions or []:
        action = str(d.get("action", "")).upper()
        if action not in ("BUY", "SELL"):
            continue
        out.append({
            "ticker": d.get("ticker"),
            "action": action,
            "target_weight": d.get("target_weight"),
            "qty": d.get("qty"),
            "source_of_capital": d.get("source_of_capital"),
        })
    return out


def _parse_errors(state: dict) -> dict:
    def _failed(bucket: str, field: str) -> list[str]:
        found = []
        for ticker, row in (state.get(bucket) or {}).items():
            if not isinstance(row, dict):
                continue
            if row.get("_empty_reason") == "parse_failed":
                found.append(ticker)
                continue
            if field and not str(row.get(field) or "").strip() and row.get("_empty_reason"):
                # An empty required field with a reason is a failed seat, not a
                # deliberate blank. Count parse_failed above; keep the rest out
                # of this list so a model that returned an empty thesis on
                # purpose is visible in the seat file, not as a parse error.
                pass
        return sorted(found)

    raw = state.get("portfolio_manager_raw") or ""
    parsed_ok = bool(state.get("portfolio_manager_parsed_ok", True))
    return {
        "portfolio_manager_parsed_ok": parsed_ok,
        "portfolio_manager_raw_excerpt": None if parsed_ok else str(raw)[:2000],
        "research_parse_failed": _failed("research", "thesis"),
        "devils_advocate_parse_failed": _failed("devils_advocate", "bear_case"),
        "cro_api_failed": bool((state.get("cro") or {}).get("api_failed")),
    }


def replay(today: str, manifest: dict, rebalance_sha: str, inputs_sha: str,
           run_id: str) -> dict:
    """Run agents + guards against the files in the current directory.

    Caller is responsible for the temp cwd. This function does not write the
    repo and does not call ``run_daily_cycle``.
    """
    import build_dossier
    import main
    from analysis import get_trade_decisions, usage_summary
    from execute import get_trade_history
    from guardrails import WASH_SALE_REENTRY_DAYS
    from health import HealthTracker
    from journal import check_kill_switches, get_recent_decisions, get_ticker_history

    portfolio = _portfolio_as_of(today)
    with open("market_snapshot.json") as fh:
        market_data = json.load(fh)
    dossier = build_dossier.load_dossier()
    ok, errors = build_dossier.validate_dossier(dossier, as_of=today)
    if not ok:
        raise RuntimeError("dossier invalid for frozen date: " + "; ".join(errors[:5]))

    quant_scores, score_source, score_note = main.select_quant_scores(
        market_data, held={p.get("symbol") for p in portfolio.get("positions", [])})
    kill_active, kill_reason = check_kill_switches(portfolio)

    recent = get_recent_decisions(n=200)
    prior_journal: dict = {}
    for entry in recent:
        ticker = entry.get("ticker")
        if ticker and ticker not in prior_journal and entry.get("status") == "open":
            prior_journal[ticker] = entry
    seen = {e.get("ticker") for e in recent if e.get("ticker")}
    ticker_history = {t: get_ticker_history(t) for t in seen}
    exited = recently_exited_as_of(WASH_SALE_REENTRY_DAYS, today)

    decisions, pipeline_state = get_trade_decisions(
        portfolio, market_data, quant_scores, get_trade_history(), prior_journal,
        ticker_history=ticker_history, recently_exited=exited, dossier=dossier,
    )

    health = HealthTracker(run_id or "shadow", today)
    real_dt = _freeze_guard_clock(today)
    try:
        guarded = main.apply_guard_chain(
            decisions, portfolio, market_data, pipeline_state,
            today=today, kill_active=kill_active, kill_reason=kill_reason,
            health=health,
        )
    finally:
        import guardrails
        guardrails.datetime = real_dt

    final = guarded["decisions"]
    parse_errors = _parse_errors(pipeline_state)
    n_parse = (
        (0 if parse_errors["portfolio_manager_parsed_ok"] else 1)
        + len(parse_errors["research_parse_failed"])
        + len(parse_errors["devils_advocate_parse_failed"])
        + (1 if parse_errors["cro_api_failed"] else 0)
    )
    regime = pipeline_state.get("regime") or {}
    cro = pipeline_state.get("cro") or {}
    positions = []
    for p in portfolio.get("positions") or []:
        positions.append({
            "symbol": p.get("symbol"),
            "qty": p.get("qty"),
            "avg_price": p.get("avg_price"),
            "current_price": p.get("current_price"),
            "market_value": p.get("market_value"),
        })
    inputs = {
        "rebalance_commit": rebalance_sha,
        "inputs_commit": inputs_sha,
        "today": today,
        "live_run_id": run_id,
        "files": manifest,
        "portfolio": {
            "as_of": portfolio.get("as_of"),
            "cash": portfolio.get("cash"),
            "buying_power": portfolio.get("buying_power"),
            "total_value": portfolio.get("total_value"),
            "positions": positions,
        },
        "quant_score_source": score_source,
        "quant_score_note": score_note,
        "note": (
            "mcp_portfolio.json is from the rebalance commit. Every other file "
            "is from inputs_commit: the nearest ancestor whose decision_journal "
            "does not contain live_run_id. The execution-claim parent of the "
            "rebalance commit already contains this run, so it is not the input. "
            "The OHLCV snapshot is not copied here; its hash and commit are."
        ),
    }
    files = {
        "inputs.json": inputs,
        "parse_errors.json": parse_errors,
        "pm_proposal.json": pipeline_state.get("portfolio_manager_proposed") or [],
        "post_cro.json": pipeline_state.get("post_cro_decisions") or [],
        "cro.json": cro,
        "final.json": final,
        "guard_report.json": {
            "checks": health.checks,
            "cash_deployment": pipeline_state.get("cash_deployment"),
            "net_edge_rejected": len(guarded["netedge_rejected"]),
            "kill_switch_active": kill_active,
        },
    }
    for name, key in SEAT_FILES.items():
        files[name] = pipeline_state.get(key) or {}
    ledger = {
        "date": today,
        "rebalance_commit": rebalance_sha,
        "inputs_commit": inputs_sha,
        "live_run_id": run_id,
        "regime": regime.get("regime"),
        "regime_confidence": regime.get("confidence"),
        "pm": _orders(pipeline_state.get("portfolio_manager_proposed") or []),
        "cro_approved": cro.get("approved"),
        "cro_vetoed": cro.get("rejected_tickers") or [],
        "final": _orders(final),
        "parse_failures": n_parse,
        "llm_usage": usage_summary(portfolio.get("total_value")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"files": files, "ledger": ledger}


def shadow_dest(repo: str, day: str, name: str) -> Path:
    """Path under ``shadow/<day>/``. Refuse anything that escapes ``shadow/``."""
    root = (Path(repo) / "shadow").resolve()
    dest = (root / day / name).resolve()
    if dest != root and root not in dest.parents:
        raise ValueError(f"refusing to write outside shadow/: {name}")
    return dest


def write_outputs(repo: str, payload: dict) -> Path:
    day = payload["ledger"]["date"]
    for name, body in payload["files"].items():
        dest = shadow_dest(repo, day, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(body, indent=2, default=str) + "\n")
    ledger_path = (Path(repo) / "shadow" / "ledger.jsonl").resolve()
    root = (Path(repo) / "shadow").resolve()
    if root not in ledger_path.parents:
        raise ValueError("ledger path escaped shadow/")
    existing = []
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                existing.append(line)
                continue
            if row.get("date") == day:
                continue
            existing.append(json.dumps(row, default=str))
    existing.append(json.dumps(payload["ledger"], default=str))
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("\n".join(existing) + "\n")
    return Path(repo) / "shadow" / day


def run_once(repo: str | None = None, date_wanted: str | None = None,
             commit: str | None = None) -> int:
    if not os.environ.get("CURSOR_API_KEY", "").strip():
        print("CURSOR_API_KEY is not set — shadow run cannot call Cursor.")
        return 1
    os.environ["LLM_BACKEND"] = "cursor"
    repo = repo or git(["rev-parse", "--show-toplevel"], os.getcwd()).stdout.decode().strip()
    ref = history_ref(repo)
    found = find_rebalance(repo, ref, date_wanted=date_wanted, commit=commit)
    if not found:
        print("no rebalance commit for the requested week — skip")
        return 0
    sha, pending = found
    today = str(pending.get("date") or "")
    run_id = str(pending.get("run_id") or "")
    if not today:
        print(f"{sha} rebalance envelope has no date")
        return 1
    inputs_sha = input_commit(sha, run_id, repo)
    print(f"rebalance {sha[:12]} inputs {inputs_sha[:12]} date {today} run {run_id}")
    work = tempfile.mkdtemp(prefix="shadow-run-")
    previous = os.getcwd()
    try:
        manifest = materialize(work, sha, inputs_sha, repo)
        os.chdir(work)
        payload = replay(today, manifest, sha, inputs_sha, run_id)
    finally:
        os.chdir(previous)
        shutil.rmtree(work, ignore_errors=True)
    out = write_outputs(repo, payload)
    try:
        import shadow_compare
        shadow_compare.write_week(repo, today)
    except Exception as exc:  # comparison is read-only; the shadow files stand either way
        print(f"shadow_compare skipped: {exc}")
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cursor shadow replay of one live rebalance")
    parser.add_argument("--date", default=None, help="Envelope date YYYY-MM-DD")
    parser.add_argument("--commit", default=None, help="Pin the rebalance commit")
    args = parser.parse_args(argv)
    if args.date and not __import__("re").fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("--date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        return run_once(date_wanted=args.date, commit=args.commit)
    except Exception as exc:
        print(f"shadow run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
