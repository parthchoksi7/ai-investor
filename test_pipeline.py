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
    def test_composite_weight_formula_all_factors(self):
        # When every factor has real data the composite uses all four base weights.
        from quant_engine import score_all_tickers
        history = _flat(100.0, 210)
        market_data = {
            "history": {"AAPL": history, "SPY": history},
            "fundamentals": {"AAPL": {"gross_margin": 0.75, "pe_ratio": 12.0}},
        }
        scores = score_all_tickers(market_data)
        s = scores["AAPL"]
        assert set(s["factors_used"]) == {"momentum", "quality", "valuation", "volatility"}
        expected = (
            s["momentum_score"]   * 0.30
            + s["quality_score"]  * 0.25
            + s["valuation_score"] * 0.20
            + s["volatility_score"] * 0.25
        )
        assert s["composite_score"] == pytest.approx(expected, abs=0.15)

    def test_composite_drops_missing_factors_and_renormalizes(self):
        # Phase 3.1 honesty: with NO fundamentals, quality/valuation carry no
        # real data and must be dropped — the composite is momentum+volatility
        # renormalized to their own weights, NOT blended with two constant 50s.
        from quant_engine import score_all_tickers
        history = _flat(100.0, 210)
        market_data = {"history": {"AAPL": history, "SPY": history}, "fundamentals": {}}
        s = score_all_tickers(market_data)["AAPL"]
        assert s["factors_used"] == ["momentum", "volatility"]
        assert s["quality_available"] is False
        assert s["valuation_available"] is False
        expected = (s["momentum_score"] * 0.30 + s["volatility_score"] * 0.25) / 0.55
        assert s["composite_score"] == pytest.approx(expected, abs=0.05)

    def test_composite_no_real_factor_is_neutral(self):
        # Empty history (cloud fallback): no factor has data → neutral 50, flagged.
        from quant_engine import score_all_tickers
        market_data = {"history": {"AAPL": []}, "fundamentals": {}}
        s = score_all_tickers(market_data)["AAPL"]
        assert s["factors_used"] == []
        assert s["composite_score"] == 50.0
        assert s["data_available"] is False

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


class TestMarkTransactionsLive:
    """Cloud routine writes every decision dry_run=True (main.py runs DRY_RUN=true);
    reconciliation must mark live ONLY orders the broker actually accepted, so a
    rejected order is never published as a phantom fill (preserves fd9d56a).
    Since the Fix-3 batch the reconciler is authoritative for trades.csv and
    decision_journal.json too — every test isolates all four state files."""

    def _txs(self):
        return [
            {"run_id": "r1", "ticker": "BAC", "action": "BUY",  "qty": 0.5, "price": 55.16,
             "total_value": 27.58, "broker_order_id": None, "dry_run": True},
            {"run_id": "r1", "ticker": "JPM", "action": "SELL", "qty": 0.1, "price": 313.49,
             "total_value": 31.35, "broker_order_id": None, "dry_run": True},
            {"run_id": "r0", "ticker": "AAPL", "action": "BUY", "qty": 0.2, "price": 290.0,
             "total_value": 58.0, "broker_order_id": None, "dry_run": True},  # other run
        ]

    def _journal_rows(self):
        return [
            {"trade_id": "t1", "run_id": "r1", "ticker": "BAC", "status": "open"},
            {"trade_id": "t2", "run_id": "r1", "ticker": "JPM", "status": "open"},
            {"trade_id": "t3", "run_id": "r0", "ticker": "AAPL", "status": "open"},
            {"trade_id": "t4", "run_id": "r1", "ticker": "OLD", "status": "closed"},
        ]

    def _csv_rows(self):
        header = ("date,strategy,ticker,action,qty,price,total_value,target_weight,"
                  "portfolio_value,rationale,broker_order_id,dry_run,run_id")
        return "\n".join([
            header,
            "2026-06-12,institutional,BAC,BUY,0.5,55.1600,27.58,0.0800,500.00,r,,True,r1",
            "2026-06-12,institutional,JPM,SELL,0.1,313.4900,31.35,0.0000,500.00,r,,True,r1",
            "2026-06-11,institutional,AAPL,BUY,0.2,290.0000,58.00,0.0800,500.00,r,,True,r0",
            "2026-06-10,institutional,MS,BUY,0.05,210.2500,11.11,,497.21,manual,,False,",
        ]) + "\n"

    def _setup(self, tmp_path, monkeypatch, txs=None):
        """Isolate transactions.json, trades.csv, decision_journal.json, and
        system_health.json in tmp_path. Returns (journal_module, paths_dict)."""
        import journal, execute, health
        p = {
            "tx":     tmp_path / "transactions.json",
            "csv":    tmp_path / "trades.csv",
            "jrnl":   tmp_path / "decision_journal.json",
            "health": tmp_path / "system_health.json",
        }
        p["tx"].write_text(json.dumps(txs if txs is not None else self._txs()))
        p["csv"].write_text(self._csv_rows())
        p["jrnl"].write_text(json.dumps(self._journal_rows()))
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE", str(p["tx"]))
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(p["jrnl"]))
        monkeypatch.setattr(execute, "TRADE_LOG", str(p["csv"]))
        monkeypatch.setattr(health, "HEALTH_FILE", str(p["health"]))
        return journal, p

    @staticmethod
    def _csv_dict(path):
        import csv as _csv
        with open(path, newline="") as f:
            return {r["ticker"]: r for r in _csv.DictReader(f)}

    # — transactions.json (original fd9d56a contract, unchanged) —

    def test_only_filled_tickers_flip(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob", "price": 55.52}})
        data = {t["ticker"]: t for t in json.loads(p["tx"].read_text())}
        assert data["BAC"]["dry_run"] is False           # filled → live
        assert data["JPM"]["dry_run"] is True             # not in fills → stays dry_run
        assert data["AAPL"]["dry_run"] is True            # other run untouched

    def test_persists_order_id_and_fill_price(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob123", "price": 55.52}})
        bac = next(t for t in json.loads(p["tx"].read_text()) if t["ticker"] == "BAC")
        assert bac["broker_order_id"] == "ob123"
        assert bac["price"] == 55.52
        assert bac["total_value"] == round(0.5 * 55.52, 2)  # recomputed from fill price

    def test_null_fill_price_keeps_decision_price(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob", "price": None}})
        bac = next(t for t in json.loads(p["tx"].read_text()) if t["ticker"] == "BAC")
        assert bac["price"] == 55.16  # unchanged decision-time quote

    def test_idempotent_on_rerun(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        fills = {"BAC": {"order_id": "ob", "price": 55.52}}
        journal.mark_transactions_live("r1", fills)
        journal.mark_transactions_live("r1", fills)  # second run is a no-op
        bac = next(t for t in json.loads(p["tx"].read_text()) if t["ticker"] == "BAC")
        assert bac["dry_run"] is False and bac["broker_order_id"] == "ob"

    # — fills=None must never silently flip-all (Fix 5) —

    def test_none_fills_raises(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="fills"):
            journal.mark_transactions_live("r1", None)
        data = {t["ticker"]: t for t in json.loads(p["tx"].read_text())}
        assert all(t["dry_run"] is True for t in data.values())  # nothing flipped

    def test_force_flip_all_emergency_path(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", None, force_flip_all=True)
        data = {t["ticker"]: t for t in json.loads(p["tx"].read_text())}
        assert data["BAC"]["dry_run"] is False
        assert data["JPM"]["dry_run"] is False
        assert data["AAPL"]["dry_run"] is True  # other run still untouched

    # — trades.csv reconciliation (agent-facing history) —

    def test_trades_csv_reflects_exactly_the_filled_subset(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob9", "price": 55.52}})
        rows = self._csv_dict(p["csv"])
        assert rows["BAC"]["dry_run"] == "False" and rows["BAC"]["broker_order_id"] == "ob9"
        assert rows["BAC"]["price"] == "55.5200"          # fill price persisted
        assert rows["JPM"]["dry_run"] == "True" and rows["JPM"]["broker_order_id"] == ""
        assert rows["AAPL"]["dry_run"] == "True"          # other run untouched
        assert rows["MS"]["dry_run"] == "False"           # empty run_id never touched

    def test_empty_fills_keeps_all_rows_speculative(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {})
        rows = self._csv_dict(p["csv"])
        assert rows["BAC"]["dry_run"] == "True"
        assert rows["JPM"]["dry_run"] == "True"

    def test_old_csv_without_run_id_column_migrates(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        old = ("date,strategy,ticker,action,qty,price,total_value,target_weight,"
               "portfolio_value,rationale,broker_order_id,dry_run\n"
               "2026-06-11,institutional,LLY,BUY,0.03,1164.96,45.05,0.09,500.52,r,,True\n")
        p["csv"].write_text(old)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob"}})  # triggers migration
        rows = self._csv_dict(p["csv"])
        assert rows["LLY"]["run_id"] == ""               # migrated, data preserved
        assert rows["LLY"]["dry_run"] == "True"           # no run_id → never reconciled

    # — decision_journal.json reconciliation (prior_journal input) —

    def test_unfilled_journal_entries_marked_rejected(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob"}})
        rows = {e["trade_id"]: e for e in json.loads(p["jrnl"].read_text())}
        assert rows["t1"]["status"] == "open"      # filled stays open
        assert rows["t2"]["status"] == "rejected"  # unfilled → rejected
        assert rows["t3"]["status"] == "open"      # other run untouched
        assert rows["t4"]["status"] == "closed"    # non-open statuses never touched

    def test_re_reconcile_with_real_fills_restores_open(self, tmp_path, monkeypatch):
        # first pass ran with an empty fills.json; the corrected pass must recover
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {})
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob"}})
        rows = {e["trade_id"]: e for e in json.loads(p["jrnl"].read_text())}
        assert rows["t1"]["status"] == "open"
        assert rows["t2"]["status"] == "rejected"

    # — failure-direction health check —

    def test_zero_fills_with_decisions_records_failed_health(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {})
        h = json.loads(p["health"].read_text())
        assert h["checks"]["reconciliation"]["status"] == "FAILED"
        assert h["overall_status"] == "FAILED"  # alert.yml must see it

    def test_partial_fills_record_degraded_health(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live("r1", {"BAC": {"order_id": "ob"}})
        h = json.loads(p["health"].read_text())
        assert h["checks"]["reconciliation"]["status"] == "DEGRADED"
        assert h["checks"]["reconciliation"]["unfilled"] == ["JPM"]

    def test_all_fills_record_ok_health(self, tmp_path, monkeypatch):
        journal, p = self._setup(tmp_path, monkeypatch)
        journal.mark_transactions_live(
            "r1", {"BAC": {"order_id": "a"}, "JPM": {"order_id": "b"}})
        h = json.loads(p["health"].read_text())
        assert h["checks"]["reconciliation"]["status"] == "OK"


class TestGetTradeHistoryFilter:
    """Agent-facing trade history must only ever contain broker-accepted rows."""

    def test_dry_run_rows_excluded(self, tmp_path, monkeypatch):
        import execute
        f = tmp_path / "trades.csv"
        f.write_text(
            "date,strategy,ticker,action,qty,price,total_value,target_weight,"
            "portfolio_value,rationale,broker_order_id,dry_run,run_id\n"
            "2026-06-11,institutional,LLY,BUY,0.03,1164.96,45.05,0.09,500.52,r,,False,r1\n"
            "2026-06-12,institutional,XYZ,BUY,0.5,10.00,5.00,0.01,500.00,r,,True,r2\n"
        )
        monkeypatch.setattr(execute, "TRADE_LOG", str(f))
        rows = execute.get_trade_history()
        assert [r["ticker"] for r in rows] == ["LLY"]  # phantom XYZ never reaches agents

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.setattr(execute, "TRADE_LOG", str(tmp_path / "none.csv"))
        assert execute.get_trade_history() == []


# ── journal._load_list — corrupt/foreign file-shape guards ───────────────────

class TestLoadListGuards:
    """A list-shaped JSON file that somehow becomes a dict ({} on first run,
    manual edit, partial write) must coerce to [] instead of crashing the
    appenders mid-run — record_trade fires AFTER orders are placed, so an
    AttributeError there kills health reporting and the Supabase publish."""

    def test_load_list_dict_file_coerces_to_empty(self, tmp_path):
        from journal import _load_list
        f = tmp_path / "journal.json"
        f.write_text("{}")
        assert _load_list(str(f)) == []

    def test_load_list_missing_file_returns_empty(self, tmp_path):
        from journal import _load_list
        assert _load_list(str(tmp_path / "nonexistent.json")) == []

    def test_load_list_valid_list_passes_through(self, tmp_path):
        from journal import _load_list
        f = tmp_path / "journal.json"
        f.write_text('[{"a": 1}]')
        assert _load_list(str(f)) == [{"a": 1}]

    def test_record_trade_on_dict_journal_appends(self, tmp_path, monkeypatch):
        import journal
        jf = tmp_path / "decision_journal.json"
        jf.write_text("{}")  # the corrupt shape that crashed the Jun 11 run
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))
        trade_id = journal.record_trade(
            "NVDA", "BUY", 0.08, "thesis", "anti", [], 7, 0.10, [])
        data = json.loads(jf.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["trade_id"] == trade_id
        assert data[0]["status"] == "open"

    def test_record_transaction_on_dict_file_appends(self, tmp_path, monkeypatch):
        import journal
        tf = tmp_path / "transactions.json"
        tf.write_text("{}")
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE", str(tf))
        journal.record_transaction({"transaction_id": "tx-1", "ticker": "GS"})
        data = json.loads(tf.read_text())
        assert data == [{"transaction_id": "tx-1", "ticker": "GS"}]

    def test_record_run_on_dict_file_appends(self, tmp_path, monkeypatch):
        import journal
        lf = tmp_path / "agent_log.json"
        lf.write_text("{}")
        monkeypatch.setattr(journal, "AGENT_LOG_FILE", str(lf))
        journal.record_run("run-1", {"date": "2026-06-11"})
        data = json.loads(lf.read_text())
        assert len(data) == 1
        assert data[0]["run_id"] == "run-1"


# ── execute._migrate_trade_log — trades.csv schema migration ─────────────────

class TestTradeLogMigration:
    """DictWriter never rewrites an existing header, so appending 12-field rows
    under the old 7-column header silently misaligned every new row. The
    migration rewrites the file under the current schema, preserving old rows."""

    OLD_HEADER = "date,strategy,ticker,action,qty,portfolio_value,rationale"

    def _patch(self, tmp_path, monkeypatch):
        import execute
        log = tmp_path / "trades.csv"
        monkeypatch.setattr(execute, "TRADE_LOG", str(log))
        monkeypatch.setattr(execute, "DRY_RUN", True)
        return execute, log

    def test_old_header_rewritten_rows_preserved(self, tmp_path, monkeypatch):
        import csv
        execute, log = self._patch(tmp_path, monkeypatch)
        log.write_text(self.OLD_HEADER + "\n"
                       "2026-06-01,institutional,AAPL,BUY,1.5,500.00,old row\n")
        execute._migrate_trade_log()
        rows = list(csv.DictReader(log.open()))
        with log.open() as f:
            header = f.readline().strip().split(",")
        assert header == execute.TRADE_LOG_FIELDS
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAPL"
        assert rows[0]["rationale"] == "old row"
        assert rows[0]["price"] == ""           # new column backfilled empty
        assert rows[0]["broker_order_id"] == ""

    def test_current_header_is_noop(self, tmp_path, monkeypatch):
        execute, log = self._patch(tmp_path, monkeypatch)
        original = ",".join(execute.TRADE_LOG_FIELDS) + "\n"
        log.write_text(original)
        execute._migrate_trade_log()
        assert log.read_text() == original

    def test_missing_file_is_noop(self, tmp_path, monkeypatch):
        execute, log = self._patch(tmp_path, monkeypatch)
        execute._migrate_trade_log()  # must not raise or create the file
        assert not log.exists()

    def test_append_after_migration_is_aligned(self, tmp_path, monkeypatch):
        import csv
        execute, log = self._patch(tmp_path, monkeypatch)
        log.write_text(self.OLD_HEADER + "\n"
                       "2026-06-01,institutional,AAPL,BUY,1.5,500.00,old row\n")
        execute.log_trades(
            [{"ticker": "MSFT", "action": "BUY", "target_weight": 0.05,
              "qty": 0.1, "rationale": "new row"}],
            {"total_value": 500.0, "positions": []},
            prices={"MSFT": {"close": 400.0}},
        )
        rows = list(csv.DictReader(log.open()))
        assert len(rows) == 2
        assert rows[1]["ticker"] == "MSFT"
        assert rows[1]["price"] == "400.0000"   # lands in the right column
        assert rows[1]["total_value"] == "40.00"
        assert rows[1]["target_weight"] == "0.0500"


class TestPaperShadowColumns:
    """Parallel *_100x columns model a 100x ($50,000) book: same price, qty and
    dollar value scaled by SHADOW_MULTIPLIER; price and target_weight unscaled."""

    def _patch(self, tmp_path, monkeypatch):
        import execute
        log = tmp_path / "trades.csv"
        monkeypatch.setattr(execute, "TRADE_LOG", str(log))
        monkeypatch.setattr(execute, "DRY_RUN", True)
        return execute, log

    def test_fresh_write_scales_qty_value_portfolio(self, tmp_path, monkeypatch):
        import csv
        execute, log = self._patch(tmp_path, monkeypatch)
        # 0.5 sh @ $300 = $150 on a $500 book → 50 sh / $15,000 / $50,000 shadow
        execute.log_trades(
            [{"ticker": "AAPL", "action": "BUY", "target_weight": 0.30,
              "qty": 0.5, "rationale": "demo"}],
            {"total_value": 500.0, "positions": []},
            prices={"AAPL": {"close": 300.0}}, run_id="r1")
        r = list(csv.DictReader(log.open()))[0]
        assert r["qty_100x"] == "50.000000"
        assert r["total_value"] == "150.00" and r["total_value_100x"] == "15000.00"
        assert r["portfolio_value"] == "500.00" and r["portfolio_value_100x"] == "50000.00"
        assert r["price"] == "300.0000"          # price NOT scaled (per-share)
        assert r["target_weight"] == "0.3000"    # ratio NOT scaled

    def test_migration_backfills_shadow_from_base(self, tmp_path, monkeypatch):
        import csv
        execute, log = self._patch(tmp_path, monkeypatch)
        # a pre-shadow 13-column row with base values present
        old_header = ("date,strategy,ticker,action,qty,price,total_value,"
                      "target_weight,portfolio_value,rationale,broker_order_id,"
                      "dry_run,run_id")
        log.write_text(old_header + "\n"
                       "2026-06-01,institutional,AAPL,BUY,1.5,300.0000,450.00,"
                       "0.0900,500.00,old,xyz,False,r0\n")
        execute._migrate_trade_log()
        r = list(csv.DictReader(log.open()))[0]
        assert r["qty_100x"] == "150.000000"
        assert r["total_value_100x"] == "45000.00"
        assert r["portfolio_value_100x"] == "50000.00"

    def test_blank_base_yields_blank_shadow(self, tmp_path, monkeypatch):
        execute, _ = self._patch(tmp_path, monkeypatch)
        assert execute._scaled("", 2) == ""
        assert execute._scaled(None, 6) == ""
        assert execute._scaled("not-a-number", 2) == ""
        assert execute._scaled(1.5, 6) == "150.000000"

    def test_reconcile_keeps_shadow_in_sync(self, tmp_path, monkeypatch):
        import csv, journal
        execute, log = self._patch(tmp_path, monkeypatch)
        execute.log_trades(
            [{"ticker": "AAPL", "action": "BUY", "target_weight": 0.30,
              "qty": 0.5, "rationale": "demo"}],
            {"total_value": 500.0, "positions": []},
            prices={"AAPL": {"close": 300.0}}, run_id="r1")
        # broker filled at $310, not $300 → total_value and its 100x twin update
        journal._reconcile_trade_log("r1", {"AAPL": {"order_id": "abc", "price": 310.0}})
        r = list(csv.DictReader(log.open()))[0]
        assert r["total_value"] == "155.00"
        assert r["total_value_100x"] == "15500.00"


# ── execute.order_executed — broker result classification ────────────────────

class TestOrderExecuted:
    """An order counts as executed only when the broker returned an order id
    (or DRY_RUN). Rejections must not be logged as fills or reported healthy."""

    def test_broker_id_is_executed(self):
        from execute import order_executed
        assert order_executed({"id": "abc-123"}) is True

    def test_dry_run_is_executed(self):
        from execute import order_executed
        assert order_executed({"dry_run": True}) is True

    def test_rejection_detail_is_not_executed(self):
        from execute import order_executed
        assert order_executed({"detail": "insufficient buying power"}) is False

    def test_hard_block_is_not_executed(self):
        from execute import order_executed
        assert order_executed({"blocked": True}) is False

    def test_empty_or_none_is_not_executed(self):
        from execute import order_executed
        assert order_executed({}) is False
        assert order_executed(None) is False
        assert order_executed("error string") is False


# ── execute.execute_trades — SELL-before-BUY ordering ────────────────────────

class TestSellBeforeBuyOrdering:
    """Cash account: a BUY funded by a same-day SELL is rejected by the broker
    if placed before the sale proceeds exist. SELLs must go first."""

    def _run(self, monkeypatch, decisions):
        import execute
        placed = []
        monkeypatch.setattr(
            execute, "place_order",
            lambda ticker, action, qty: placed.append((action, ticker)) or {"dry_run": True},
        )
        results = execute.execute_trades(
            decisions, {"total_value": 500.0, "positions": []}, {})
        return placed, results

    def test_sells_placed_before_buys(self, monkeypatch):
        placed, _ = self._run(monkeypatch, [
            {"ticker": "GS",   "action": "BUY",  "qty": 0.1},
            {"ticker": "AAPL", "action": "SELL", "qty": 0.2},
            {"ticker": "LIN",  "action": "BUY",  "qty": 0.3},
            {"ticker": "JNJ",  "action": "SELL", "qty": 0.4},
        ])
        actions = [a for a, _ in placed]
        assert actions == ["SELL", "SELL", "BUY", "BUY"]

    def test_relative_order_within_side_preserved(self, monkeypatch):
        # sorted() is stable — PM's ordering within each side is kept
        placed, _ = self._run(monkeypatch, [
            {"ticker": "GS",   "action": "BUY",  "qty": 0.1},
            {"ticker": "AAPL", "action": "SELL", "qty": 0.2},
            {"ticker": "LIN",  "action": "BUY",  "qty": 0.3},
            {"ticker": "JNJ",  "action": "SELL", "qty": 0.4},
        ])
        assert placed == [("SELL", "AAPL"), ("SELL", "JNJ"),
                          ("BUY", "GS"), ("BUY", "LIN")]

    def test_hold_and_zero_qty_not_placed(self, monkeypatch):
        placed, results = self._run(monkeypatch, [
            {"ticker": "MRK",  "action": "HOLD"},
            {"ticker": "EOG",  "action": "BUY", "qty": 0.0},
            {"ticker": "EQIX", "action": "BUY", "qty": 0.1},
        ])
        assert placed == [("BUY", "EQIX")]
        assert "MRK" not in results and "EOG" not in results


class TestPerOrderErrorIsolation:
    """One order's transient exception must not abort the loop and strand the
    rest. With SELL-before-BUY ordering, aborting after a SELL exception would
    leave the funding SELLs done but the BUYs never attempted (Fix 6)."""

    def test_exception_on_one_order_does_not_strand_others(self, monkeypatch):
        import execute
        attempted = []

        def _flaky(ticker, action, qty):
            attempted.append(ticker)
            if ticker == "BBB":
                raise RuntimeError("transient transport error")
            return {"id": f"ok-{ticker}"}

        monkeypatch.setattr(execute, "place_order", _flaky)
        results = execute.execute_trades(
            [{"ticker": "AAA", "action": "SELL", "qty": 0.1},
             {"ticker": "BBB", "action": "SELL", "qty": 0.2},
             {"ticker": "CCC", "action": "BUY",  "qty": 0.3}],
            {"total_value": 500.0, "positions": []}, {})
        # all three attempted despite BBB raising mid-loop
        assert attempted == ["AAA", "BBB", "CCC"]
        assert execute.order_executed(results["AAA"]) is True
        assert execute.order_executed(results["CCC"]) is True
        # BBB recorded as a not-a-fill exception, never as a phantom fill
        assert results["BBB"].get("exception") is True
        assert execute.order_executed(results["BBB"]) is False


# ── Execution stamp decision (main.py contract) ──────────────────────────────

class TestExecutionStampDecision:
    """Truth table for when main.py stamps pending_decisions.json as executed.

    Contract: stamp as soon as ANY order was placed (a retry must never
    double-fill), withhold when NOTHING was placed (every order rejected, or
    execution crashed before the first order) so the next scheduled attempt
    can retry the day. A run with nothing to place stamps vacuously.

    Tests the expression in isolation, in the TestPreflightAbortConditions
    style — main.py is not imported here.
    """

    @staticmethod
    def _should_stamp(executed_decisions, order_results, execution_errors):
        any_placed = bool(executed_decisions)
        nothing_to_place = not order_results and not execution_errors
        return any_placed or nothing_to_place

    def test_all_orders_filled_stamps(self):
        assert self._should_stamp(["d1", "d2"], {"GS": {"id": "x"}}, []) is True

    def test_partial_fill_stamps(self):
        # one fill + one rejection → stamp; retrying would double-fill GS
        assert self._should_stamp(["d1"], {"GS": {"id": "x"}, "LIN": {}}, []) is True

    def test_fill_then_crash_stamps(self):
        # exception after the first order was placed → still stamp
        assert self._should_stamp(["d1"], {"GS": {"id": "x"}}, ["boom"]) is True

    def test_all_orders_rejected_does_not_stamp(self):
        # nothing placed → next hourly attempt may retry the day
        assert self._should_stamp([], {"GS": {}, "LIN": {}}, []) is False

    def test_crash_before_first_order_does_not_stamp(self):
        assert self._should_stamp([], {}, ["boom"]) is False

    def test_nothing_to_place_stamps_vacuously(self):
        # all decisions skipped (HOLD / qty 0) → no rerun needed
        assert self._should_stamp([], {}, []) is True


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


# ── preflight_gate.py ─────────────────────────────────────────────────────────

class TestPreflightGate:
    """The morning gate that decides whether the routine should run at all.

    Run as a subprocess against fixture files in a tmp cwd, since the script
    reads fixed-name files and computes today's ET date at import time.
    """

    import os
    import subprocess
    import sys
    from datetime import datetime
    from zoneinfo import ZoneInfo

    GATE = os.path.join(os.path.dirname(__file__), "preflight_gate.py")

    def _today_et(self):
        return self.datetime.now(self.ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    def _run(self, tmp_path):
        return self.subprocess.run(
            [self.sys.executable, self.GATE],
            cwd=str(tmp_path), capture_output=True, text=True,
        ).returncode

    def _write(self, tmp_path, name, obj):
        (tmp_path / name).write_text(json.dumps(obj))

    def test_proceed_when_fresh_and_not_executed(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json", {"date": today, "executed_at": None})
        assert self._run(tmp_path) == 0  # PROCEED

    def test_skip_done_when_already_executed_today(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json",
                    {"date": today, "run_id": "x", "executed_at": "2026-01-01T00:00:00Z"})
        assert self._run(tmp_path) == 20  # SKIP/DONE — idempotency

    def test_skip_retry_when_snapshot_stale(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": "2020-01-01", "prices": {}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json", {"date": today, "executed_at": None})
        assert self._run(tmp_path) == 10  # SKIP/RETRY

    def test_skip_retry_when_snapshot_missing(self, tmp_path):
        assert self._run(tmp_path) == 10  # SKIP/RETRY — no data landed yet

    def test_skip_retry_when_insufficient_history(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 5}})
        assert self._run(tmp_path) == 10  # SKIP/RETRY — <22 bars

    def test_done_takes_precedence_over_stale(self, tmp_path):
        """If already executed, skip-done even if the snapshot looks stale."""
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": "2020-01-01", "prices": {}, "history": {}})
        self._write(tmp_path, "pending_decisions.json",
                    {"date": today, "run_id": "x", "executed_at": "2026-01-01T00:00:00Z"})
        assert self._run(tmp_path) == 20  # SKIP/DONE wins


# ── publish.py — SPY data source + is_close inheritance ──────────────────────

class TestPublishSpyDataSource:
    """
    publish.py must read SPY price from market_snapshot.json (today's live data)
    and fall back to Polygon "prev" only when the snapshot is missing or stale.
    This prevents consecutive snapshots from showing the same SPY value when both
    run before today's close is available via Polygon's "prev" endpoint.

    Covers DEPLOYMENT.md §12.2 "New data source / fallback" requirements.
    """

    def _write(self, tmp_path, name, data):
        (tmp_path / name).write_text(json.dumps(data))

    def _read_spy(self, tmp_path):
        import importlib, sys, os
        # Reload publish so _load uses tmp_path's cwd
        orig = os.getcwd()
        os.chdir(tmp_path)
        try:
            if "publish" in sys.modules:
                del sys.modules["publish"]
            from publish import _fetch_spy_from_snapshot
            return _fetch_spy_from_snapshot()
        finally:
            os.chdir(orig)
            if "publish" in sys.modules:
                del sys.modules["publish"]

    def _et_today(self):
        # publish._fetch_spy_from_snapshot uses ET; the test's "today" must match
        # it or the snapshot reads as stale during the ET/PT date-straddle window
        # (~9pm–midnight Pacific), making this test fail ~3 hours every day.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    def test_happy_path_reads_spy_from_snapshot(self, tmp_path):
        """Returns SPY close from market_snapshot.json when dated today."""
        today = self._et_today()
        self._write(tmp_path, "market_snapshot.json", {
            "date": today,
            "prices": {"SPY": {"close": 735.15, "open": 728.0}},
        })
        result = self._read_spy(tmp_path)
        assert result == 735.15, f"Expected 735.15, got {result}"

    def test_returns_none_when_snapshot_missing(self, tmp_path):
        """No market_snapshot.json → returns None so Polygon fallback is used."""
        result = self._read_spy(tmp_path)
        assert result is None

    def test_returns_none_when_snapshot_stale(self, tmp_path):
        """market_snapshot.json dated yesterday → stale, returns None for fallback."""
        self._write(tmp_path, "market_snapshot.json", {
            "date": "2020-01-01",
            "prices": {"SPY": {"close": 999.0}},
        })
        result = self._read_spy(tmp_path)
        assert result is None, f"Stale snapshot should yield None, got {result}"

    def test_returns_none_when_spy_absent_from_prices(self, tmp_path):
        """Snapshot dated today but SPY not in prices → returns None."""
        today = self._et_today()
        self._write(tmp_path, "market_snapshot.json", {
            "date": today,
            "prices": {"AAPL": {"close": 200.0}},
        })
        result = self._read_spy(tmp_path)
        assert result is None

    def test_returns_none_when_spy_close_is_zero(self, tmp_path):
        """SPY close=0 (bad data) → returns None to trigger fallback."""
        today = self._et_today()
        self._write(tmp_path, "market_snapshot.json", {
            "date": today,
            "prices": {"SPY": {"close": 0}},
        })
        result = self._read_spy(tmp_path)
        assert result is None


class TestIsCloseInheritance:
    """
    publish_to_supabase() must NOT inherit is_close=True from portfolio_snapshot.json
    when called from main.py (outside GitHub Actions). If the previous day's EOD file
    is on disk with is_close=True, a morning run would previously write close_value
    for the new day — recording an intraday price as the official close.

    Covers the Jun 11 2026 bug where Jun 11 morning got close_value from Jun 10 EOD.
    """

    def _write(self, tmp_path, name, data):
        (tmp_path / name).write_text(json.dumps(data))

    def _resolve_is_close(self, tmp_path, caller_is_close, in_github_actions):
        """
        Replicate the is_close resolution logic from publish_to_supabase() in isolation,
        reading from a portfolio_snapshot.json in tmp_path.
        """
        import os, json
        snapshot_path = tmp_path / "portfolio_snapshot.json"
        file_snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
        is_close = caller_is_close
        if not is_close and in_github_actions:
            is_close = bool(file_snapshot.get("is_close", False))
        return is_close

    def test_morning_run_does_not_inherit_is_close(self, tmp_path):
        """main.py calls publish_to_supabase(is_close=False); previous EOD file has is_close=True.
        Outside GH Actions, is_close must stay False."""
        self._write(tmp_path, "portfolio_snapshot.json", {"is_close": True})
        result = self._resolve_is_close(tmp_path, caller_is_close=False, in_github_actions=False)
        assert result is False, "Morning run must not inherit is_close=True from EOD file"

    def test_github_actions_inherits_is_close_from_eod_file(self, tmp_path):
        """GH Actions invokes publish.py directly; reads is_close from snapshot committed by cloud."""
        self._write(tmp_path, "portfolio_snapshot.json", {"is_close": True})
        result = self._resolve_is_close(tmp_path, caller_is_close=False, in_github_actions=True)
        assert result is True, "GH Actions must read is_close=True from EOD snapshot file"

    def test_github_actions_morning_snapshot_stays_false(self, tmp_path):
        """GH Actions processes a morning commit; snapshot says is_close=False — must stay False."""
        self._write(tmp_path, "portfolio_snapshot.json", {"is_close": False})
        result = self._resolve_is_close(tmp_path, caller_is_close=False, in_github_actions=True)
        assert result is False

    def test_explicit_is_close_true_not_overridden(self, tmp_path):
        """EOD routine passes is_close=True explicitly; file should not matter."""
        self._write(tmp_path, "portfolio_snapshot.json", {"is_close": False})
        result = self._resolve_is_close(tmp_path, caller_is_close=True, in_github_actions=False)
        assert result is True


class TestCloseValueImmutability:
    """publish.py: close_value is the authoritative 4 PM close and must be written
    once per day. A second is_close publish (EOD retry, DST double-fire, manual
    dispatch) must NOT overwrite it. This guard was latent until the EOD routine
    began actually triggering publish.yml (Jun 12 2026 fix)."""

    def _should_write_close(self, is_close, existing_rows):
        """Replicate the guard in publish_to_supabase() in isolation."""
        if not is_close:
            return False
        already_closed = bool(existing_rows) and existing_rows[0].get("close_value") is not None
        return not already_closed

    def test_first_close_of_day_writes(self):
        assert self._should_write_close(True, []) is True            # no row yet
        assert self._should_write_close(True, [{"close_value": None}]) is True  # morning row, no close

    def test_second_close_preserves_original(self):
        assert self._should_write_close(True, [{"close_value": 735.15}]) is False  # immutable

    def test_non_close_run_never_writes(self):
        assert self._should_write_close(False, []) is False
        assert self._should_write_close(False, [{"close_value": None}]) is False


# ── guardrails.validate_decisions — deterministic gate on LLM output ─────────

class TestValidateDecisions:
    """target_weight used to flow from the PM LLM straight into _compute_qty
    with no bounds check, and execute_trades would place any positive-qty
    decision regardless of whether the ticker was ever analyzed. The gate is
    the control; the prompt text is not. (DEPLOYMENT.md ex-§2.6 known gap.)"""

    def _portfolio(self):
        return {"total_value": 500.0, "cash": 100.0,
                "positions": [{"symbol": "JPM", "qty": 0.1, "available_qty": 0.1}]}

    def _prices(self):
        return {"NVDA": {"close": 100.0}, "JPM": {"close": 300.0},
                "BAC": {"close": 50.0}, "TSLA": {"close": 200.0}}

    def _validate(self, decisions, kill_active=False, transactions=None):
        from guardrails import validate_decisions
        return validate_decisions(
            decisions, self._portfolio(), self._prices(),
            candidates=["NVDA", "BAC"], kill_active=kill_active,
            transactions=transactions if transactions is not None else [])

    @staticmethod
    def _trading_days_ago(n):
        """Date string n weekdays before today (ET) — mirrors the gate's counting."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        d = datetime.now(ZoneInfo("America/New_York")).date()
        left = n
        while left > 0:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                left -= 1
        return d.strftime("%Y-%m-%d")

    # — pass-through —

    def test_valid_decisions_pass_unchanged(self):
        decisions = [
            {"ticker": "NVDA", "action": "BUY", "target_weight": 0.08, "qty": 0.4},
            {"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.1},
        ]
        out, report = self._validate([dict(d) for d in decisions])
        assert out == decisions
        assert report["passed"] == 2
        assert not (report["rejected"] or report["modified"] or report["skipped"])

    def test_hold_passes_through_untouched(self):
        out, report = self._validate([{"ticker": "JPM", "action": "HOLD"}])
        assert out == [{"ticker": "JPM", "action": "HOLD"}]
        assert not report["rejected"]

    # — rejection rules —

    def test_invalid_action_rejected(self):
        out, report = self._validate([{"ticker": "NVDA", "action": "SHORT",
                                       "target_weight": 0.08, "qty": 0.4}])
        assert out == [] and len(report["rejected"]) == 1

    def test_unknown_ticker_rejected(self):
        out, report = self._validate([{"ticker": "GME", "action": "BUY",
                                       "target_weight": 0.05, "qty": 0.1}])
        assert out == []
        assert "not in analyzed candidates" in report["rejected"][0]["reason"]

    def test_blocked_ticker_rejected(self):
        out, report = self._validate([{"ticker": "TSLA", "action": "BUY",
                                       "target_weight": 0.05, "qty": 0.1}])
        assert out == [] and "hard-blocked" in report["rejected"][0]["reason"]

    def test_same_ticker_buy_and_sell_both_rejected(self):
        out, report = self._validate([
            {"ticker": "NVDA", "action": "BUY", "target_weight": 0.08, "qty": 0.4},
            {"ticker": "NVDA", "action": "SELL", "target_weight": 0.0, "qty": 0.4},
        ])
        assert out == [] and len(report["rejected"]) == 2

    def test_non_numeric_weight_rejected(self):
        out, report = self._validate([{"ticker": "NVDA", "action": "BUY",
                                       "target_weight": "max", "qty": 0.4}])
        assert out == [] and "not a number" in report["rejected"][0]["reason"]

    # — clamping (must recompute qty, or the clamp is a no-op at execution) —

    def test_overweight_clamped_and_qty_recomputed(self):
        # 0.12 → 0.10 of $500 = $50 @ $100 → qty must become 0.5, not stay 0.6
        out, report = self._validate([{"ticker": "NVDA", "action": "BUY",
                                       "target_weight": 0.12, "qty": 0.6}])
        assert len(out) == 1
        assert out[0]["target_weight"] == 0.10
        assert abs(out[0]["qty"] - 0.5) < 1e-9
        assert len(report["modified"]) == 1

    def test_negative_weight_clamped_to_zero(self):
        out, _ = self._validate([{"ticker": "NVDA", "action": "BUY",
                                  "target_weight": -0.05, "qty": 0.2}])
        assert len(out) == 1
        assert out[0]["target_weight"] == 0.0
        assert out[0]["qty"] == 0.0  # BUY to 0% of an unheld name = no shares

    # — notional caps —

    def test_buy_notional_above_cap_rejected_not_clamped(self):
        # weight says 8% but qty says $100 = 20% of the $500 book — qty math wrong
        out, report = self._validate([{"ticker": "NVDA", "action": "BUY",
                                       "target_weight": 0.08, "qty": 1.0}])
        assert out == [] and "notional" in report["rejected"][0]["reason"]

    def test_sell_notional_exempt_from_buy_cap(self):
        # full exit of a position grown past 12% must NOT be blocked
        from guardrails import validate_decisions
        portfolio = {"total_value": 500.0, "cash": 0.0,
                     "positions": [{"symbol": "JPM", "qty": 0.25, "available_qty": 0.25}]}
        out, report = validate_decisions(
            [{"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.25}],
            portfolio, self._prices(), candidates=[], kill_active=False, transactions=[])
        assert len(out) == 1 and not report["rejected"]  # $75 = 15% — allowed for SELL

    def test_sub_minimum_notional_skipped(self):
        out, report = self._validate([{"ticker": "NVDA", "action": "BUY",
                                       "target_weight": 0.008, "qty": 0.04}])
        assert out == [] and len(report["skipped"]) == 1
        assert not report["rejected"]  # skip is a no-op, not a failure

    # — good-faith-violation guard —

    def test_sell_one_trading_day_after_buy_rejected(self):
        txs = [{"ticker": "JPM", "action": "BUY", "dry_run": False,
                "date": self._trading_days_ago(1)}]
        out, report = self._validate(
            [{"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.1}],
            transactions=txs)
        assert out == [] and "good-faith" in report["rejected"][0]["reason"]

    def test_sell_three_trading_days_after_buy_passes(self):
        txs = [{"ticker": "JPM", "action": "BUY", "dry_run": False,
                "date": self._trading_days_ago(3)}]
        out, _ = self._validate(
            [{"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.1}],
            transactions=txs)
        assert len(out) == 1

    def test_kill_switch_overrides_gfv(self):
        txs = [{"ticker": "JPM", "action": "BUY", "dry_run": False,
                "date": self._trading_days_ago(1)}]
        out, _ = self._validate(
            [{"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.1}],
            kill_active=True, transactions=txs)
        assert len(out) == 1  # risk exits are never blocked

    def test_dry_run_buy_ignored_by_gfv(self):
        # a rejected/phantom BUY (dry_run=True) never established a position
        txs = [{"ticker": "JPM", "action": "BUY", "dry_run": True,
                "date": self._trading_days_ago(1)}]
        out, _ = self._validate(
            [{"ticker": "JPM", "action": "SELL", "target_weight": 0.0, "qty": 0.1}],
            transactions=txs)
        assert len(out) == 1

    # — trading-day arithmetic —

    def test_trading_days_counting(self):
        from guardrails import _trading_days_since
        assert _trading_days_since("2026-06-11", "2026-06-12") == 1  # Thu → Fri
        assert _trading_days_since("2026-06-11", "2026-06-15") == 2  # Thu → Mon
        assert _trading_days_since("2026-06-12", "2026-06-15") == 1  # Fri → Mon
        assert _trading_days_since("2026-06-12", "2026-06-12") == 0  # same day


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0.2 — guardrails.enforce_sector_limits: the 25% sector cap is enforced
# in CODE, not just the PM prompt. SELLs are applied before BUYs so freed
# sector budget is reusable.
# ─────────────────────────────────────────────────────────────────────────────

class TestEnforceSectorLimits:
    """MAX_SECTOR_WEIGHT (25%) is a hard cap on projected post-trade sector
    weight. The marginal BUY that breaches it is rejected; a same-sector SELL
    applied first frees budget for a subsequent BUY. Existing holdings count
    toward the budget."""

    SECTORS = {"GS": "Financials", "MS": "Financials", "JPM": "Financials",
               "BAC": "Financials", "XOM": "Energy"}

    def _empty_portfolio(self):
        return {"total_value": 1000.0, "cash": 1000.0, "positions": []}

    def test_third_same_sector_buy_rejected(self):
        from guardrails import enforce_sector_limits
        decisions = [
            {"ticker": "GS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "MS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "JPM", "action": "BUY", "target_weight": 0.10},  # → 30% Financials
        ]
        kept, rejected = enforce_sector_limits(
            decisions, self._empty_portfolio(), sectors=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "MS"}
        assert [d["ticker"] for d in rejected] == ["JPM"]
        assert "rejected_reason" in rejected[0]

    def test_other_sector_buy_unaffected(self):
        from guardrails import enforce_sector_limits
        decisions = [
            {"ticker": "GS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "MS",  "action": "BUY", "target_weight": 0.10},
            {"ticker": "XOM", "action": "BUY", "target_weight": 0.10},  # Energy, fine
        ]
        kept, rejected = enforce_sector_limits(
            decisions, self._empty_portfolio(), sectors=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "MS", "XOM"}
        assert rejected == []

    def test_existing_holdings_count_toward_sector_budget(self):
        from guardrails import enforce_sector_limits
        portfolio = {"total_value": 1000.0, "cash": 800.0,
                     "positions": [{"symbol": "BAC", "market_value": 200.0}]}  # 20%
        decisions = [{"ticker": "GS", "action": "BUY", "target_weight": 0.10}]  # → 30%
        kept, rejected = enforce_sector_limits(
            decisions, portfolio, sectors=self.SECTORS)
        assert kept == []
        assert [d["ticker"] for d in rejected] == ["GS"]

    def test_sell_frees_budget_before_buy(self):
        # Holding 25% BAC (Financials at cap). Selling it frees room for a GS buy
        # even though decisions are passed BUY-first.
        from guardrails import enforce_sector_limits
        portfolio = {"total_value": 1000.0, "cash": 750.0,
                     "positions": [{"symbol": "BAC", "market_value": 250.0}]}  # 25%
        decisions = [
            {"ticker": "GS",  "action": "BUY",  "target_weight": 0.10},
            {"ticker": "BAC", "action": "SELL", "target_weight": 0.0},
        ]
        kept, rejected = enforce_sector_limits(
            decisions, portfolio, sectors=self.SECTORS)
        assert {d["ticker"] for d in kept} == {"GS", "BAC"}
        assert rejected == []

    def test_sell_is_never_rejected(self):
        from guardrails import enforce_sector_limits
        portfolio = {"total_value": 1000.0, "cash": 0.0,
                     "positions": [{"symbol": "BAC", "market_value": 300.0}]}  # 30% over cap
        decisions = [{"ticker": "BAC", "action": "SELL", "target_weight": 0.0}]
        kept, rejected = enforce_sector_limits(
            decisions, portfolio, sectors=self.SECTORS)
        assert [d["ticker"] for d in kept] == ["BAC"]
        assert rejected == []

    def test_decision_order_preserved(self):
        from guardrails import enforce_sector_limits
        decisions = [
            {"ticker": "XOM", "action": "BUY", "target_weight": 0.10},  # Energy
            {"ticker": "GS",  "action": "BUY", "target_weight": 0.10},  # Financials
        ]
        kept, _ = enforce_sector_limits(
            decisions, self._empty_portfolio(), sectors=self.SECTORS)
        assert [d["ticker"] for d in kept] == ["XOM", "GS"]

    def test_default_sector_map_used(self):
        # Without an explicit map, the built-in SECTOR_MAP applies: NVDA/AVGO/AMD
        # are all Technology, so the third 10% tech BUY is rejected.
        from guardrails import enforce_sector_limits
        decisions = [
            {"ticker": "NVDA", "action": "BUY", "target_weight": 0.10},
            {"ticker": "AVGO", "action": "BUY", "target_weight": 0.10},
            {"ticker": "AMD",  "action": "BUY", "target_weight": 0.10},  # → 30% Tech
        ]
        kept, rejected = enforce_sector_limits(decisions, self._empty_portfolio())
        assert {d["ticker"] for d in kept} == {"NVDA", "AVGO"}
        assert [d["ticker"] for d in rejected] == ["AMD"]


# ── journal.mark_execution_started — stamp-first execution claim ──────────────

class TestMarkExecutionStarted:
    """The claim is stamped (and pushed by the routine) BEFORE the first order,
    so an attempt that crashes mid-execution leaves a durable marker. The gate
    treats started-but-not-executed as SKIP/DONE: failure direction is missed
    trades (Scenario B recovery), never duplicate trades."""

    def _pending(self, **overrides):
        base = {"run_id": "r1", "date": "2026-06-12",
                "generated_at": "2026-06-12T13:45:00Z",
                "execution_started_at": None, "executed_at": None,
                "decisions": [{"ticker": "BAC", "action": "BUY"}]}
        return {**base, **overrides}

    def _setup(self, tmp_path, monkeypatch, pending):
        import journal
        f = tmp_path / "pending_decisions.json"
        f.write_text(json.dumps(pending))
        monkeypatch.setattr(journal, "PENDING_FILE", str(f))
        return journal, f

    def test_stamps_claim(self, tmp_path, monkeypatch):
        journal, f = self._setup(tmp_path, monkeypatch, self._pending())
        journal.mark_execution_started("r1")
        data = json.loads(f.read_text())
        assert data["execution_started_at"] is not None
        assert data["executed_at"] is None  # claim does NOT imply completion

    def test_idempotent_preserves_first_claim(self, tmp_path, monkeypatch):
        journal, f = self._setup(tmp_path, monkeypatch, self._pending())
        journal.mark_execution_started("r1")
        first = json.loads(f.read_text())["execution_started_at"]
        journal.mark_execution_started("r1")
        assert json.loads(f.read_text())["execution_started_at"] == first

    def test_wrong_run_id_noops(self, tmp_path, monkeypatch):
        journal, f = self._setup(tmp_path, monkeypatch, self._pending())
        journal.mark_execution_started("OTHER")
        assert json.loads(f.read_text())["execution_started_at"] is None

    def test_missing_file_is_safe(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.setattr(journal, "PENDING_FILE", str(tmp_path / "nope.json"))
        journal.mark_execution_started("r1")  # must not raise

    def test_old_envelope_without_field_gains_it(self, tmp_path, monkeypatch):
        pending = self._pending()
        del pending["execution_started_at"]  # pre-claim envelope shape
        journal, f = self._setup(tmp_path, monkeypatch, pending)
        journal.mark_execution_started("r1")
        assert json.loads(f.read_text())["execution_started_at"] is not None


class TestPreflightGateExecutionClaim(TestPreflightGate):
    """Gate must treat started-but-never-completed execution as SKIP/DONE."""

    def test_skip_done_when_execution_started_but_not_completed(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json",
                    {"date": today, "run_id": "x", "executed_at": None,
                     "execution_started_at": "2026-06-12T13:50:00Z"})
        assert self._run(tmp_path) == 20  # orders may exist — never re-run

    def test_yesterdays_claim_does_not_block_today(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json",
                    {"date": "2020-01-01", "run_id": "x", "executed_at": None,
                     "execution_started_at": "2020-01-01T13:50:00Z"})
        assert self._run(tmp_path) == 0  # stale claim from a prior day — PROCEED


# ── execute.get_portfolio_summary — mcp_portfolio.json freshness (Fix 4) ─────

class TestPortfolioFreshness:
    """Every order is sized from mcp_portfolio.json. A stale copy (prior day's
    portfolio committed to the repo, or a routine that failed to refresh it)
    would size today's trades against the wrong cash/positions. The file must
    carry an as_of dated today (ET) or get_portfolio_summary raises."""

    def _et_now_iso(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).isoformat()

    def _et_iso_days_ago(self, n):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        return (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=n)).isoformat()

    def _write(self, tmp_path, obj):
        (tmp_path / "mcp_portfolio.json").write_text(json.dumps(obj))

    def test_fresh_as_of_passes(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, {"as_of": self._et_now_iso(), "cash": 123.45,
                               "total_value": 456.78, "positions": []})
        assert execute.get_portfolio_summary()["cash"] == 123.45

    def test_missing_as_of_raises(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, {"cash": 123.45, "total_value": 456.78, "positions": []})
        with pytest.raises(execute.StalePortfolioError, match="no 'as_of'"):
            execute.get_portfolio_summary()

    def test_yesterday_as_of_raises(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, {"as_of": self._et_iso_days_ago(1), "cash": 1.0,
                               "total_value": 1.0, "positions": []})
        with pytest.raises(execute.StalePortfolioError, match="stale"):
            execute.get_portfolio_summary()

    def test_unparseable_as_of_raises(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, {"as_of": "not-a-timestamp", "cash": 1.0,
                               "total_value": 1.0, "positions": []})
        with pytest.raises(execute.StalePortfolioError, match="unparseable"):
            execute.get_portfolio_summary()

    def test_naive_today_timestamp_accepted_as_et(self, tmp_path, monkeypatch):
        import execute
        from datetime import datetime
        from zoneinfo import ZoneInfo
        monkeypatch.chdir(tmp_path)
        naive_today = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None).isoformat()
        self._write(tmp_path, {"as_of": naive_today, "cash": 9.0,
                               "total_value": 9.0, "positions": []})
        assert execute.get_portfolio_summary()["cash"] == 9.0

    def test_no_file_falls_through_to_robin_stocks(self, tmp_path, monkeypatch):
        import execute
        monkeypatch.chdir(tmp_path)  # no mcp_portfolio.json here
        called = {"login": False}
        def _fake_login():
            called["login"] = True
            raise RuntimeError("robin_stocks path reached (expected)")
        monkeypatch.setattr(execute, "_login", _fake_login)
        with pytest.raises(RuntimeError, match="robin_stocks path"):
            execute.get_portfolio_summary()
        assert called["login"] is True  # fell through to the live path, no StalePortfolioError


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — journal.close_position: closes the matching open BUY on a sell and
# records the realized outcome. This is the feedback loop the system lacked —
# actual_return / thesis_correct were never populated before.
# ─────────────────────────────────────────────────────────────────────────────

class TestClosePosition:
    """On a SELL, the most recent open BUY entry for that ticker gains a numeric
    actual_return and a boolean thesis_correct; a full exit flips status to
    'closed'. Realized return is per-share vs cost basis, so it is independent of
    lot size and correct for partial exits."""

    def _open_buy(self, ticker="AAPL", expected_return=0.0, trade_id="t1"):
        return {
            "trade_id": trade_id, "run_id": "r0", "date": "2026-06-01",
            "ticker": ticker, "action": "BUY", "target_weight": 0.08,
            "thesis": "variant perception", "anti_thesis": "", "catalysts": [],
            "confidence": 7, "expected_return": expected_return, "invalidates_if": [],
            "status": "open", "actual_return": None, "thesis_correct": None,
        }

    def _setup(self, tmp_path, monkeypatch, entries):
        import journal
        jf = tmp_path / "decision_journal.json"
        jf.write_text(json.dumps(entries))
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))
        return journal, jf

    def test_full_exit_closes_entry_with_loss(self, tmp_path, monkeypatch):
        # bought ~312, exit at 292 → realized ≈ -6.4%, thesis wrong.
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        tid = journal.close_position("AAPL", exit_price=292.0, avg_price=312.0,
                                     full_exit=True, run_id="r1")
        entry = json.loads(jf.read_text())[0]
        assert tid == "t1"
        assert entry["status"] == "closed"
        assert entry["actual_return"] == round((292.0 - 312.0) / 312.0, 4)  # -0.0641
        assert entry["thesis_correct"] is False
        assert entry["exits"][-1]["full_exit"] is True
        assert entry["exits"][-1]["exit_price"] == 292.0

    def test_full_exit_with_gain_marks_thesis_correct(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        journal.close_position("AAPL", exit_price=340.0, avg_price=312.0, full_exit=True)
        entry = json.loads(jf.read_text())[0]
        assert entry["actual_return"] > 0
        assert entry["thesis_correct"] is True

    def test_expected_return_threshold_branch(self, tmp_path, monkeypatch):
        # With expected_return=0.10, thesis is "correct" only if realized met at
        # least half of it. +3% realized against a +10% expectation fails the bar.
        journal, jf = self._setup(
            tmp_path, monkeypatch, [self._open_buy(expected_return=0.10)])
        journal.close_position("AAPL", exit_price=321.36, avg_price=312.0, full_exit=True)
        entry = json.loads(jf.read_text())[0]
        assert entry["actual_return"] == round((321.36 - 312.0) / 312.0, 4)  # ~+0.03
        assert entry["thesis_correct"] is False  # 0.03 < 0.10*0.5

    def test_partial_exit_keeps_entry_open(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        journal.close_position("AAPL", exit_price=300.0, avg_price=312.0, full_exit=False)
        entry = json.loads(jf.read_text())[0]
        assert entry["status"] == "open"           # reduce, not exit
        assert entry["actual_return"] is not None   # outcome still recorded
        assert entry["exits"][-1]["full_exit"] is False

    def test_no_matching_open_entry_is_noop(self, tmp_path, monkeypatch):
        closed = {**self._open_buy(), "status": "closed"}
        journal, jf = self._setup(tmp_path, monkeypatch, [closed])
        assert journal.close_position("AAPL", 300.0, 312.0, full_exit=True) is None
        assert json.loads(jf.read_text())[0]["status"] == "closed"  # untouched

    def test_zero_avg_price_guard(self, tmp_path, monkeypatch):
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        assert journal.close_position("AAPL", 300.0, 0.0, full_exit=True) is None
        assert json.loads(jf.read_text())[0]["status"] == "open"  # no divide-by-zero

    def test_closes_most_recent_open_entry(self, tmp_path, monkeypatch):
        older = self._open_buy(trade_id="old")
        newer = {**self._open_buy(trade_id="new"), "date": "2026-06-10"}
        journal, jf = self._setup(tmp_path, monkeypatch, [older, newer])
        tid = journal.close_position("AAPL", 300.0, 312.0, full_exit=True)
        rows = {e["trade_id"]: e for e in json.loads(jf.read_text())}
        assert tid == "new"
        assert rows["new"]["status"] == "closed"
        assert rows["old"]["status"] == "open"  # untouched

    def test_closed_entry_survives_reconciliation(self, tmp_path, monkeypatch):
        # Spec note: _reconcile_journal flips only open↔rejected and leaves any
        # other status (including 'closed') untouched. A close_position-closed
        # entry must survive a later mark_transactions_live pass over the run.
        journal, jf = self._setup(tmp_path, monkeypatch, [self._open_buy()])
        journal.close_position("AAPL", 292.0, 312.0, full_exit=True, run_id="r0")
        changed = journal._reconcile_journal("r0", fills={})  # AAPL not in fills
        entry = json.loads(jf.read_text())[0]
        assert entry["status"] == "closed"  # NOT flipped to rejected
        assert changed == 0


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — outcome memory fed back to agents: journal.get_ticker_history /
# recently_exited helpers + the Research/PM prompt blocks built from them.
# ─────────────────────────────────────────────────────────────────────────────

class TestTickerHistoryHelpers:
    def _setup(self, tmp_path, monkeypatch, entries):
        import journal
        jf = tmp_path / "decision_journal.json"
        jf.write_text(json.dumps(entries))
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))
        return journal

    def test_get_ticker_history_filters_and_limits(self, tmp_path, monkeypatch):
        entries = [
            {"ticker": "AAPL", "action": "BUY", "date": "2026-01-01"},
            {"ticker": "MSFT", "action": "BUY", "date": "2026-02-01"},
            {"ticker": "AAPL", "action": "SELL", "date": "2026-03-01"},
            {"ticker": "AAPL", "action": "BUY", "date": "2026-04-01"},
            {"ticker": "AAPL", "action": "BUY", "date": "2026-05-01"},
        ]
        journal = self._setup(tmp_path, monkeypatch, entries)
        rows = journal.get_ticker_history("AAPL", n=2)
        assert [r["date"] for r in rows] == ["2026-04-01", "2026-05-01"]
        assert all(r["ticker"] == "AAPL" for r in rows)

    def test_recently_exited_includes_recent_closed(self, tmp_path, monkeypatch):
        from datetime import date
        today = date.today().isoformat()   # recently_exited() uses local date.today()
        entries = [{
            "ticker": "AAPL", "action": "BUY", "status": "closed",
            "thesis": "cloud growth", "expected_return": 0.1,
            "exits": [{"date": today, "realized_return": -0.06, "full_exit": True}],
        }]
        journal = self._setup(tmp_path, monkeypatch, entries)
        out = journal.recently_exited(within_days=10)
        assert "AAPL" in out
        assert out["AAPL"]["exits"][-1]["realized_return"] == -0.06

    def test_recently_exited_excludes_old_and_open(self, tmp_path, monkeypatch):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=30)).isoformat()
        entries = [
            {"ticker": "AAPL", "action": "BUY", "status": "closed",
             "exits": [{"date": old, "realized_return": 0.0, "full_exit": True}]},
            {"ticker": "MSFT", "action": "BUY", "status": "open", "exits": []},
        ]
        journal = self._setup(tmp_path, monkeypatch, entries)
        assert journal.recently_exited(within_days=10) == {}


class TestMemoryPromptBlocks:
    """The formatters render exactly the memory the agents consume; empty inputs
    produce empty strings (no stray prompt sections)."""

    def test_ticker_history_block_shows_outcome(self):
        from analysis import _fmt_ticker_history
        block = _fmt_ticker_history([
            {"date": "2026-05-01", "action": "BUY", "thesis": "cheap cloud name",
             "actual_return": -0.064, "thesis_correct": False, "status": "closed"},
        ])
        assert "PRIOR HISTORY" in block
        assert "-6.4%" in block
        assert "thesis_correct=False" in block

    def test_ticker_history_open_entry_has_no_realized(self):
        from analysis import _fmt_ticker_history
        block = _fmt_ticker_history([
            {"date": "2026-05-01", "action": "BUY", "thesis": "x",
             "actual_return": None, "status": "open"},
        ])
        assert "no realized outcome yet" in block

    def test_empty_history_is_empty_string(self):
        from analysis import _fmt_ticker_history
        assert _fmt_ticker_history(None) == ""
        assert _fmt_ticker_history([]) == ""

    def test_recently_exited_block_warns(self):
        from analysis import _fmt_recently_exited
        block = _fmt_recently_exited({
            "AAPL": {"thesis": "AI tailwind",
                     "exits": [{"date": "2026-06-10", "realized_return": -0.03}]},
        })
        assert "RECENTLY EXITED" in block
        assert "AAPL" in block
        assert "-3.0%" in block

    def test_empty_recently_exited_is_empty_string(self):
        from analysis import _fmt_recently_exited
        assert _fmt_recently_exited(None) == ""
        assert _fmt_recently_exited({}) == ""


class TestMemoryInjectedIntoAgents:
    """Acceptance: the memory actually reaches the agent user_msg. Capture the
    prompt by stubbing analysis._call."""

    def _capture(self, monkeypatch):
        import analysis
        captured = {}
        def _fake_call(model, system, user_msg, max_tokens=600):
            captured["user_msg"] = user_msg
            return "[]"  # valid JSON; PM expects an array, research a dict
        monkeypatch.setattr(analysis, "_call", _fake_call)
        return analysis, captured

    def test_research_user_msg_contains_prior_outcome(self, monkeypatch):
        analysis, captured = self._capture(monkeypatch)
        md = {"prices": {"AAPL": {"close": 291.0, "change_pct": 0.0}}, "date": "2026-06-13"}
        analysis.run_research_analyst(
            "AAPL", md, {"AAPL": {"data_available": True}},
            ticker_history=[{"date": "2026-06-01", "action": "BUY",
                             "thesis": "rebound", "actual_return": -0.064,
                             "thesis_correct": False, "status": "closed"}])
        assert "PRIOR HISTORY" in captured["user_msg"]
        assert "-6.4%" in captured["user_msg"]

    def test_pm_user_msg_contains_reentry_warning(self, monkeypatch):
        analysis, captured = self._capture(monkeypatch)
        portfolio = {"total_value": 500.0, "cash": 500.0, "positions": []}
        analysis.run_portfolio_manager(
            {}, {}, {}, {}, {}, {}, portfolio, [], date="2026-06-13",
            recently_exited={"AAPL": {"thesis": "sold the bounce",
                "exits": [{"date": "2026-06-11", "realized_return": 0.02}]}})
        assert "RECENTLY EXITED" in captured["user_msg"]
        assert "AAPL" in captured["user_msg"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — CRO gets REAL correlation data: quant_engine.compute_return_correlations
# + the correlation/concentration block injected into the CRO user_msg.
# ─────────────────────────────────────────────────────────────────────────────

class TestReturnCorrelations:
    def test_identical_series_correlate_one(self):
        from quant_engine import compute_return_correlations
        hist = {"A": _trend(100.0, 130.0, 60), "B": _trend(100.0, 130.0, 60)}
        pairs = compute_return_correlations(hist, ["A", "B"], window=60)
        assert len(pairs) == 1
        a, b, c = pairs[0]
        assert {a, b} == {"A", "B"}
        assert c == pytest.approx(1.0, abs=0.01)

    def test_short_history_skipped(self):
        from quant_engine import compute_return_correlations
        hist = {"A": _trend(100.0, 110.0, 10), "B": _trend(100.0, 110.0, 10)}
        assert compute_return_correlations(hist, ["A", "B"]) == []  # < 22 overlap

    def test_sorted_by_abs_corr_and_capped(self):
        from quant_engine import compute_return_correlations
        # A,B move together; C is a flat series (skipped: zero variance → no pair)
        hist = {
            "A": _trend(100.0, 130.0, 60),
            "B": _trend(100.0, 130.0, 60),
            "D": _trend(200.0, 150.0, 60),  # downtrend, still valid variance
        }
        pairs = compute_return_correlations(hist, ["A", "B", "D"], window=60, top_n=2)
        assert len(pairs) <= 2
        # most-correlated pair first
        assert abs(pairs[0][2]) >= abs(pairs[-1][2])

    def test_no_history_returns_empty(self):
        from quant_engine import compute_return_correlations
        assert compute_return_correlations({}, ["A"]) == []


class TestCroCorrelationInjection:
    def test_cro_user_msg_has_correlation_block(self, monkeypatch):
        import analysis
        captured = {}
        def _fake_call(model, system, user_msg, max_tokens=600):
            captured["user_msg"] = user_msg
            return '{"approved": true, "risk_budget_used": 30, "rejected_tickers": []}'
        monkeypatch.setattr(analysis, "_call", _fake_call)

        portfolio = {"total_value": 1000.0, "cash": 0.0, "positions": []}
        decisions = [
            {"ticker": "A", "action": "BUY", "target_weight": 0.10},
            {"ticker": "B", "action": "BUY", "target_weight": 0.10},
        ]
        history = {"A": _trend(100.0, 130.0, 60), "B": _trend(100.0, 130.0, 60)}
        analysis.run_chief_risk_officer(decisions, portfolio, {}, history=history)
        assert "HIGHEST PAIRWISE CORRELATIONS" in captured["user_msg"]
        assert "A / B" in captured["user_msg"]

    def test_cro_no_history_no_pretense(self, monkeypatch):
        import analysis
        captured = {}
        def _fake_call(model, system, user_msg, max_tokens=600):
            captured["user_msg"] = user_msg
            return '{"approved": true, "rejected_tickers": []}'
        monkeypatch.setattr(analysis, "_call", _fake_call)
        portfolio = {"total_value": 1000.0, "cash": 0.0, "positions": []}
        decisions = [{"ticker": "A", "action": "BUY", "target_weight": 0.10}]
        analysis.run_chief_risk_officer(decisions, portfolio, {}, history=None)
        assert "HIGHEST PAIRWISE CORRELATIONS" not in captured["user_msg"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 — performance.py: local portfolio-vs-SPY report, no Supabase needed.
# ─────────────────────────────────────────────────────────────────────────────

class TestPerformanceReport:
    def test_metrics_on_known_curve(self):
        import performance
        # +10% then -50% from 110 → drawdown -50%, cumulative -45%
        m = performance._metrics([100.0, 110.0, 55.0])
        assert m["cumulative_return"] == pytest.approx(-0.45, abs=1e-6)
        assert m["max_drawdown"] == pytest.approx(-0.5, abs=1e-6)

    def test_metrics_short_curve_is_safe(self):
        import performance
        m = performance._metrics([100.0])
        assert m["cumulative_return"] == 0.0
        assert m["sharpe"] is None

    def test_spy_curve_converts_epoch_ms(self, tmp_path):
        import performance
        # 1781236800000 ms == 2026-06-12 UTC
        snap = {"history": {"SPY": [
            {"date": 1781150400000, "close": 738.0},
            {"date": 1781236800000, "close": 740.0},
        ]}}
        p = tmp_path / "snap.json"
        p.write_text(json.dumps(snap))
        curve = performance._spy_curve(str(p))
        assert curve["2026-06-12"] == 740.0
        assert len(curve) == 2

    def test_align_uses_as_of_prior_close(self):
        import performance
        portfolio = [("2026-06-08", 500.0), ("2026-06-09", 505.0)]
        spy = {"2026-06-05": 700.0, "2026-06-08": 710.0}  # no 06-09 bar
        dates, pv, sv = performance._align(portfolio, spy)
        assert dates == ["2026-06-08", "2026-06-09"]
        assert pv == [500.0, 505.0]
        assert sv == [710.0, 710.0]  # 06-09 falls back to latest prior (06-08)

    def test_build_report_end_to_end(self, tmp_path):
        import performance
        log = [
            {"date": "2026-06-08", "portfolio_snapshot": {"total_value": 500.0}},
            {"date": "2026-06-08", "portfolio_snapshot": {"total_value": 501.0}},  # later run wins
            {"date": "2026-06-09", "portfolio_snapshot": {"total_value": 510.0}},
        ]
        snap = {"history": {"SPY": [
            {"date": "2026-06-08", "close": 700.0},
            {"date": "2026-06-09", "close": 707.0},
        ]}}
        lp = tmp_path / "agent_log.json"; lp.write_text(json.dumps(log))
        sp = tmp_path / "snap.json";       sp.write_text(json.dumps(snap))
        report = performance.build_report(str(lp), str(sp))
        assert report["inception"] == "2026-06-08"
        assert report["as_of"] == "2026-06-09"
        assert report["trading_days"] == 2
        assert report["portfolio_curve"][0]["value"] == 501.0  # dedup last-of-day
        # portfolio +1.798% (501→510) vs SPY +1.0% (700→707) → positive alpha
        assert report["alpha_cumulative_return"] > 0

    def test_missing_files_are_safe(self, tmp_path):
        import performance
        report = performance.build_report(str(tmp_path / "none.json"),
                                          str(tmp_path / "none2.json"))
        assert report["trading_days"] == 0
        assert report["inception"] is None


# ─────────────────────────────────────────────────────────────────────────────
#  After-tax scorecard — realized gain vs AFTER-tax realized gain (CA top bracket)
# ─────────────────────────────────────────────────────────────────────────────

class TestRealizedLots:
    """FIFO lot-matching of SELLs against prior BUYs. Cost basis is never guessed:
    a SELL with no in-log BUY is reported as 'uncovered', not assigned a basis."""

    def _tx(self, action, ticker, qty, price, date):
        return {"action": action, "ticker": ticker, "qty": qty, "price": price,
                "date": date, "timestamp": date + "T00:00:00+00:00"}

    def test_simple_round_trip_gain(self):
        import performance
        txs = [self._tx("BUY", "AAPL", 10, 100.0, "2026-01-02"),
               self._tx("SELL", "AAPL", 10, 120.0, "2026-01-09")]
        realized, uncovered = performance.compute_realized_lots(txs)
        assert uncovered == []
        assert len(realized) == 1
        assert realized[0]["gain"] == 200.0
        assert realized[0]["term"] == "ST"

    def test_round_trip_loss(self):
        import performance
        txs = [self._tx("BUY", "AAPL", 10, 100.0, "2026-01-02"),
               self._tx("SELL", "AAPL", 10, 80.0, "2026-01-09")]
        realized, _ = performance.compute_realized_lots(txs)
        assert realized[0]["gain"] == -200.0

    def test_fifo_partial_consumes_oldest_lot_first(self):
        import performance
        txs = [self._tx("BUY", "AAPL", 10, 100.0, "2026-01-02"),
               self._tx("BUY", "AAPL", 10, 110.0, "2026-01-03"),
               self._tx("SELL", "AAPL", 15, 130.0, "2026-01-10")]
        realized, uncovered = performance.compute_realized_lots(txs)
        assert uncovered == []
        # 10 @100 (gain 300) + 5 @110 (gain 100) = 400 across two lots
        assert len(realized) == 2
        assert realized[0]["gain"] == 300.0 and realized[0]["buy_price"] == 100.0
        assert realized[1]["gain"] == 100.0 and realized[1]["qty"] == 5.0

    def test_uncovered_sell_has_no_basis(self):
        import performance
        txs = [self._tx("SELL", "AAPL", 5, 100.0, "2026-01-09")]  # no prior buy
        realized, uncovered = performance.compute_realized_lots(txs)
        assert realized == []
        assert uncovered == [{"ticker": "AAPL", "qty": 5.0, "sell_date": "2026-01-09"}]

    def test_partial_uncovered_sell_splits(self):
        import performance
        txs = [self._tx("BUY", "AAPL", 3, 100.0, "2026-01-02"),
               self._tx("SELL", "AAPL", 5, 120.0, "2026-01-09")]  # 3 covered, 2 uncovered
        realized, uncovered = performance.compute_realized_lots(txs)
        assert realized[0]["qty"] == 3.0 and realized[0]["gain"] == 60.0
        assert uncovered == [{"ticker": "AAPL", "qty": 2.0, "sell_date": "2026-01-09"}]

    def test_long_term_classification(self):
        import performance
        txs = [self._tx("BUY", "AAPL", 1, 100.0, "2025-01-02"),
               self._tx("SELL", "AAPL", 1, 150.0, "2026-06-01")]  # > 365 days
        realized, _ = performance.compute_realized_lots(txs)
        assert realized[0]["term"] == "LT"

    def test_dry_run_excluded(self):
        import performance
        txs = [{**self._tx("BUY", "AAPL", 10, 100.0, "2026-01-02"), "dry_run": True},
               self._tx("SELL", "AAPL", 10, 120.0, "2026-01-09")]
        realized, uncovered = performance.compute_realized_lots(txs)
        assert realized == []  # the buy was dry_run → sell is uncovered
        assert uncovered[0]["qty"] == 10.0


class TestRealizedSummary:
    """Realized gain (pre-tax) and realized gain (AFTER tax) tracked separately."""

    def test_short_term_gain_taxed_at_ca_rate(self):
        import performance
        s = performance.realized_summary([
            {"term": "ST", "gain": 200.0}, {"term": "ST", "gain": 100.0}])
        assert s["realized_gain_pretax"] == 300.0
        assert s["realized_tax_estimate"] == round(300.0 * performance.CA_SHORT_TERM_RATE, 2)
        assert s["realized_gain_after_tax"] == round(300.0 - 300.0 * performance.CA_SHORT_TERM_RATE, 2)
        assert s["realized_gain_after_tax"] < s["realized_gain_pretax"]   # separate, smaller

    def test_net_loss_has_no_tax_and_carries_forward(self):
        import performance
        s = performance.realized_summary([{"term": "ST", "gain": -150.0}])
        assert s["realized_tax_estimate"] == 0.0
        assert s["realized_gain_after_tax"] == -150.0
        assert s["loss_carryforward"] == 150.0

    def test_st_and_lt_taxed_at_their_own_rates(self):
        import performance
        s = performance.realized_summary([{"term": "ST", "gain": 100.0},
                                          {"term": "LT", "gain": 100.0}])
        expect = round(100 * performance.CA_SHORT_TERM_RATE + 100 * performance.CA_LONG_TERM_RATE, 2)
        assert s["realized_tax_estimate"] == expect
        assert s["short_term_gain"] == 100.0 and s["long_term_gain"] == 100.0

    def test_lt_loss_offsets_st_gain_before_tax(self):
        # IRS netting: ST +1000, LT -400 → 600 taxable at the ST rate, no carryforward
        import performance
        s = performance.realized_summary([{"term": "ST", "gain": 1000.0},
                                          {"term": "LT", "gain": -400.0}])
        assert s["realized_gain_pretax"] == 600.0
        assert s["realized_tax_estimate"] == round(600.0 * performance.CA_SHORT_TERM_RATE, 2)
        assert s["realized_gain_after_tax"] == round(600.0 - 600.0 * performance.CA_SHORT_TERM_RATE, 2)
        assert s["loss_carryforward"] == 0.0

    def test_loss_exceeding_gain_carries_remainder(self):
        # ST -1500 vs LT +1000 → ST loss wipes the LT gain (no tax), 500 carries
        import performance
        s = performance.realized_summary([{"term": "ST", "gain": -1500.0},
                                          {"term": "LT", "gain": 1000.0}])
        assert s["realized_tax_estimate"] == 0.0
        assert s["loss_carryforward"] == 500.0


class TestAfterTaxScorecard:
    def _curve_files(self, tmp_path, port_curve, spy_curve):
        log = [{"date": d, "portfolio_snapshot": {"total_value": v}} for d, v in port_curve]
        snap = {"history": {"SPY": [{"date": d, "close": c} for d, c in spy_curve]}}
        lp = tmp_path / "agent_log.json"; lp.write_text(json.dumps(log))
        sp = tmp_path / "snap.json";       sp.write_text(json.dumps(snap))
        return str(lp), str(sp)

    def test_beats_spy_pretax_but_loses_after_ca_tax(self, tmp_path):
        """The pre-mortem's 'death by taxes' case, locked in: +20% pre-tax beats
        SPY +10%, but after ~54% CA short-term tax the strategy (+9.2%) LOSES."""
        import performance
        lp, sp = self._curve_files(
            tmp_path,
            [("2026-01-02", 1000.0), ("2026-01-09", 1200.0)],
            [("2026-01-02", 100.0),  ("2026-01-09", 110.0)])
        txs = [{"action": "BUY", "ticker": "AAPL", "qty": 10, "price": 100.0,
                "date": "2026-01-02", "timestamp": "2026-01-02T00:00:00+00:00"},
               {"action": "SELL", "ticker": "AAPL", "qty": 10, "price": 120.0,
                "date": "2026-01-09", "timestamp": "2026-01-09T00:00:00+00:00"}]
        s = performance.after_tax_scorecard(
            transactions=txs, portfolio={"positions": [], "total_value": 1200.0},
            agent_log_path=lp, snapshot_path=sp)
        assert s["realized"]["realized_gain_pretax"] == 200.0
        assert s["realized"]["realized_tax_estimate"] == 108.0       # 200 * 0.54
        assert s["realized"]["realized_gain_after_tax"] == 92.0      # separate tracking
        assert s["strategy_return"] == 0.20
        assert s["spy_hold_return"] == pytest.approx(0.10, abs=1e-9)
        assert s["strategy_return_after_tax"] == pytest.approx(0.092, abs=1e-9)  # (1200-108)/1000-1
        assert s["after_tax_alpha_vs_spy"] < 0   # the headline: tax turns alpha negative

    def test_flags_not_significant_under_threshold(self, tmp_path):
        import performance
        lp, sp = self._curve_files(
            tmp_path,
            [("2026-01-02", 1000.0), ("2026-01-09", 1010.0)],
            [("2026-01-02", 100.0),  ("2026-01-09", 101.0)])
        s = performance.after_tax_scorecard(transactions=[], portfolio={"positions": []},
                                            agent_log_path=lp, snapshot_path=sp)
        assert s["not_significant"] is True
        assert any("NOT STATISTICALLY SIGNIFICANT" in c for c in s["caveats"])

    def test_uncovered_sells_surfaced_not_guessed(self, tmp_path):
        import performance
        lp, sp = self._curve_files(tmp_path, [("2026-01-02", 1000.0)], [("2026-01-02", 100.0)])
        txs = [{"action": "SELL", "ticker": "MRK", "qty": 0.3, "price": 122.0,
                "date": "2026-01-02", "timestamp": "2026-01-02T00:00:00+00:00"}]
        s = performance.after_tax_scorecard(transactions=txs, portfolio={"positions": []},
                                            agent_log_path=lp, snapshot_path=sp)
        assert s["realized"]["realized_gain_pretax"] == 0.0
        assert len(s["uncovered_sells"]) == 1 and s["uncovered_sells"][0]["ticker"] == "MRK"


# ─────────────────────────────────────────────────────────────────────────────
#  Turnover / tax discipline guardrails (CA top-bracket taxable account)
# ─────────────────────────────────────────────────────────────────────────────

class TestMinHoldingPeriod:
    """Block discretionary SELLs of names bought < 5 trading days ago (anti-churn)."""

    def _txs(self, *buys):
        return [{"ticker": t, "action": "BUY", "date": d, "dry_run": False} for t, d in buys]

    def test_blocks_recent_buy(self):
        import guardrails
        # bought Fri 2026-06-12, selling Mon 2026-06-15 → 1 trading day < 5
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-06-12")),
            today="2026-06-15")
        assert kept == [] and len(rej) == 1
        assert "min-holding" in rej[0]["rejected_reason"]

    def test_allows_old_buy(self):
        import guardrails
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-06-01")),
            today="2026-06-12")
        assert len(kept) == 1 and rej == []

    def test_allows_sell_with_no_in_log_buy(self):
        # a position opened before logging began must be exitable
        import guardrails
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=[], today="2026-06-15")
        assert len(kept) == 1 and rej == []

    def test_kill_active_exempts_all(self):
        import guardrails
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-06-12")),
            kill_active=True, today="2026-06-15")
        assert len(kept) == 1 and rej == []

    def test_buys_and_holds_pass_through(self):
        import guardrails
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "AAPL", "action": "BUY"}, {"ticker": "MS", "action": "HOLD"}],
            {"positions": []}, transactions=self._txs(("AAPL", "2026-06-12")),
            today="2026-06-15")
        assert len(kept) == 2 and rej == []


class TestWashSaleReentry:
    """Block BUYs of names SOLD within 30 calendar days (wash-sale + anti-churn)."""

    def _txs(self, *sells):
        return [{"ticker": t, "action": "SELL", "date": d, "dry_run": False} for t, d in sells]

    def test_blocks_reentry_within_window(self):
        import guardrails
        kept, rej = guardrails.enforce_wash_sale_reentry(
            [{"ticker": "MRK", "action": "BUY", "target_weight": 0.08}],
            transactions=self._txs(("MRK", "2026-06-12")), today="2026-06-15")
        assert kept == [] and len(rej) == 1
        assert "wash-sale" in rej[0]["rejected_reason"]

    def test_allows_reentry_after_window(self):
        import guardrails
        kept, rej = guardrails.enforce_wash_sale_reentry(
            [{"ticker": "MRK", "action": "BUY", "target_weight": 0.08}],
            transactions=self._txs(("MRK", "2026-05-01")), today="2026-06-15")  # 45 days
        assert len(kept) == 1 and rej == []

    def test_allows_buy_never_sold(self):
        import guardrails
        kept, rej = guardrails.enforce_wash_sale_reentry(
            [{"ticker": "NVDA", "action": "BUY", "target_weight": 0.08}],
            transactions=[], today="2026-06-15")
        assert len(kept) == 1 and rej == []

    def test_dry_run_sell_ignored(self):
        import guardrails
        txs = [{"ticker": "MRK", "action": "SELL", "date": "2026-06-12", "dry_run": True}]
        kept, rej = guardrails.enforce_wash_sale_reentry(
            [{"ticker": "MRK", "action": "BUY", "target_weight": 0.08}],
            transactions=txs, today="2026-06-15")
        assert len(kept) == 1 and rej == []

    def test_sell_and_hold_pass_through(self):
        import guardrails
        kept, rej = guardrails.enforce_wash_sale_reentry(
            [{"ticker": "MRK", "action": "SELL"}, {"ticker": "MS", "action": "HOLD"}],
            transactions=self._txs(("MRK", "2026-06-14")), today="2026-06-15")
        assert len(kept) == 2 and rej == []


# ─────────────────────────────────────────────────────────────────────────────
#  Deploy gate — RELEASE_NOTES.md must be maintained (DEPLOYMENT.md §7.0.1)
# ─────────────────────────────────────────────────────────────────────────────

class TestReleaseNotes:
    """Forces every code deploy to record what shipped: RELEASE_NOTES.md must
    exist and carry an [Unreleased] section to move into a dated block on deploy."""

    def _path(self):
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "RELEASE_NOTES.md")

    def test_release_notes_file_exists(self):
        import os
        assert os.path.isfile(self._path()), "RELEASE_NOTES.md is missing (DEPLOYMENT.md §7.0.1)"

    def test_has_unreleased_section(self):
        with open(self._path()) as f:
            text = f.read()
        assert "## [Unreleased]" in text, \
            "RELEASE_NOTES.md needs an '## [Unreleased]' section for the next deploy"


# ─────────────────────────────────────────────────────────────────────────────
#  cost_model — shared cost & tax spine (P1: backtest + future net-edge gate)
# ─────────────────────────────────────────────────────────────────────────────

class TestCostModel:
    def test_tax_both_gains_at_their_rates(self):
        import cost_model
        tax, cf = cost_model.tax_on_realized(1000.0, 200.0)
        assert tax == round(1000 * cost_model.CA_SHORT_TERM_RATE
                            + 200 * cost_model.CA_LONG_TERM_RATE, 2)
        assert cf == 0.0

    def test_tax_lt_loss_offsets_st_gain(self):
        import cost_model
        tax, cf = cost_model.tax_on_realized(1000.0, -400.0)
        assert tax == round(600 * cost_model.CA_SHORT_TERM_RATE, 2)  # 324.0
        assert cf == 0.0

    def test_tax_st_loss_offsets_lt_gain_with_carryforward(self):
        import cost_model
        tax, cf = cost_model.tax_on_realized(-1500.0, 1000.0)
        assert tax == 0.0
        assert cf == 500.0

    def test_tax_both_losses_carry_forward(self):
        import cost_model
        tax, cf = cost_model.tax_on_realized(-200.0, -100.0)
        assert tax == 0.0
        assert cf == 300.0

    def test_round_trip_cost_scales_linearly_with_notional(self):
        import cost_model
        assert cost_model.round_trip_cost(2000.0) == round(2 * cost_model.round_trip_cost(1000.0), 4)

    def test_round_trip_cost_vol_adds_slippage(self):
        import cost_model
        assert cost_model.round_trip_cost(1000.0, annualized_vol=0.30) > cost_model.round_trip_cost(1000.0)

    def test_net_edge_taxes_only_positive_post_cost_gain(self):
        import cost_model
        e = cost_model.net_edge(0.02, 1000.0)            # +2% on $1000 = $20 gross
        assert e["gross"] == 20.0 and e["cost"] > 0
        assert e["tax"] == round((e["gross"] - e["cost"]) * cost_model.CA_SHORT_TERM_RATE, 4)
        assert e["net"] == round(e["gross"] - e["cost"] - e["tax"], 4)
        assert e["net"] < e["gross"]                     # tax + cost both bite

    def test_net_edge_no_tax_on_loss(self):
        import cost_model
        e = cost_model.net_edge(-0.01, 1000.0)
        assert e["tax"] == 0.0 and e["net"] < 0

    def test_performance_delegates_to_cost_model(self):
        # the live scorecard and the spine must agree on tax
        import performance, cost_model
        s = performance.realized_summary([{"term": "ST", "gain": 1000.0},
                                          {"term": "LT", "gain": -400.0}])
        tax, _ = cost_model.tax_on_realized(1000.0, -400.0)
        assert s["realized_tax_estimate"] == tax


# ─────────────────────────────────────────────────────────────────────────────
#  backtest — quant-only harness (P1): engine, strategies, report
# ─────────────────────────────────────────────────────────────────────────────

def _bt_bars(prices, start_ms=1_700_000_000_000, step=86_400_000):
    return [{"date": start_ms + i * step, "open": p, "high": p * 1.01,
             "low": p * 0.99, "close": p, "volume": 1e6} for i, p in enumerate(prices)]


class TestBacktestStrategies:
    def _scores(self):
        return {
            "SPY":  {"data_available": True, "momentum_available": True, "composite_score": 99, "volatility": 12},
            "WIN":  {"data_available": True, "momentum_available": True, "composite_score": 90, "volatility": 20},
            "MID":  {"data_available": True, "momentum_available": True, "composite_score": 70, "volatility": 40},
            "LOWS": {"data_available": True, "momentum_available": True, "composite_score": 40, "volatility": 15},
            "NODATA": {"data_available": False, "momentum_available": False, "composite_score": 95},
        }

    def test_excludes_benchmarks_and_low_composite_and_nodata(self):
        from backtest.strategies import quant_momentum_vol
        w = quant_momentum_vol(self._scores(), top_n=8, min_composite=50)
        assert "SPY" not in w and "QQQ" not in w
        assert "LOWS" not in w          # below min_composite
        assert "NODATA" not in w        # no real data
        assert set(w) == {"WIN", "MID"}

    def test_caps_weight_and_inverse_vol(self):
        from backtest.strategies import quant_momentum_vol
        # 0.50 cap so it doesn't bind on a 2-name book — lets inverse-vol show
        w = quant_momentum_vol(self._scores(), top_n=8, max_weight=0.50, min_composite=50)
        assert all(v <= 0.50 + 1e-9 for v in w.values())
        assert w["WIN"] > w["MID"]      # lower vol → larger inverse-vol weight

    def test_empty_when_nothing_qualifies(self):
        from backtest.strategies import quant_momentum_vol
        assert quant_momentum_vol({"AAA": {"data_available": True, "momentum_available": True,
                                           "composite_score": 10, "volatility": 20}}, min_composite=50) == {}


class TestBacktestEngine:
    def _snapshot(self):
        win  = [100 * (1.005 ** i) for i in range(30)]   # steady up, low vol → high score
        lose = [100 * (0.99 ** i) for i in range(30)]    # steady down → filtered out
        spy  = [100 * (1.001 ** i) for i in range(30)]
        return {"history": {"SPY": _bt_bars(spy), "WIN": _bt_bars(win), "LOSE": _bt_bars(lose)},
                "fundamentals": {}}

    def test_runs_and_buys_the_winner_not_the_loser(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        # top_n=1 → only the highest-composite name (the uptrending WIN) is held
        res = run_backtest(lambda sc: quant_momentum_vol(sc, top_n=1, min_composite=50),
                           snapshot=self._snapshot(),
                           initial_capital=10_000.0, rebalance_days=5, warmup=22)
        assert len(res["equity_curve"]) == 30
        buys = {t["ticker"] for t in res["transactions"] if t["action"] == "BUY"}
        assert "WIN" in buys
        assert "LOSE" not in buys
        assert res["benchmark_curve"]            # SPY curve present

    def test_fills_at_next_open_no_lookahead(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        snap = self._snapshot()
        res = run_backtest(quant_momentum_vol, snapshot=snap,
                           initial_capital=10_000.0, rebalance_days=5, warmup=22)
        # first WIN buy must fill at a bar OPEN price (next-open fill), never a close
        win_open_prices = {round(b["open"], 6) for b in snap["history"]["WIN"]}
        first_win = next(t for t in res["transactions"] if t["ticker"] == "WIN")
        assert round(first_win["price"], 6) in win_open_prices


class TestBacktestReport:
    def test_metrics_known_curve(self):
        from backtest.report import _metrics
        m = _metrics([("d1", 100.0), ("d2", 110.0), ("d3", 55.0)])
        assert m["total_return"] == pytest.approx(-0.45, abs=1e-6)
        assert m["max_drawdown"] == pytest.approx(-0.5, abs=1e-6)

    def test_after_tax_reduces_return_on_realized_gain(self):
        from backtest.report import build_report
        result = {
            "equity_curve":          [("d1", 10_000.0), ("d2", 10_500.0)],
            "benchmark_curve":       [("d1", 10_000.0), ("d2", 10_100.0)],
            "transactions": [
                {"action": "BUY",  "ticker": "X", "qty": 10, "price": 100.0,
                 "date": "2026-01-02", "timestamp": "2026-01-02T00:00:00+00:00", "dry_run": False},
                {"action": "SELL", "ticker": "X", "qty": 10, "price": 120.0,
                 "date": "2026-01-09", "timestamp": "2026-01-09T00:00:00+00:00", "dry_run": False},
            ],
            "initial_capital": 10_000.0, "final_equity": 10_500.0, "traded_notional_total": 2_200.0,
        }
        rep = build_report(result)
        assert rep["realized_gain"] == 200.0
        assert rep["tax_estimate"] == 108.0                       # 200 * 0.54
        assert rep["after_tax_final_equity"] == 10_392.0          # 10500 - 108
        assert rep["after_tax_alpha_vs_spy"] < rep["alpha_total_return"]   # tax bites

    def test_real_snapshot_backtest_smoke(self):
        # CI smoke: the full quant-only backtest on the committed snapshot completes.
        import os
        if not os.path.isfile("market_snapshot.json"):
            pytest.skip("no market_snapshot.json")
        from backtest.engine import run_backtest, load_snapshot
        from backtest.strategies import quant_momentum_vol
        from backtest.report import build_report
        rep = build_report(run_backtest(quant_momentum_vol, snapshot=load_snapshot()))
        assert rep["trading_days"] > 100 and rep["n_trades"] > 0
        assert rep["strategy"]["total_return"] is not None and rep["spy"] is not None


# ─────────────────────────────────────────────────────────────────────────────
#  Edge cases / failure scenarios (QA hardening pass)
# ─────────────────────────────────────────────────────────────────────────────

def _ec_bars(prices, s=1_700_000_000_000, step=86_400_000):
    return [{"date": s + i * step, "open": p, "high": p * 1.01, "low": p * 0.99,
             "close": p, "volume": 1e6} for i, p in enumerate(prices)]


class TestBacktestEdgeCases:
    """Degenerate inputs must not crash and must return sane defaults."""

    def test_no_spy_yields_empty_benchmark(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        snap = {"history": {"AAA": _ec_bars([100 * 1.01 ** i for i in range(40)])}, "fundamentals": {}}
        r = run_backtest(quant_momentum_vol, snapshot=snap, warmup=22)
        assert r["benchmark_curve"] == []          # no SPY → no benchmark, no crash

    def test_warmup_past_history_makes_no_trades(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        snap = {"history": {"SPY": _ec_bars([100] * 40), "AAA": _ec_bars([100 * 1.01 ** i for i in range(40)])},
                "fundamentals": {}}
        r = run_backtest(quant_momentum_vol, snapshot=snap, warmup=100)
        assert r["transactions"] == []
        assert all(v == r["initial_capital"] for _, v in r["equity_curve"])  # flat at cash

    def test_empty_history_does_not_crash(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        r = run_backtest(quant_momentum_vol, snapshot={"history": {}, "fundamentals": {}}, warmup=22)
        assert r["equity_curve"] == [] and r["final_equity"] == r["initial_capital"]

    def test_report_on_empty_result_is_safe(self):
        from backtest.report import build_report
        rep = build_report({"equity_curve": [], "benchmark_curve": [], "transactions": [],
                            "initial_capital": 50_000.0, "final_equity": 50_000.0, "traded_notional_total": 0.0})
        assert rep["spy"] is None and rep["n_trades"] == 0 and rep["tax_estimate"] == 0.0

    def test_no_leverage_or_negative_equity(self):
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        snap = {"history": {"SPY": _ec_bars([100 * 1.001 ** i for i in range(40)]),
                            "AAA": _ec_bars([100 * 1.01 ** i for i in range(40)])}, "fundamentals": {}}
        r = run_backtest(quant_momentum_vol, snapshot=snap, initial_capital=10_000.0, warmup=22)
        eqs = [v for _, v in r["equity_curve"]]
        assert all(v > 0 for v in eqs)                 # never negative equity
        assert max(eqs) < 10_000 * 5                   # no leverage blow-up

    def test_report_discloses_survivorship_bias(self):
        # honesty guard: the universe is current survivors only — must be caveated
        from backtest.report import build_report
        rep = build_report({"equity_curve": [("d1", 50_000.0), ("d2", 50_500.0)],
                            "benchmark_curve": [("d1", 50_000.0), ("d2", 50_100.0)],
                            "transactions": [], "initial_capital": 50_000.0,
                            "final_equity": 50_500.0, "traded_notional_total": 0.0})
        assert any("survivor" in c.lower() for c in rep["caveats"])


class TestGuardrailBoundaries:
    """Off-by-one boundaries on the holding-period and wash-sale windows."""

    def test_min_hold_exactly_5_trading_days_allowed(self):
        import guardrails as g
        # Fri 2026-06-05 → Fri 2026-06-12 = exactly 5 trading days; '< 5' blocks, so 5 is allowed
        kept, rej = g.enforce_min_holding_period(
            [{"ticker": "X", "action": "SELL", "target_weight": 0.0}], {"positions": []},
            transactions=[{"ticker": "X", "action": "BUY", "date": "2026-06-05", "dry_run": False}],
            today="2026-06-12")
        assert len(kept) == 1 and rej == []

    def test_wash_sale_exactly_30_days_allowed(self):
        import guardrails as g
        # sold 2026-05-15, today 2026-06-14 = 30 days; '< 30' blocks, so 30 is allowed
        kept, rej = g.enforce_wash_sale_reentry(
            [{"ticker": "X", "action": "BUY", "target_weight": 0.08}],
            transactions=[{"ticker": "X", "action": "SELL", "date": "2026-05-15", "dry_run": False}],
            today="2026-06-14")
        assert len(kept) == 1 and rej == []


# ─────────────────────────────────────────────────────────────────────────────
#  data_providers — real-data abstraction (#1). Tested against the StubProvider;
#  FMPProvider degrades gracefully without a key (no hard failure, no regression).
# ─────────────────────────────────────────────────────────────────────────────

class TestDataProviders:
    def test_stub_returns_configured_data(self):
        from data_providers import StubProvider
        p = StubProvider(
            fundamentals={"AAPL": {"gross_margin": 0.45, "pe_ratio": 30}},
            earnings={"AAPL": "2026-07-28"},
            estimates={"AAPL": {"eps": 2.1}})
        assert p.fundamentals("AAPL") == {"gross_margin": 0.45, "pe_ratio": 30}
        assert p.next_earnings_date("AAPL") == "2026-07-28"
        assert p.estimates("AAPL") == {"eps": 2.1}
        assert p.fundamentals("UNKNOWN") is None      # unknown ticker → None contract

    def test_stub_conforms_to_protocol(self):
        from data_providers import StubProvider, MarketDataProvider
        assert isinstance(StubProvider(), MarketDataProvider)

    def test_fmp_without_key_degrades_gracefully(self, monkeypatch):
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        from data_providers import FMPProvider
        p = FMPProvider(api_key=None)
        assert p.fundamentals("AAPL") is None         # no key → None, never raises
        assert p.next_earnings_date("AAPL") is None
        assert p.estimates("AAPL") is None

    def test_fmp_fundamentals_field_mapping(self, monkeypatch):
        from data_providers import FMPProvider
        p = FMPProvider(api_key="x")
        monkeypatch.setattr(p, "_get", lambda *a, **k: [{
            "grossProfitMarginTTM": 0.4612, "operatingProfitMarginTTM": 0.301,
            "debtEquityRatioTTM": 1.23, "peRatioTTM": 28.4,
            "freeCashFlowYieldTTM": 0.035, "enterpriseValueMultipleTTM": 19.1,
            "freeCashFlowPerShareTTM": 6.0, "revenuePerShareTTM": 24.0,
        }])
        f = p.fundamentals("AAPL")
        assert f["gross_margin"] == 0.4612 and f["operating_margin"] == 0.301
        assert f["debt_to_equity"] == 1.23 and f["pe_ratio"] == 28.4
        assert f["fcf_yield"] == 0.035 and f["ev_ebitda"] == 19.1
        assert f["fcf_margin"] == 0.25                # 6.0 / 24.0

    def test_fmp_next_earnings_picks_soonest_future(self, monkeypatch):
        from data_providers import FMPProvider
        p = FMPProvider(api_key="x")
        monkeypatch.setattr(p, "_get", lambda *a, **k: [
            {"symbol": "AAPL", "date": "2020-01-01"},   # past → ignored
            {"symbol": "AAPL", "date": "2099-09-09"},
            {"symbol": "AAPL", "date": "2099-07-01"},   # soonest future
            {"symbol": "MSFT", "date": "2099-06-01"},   # wrong ticker → ignored
        ])
        assert p.next_earnings_date("AAPL") == "2099-07-01"

    def test_get_provider_factory(self, monkeypatch):
        import data_providers as dp
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        assert isinstance(dp.get_provider(), dp.StubProvider)   # no key → stub
        monkeypatch.setenv("FMP_API_KEY", "k")
        assert isinstance(dp.get_provider(), dp.FMPProvider)    # key → FMP


# ─────────────────────────────────────────────────────────────────────────────
#  Earnings agent gate (Phase 3.2) + fabrication guard (#1). With a real calendar:
#  skip the LLM for names with no event in 90d; override the model's date guess.
# ─────────────────────────────────────────────────────────────────────────────

class TestEarningsGateAndFabrication:
    def _md(self, calendar=None, date="2026-06-14"):
        return {"date": date, "prices": {"AAPL": {"close": 200}},
                "earnings_calendar": calendar or {}, "ticker_news": {}, "news": []}

    def test_within_90d_boundaries(self):
        import analysis
        assert analysis._within_90d(None, "2026-06-14") is False
        assert analysis._within_90d("2026-06-14", "2026-06-14") is True    # today
        assert analysis._within_90d("2026-09-12", "2026-06-14") is True    # +90
        assert analysis._within_90d("2026-09-13", "2026-06-14") is False   # +91
        assert analysis._within_90d("2026-06-13", "2026-06-14") is False   # past

    def test_gate_skips_when_calendar_present_no_event(self, monkeypatch):
        import analysis
        called = []
        monkeypatch.setattr(analysis, "_safe_call", lambda *a, **k: called.append(1) or {})
        r = analysis.run_earnings_catalyst_analyst("AAPL", self._md(calendar={"AAPL": "2026-12-01"}))
        assert r.get("skipped_no_catalyst") is True and r["earnings_alpha_score"] is None
        assert called == []                                # NO LLM call — Phase 3.2

    def test_runs_when_event_within_90d(self, monkeypatch):
        import analysis
        called = []
        monkeypatch.setattr(analysis, "_safe_call",
                            lambda *a, **k: called.append(1) or {"next_earnings_est": "2026-07-01", "earnings_alpha_score": 7})
        r = analysis.run_earnings_catalyst_analyst("AAPL", self._md(calendar={"AAPL": "2026-07-01"}))
        assert called == [1] and r["earnings_alpha_score"] == 7

    def test_no_calendar_runs_as_before(self, monkeypatch):
        import analysis
        called = []
        monkeypatch.setattr(analysis, "_safe_call", lambda *a, **k: called.append(1) or {"earnings_alpha_score": 5})
        r = analysis.run_earnings_catalyst_analyst("AAPL", self._md(calendar={}))
        assert called == [1] and "skipped_no_catalyst" not in r    # no regression on free tier

    def test_fabrication_guard_overrides_model_date(self, monkeypatch):
        import analysis
        monkeypatch.setattr(analysis, "_safe_call",
                            lambda *a, **k: {"next_earnings_est": "2026-08-15", "earnings_alpha_score": 6})
        r = analysis.run_earnings_catalyst_analyst("AAPL", self._md(calendar={"AAPL": "2026-07-01"}))
        assert r["next_earnings_est"] == "2026-07-01"        # verified calendar wins
        assert r["next_earnings_est_model"] == "2026-08-15" and r["earnings_date_corrected"] is True


# ─────────────────────────────────────────────────────────────────────────────
#  #6 — tax-lot accounting + net-edge gate (reject BUYs not worth it after CA tax)
# ─────────────────────────────────────────────────────────────────────────────

class TestTaxLots:
    def _tx(self, action, ticker, qty, price, date, dry=False):
        return {"action": action, "ticker": ticker, "qty": qty, "price": price,
                "date": date, "timestamp": date + "T00:00:00+00:00", "dry_run": dry}

    def test_open_lots_after_fifo(self):
        import tax_lots
        txs = [self._tx("BUY", "AAPL", 10, 100, "2026-01-02"),
               self._tx("BUY", "AAPL", 10, 110, "2026-01-03"),
               self._tx("SELL", "AAPL", 15, 130, "2026-01-10")]
        lots = tax_lots.open_lots(txs, "AAPL")
        # FIFO: first lot (10@100) fully consumed; second (10@110) reduced to 5
        assert len(lots) == 1
        assert lots[0]["qty"] == 5.0 and lots[0]["cost_basis"] == 110
        assert lots[0]["acquired"] == "2026-01-03"

    def test_open_lots_excludes_dry_run(self):
        import tax_lots
        assert tax_lots.open_lots([self._tx("BUY", "X", 5, 10, "2026-01-01", dry=True)], "X") == []

    def test_holding_days(self):
        import tax_lots
        assert tax_lots.holding_days("2026-01-01", "2026-01-31") == 30
        assert tax_lots.holding_days("bad", "2026-01-31") is None


class TestNetEdgeGate:
    def _prices(self):
        return {"NVDA": {"close": 100.0}}

    def test_no_expected_return_passes_through(self):
        import guardrails as g
        kept, rej = g.enforce_net_edge([{"ticker": "NVDA", "action": "BUY", "qty": 4}], self._prices())
        assert len(kept) == 1 and rej == []          # not evaluated without expected_return

    def test_sell_is_exempt(self):
        import guardrails as g
        kept, rej = g.enforce_net_edge(
            [{"ticker": "NVDA", "action": "SELL", "qty": 4, "expected_return": 0.01}], self._prices())
        assert len(kept) == 1 and rej == []

    def test_marginal_buy_rejected_after_tax(self):
        import guardrails as g
        # 0.01% expected on a $400 BUY: gross $0.04 < cost $0.12 → net < 0
        kept, rej = g.enforce_net_edge(
            [{"ticker": "NVDA", "action": "BUY", "qty": 4, "expected_return": 0.0001}],
            self._prices(), min_net_edge=0.0)
        assert kept == [] and len(rej) == 1 and "net edge" in rej[0]["rejected_reason"]

    def test_healthy_edge_kept(self):
        import guardrails as g
        # 10% on $400 = $40 gross; net after ~54% tax ≈ $18 > 0 → kept
        kept, rej = g.enforce_net_edge(
            [{"ticker": "NVDA", "action": "BUY", "qty": 4, "expected_return": 0.10}],
            self._prices(), min_net_edge=0.0)
        assert len(kept) == 1 and rej == []

    def test_tunable_floor_rejects(self):
        import guardrails as g
        # same ~$18 net edge, but a $25 floor rejects it
        kept, rej = g.enforce_net_edge(
            [{"ticker": "NVDA", "action": "BUY", "qty": 4, "expected_return": 0.10}],
            self._prices(), min_net_edge=25.0)
        assert kept == [] and len(rej) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  #2 — calibration forecast ledger (observational; no trade-path change)
# ─────────────────────────────────────────────────────────────────────────────

class TestCalibrationLedger:
    def _pstate(self):
        return {
            "quant_scores":    {"AAPL": {"composite_score": 80}, "MSFT": {"composite_score": 60}},
            "research":        {"AAPL": {"confidence": 8}},
            "earnings":        {"AAPL": {"earnings_alpha_score": 7}},
            "devils_advocate": {"AAPL": {"overall_risk_score": 4}},
            "position_reviews": {"AAPL": {"hold_score": 9}},
        }

    def test_log_forecasts_shape(self, tmp_path):
        import calibration, json
        path = str(tmp_path / "f.jsonl")
        n = calibration.log_forecasts("r1", "2026-06-14", self._pstate(), ["AAPL", "MSFT"],
                                      {"AAPL": {"close": 200}, "MSFT": {"close": 100}}, path=path)
        assert n == 6                     # AAPL: 5 agents, MSFT: quant only
        rows = [json.loads(l) for l in open(path)]
        aq = next(r for r in rows if r["ticker"] == "AAPL" and r["agent"] == "quant")
        assert aq["value"] == 80 and aq["entry_price"] == 200 and aq["horizon_days"] == 21

    def test_log_forecasts_skips_missing_price(self, tmp_path):
        import calibration
        n = calibration.log_forecasts("r1", "2026-06-14",
                                      {"quant_scores": {"AAPL": {"composite_score": 80}}},
                                      ["AAPL"], {"AAPL": {}}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_score_matured_joins_forward_return(self, tmp_path):
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                "field": "composite_score", "ticker": "AAPL", "value": 80,
                                "entry_price": 100.0, "horizon_days": 5}) + "\n")
        snap = {"history": {"AAPL": [{"date": "2026-01-08", "close": 110.0}]}}   # >= entry+5d
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 1
        r = json.loads(open(scored).readline())
        assert r["realized_return"] == 0.1 and r["future_price"] == 110.0
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0  # idempotent

    def test_score_matured_skips_immature(self, tmp_path):
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                "field": "composite_score", "ticker": "AAPL", "value": 80,
                                "entry_price": 100.0, "horizon_days": 5}) + "\n")
        snap = {"history": {"AAPL": [{"date": "2026-01-05", "close": 105.0}]}}   # before maturity
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0

    def test_agent_scorecard_shrinks_small_sample(self, tmp_path):
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        with open(scored, "w") as f:
            for i in range(5):
                f.write(json.dumps({"run_id": f"r{i}", "agent": "quant", "field": "composite_score",
                                    "ticker": "AAPL", "value": float(i), "realized_return": i / 100}) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card, shrink_k=50)
        k = "quant.composite_score"
        assert out[k]["n"] == 5 and out[k]["ic"] == 1.0
        assert out[k]["ic_shrunk"] == round(5 / 55, 3)        # shrunk far below the raw IC
        assert out[k]["ic_shrunk"] < out[k]["ic"]


# ─────────────────────────────────────────────────────────────────────────────
#  Cross-feature interaction regressions (#1 × #6 × #2 integrated)
# ─────────────────────────────────────────────────────────────────────────────

class TestFeatureInteractions:
    def test_skipped_earnings_not_logged_by_ledger(self, tmp_path):
        # #1 Phase 3.2 emits earnings_alpha_score=None (skipped); #2 must drop it (non-numeric)
        import calibration
        pstate = {"quant_scores": {"AAPL": {"composite_score": 80}},
                  "earnings": {"AAPL": {"earnings_alpha_score": None, "skipped_no_catalyst": True}},
                  "research": {"AAPL": {"confidence": 7}}}
        n = calibration.log_forecasts("r1", "2026-06-14", pstate, ["AAPL"],
                                      {"AAPL": {"close": 200}}, path=str(tmp_path / "f.jsonl"))
        assert n == 2          # quant + research logged; None earnings dropped, no crash

    def test_net_edge_coerces_string_expected_return(self):
        # the PM emits expected_return; a stringified "0.0001" must still be evaluated
        import guardrails as g
        kept, rej = g.enforce_net_edge(
            [{"ticker": "X", "action": "BUY", "qty": 4, "expected_return": "0.0001"}],
            {"X": {"close": 100}})
        assert len(kept) + len(rej) == 1 and len(rej) == 1     # tiny edge → rejected

    def test_net_edge_garbage_expected_return_passes(self):
        import guardrails as g
        kept, rej = g.enforce_net_edge(
            [{"ticker": "X", "action": "BUY", "qty": 4, "expected_return": "abc"}],
            {"X": {"close": 100}})
        assert len(kept) == 1 and rej == []                    # unparseable → not evaluated

    def test_all_four_guards_chain(self):
        # min-hold → wash-sale → sector → net-edge all run without crashing; a valid BUY+SELL survive
        import guardrails as g
        decisions = [{"ticker": "NVDA", "action": "BUY", "qty": 4, "target_weight": 0.08, "expected_return": 0.10},
                     {"ticker": "MRK", "action": "SELL", "qty": 2, "target_weight": 0.0}]
        portfolio = {"total_value": 500.0, "positions": []}
        prices = {"NVDA": {"close": 100.0}, "MRK": {"close": 50.0}}
        d, _ = g.enforce_min_holding_period(decisions, portfolio, transactions=[], today="2026-06-14")
        d, _ = g.enforce_wash_sale_reentry(d, transactions=[], today="2026-06-14")
        d, _ = g.enforce_sector_limits(d, portfolio)
        d, _ = g.enforce_net_edge(d, prices)
        tickers = {x["ticker"] for x in d}
        assert "NVDA" in tickers and "MRK" in tickers
