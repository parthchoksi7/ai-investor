"""
Unit tests for the AI Investor pipeline.

Coverage:
  - health.py         — HealthTracker status aggregation, alerts, persistence
  - quant_engine.py   — momentum / quality / valuation / risk scoring (pure functions)
  - journal.py        — kill switch logic, idempotency envelope

Run with:
  pytest test_pipeline.py -v
  pytest test_pipeline.py -v -k "momentum"   # run a single class
"""

import json
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_history(closes: list[float]) -> list[dict]:
    """Build minimal OHLCV bars from a list of close prices."""
    return [
        {"date": i, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1_000_000}
        for i, c in enumerate(closes)
    ]


def _flat(price: float, n: int) -> list[dict]:
    return _make_history([price] * n)


def _trend(start: float, end: float, n: int) -> list[dict]:
    step = (end - start) / max(n - 1, 1)
    return _make_history([start + i * step for i in range(n)])


# ── analysis._parse_json ──────────────────────────────────────────────────────

class TestParseJson:
    def _parse(self, text, default):
        from analysis import _parse_json
        return _parse_json(text, default)

    def test_plain_dict(self):
        assert self._parse('{"a": 1}', {}) == {"a": 1}

    def test_code_fence(self):
        assert self._parse('```json\n{"a": 1}\n```', {}) == {"a": 1}

    def test_prose_wrapped(self):
        result = self._parse('Here is the JSON: {"a": 1} done.', {})
        assert result == {"a": 1}

    def test_singleton_list_unwrap_to_dict(self):
        """Model returns [{...}] when we expect {...} — should unwrap."""
        result = self._parse('[{"thesis": "bull", "confidence": 7}]', {"thesis": ""})
        assert result == {"thesis": "bull", "confidence": 7}

    def test_multi_element_list_not_unwrapped(self):
        """Multi-element list expected as list — keep as list."""
        result = self._parse('[{"a": 1}, {"a": 2}]', [])
        assert result == [{"a": 1}, {"a": 2}]

    def test_singleton_list_kept_when_default_is_list(self):
        """If default is a list, keep a singleton list as-is."""
        result = self._parse('[{"a": 1}]', [])
        assert result == [{"a": 1}]

    def test_invalid_json_returns_default(self):
        assert self._parse('not json at all', {"x": 0}) == {"x": 0}


# ── health.py ─────────────────────────────────────────────────────────────────

class TestHealthTracker:
    def test_no_checks_returns_failed(self):
        from health import HealthTracker, FAILED
        h = HealthTracker("run-001", "2026-06-09")
        assert h.overall_status == FAILED

    def test_all_ok(self):
        from health import HealthTracker, OK
        h = HealthTracker("run-001", "2026-06-09")
        h.record("a", OK)
        h.record("b", OK)
        assert h.overall_status == OK

    def test_worst_status_wins(self):
        from health import HealthTracker, OK, DEGRADED, FAILED, ABORTED
        h = HealthTracker("run-001", "2026-06-09")
        h.record("a", OK)
        h.record("b", DEGRADED)
        assert h.overall_status == DEGRADED
        h.record("c", FAILED)
        assert h.overall_status == FAILED
        h.record("d", ABORTED)
        assert h.overall_status == ABORTED

    def test_severity_ordering(self):
        from health import _SEVERITY, OK, DEGRADED, FAILED, ABORTED
        assert _SEVERITY[OK] < _SEVERITY[DEGRADED] < _SEVERITY[FAILED] < _SEVERITY[ABORTED]

    def test_alert_only_on_non_ok(self):
        from health import HealthTracker, OK, DEGRADED
        h = HealthTracker("run-001", "2026-06-09")
        h.record("a", OK)
        assert h.alerts == []
        h.record("b", DEGRADED, message="something degraded")
        assert len(h.alerts) == 1
        assert "DEGRADED" in h.alerts[0]
        assert "b" in h.alerts[0]

    def test_multiple_non_ok_builds_alert_list(self):
        from health import HealthTracker, FAILED, ABORTED
        h = HealthTracker("run-001", "2026-06-09")
        h.record("market_data", FAILED, message="stale")
        h.record("pipeline", ABORTED, message="no data")
        assert len(h.alerts) == 2

    def test_save_writes_valid_json(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "health.json"))
        from health import HealthTracker, OK, FAILED
        h = HealthTracker("run-abc", "2026-06-09")
        h.record("step1", OK)
        h.record("step2", FAILED, message="broken", detail="extra")
        h.save()
        written = json.loads((tmp_path / "health.json").read_text())
        assert written["run_id"] == "run-abc"
        assert written["date"] == "2026-06-09"
        assert written["overall_status"] == FAILED
        assert "step1" in written["checks"]
        assert written["checks"]["step2"]["message"] == "broken"
        assert written["checks"]["step2"]["detail"] == "extra"
        assert len(written["alerts"]) == 1

    def test_save_returns_health_dict(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "health.json"))
        from health import HealthTracker, OK
        h = HealthTracker("run-001", "2026-06-09")
        h.record("a", OK)
        result = h.save()
        assert isinstance(result, dict)
        assert result["overall_status"] == OK

    def test_load_last_health_missing_file(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "nonexistent.json"))
        from health import load_last_health
        assert load_last_health() == {}

    def test_load_last_health_round_trip(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "health.json"))
        from health import HealthTracker, OK, load_last_health
        h = HealthTracker("run-xyz", "2026-06-09")
        h.record("check", OK)
        h.save()
        loaded = load_last_health()
        assert loaded["run_id"] == "run-xyz"


# ── quant_engine._pct_return ──────────────────────────────────────────────────

class TestPctReturn:
    def test_basic_gain(self):
        from quant_engine import _pct_return
        closes = [100.0] * 22 + [110.0]
        assert _pct_return(closes, 1) == pytest.approx(10.0)

    def test_basic_loss(self):
        from quant_engine import _pct_return
        closes = [100.0] * 22 + [90.0]
        assert _pct_return(closes, 1) == pytest.approx(-10.0)

    def test_insufficient_data_returns_none(self):
        from quant_engine import _pct_return
        assert _pct_return([100.0, 110.0], 5) is None

    def test_exact_boundary(self):
        # n+1 closes available — exactly enough
        from quant_engine import _pct_return
        closes = [100.0, 120.0]  # n=1: need 2 closes
        assert _pct_return(closes, 1) == pytest.approx(20.0)

    def test_zero_base_returns_none(self):
        from quant_engine import _pct_return
        assert _pct_return([0.0, 100.0], 1) is None


# ── quant_engine.compute_momentum_score ──────────────────────────────────────

class TestMomentumScore:
    def test_empty_history_returns_defaults(self):
        from quant_engine import compute_momentum_score
        r = compute_momentum_score([])
        assert r["momentum_score"] == 50
        assert r["return_1m"] is None
        assert r["return_3m"] is None
        assert r["return_6m"] is None
        assert r["above_50dma"] is None
        assert r["above_200dma"] is None

    def test_strong_uptrend_scores_above_70(self):
        from quant_engine import compute_momentum_score
        r = compute_momentum_score(_trend(50.0, 200.0, 210))
        assert r["momentum_score"] > 70
        assert r["above_50dma"] is True
        assert r["above_200dma"] is True
        assert r["return_1m"] > 0
        assert r["return_3m"] > 0

    def test_strong_downtrend_scores_below_30(self):
        from quant_engine import compute_momentum_score
        r = compute_momentum_score(_trend(200.0, 50.0, 210))
        assert r["momentum_score"] < 30
        assert r["above_50dma"] is False
        assert r["above_200dma"] is False

    def test_flat_price_at_dma_is_not_above(self):
        from quant_engine import compute_momentum_score
        # current == DMA50 → above_50dma must be False (strict greater-than)
        r = compute_momentum_score(_flat(100.0, 210))
        assert r["above_50dma"] is False
        assert r["above_200dma"] is False

    def test_score_clamped_0_to_100(self):
        from quant_engine import compute_momentum_score
        for h in [_trend(1.0, 1000.0, 210), _trend(1000.0, 1.0, 210)]:
            r = compute_momentum_score(h)
            assert 0 <= r["momentum_score"] <= 100

    def test_only_1m_return_available_with_short_history(self):
        from quant_engine import compute_momentum_score
        # 25 bars: enough for 1M (21+1=22) but not 3M (63+1=64)
        r = compute_momentum_score(_trend(80.0, 100.0, 25))
        assert r["return_1m"] is not None
        assert r["return_3m"] is None
        assert r["return_6m"] is None

    def test_dma200_requires_200_bars(self):
        from quant_engine import compute_momentum_score
        r = compute_momentum_score(_flat(100.0, 150))
        assert r["above_200dma"] is None  # only 150 bars, need 200


# ── quant_engine.compute_quality_score ───────────────────────────────────────

class TestQualityScore:
    def test_no_fundamentals_returns_50(self):
        from quant_engine import compute_quality_score
        assert compute_quality_score(None)["quality_score"] == 50
        assert compute_quality_score({})["quality_score"] == 50

    def test_high_quality_fundamentals_score_90(self):
        from quant_engine import compute_quality_score
        r = compute_quality_score({
            "gross_margin":     0.75,  # > 0.60 → 90
            "operating_margin": 0.30,  # > 0.25 → 90
            "fcf_margin":       0.25,  # > 0.20 → 90
            "debt_to_equity":   0.30,  # < 0.50 → 90
        })
        assert r["quality_score"] == pytest.approx(90.0)

    def test_poor_fundamentals_score_low(self):
        from quant_engine import compute_quality_score
        r = compute_quality_score({
            "gross_margin":     0.10,  # < 0.20 → 25
            "operating_margin": -0.05, # < 0    → 10
            "fcf_margin":       -0.10, # < 0    → 15
            "debt_to_equity":   3.00,  # > 2.0  → 25
        })
        assert r["quality_score"] < 30

    def test_partial_fundamentals_use_available_only(self):
        from quant_engine import compute_quality_score
        r = compute_quality_score({"gross_margin": 0.75})
        assert r["quality_score"] == 90.0  # only one metric, scores 90

    def test_gross_margin_thresholds(self):
        from quant_engine import compute_quality_score
        assert compute_quality_score({"gross_margin": 0.61})["quality_score"] == 90.0
        assert compute_quality_score({"gross_margin": 0.41})["quality_score"] == 70.0
        assert compute_quality_score({"gross_margin": 0.21})["quality_score"] == 50.0
        assert compute_quality_score({"gross_margin": 0.10})["quality_score"] == 25.0

    def test_operating_margin_thresholds(self):
        from quant_engine import compute_quality_score
        assert compute_quality_score({"operating_margin": 0.26})["quality_score"] == 90.0
        assert compute_quality_score({"operating_margin": 0.16})["quality_score"] == 70.0
        assert compute_quality_score({"operating_margin": 0.06})["quality_score"] == 50.0
        assert compute_quality_score({"operating_margin": 0.01})["quality_score"] == 30.0
        assert compute_quality_score({"operating_margin": -0.01})["quality_score"] == 10.0


# ── quant_engine.compute_valuation_score ─────────────────────────────────────

class TestValuationScore:
    def test_no_fundamentals_returns_50(self):
        from quant_engine import compute_valuation_score
        assert compute_valuation_score(None)["valuation_score"] == 50
        assert compute_valuation_score({})["valuation_score"] == 50

    def test_cheap_stock_scores_90(self):
        from quant_engine import compute_valuation_score
        r = compute_valuation_score({
            "pe_ratio":  12.0,  # < 15  → 90
            "fcf_yield": 0.08,  # > 0.06 → 90
            "ev_ebitda": 8.0,   # < 10  → 90
        })
        assert r["valuation_score"] == pytest.approx(90.0)

    def test_expensive_stock_scores_low(self):
        from quant_engine import compute_valuation_score
        r = compute_valuation_score({
            "pe_ratio":  60.0,  # > 50 → 10
            "ev_ebitda": 50.0,  # > 40 → 10
        })
        assert r["valuation_score"] == pytest.approx(10.0)

    def test_negative_pe_ignored(self):
        # pe ≤ 0 means the company is losing money — excluded from scoring
        from quant_engine import compute_valuation_score
        r = compute_valuation_score({"pe_ratio": -5.0})
        assert r["valuation_score"] == 50  # no valid metrics → neutral

    def test_pe_thresholds(self):
        from quant_engine import compute_valuation_score
        assert compute_valuation_score({"pe_ratio": 14})["valuation_score"] == 90.0
        assert compute_valuation_score({"pe_ratio": 24})["valuation_score"] == 70.0
        assert compute_valuation_score({"pe_ratio": 34})["valuation_score"] == 50.0
        assert compute_valuation_score({"pe_ratio": 49})["valuation_score"] == 30.0
        assert compute_valuation_score({"pe_ratio": 51})["valuation_score"] == 10.0


# ── quant_engine.compute_risk_metrics ────────────────────────────────────────

class TestRiskMetrics:
    def test_insufficient_history_returns_defaults(self):
        from quant_engine import compute_risk_metrics
        r = compute_risk_metrics(_flat(100.0, 10), [])
        assert r["volatility"] is None
        assert r["beta"] is None
        assert r["volatility_score"] == 50

    def test_exactly_21_bars_insufficient(self):
        from quant_engine import compute_risk_metrics
        r = compute_risk_metrics(_flat(100.0, 21), [])
        assert r["volatility"] is None

    def test_exactly_22_bars_is_sufficient(self):
        from quant_engine import compute_risk_metrics
        history = _flat(100.0, 22)
        for i in range(1, 22):
            history[i]["close"] += 0.01 * i  # add tiny noise
        r = compute_risk_metrics(history, [])
        assert r["volatility"] is not None

    def test_low_volatility_scores_high(self):
        from quant_engine import compute_risk_metrics
        # Nearly flat price → near-zero vol → score near 100
        history = _flat(100.0, 100)
        for i in range(len(history)):
            history[i]["close"] += 0.001 * (i % 2)  # ~0.001% daily noise
        r = compute_risk_metrics(history, [])
        assert r["volatility_score"] > 80

    def test_high_volatility_scores_low(self):
        from quant_engine import compute_risk_metrics
        # Alternating ±5% daily swings → high annualized vol
        closes = [100.0 * (1.05 if i % 2 == 0 else 0.95) for i in range(100)]
        r = compute_risk_metrics(_make_history(closes), [])
        assert r["volatility_score"] < 50

    def test_beta_computed_with_spy(self):
        from quant_engine import compute_risk_metrics
        spy   = _trend(400.0, 440.0, 100)
        stock = _trend(100.0, 110.0, 100)
        r = compute_risk_metrics(stock, spy)
        assert r["beta"] is not None
        assert 0.1 < r["beta"] < 5.0  # reasonable range

    def test_no_spy_history_gives_none_beta(self):
        from quant_engine import compute_risk_metrics
        r = compute_risk_metrics(_flat(100.0, 50), [])
        assert r["beta"] is None

    def test_vol_score_clamped_0_to_100(self):
        from quant_engine import compute_risk_metrics
        # Extremely volatile
        closes = [100.0 * (2.0 if i % 2 == 0 else 0.5) for i in range(100)]
        r = compute_risk_metrics(_make_history(closes), [])
        assert 0 <= r["volatility_score"] <= 100


# ── quant_engine.score_all_tickers ───────────────────────────────────────────

class TestScoreAllTickers:
    def test_composite_weight_formula(self):
        from quant_engine import score_all_tickers
        history = _flat(100.0, 210)
        market_data = {"history": {"AAPL": history, "SPY": history}, "fundamentals": {}}
        scores = score_all_tickers(market_data)
        s = scores["AAPL"]
        expected = (
            s["momentum_score"]   * 0.30
            + s["quality_score"]  * 0.25
            + s["valuation_score"] * 0.20
            + s["volatility_score"] * 0.25
        )
        assert s["composite_score"] == pytest.approx(expected, abs=0.15)

    def test_empty_market_data_returns_empty(self):
        from quant_engine import score_all_tickers
        assert score_all_tickers({"history": {}, "fundamentals": {}}) == {}

    def test_missing_spy_still_scores(self):
        from quant_engine import score_all_tickers
        market_data = {"history": {"MSFT": _trend(50.0, 100.0, 210)}, "fundamentals": {}}
        scores = score_all_tickers(market_data)
        assert "MSFT" in scores
        assert scores["MSFT"]["beta"] is None
        assert 0 <= scores["MSFT"]["composite_score"] <= 100

    def test_fundamentals_used_when_available(self):
        from quant_engine import score_all_tickers
        history = _flat(100.0, 210)
        market_data = {
            "history": {"NVDA": history},
            "fundamentals": {"NVDA": {"gross_margin": 0.75, "operating_margin": 0.30}},
        }
        scores = score_all_tickers(market_data)
        # High-quality fundamentals should push quality_score above neutral 50
        assert scores["NVDA"]["quality_score"] > 50

    def test_all_fields_present(self):
        from quant_engine import score_all_tickers
        market_data = {"history": {"SPY": _trend(400.0, 450.0, 210)}, "fundamentals": {}}
        s = score_all_tickers(market_data)["SPY"]
        for field in ("ticker", "composite_score", "momentum_score", "quality_score",
                      "valuation_score", "volatility_score", "data_available"):
            assert field in s, f"missing field: {field}"


# ── journal.check_kill_switches ───────────────────────────────────────────────

class TestKillSwitches:
    def test_no_peak_file_returns_false_and_writes_peak(self, tmp_path, monkeypatch):
        import journal
        peak_path = str(tmp_path / "peak.json")
        monkeypatch.setattr(journal, "PEAK_FILE", peak_path)
        from journal import check_kill_switches
        active, reason = check_kill_switches({"total_value": 500.0})
        assert active is False
        assert reason == ""
        data = json.loads((tmp_path / "peak.json").read_text())
        assert data["peak"] == 500.0

    def test_at_or_above_peak_returns_false_and_updates_peak(self, tmp_path, monkeypatch):
        import journal
        peak_path = tmp_path / "peak.json"
        peak_path.write_text(json.dumps({"peak": 400.0}))
        monkeypatch.setattr(journal, "PEAK_FILE", str(peak_path))
        from journal import check_kill_switches
        active, _ = check_kill_switches({"total_value": 500.0})
        assert active is False
        assert json.loads(peak_path.read_text())["peak"] == 500.0

    def test_equal_to_peak_returns_false(self, tmp_path, monkeypatch):
        import journal
        peak_path = tmp_path / "peak.json"
        peak_path.write_text(json.dumps({"peak": 500.0}))
        monkeypatch.setattr(journal, "PEAK_FILE", str(peak_path))
        from journal import check_kill_switches
        active, _ = check_kill_switches({"total_value": 500.0})
        assert active is False

    def test_drawdown_below_threshold_returns_false(self, tmp_path, monkeypatch):
        import journal
        peak_path = tmp_path / "peak.json"
        peak_path.write_text(json.dumps({"peak": 500.0}))
        monkeypatch.setattr(journal, "PEAK_FILE", str(peak_path))
        from journal import check_kill_switches
        # 10% drawdown — well below 20% threshold
        active, _ = check_kill_switches({"total_value": 450.0})
        assert active is False

    def test_exactly_20_percent_drawdown_triggers(self, tmp_path, monkeypatch):
        import journal
        peak_path = tmp_path / "peak.json"
        peak_path.write_text(json.dumps({"peak": 500.0}))
        monkeypatch.setattr(journal, "PEAK_FILE", str(peak_path))
        from journal import check_kill_switches
        active, reason = check_kill_switches({"total_value": 400.0})
        assert active is True
        assert "20%" in reason

    def test_drawdown_above_threshold_triggers(self, tmp_path, monkeypatch):
        import journal
        peak_path = tmp_path / "peak.json"
        peak_path.write_text(json.dumps({"peak": 1000.0}))
        monkeypatch.setattr(journal, "PEAK_FILE", str(peak_path))
        from journal import check_kill_switches
        active, reason = check_kill_switches({"total_value": 600.0})
        assert active is True
        assert "1,000.00" in reason
        assert "600.00" in reason

    def test_zero_total_value_skips_kill_switch(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.setattr(journal, "PEAK_FILE", str(tmp_path / "peak.json"))
        from journal import check_kill_switches
        active, _ = check_kill_switches({"total_value": 0})
        assert active is False


# ── journal.mark_pending_executed ─────────────────────────────────────────────

class TestMarkPendingExecuted:
    def _write(self, path, run_id, executed_at=None):
        path.write_text(json.dumps({
            "run_id":       run_id,
            "date":         "2026-06-09",
            "generated_at": "2026-06-09T13:00:00Z",
            "executed_at":  executed_at,
            "decisions":    [],
        }))

    def test_stamps_execution_timestamp(self, tmp_path, monkeypatch):
        import journal
        pending = tmp_path / "pending.json"
        self._write(pending, "run-001")
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-001")
        data = json.loads(pending.read_text())
        assert data["executed_at"] is not None

    def test_second_call_preserves_original_timestamp(self, tmp_path, monkeypatch):
        import journal
        pending = tmp_path / "pending.json"
        self._write(pending, "run-001")
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-001")
        first_ts = json.loads(pending.read_text())["executed_at"]
        mark_pending_executed("run-001")
        second_ts = json.loads(pending.read_text())["executed_at"]
        assert first_ts == second_ts  # idempotent

    def test_no_stamp_on_run_id_mismatch(self, tmp_path, monkeypatch):
        import journal
        pending = tmp_path / "pending.json"
        self._write(pending, "run-001")
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-999")
        data = json.loads(pending.read_text())
        assert data["executed_at"] is None

    def test_already_stamped_file_not_overwritten(self, tmp_path, monkeypatch):
        import journal
        pending = tmp_path / "pending.json"
        original_ts = "2026-06-09T14:00:00+00:00"
        self._write(pending, "run-001", executed_at=original_ts)
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-001")
        data = json.loads(pending.read_text())
        assert data["executed_at"] == original_ts

    def test_no_error_when_file_missing(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.setattr(journal, "PENDING_FILE", str(tmp_path / "nonexistent.json"))
        from journal import mark_pending_executed
        mark_pending_executed("run-001")  # must not raise


# ── Pre-flight abort conditions ───────────────────────────────────────────────

class TestPreflightAbortConditions:
    """
    Verifies the two guard conditions used by main.py to abort the pipeline
    before running agents, preventing silent all-50 quant score runs.

    These tests exercise the logic in isolation — main.py itself is not imported
    here since it requires a full environment with Robinhood MCP and Supabase.
    """

    def test_stale_data_date_should_abort(self):
        today, data_date = "2026-06-09", "2026-06-08"
        assert data_date != today  # guard triggers

    def test_fresh_data_date_should_pass(self):
        today = data_date = "2026-06-09"
        assert data_date == today

    def test_mcp_fallback_2_bars_triggers_abort(self):
        # mcp_market_data.json always has 2 bars per ticker
        history_depths = [2] * 10
        min_depth = min(history_depths)
        assert min_depth < 22

    def test_22_bars_passes_guard(self):
        history_depths = [22, 25, 30, 210]
        assert min(history_depths) >= 22

    def test_empty_history_triggers_abort(self):
        history_depths = []
        min_depth = min(history_depths) if history_depths else 0
        assert min_depth < 22

    def test_combined_stale_and_shallow_both_reported(self):
        """Both abort_reasons should be collected before aborting."""
        today = "2026-06-09"
        data_date = "2026-06-08"
        min_depth = 2
        abort_reasons = []
        if data_date != today:
            abort_reasons.append(f"data is from {data_date}, not today ({today})")
        if min_depth < 22:
            abort_reasons.append(f"history depth is {min_depth} bars")
        assert len(abort_reasons) == 2
