"""Cursor shadow: flag gating, frozen-input walker, write scope, guard extraction."""

import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(repo, *args):
    subprocess.check_call(["git", *args], cwd=repo)


def _init_repo(path):
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "shadow-test@example.com")
    _git(path, "config", "user.name", "shadow-test")


def _commit(path, message, files: dict) -> str:
    for name, body in files.items():
        dest = path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_api_key_alone_does_not_route(monkeypatch):
    import analysis
    monkeypatch.setenv("CURSOR_API_KEY", "present")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    called = []
    monkeypatch.setattr(
        "cursor_llm.complete",
        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError("cursor")),
    )

    class _Block:
        text = '{"ok": true}'

    class _Msg:
        content = [_Block()]
        stop_reason = "end_turn"
        model = "claude-haiku-4-5-20251001"
        usage = None

    class _Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _Msg()

    monkeypatch.setattr(analysis, "_get_client", lambda: _Client())
    text, reason = analysis._call("claude-haiku-4-5-20251001", "system", "user")
    assert called == []
    assert reason == "end_turn"
    assert "ok" in text


def test_llm_backend_cursor_routes_and_flattens_system(monkeypatch):
    import analysis
    monkeypatch.setenv("LLM_BACKEND", "cursor")
    monkeypatch.setenv("CURSOR_API_KEY", "present")
    seen = {}

    def fake(model, system, user, max_tokens=600):
        seen["model"] = model
        seen["system"] = system
        seen["user"] = user
        return '{"ok": true}', "end_turn", SimpleNamespace(
            model="claude-haiku-4-5", usage=None)

    monkeypatch.setattr("cursor_llm.complete", fake)
    text, reason = analysis._call(
        "claude-haiku-4-5-20251001",
        [{"type": "text", "text": "ROLE"}],
        "payload",
    )
    assert text == '{"ok": true}'
    assert reason == "end_turn"
    assert seen["system"] == "ROLE"
    assert seen["user"] == "payload"
    assert seen["model"] == "claude-haiku-4-5-20251001"


def test_backend_flag_is_explicit(monkeypatch):
    import analysis
    monkeypatch.setenv("CURSOR_API_KEY", "present")
    for value, expected in (("cursor", True), (" Cursor ", True),
                            ("CURSOR", True), ("anthropic", False), ("", False)):
        monkeypatch.setenv("LLM_BACKEND", value)
        assert analysis._cursor_backend() is expected


def test_input_commit_skips_the_claim(tmp_path):
    import shadow_run
    repo = tmp_path / "repo"
    _init_repo(repo)
    pre = _commit(repo, "chore: health", {
        "decision_journal.json": "[]",
        "market_snapshot.json": "{}",
        "research_dossier.json": "{}",
        "trades.csv": "ticker\n",
        "last_rebalance.json": "{}",
        "transactions.json": "[]",
        "portfolio_peak.json": "{}",
    })
    _commit(repo, "chore: execution claim", {
        "decision_journal.json": json.dumps([{"run_id": "RUN1", "ticker": "NVDA"}]),
        "last_rebalance.json": json.dumps({"run_id": "RUN1", "executed_at": None}),
        "trades.csv": "ticker\nNVDA\n",
        "transactions.json": json.dumps([{"run_id": "RUN1"}]),
    })
    rebalance = _commit(repo, "chore: daily cycle", {
        "pending_decisions.json": json.dumps({
            "mode": "rebalance", "date": "2026-10-14", "run_id": "RUN1", "decisions": [],
        }),
        "mcp_portfolio.json": json.dumps({"as_of": "2026-10-14T11:00:00-04:00", "cash": 1}),
        "decision_journal.json": json.dumps([{"run_id": "RUN1", "ticker": "NVDA"}]),
    })
    _commit(repo, "chore: daily cycle", {
        "pending_decisions.json": json.dumps({
            "mode": "risk_watch", "date": "2026-10-15", "run_id": "OTHER", "decisions": [],
        }),
    })
    found = shadow_run.find_rebalance(str(repo), "HEAD", date_wanted="2026-10-14")
    assert found is not None
    assert found[0] == rebalance
    assert found[1]["run_id"] == "RUN1"
    assert shadow_run.input_commit(rebalance, "RUN1", str(repo)) == pre
    assert shadow_run.find_rebalance(str(repo), "HEAD", date_wanted="2026-10-15") is None


def test_writes_only_under_shadow(tmp_path, monkeypatch):
    import shadow_run
    repo = tmp_path / "repo"
    _init_repo(repo)
    pre = _commit(repo, "base", {
        "decision_journal.json": "[]",
        "market_snapshot.json": "{}",
        "mcp_portfolio.json": "{}",
    })
    rebalance = _commit(repo, "chore: daily cycle", {
        "pending_decisions.json": json.dumps({
            "mode": "rebalance", "date": "2026-10-14", "run_id": "RUN1", "decisions": [],
        }),
        "mcp_portfolio.json": "{}",
        "decision_journal.json": json.dumps([{"run_id": "RUN1"}]),
    })
    assert pre and rebalance

    def fake_replay(*args, **kwargs):
        return {
            "files": {"final.json": [], "inputs.json": {"ok": True}},
            "ledger": {
                "date": "2026-10-14",
                "rebalance_commit": rebalance,
                "inputs_commit": pre,
                "live_run_id": "RUN1",
                "final": [],
                "parse_failures": 0,
            },
        }

    monkeypatch.setattr(shadow_run, "replay", fake_replay)
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    before = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    code = shadow_run.run_once(repo=str(repo), date_wanted="2026-10-14")
    assert code == 0
    after = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    added = sorted(f for f in after - before if ".git/" not in f)
    assert added == ["shadow/2026-10-14/final.json", "shadow/2026-10-14/inputs.json",
                     "shadow/ledger.jsonl"]
    assert not (repo / "system_health.json").exists()
    assert not (repo / "trades.csv").exists()


def test_shadow_dest_refuses_escape(tmp_path):
    import shadow_run
    with pytest.raises(ValueError):
        shadow_run.shadow_dest(str(tmp_path), "..", "pending_decisions.json")
    with pytest.raises(ValueError):
        shadow_run.shadow_dest(str(tmp_path), "2026-10-14", "../../trades.csv")


def test_missing_key_writes_nothing(tmp_path, monkeypatch):
    import shadow_run
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert shadow_run.run_once(repo=str(tmp_path), date_wanted="2026-10-14") == 1
    assert not (tmp_path / "shadow").exists()


def test_shadow_source_does_not_call_daily_cycle():
    text = (Path(__file__).resolve().parent / "shadow_run.py").read_text()
    assert "run_daily_cycle(" not in text


def test_guard_chain_is_the_only_live_call_site():
    import main
    chain = inspect.getsource(main.apply_guard_chain)
    order = [
        "apply_pm_backstop", "strip_llm_risk_exit", "plan_cash_deployment",
        "validate_decisions", "enforce_research_backed_buys", "enforce_safe_mode",
        "enforce_kill_switch", "enforce_min_holding_period", "enforce_wash_sale_reentry",
        "enforce_tax_aware_hold", "enforce_capital_dependency", "enforce_sector_limits",
        "enforce_net_edge", "enforce_settled_cash", "enforce_turnover_circuit_breaker",
        "inject_risk_stops", "flag_wash_sale_presale",
    ]
    positions = [chain.index(name) for name in order]
    assert positions == sorted(positions)
    cycle = inspect.getsource(main.run_daily_cycle)
    assert "apply_guard_chain(" in cycle
    for name in ("enforce_net_edge(", "enforce_sector_limits(", "validate_decisions(",
                 "inject_risk_stops("):
        assert name not in cycle


def test_empty_guard_chain_matches_live_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import main
    from health import OK, HealthTracker
    health = HealthTracker("t", "2026-10-14")
    portfolio = {"cash": 100.0, "buying_power": 100.0, "total_value": 100.0, "positions": []}
    market = {"prices": {"SPY": {"close": 100.0, "change_pct": 0.1}}}
    state = {"candidates": [], "research": {}, "cro": {}}
    out = main.apply_guard_chain(
        [], portfolio, market, state,
        today="2026-10-14", kill_active=False, kill_reason="", health=health,
    )
    assert out["decisions"] == []
    assert out["netedge_rejected"] == []
    assert isinstance(out["transactions"], list)
    assert health.checks["decision_validation"]["status"] == OK
    assert not (tmp_path / "pending_decisions.json").exists()
    assert not (tmp_path / "system_health.json").exists()


def test_paper_book_uses_one_price_set():
    from shadow_compare import apply_orders, mark_to_market
    cash, pos = apply_orders(
        {"cash": 50.0, "positions": [{"symbol": "AAA", "qty": 1}]},
        [{"action": "SELL", "ticker": "AAA", "qty": 1},
         {"action": "BUY", "ticker": "BBB", "qty": 0.5}],
        {"AAA": {"close": 10}, "BBB": {"close": 20}},
    )
    assert cash == pytest.approx(50.0)
    assert pos["AAA"] == pytest.approx(0.0)
    assert pos["BBB"] == pytest.approx(0.5)
    value, missing = mark_to_market(cash, pos, {"AAA": {"close": 10}, "BBB": {"close": 40}})
    assert missing == []
    assert value == pytest.approx(70.0)


def test_verdict_diff_is_per_field():
    from shadow_compare import verdict_diffs
    diffs = verdict_diffs(
        {"NVDA": {"research_confidence": 7, "da_reject": False}},
        {"NVDA": {"research_confidence": 4, "da_reject": False},
         "BKNG": {"position_action": "EXIT"}},
    )
    tickers = {d["ticker"] for d in diffs}
    assert tickers == {"NVDA", "BKNG"}
    nvda = next(d for d in diffs if d["ticker"] == "NVDA")
    assert "da_reject" not in nvda["fields"]
    assert nvda["fields"]["research_confidence"]["cursor"] == 7
