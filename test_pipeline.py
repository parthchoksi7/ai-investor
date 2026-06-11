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

    def test_happy_path_reads_spy_from_snapshot(self, tmp_path):
        """Returns SPY close from market_snapshot.json when dated today."""
        from datetime import date
        today = date.today().isoformat()
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
        from datetime import date
        today = date.today().isoformat()
        self._write(tmp_path, "market_snapshot.json", {
            "date": today,
            "prices": {"AAPL": {"close": 200.0}},
        })
        result = self._read_spy(tmp_path)
        assert result is None

    def test_returns_none_when_spy_close_is_zero(self, tmp_path):
        """SPY close=0 (bad data) → returns None to trigger fallback."""
        from datetime import date
        today = date.today().isoformat()
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
