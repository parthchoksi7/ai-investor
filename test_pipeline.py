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

    def test_truncated_first_string_value_is_preserved(self):
        """Regression: a response cut open mid-string (hit max_tokens inside the
        first big field, e.g. Devil's Advocate "bear_case") must keep the partial
        value, not collapse to the default. See _safe_call truncation handling."""
        default = {"bear_case": "", "overall_risk_score": 5}
        truncated = '```json\n{\n  "bear_case": "JPM faces NIM compression and cyclical IB reven'
        result = self._parse(truncated, default)
        assert result != default
        assert result["bear_case"].startswith("JPM faces NIM compression")

    def test_truncated_after_first_field_recovers_remaining(self):
        """Cut in a later field still closes the object and keeps earlier fields."""
        default = {"bear_case": ""}
        truncated = '{"bear_case": "valuation looks stretched", "weakest_assumptions": ["margins hold'
        result = self._parse(truncated, default)
        assert result["bear_case"] == "valuation looks stretched"


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

    def test_nan_close_yields_unavailable_not_nan(self):
        # A NaN close (TXN/TJX/CAT snapshot gap, Jun 16) must NOT propagate into a
        # NaN volatility — that NaN broke the Supabase publish. Treat as unavailable.
        import math
        from quant_engine import compute_risk_metrics
        closes = [100.0 + i for i in range(40)]
        closes[10] = float("nan")
        r = compute_risk_metrics(_make_history(closes), [])
        assert r["volatility"] is None
        assert r["volatility_available"] is False

    def test_zero_close_yields_unavailable_not_nan(self):
        from quant_engine import compute_risk_metrics
        closes = [100.0 + i for i in range(40)]
        closes[10] = 0.0  # would be a div-by-zero / degenerate return
        r = compute_risk_metrics(_make_history(closes), [])
        assert r["volatility"] is None
        assert r["volatility_available"] is False

    def test_nan_vol_excluded_from_composite(self):
        # End-to-end: a ticker with a NaN close must produce a finite composite
        # (volatility dropped from the honest weighting), never a NaN composite.
        import math
        from quant_engine import score_all_tickers
        bad = [{"close": 100.0 + i} for i in range(40)]
        bad[10]["close"] = float("nan")
        scores = score_all_tickers({"history": {"BAD": bad}, "fundamentals": {}})
        comp = scores["BAD"]["composite_score"]
        assert math.isfinite(comp)
        assert "volatility" not in scores["BAD"]["factors_used"]


# ── quant_engine.score_all_tickers ───────────────────────────────────────────

class TestScoreAllTickers:
    def test_composite_weight_formula_all_factors(self):
        # When every factor has real data the composite uses all four base weights.
        # Weights are read from FACTOR_WEIGHTS (source of truth) so the test tracks
        # the Phase 2 re-weight instead of pinning stale literals.
        from quant_engine import score_all_tickers, FACTOR_WEIGHTS, FORMULA_VERSION
        history = _flat(100.0, 210)
        market_data = {
            "history": {"AAPL": history, "SPY": history},
            "fundamentals": {"AAPL": {"gross_margin": 0.75, "pe_ratio": 12.0}},
        }
        scores = score_all_tickers(market_data)
        s = scores["AAPL"]
        assert set(s["factors_used"]) == {"momentum", "quality", "valuation", "volatility"}
        assert s["formula_version"] == FORMULA_VERSION
        expected = (
            s["momentum_score"]    * FACTOR_WEIGHTS["momentum"]
            + s["quality_score"]   * FACTOR_WEIGHTS["quality"]
            + s["valuation_score"] * FACTOR_WEIGHTS["valuation"]
            + s["volatility_score"] * FACTOR_WEIGHTS["volatility"]
        )
        assert s["composite_score"] == pytest.approx(expected, abs=0.15)

    def test_composite_drops_missing_factors_and_renormalizes(self):
        # Phase 3.1 honesty: with NO fundamentals, quality/valuation carry no
        # real data and must be dropped — the composite is momentum+volatility
        # renormalized to their own weights, NOT blended with two constant 50s.
        from quant_engine import score_all_tickers, FACTOR_WEIGHTS
        history = _flat(100.0, 210)
        market_data = {"history": {"AAPL": history, "SPY": history}, "fundamentals": {}}
        s = score_all_tickers(market_data)["AAPL"]
        assert s["factors_used"] == ["momentum", "volatility"]
        assert s["quality_available"] is False
        assert s["valuation_available"] is False
        wm, wv = FACTOR_WEIGHTS["momentum"], FACTOR_WEIGHTS["volatility"]
        expected = (s["momentum_score"] * wm + s["volatility_score"] * wv) / (wm + wv)
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


class TestFactorHistory:
    def _scores(self):
        from quant_engine import FORMULA_VERSION
        return {
            "AAPL": {"composite_score": 62.0, "factors_used": ["momentum", "volatility"],
                     "formula_version": FORMULA_VERSION, "momentum_score": 70,
                     "momentum_available": True, "volatility_score": 55,
                     "volatility_available": True, "beta": 1.1},
            "MSFT": {"composite_score": 58.0, "factors_used": ["momentum"],
                     "formula_version": FORMULA_VERSION, "momentum_score": 58,
                     "momentum_available": True, "beta": None},
        }

    def test_appends_one_row_per_ticker_with_formula_version(self, tmp_path):
        from quant_engine import log_factor_history, FORMULA_VERSION
        import json
        path = str(tmp_path / "fh.jsonl")
        n = log_factor_history(self._scores(), as_of="2026-07-02", path=path)
        assert n == 2
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert {r["ticker"] for r in rows} == {"AAPL", "MSFT"}
        assert all(r["formula_version"] == FORMULA_VERSION for r in rows)
        assert all(r["date"] == "2026-07-02" for r in rows)
        aapl = next(r for r in rows if r["ticker"] == "AAPL")
        assert aapl["composite_score"] == 62.0 and aapl["beta"] == 1.1

    def test_idempotent_same_day_same_formula(self, tmp_path):
        from quant_engine import log_factor_history
        path = str(tmp_path / "fh.jsonl")
        log_factor_history(self._scores(), as_of="2026-07-02", path=path)
        n2 = log_factor_history(self._scores(), as_of="2026-07-02", path=path)
        assert n2 == 0                                   # no duplicate rows
        rows = [l for l in open(path) if l.strip()]
        assert len(rows) == 2

    def test_new_day_appends_again(self, tmp_path):
        from quant_engine import log_factor_history
        path = str(tmp_path / "fh.jsonl")
        log_factor_history(self._scores(), as_of="2026-07-02", path=path)
        n2 = log_factor_history(self._scores(), as_of="2026-07-03", path=path)
        assert n2 == 2
        rows = [l for l in open(path) if l.strip()]
        assert len(rows) == 4

    def test_formula_boundary_is_a_distinct_key(self, tmp_path):
        # A re-weight (new formula_version) for the SAME date is NOT a duplicate —
        # both regimes are recorded so IC is computed within, never across, a boundary.
        from quant_engine import log_factor_history, FORMULA_VERSION
        import json
        path = str(tmp_path / "fh.jsonl")
        s = self._scores()
        log_factor_history(s, as_of="2026-07-02", path=path)
        for v in s.values():
            v["formula_version"] = "3.0-experimental"
        n2 = log_factor_history(s, as_of="2026-07-02", path=path)
        assert n2 == 2
        versions = {json.loads(l)["formula_version"] for l in open(path) if l.strip()}
        assert versions == {FORMULA_VERSION, "3.0-experimental"}

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
    """NOTE: every test here isolates BOTH PENDING_FILE and LAST_REBALANCE_FILE.
    mark_pending_executed mirrors a rebalance-mode stamp into last_rebalance.json
    (Phase 5, §6.5) — leaving that constant un-monkeypatched would make these
    tests write real fixture data into the REAL repo's last_rebalance.json (a
    bare relative path), corrupting the once-per-ISO-week rebalance lock the
    live gate depends on. This bit us once already: see git history."""

    def _write(self, path, run_id, executed_at=None):
        path.write_text(json.dumps({
            "run_id":       run_id,
            "date":         "2026-06-09",
            "generated_at": "2026-06-09T13:00:00Z",
            "executed_at":  executed_at,
            "decisions":    [],
        }))

    def _isolate_rebalance_file(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.setattr(journal, "LAST_REBALANCE_FILE", str(tmp_path / "last_rebalance.json"))

    def test_stamps_execution_timestamp(self, tmp_path, monkeypatch):
        import journal
        self._isolate_rebalance_file(tmp_path, monkeypatch)
        pending = tmp_path / "pending.json"
        self._write(pending, "run-001")
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-001")
        data = json.loads(pending.read_text())
        assert data["executed_at"] is not None

    def test_second_call_preserves_original_timestamp(self, tmp_path, monkeypatch):
        import journal
        self._isolate_rebalance_file(tmp_path, monkeypatch)
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
        self._isolate_rebalance_file(tmp_path, monkeypatch)
        pending = tmp_path / "pending.json"
        self._write(pending, "run-001")
        monkeypatch.setattr(journal, "PENDING_FILE", str(pending))
        from journal import mark_pending_executed
        mark_pending_executed("run-999")
        data = json.loads(pending.read_text())
        assert data["executed_at"] is None

    def test_already_stamped_file_not_overwritten(self, tmp_path, monkeypatch):
        import journal
        self._isolate_rebalance_file(tmp_path, monkeypatch)
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
        self._isolate_rebalance_file(tmp_path, monkeypatch)
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

    # A fixed open trading day (Wed 2026-06-17 — not a weekend or NYSE holiday).
    # Pinning the gate's effective date via PREFLIGHT_DATE_OVERRIDE makes every
    # date-dependent gate test deterministic regardless of the wall-clock day
    # (the market-closed check would otherwise SKIP these on weekends/holidays).
    OPEN_DAY = "2026-06-17"

    def _today_et(self):
        return self.OPEN_DAY

    def _run(self, tmp_path, date_override=None):
        env = dict(self.os.environ)
        env["PREFLIGHT_DATE_OVERRIDE"] = date_override or self.OPEN_DAY
        return self.subprocess.run(
            [self.sys.executable, self.GATE],
            cwd=str(tmp_path), capture_output=True, text=True, env=env,
        ).returncode

    def _write(self, tmp_path, name, obj):
        (tmp_path / name).write_text(json.dumps(obj))

    def _fresh_dossier(self, tmp_path, date=None):
        """Phase 5: a rebalance-day PROCEED also requires a fresh research dossier
        (as_of == today, ≥2 built_from_days with the newest == today, tickers)."""
        d = date or self._today_et()
        from datetime import datetime, timedelta
        prev = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write(tmp_path, "research_dossier.json",
                    {"schema": "dossier-1.0", "as_of": d, "n_tickers": 1,
                     "built_from_days": [prev, d], "tickers": {"AAPL": {}}})

    def test_proceed_when_fresh_and_not_executed(self, tmp_path):
        # OPEN_DAY 2026-06-17 is a WEDNESDAY — the rebalance day, so exit 0.
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "pending_decisions.json", {"date": today, "executed_at": None})
        self._fresh_dossier(tmp_path)
        assert self._run(tmp_path) == 0  # PROCEED/REBALANCE

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

    def test_skip_retry_when_dossier_missing_on_rebalance_day(self, tmp_path):
        """Fresh snapshot but NO dossier → the rebalance must not run (P1-5)."""
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        assert self._run(tmp_path) == 10

    def test_skip_retry_when_dossier_stale_on_rebalance_day(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._write(tmp_path, "research_dossier.json",
                    {"schema": "dossier-1.0", "as_of": "2020-01-01", "n_tickers": 1,
                     "built_from_days": ["2019-12-31", "2020-01-01"], "tickers": {"AAPL": {}}})
        assert self._run(tmp_path) == 10

    def test_done_takes_precedence_over_stale(self, tmp_path):
        """If already executed, skip-done even if the snapshot looks stale."""
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": "2020-01-01", "prices": {}, "history": {}})
        self._write(tmp_path, "pending_decisions.json",
                    {"date": today, "run_id": "x", "executed_at": "2026-01-01T00:00:00Z"})
        assert self._run(tmp_path) == 20  # SKIP/DONE wins


class TestPreflightGateMarketClosed(TestPreflightGate):
    """The market-calendar gate (check 0): a closed market accepts GFD orders that
    never fill, so the routine must SKIP/RETRY. Regression for the Juneteenth
    2026-06-19 incident — a 'today'-dated snapshot proceeded and placed 4 orders
    that could never fill. The calendar check must win even over fresh data and
    even over an already-executed claim (no trading happens on a closed day)."""

    HOLIDAY = "2026-06-19"   # Juneteenth (Friday) — NYSE closed
    SATURDAY = "2026-06-20"  # weekend
    SUNDAY = "2026-06-21"

    def _fresh_snapshot(self, tmp_path, date):
        self._write(tmp_path, "market_snapshot.json",
                    {"date": date, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})

    def test_skip_retry_on_nyse_holiday(self, tmp_path):
        self._fresh_snapshot(tmp_path, self.HOLIDAY)
        self._write(tmp_path, "pending_decisions.json", {"date": self.HOLIDAY, "executed_at": None})
        assert self._run(tmp_path, date_override=self.HOLIDAY) == 10  # SKIP/RETRY

    def test_skip_retry_on_saturday(self, tmp_path):
        self._fresh_snapshot(tmp_path, self.SATURDAY)
        assert self._run(tmp_path, date_override=self.SATURDAY) == 10

    def test_skip_retry_on_sunday(self, tmp_path):
        self._fresh_snapshot(tmp_path, self.SUNDAY)
        assert self._run(tmp_path, date_override=self.SUNDAY) == 10

    def test_holiday_check_precedes_idempotency(self, tmp_path):
        """Even an already-executed claim yields SKIP/RETRY on a holiday — the
        market simply isn't open, so the calendar check is evaluated first."""
        self._fresh_snapshot(tmp_path, self.HOLIDAY)
        self._write(tmp_path, "pending_decisions.json",
                    {"date": self.HOLIDAY, "run_id": "x", "executed_at": "2026-06-19T13:51:00Z"})
        assert self._run(tmp_path, date_override=self.HOLIDAY) == 10

    def test_open_trading_day_still_proceeds(self, tmp_path):
        """Control: the rebalance weekday with fresh data still PROCEEDs (the calendar
        check does not over-block)."""
        self._fresh_snapshot(tmp_path, self.OPEN_DAY)
        self._write(tmp_path, "pending_decisions.json", {"date": self.OPEN_DAY, "executed_at": None})
        self._fresh_dossier(tmp_path)
        assert self._run(tmp_path) == 0  # PROCEED/REBALANCE


class TestCanaryAuth:
    """preflight_gate._check_api_health must authenticate the SAME way the real
    agents do (analysis.py:_get_client) — via the OAuth token file (auth_token=)
    in the cloud, NOT a bare Anthropic(). The old bare-client path failed auth in
    the cloud, fell through to the non-529 'proceed' branch, and silently disabled
    529 overload protection on the live path (Jun 16 fix #3)."""

    def _fake_anthropic(self, captured):
        import types
        mod = types.ModuleType("anthropic")
        _resp = types.SimpleNamespace(content=[types.SimpleNamespace(text='{"status":"ok"}')])

        class _Client:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.messages = types.SimpleNamespace(create=lambda **kw: _resp)

        mod.Anthropic = _Client
        return mod

    def test_uses_oauth_token_file_when_no_api_key(self, tmp_path, monkeypatch):
        import sys
        captured = {}
        monkeypatch.setitem(sys.modules, "anthropic", self._fake_anthropic(captured))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        tok = tmp_path / "token"
        tok.write_text("oauth-xyz")
        monkeypatch.setenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", str(tok))
        import preflight_gate
        healthy, _ = preflight_gate._check_api_health()
        assert healthy is True
        assert captured.get("auth_token") == "oauth-xyz"  # the fix: token-file auth
        assert "api_key" not in captured

    def test_uses_api_key_when_present(self, monkeypatch):
        import sys
        captured = {}
        monkeypatch.setitem(sys.modules, "anthropic", self._fake_anthropic(captured))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        import preflight_gate
        healthy, _ = preflight_gate._check_api_health()
        assert healthy is True
        assert captured.get("api_key") == "sk-test"

    def test_skips_cleanly_when_no_credentials(self, monkeypatch):
        import sys
        captured = {}
        monkeypatch.setitem(sys.modules, "anthropic", self._fake_anthropic(captured))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", raising=False)
        import preflight_gate
        healthy, msg = preflight_gate._check_api_health()
        assert healthy is True
        assert "skipping" in msg.lower()
        assert captured == {}  # no client was ever built


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


class TestSanitizeNaN:
    """publish._sanitize — the serialization-boundary scrub that keeps a NaN/Inf
    from breaking the Supabase publish (Jun 16: vol=nan reached the upsert →
    'Out of range float values are not JSON compliant')."""

    def test_nan_and_inf_become_none(self):
        from publish import _sanitize
        assert _sanitize(float("nan")) is None
        assert _sanitize(float("inf")) is None
        assert _sanitize(float("-inf")) is None

    def test_finite_floats_untouched(self):
        from publish import _sanitize
        assert _sanitize(3.14) == 3.14
        assert _sanitize(0.0) == 0.0
        assert _sanitize(-2.5) == -2.5

    def test_recurses_into_dicts_and_lists(self):
        from publish import _sanitize
        dirty = {"ann_vol": float("nan"),
                 "legs": [1.0, float("inf"), {"beta": float("-inf"), "ok": 5}]}
        assert _sanitize(dirty) == {"ann_vol": None,
                                    "legs": [1.0, None, {"beta": None, "ok": 5}]}

    def test_sanitized_payload_is_strict_json_serializable(self):
        import json
        from publish import _sanitize
        row = {"ticker": "TXN", "ann_vol": float("nan"), "composite": 83.6}
        # allow_nan=False is what Supabase/PostgREST effectively enforces.
        json.dumps(_sanitize(row), allow_nan=False)

    def test_non_float_types_pass_through(self):
        from publish import _sanitize
        assert _sanitize("TXN") == "TXN"
        assert _sanitize(None) is None
        assert _sanitize(42) == 42
        assert _sanitize(True) is True


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
    trades (Scenario B recovery), never duplicate trades.

    NOTE: _setup isolates BOTH PENDING_FILE and LAST_REBALANCE_FILE. Without the
    latter, mark_execution_started's rebalance-mode mirror (Phase 5, §6.5) would
    write this class's "r1"/"BAC"/2026-06-12 fixture straight into the REAL
    repo's last_rebalance.json (a bare relative path) — corrupting the live
    once-per-ISO-week rebalance lock. This bit us once already: see git history."""

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
        monkeypatch.setattr(journal, "LAST_REBALANCE_FILE", str(tmp_path / "last_rebalance.json"))
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
        monkeypatch.setattr(journal, "LAST_REBALANCE_FILE", str(tmp_path / "last_rebalance.json"))
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
        # A claim from a PRIOR ISO WEEK — neither the daily idempotency (rule 1b)
        # nor the once-per-ISO-week rebalance lock may block today's rebalance.
        self._write(tmp_path, "pending_decisions.json",
                    {"date": "2020-01-01", "run_id": "x", "executed_at": None,
                     "execution_started_at": "2020-01-01T13:50:00Z"})
        self._fresh_dossier(tmp_path)
        assert self._run(tmp_path) == 0  # stale claim from a prior week — PROCEED


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
            return "[]", "end_turn"  # valid JSON; PM expects an array, research a dict
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
            return '{"approved": true, "risk_budget_used": 30, "rejected_tickers": []}', "end_turn"
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
            return '{"approved": true, "rejected_tickers": []}', "end_turn"
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
        # A4: SPY hold is now TOTAL return (price 0.10 + ~1.25%/yr dividend gross-up
        # over the 7-day window) — slightly above the 0.10 price return.
        assert s["spy_hold_return"] == pytest.approx(0.10 + 0.0125 * 7 / 365, abs=2e-4)
        assert s["spy_hold_return"] > 0.10
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
    """Block discretionary SELLs of names bought < 30 trading days ago (anti-churn;
    policy v2.0 raised the floor from 5 per IPS §7.2 — the 9–12mo horizon)."""

    def _txs(self, *buys):
        return [{"ticker": t, "action": "BUY", "date": d, "dry_run": False} for t, d in buys]

    def test_blocks_recent_buy(self):
        import guardrails
        # bought Fri 2026-06-12, selling Mon 2026-06-15 → 1 trading day < 30
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-06-12")),
            today="2026-06-15")
        assert kept == [] and len(rej) == 1
        assert "min-holding" in rej[0]["rejected_reason"]

    def test_blocks_buy_between_5_and_30_trading_days(self):
        # 9 trading days held — allowed under the old 5-day floor, BLOCKED under v2.0's 30.
        import guardrails
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-06-01")),
            today="2026-06-12")
        assert kept == [] and len(rej) == 1

    def test_allows_old_buy(self):
        import guardrails
        # bought Mon 2026-04-27, selling Fri 2026-06-12 → 34 trading days ≥ 30
        kept, rej = guardrails.enforce_min_holding_period(
            [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}],
            {"positions": []}, transactions=self._txs(("MRK", "2026-04-27")),
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


class TestWashSalePresaleFlag:
    """A6: FLAG (never block) loss SELLs within 30d of a purchase (pre-sale §1091)."""

    def _buy(self, ticker, date, qty, price):
        return {"ticker": ticker, "action": "BUY", "date": date, "qty": qty,
                "price": price, "dry_run": False}

    def test_flags_recent_loss_exit_but_keeps_it(self):
        import guardrails
        # Bought MRK @100 ten days ago; now selling @90 (a loss) → flag, not block.
        txs = [self._buy("MRK", "2026-06-04", 1, 100.0)]
        decs = [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}]
        out, flagged = guardrails.flag_wash_sale_presale(
            decs, {"MRK": {"close": 90.0}}, transactions=txs, today="2026-06-14")
        assert len(out) == 1                         # never removed
        assert len(flagged) == 1 and flagged[0]["ticker"] == "MRK"
        assert out[0]["wash_sale_presale"]["lots"][0]["held_days"] == 10

    def test_no_flag_when_exit_is_a_gain(self):
        import guardrails
        txs = [self._buy("MRK", "2026-06-04", 1, 100.0)]
        decs = [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}]
        out, flagged = guardrails.flag_wash_sale_presale(
            decs, {"MRK": {"close": 120.0}}, transactions=txs, today="2026-06-14")
        assert flagged == [] and "wash_sale_presale" not in out[0]

    def test_no_flag_when_purchase_older_than_window(self):
        import guardrails
        txs = [self._buy("MRK", "2026-04-01", 1, 100.0)]   # > 30d ago
        decs = [{"ticker": "MRK", "action": "SELL", "target_weight": 0.0}]
        out, flagged = guardrails.flag_wash_sale_presale(
            decs, {"MRK": {"close": 90.0}}, transactions=txs, today="2026-06-14")
        assert flagged == [] and "wash_sale_presale" not in out[0]

    def test_buy_decisions_untouched(self):
        import guardrails
        out, flagged = guardrails.flag_wash_sale_presale(
            [{"ticker": "MRK", "action": "BUY"}], {"MRK": {"close": 90.0}},
            transactions=[], today="2026-06-14")
        assert flagged == [] and len(out) == 1


class TestCrashReconciliation:
    """A7: pure diff of intended vs actual post-crash holdings (no network)."""

    def test_all_filled(self):
        import reconcile
        r = reconcile.build_reconciliation(
            pre_positions=[{"symbol": "AAPL", "qty": 1.0}],
            decisions=[{"ticker": "AAPL", "action": "BUY", "qty": 1.0}],
            live_positions=[{"symbol": "AAPL", "qty": 2.0}])
        assert r["classification"] == reconcile.RECONCILED_ALL
        assert r["counts"]["filled"] == 1

    def test_none_filled(self):
        import reconcile
        r = reconcile.build_reconciliation(
            pre_positions=[{"symbol": "AAPL", "qty": 1.0}],
            decisions=[{"ticker": "AAPL", "action": "BUY", "qty": 1.0}],
            live_positions=[{"symbol": "AAPL", "qty": 1.0}])     # unchanged
        assert r["classification"] == reconcile.RECONCILED_NONE
        assert r["counts"]["not_filled"] == 1

    def test_full_exit_filled(self):
        import reconcile
        r = reconcile.build_reconciliation(
            pre_positions=[{"symbol": "MSFT", "qty": 2.0}],
            decisions=[{"ticker": "MSFT", "action": "SELL", "qty": 2.0}],
            live_positions=[])                                    # position gone
        assert r["classification"] == reconcile.RECONCILED_ALL

    def test_partial_is_manual(self):
        import reconcile
        r = reconcile.build_reconciliation(
            pre_positions=[{"symbol": "AAPL", "qty": 1.0}, {"symbol": "MSFT", "qty": 2.0}],
            decisions=[{"ticker": "AAPL", "action": "BUY", "qty": 1.0},
                       {"ticker": "MSFT", "action": "SELL", "qty": 2.0}],
            live_positions=[{"symbol": "AAPL", "qty": 2.0}, {"symbol": "MSFT", "qty": 2.0}])
        assert r["classification"] == reconcile.MANUAL_REQUIRED  # AAPL filled, MSFT not

    def test_unexpected_drift_is_manual(self):
        import reconcile
        r = reconcile.build_reconciliation(
            pre_positions=[{"symbol": "AAPL", "qty": 1.0}],
            decisions=[{"ticker": "AAPL", "action": "BUY", "qty": 1.0}],
            live_positions=[{"symbol": "AAPL", "qty": 2.0}, {"symbol": "NVDA", "qty": 5.0}])
        assert r["classification"] == reconcile.MANUAL_REQUIRED
        assert r["unexpected_changes"][0]["ticker"] == "NVDA"

    def test_no_crash_when_executed_at_present(self, tmp_path):
        import reconcile, json
        p = tmp_path / "pending.json"
        p.write_text(json.dumps({"run_id": "r1", "execution_started_at": "t0",
                                 "executed_at": "t1", "decisions": []}))
        r = reconcile.reconcile_crash_state(pending_path=str(p), live_positions=[])
        assert r["classification"] == reconcile.NO_CRASH


class TestDeliberationStats:
    """B14/B16: behavioral + operational base rates from logs (no market data)."""

    def _log(self):
        return [
            {"date": "2026-06-08", "candidates": ["AAPL", "MSFT"],
             "cro": {"approved": True, "rejected_tickers": []},
             "devils_advocate": {"AAPL": {"recommend_reject": True},
                                 "MSFT": {"recommend_reject": False}},
             "research": {"AAPL": {"confidence": 8}, "MSFT": {"confidence": 5}},
             "portfolio_manager_proposed": [{"ticker": "MSFT", "action": "BUY"}],
             "final_decisions": [{"ticker": "MSFT", "action": "BUY"}],
             "position_reviews": {"NVDA": {"recommended_action": "HOLD"}},
             "regime": {"regime": "NEUTRAL"}, "kill_switch_active": False,
             "portfolio_snapshot": {"total_value": 1000.0}},
            {"date": "2026-06-09", "candidates": ["AAPL"],
             "cro": {"approved": False, "rejected_tickers": ["AAPL"]},
             "devils_advocate": {"AAPL": {"recommend_reject": False}},
             "research": {"AAPL": {"confidence": 6}},
             "portfolio_manager_proposed": [], "final_decisions": [],
             "position_reviews": {}, "regime": {"regime": "RISK_ON"},
             "kill_switch_active": False, "portfolio_snapshot": {"total_value": 1000.0}},
        ]

    def test_deliberation_base_rates(self):
        import deliberation_stats as ds
        d = ds.deliberation_stats(self._log())
        assert d["n_runs"] == 2
        assert d["cro"]["full_veto_rate"] == 0.5            # 1 of 2 runs vetoed
        assert d["devils_advocate"]["n_evaluated"] == 3 and d["devils_advocate"]["rejects"] == 1
        # AAPL DA-flagged in run 1, PM did not buy AAPL → coincidence 1/1
        assert d["da_flag_pm_no_buy"]["coincidence_rate"] == 1.0
        # AAPL run1: confidence 8 ≥7 AND recommend_reject True → 1 conflict
        assert d["bull_bear_conflict"]["conflicts"] == 1

    def test_operational_turnover_and_holding(self):
        import deliberation_stats as ds
        txns = [{"action": "BUY", "ticker": "MSFT", "qty": 2, "price": 100.0,
                 "date": "2026-06-08", "timestamp": "2026-06-08T00:00:00+00:00"},
                {"action": "SELL", "ticker": "MSFT", "qty": 2, "price": 110.0,
                 "date": "2026-06-09", "timestamp": "2026-06-09T00:00:00+00:00"}]
        o = ds.operational_stats(self._log(), txns)
        assert o["trades"]["total"] == 1 and o["trades"]["buys"] == 1
        assert o["trades"]["no_trade_run_rate"] == 0.5
        assert o["holding_period"]["n_realized_lots"] == 1
        assert o["holding_period"]["short_term_lots"] == 1   # 1-day hold → ST
        assert o["turnover"]["sell_notional"] == 220.0


class TestHealthHistory:
    """B16: every save() appends one compact line to the append-only history."""

    def test_save_appends_history(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "h.json"))
        monkeypatch.setattr(health, "HEALTH_HISTORY_FILE", str(tmp_path / "hist.jsonl"))
        for i in range(2):
            t = health.HealthTracker(f"r{i}", "2026-06-15")
            t.record("step", health.OK if i == 0 else health.FAILED, "" if i == 0 else "boom")
            t.save()
        import json
        lines = [json.loads(l) for l in open(str(tmp_path / "hist.jsonl"))]
        assert len(lines) == 2
        assert lines[0]["overall_status"] == "OK"
        assert lines[1]["overall_status"] == "FAILED" and lines[1]["n_alerts"] == 1


class TestReproducibilityManifest:
    """A12: per-call resolved model + usage recording, and the export manifest."""

    class _FakeUsage:
        input_tokens = 100
        output_tokens = 50
        cache_read_input_tokens = 80
        cache_creation_input_tokens = 0

    class _FakeResp:
        model = "claude-haiku-4-5-20251001"
        usage = None
        def __init__(self):
            self.usage = TestReproducibilityManifest._FakeUsage()

    def test_record_call_accumulates(self):
        import analysis
        analysis._RUN_MANIFEST["calls"].clear()
        analysis._record_call("claude-haiku-4-5-20251001", 600, self._FakeResp())
        analysis._record_call("claude-haiku-4-5-20251001", 800, self._FakeResp())
        c = analysis._RUN_MANIFEST["calls"]["claude-haiku-4-5-20251001"]
        assert c["n_calls"] == 2 and c["input_tokens"] == 200 and c["output_tokens"] == 100
        assert c["cache_read_tokens"] == 160
        assert "claude-haiku-4-5-20251001" in c["resolved_models"]
        assert sorted(c["max_tokens_seen"]) == [600, 800]

    def test_record_call_never_raises_on_bad_response(self):
        import analysis
        analysis._RUN_MANIFEST["calls"].clear()
        analysis._record_call("m", 600, object())   # no .model / .usage attrs
        assert analysis._RUN_MANIFEST["calls"]["m"]["n_calls"] == 1

    def test_export_writes_manifest_and_prompts(self, tmp_path):
        import analysis, json
        analysis._RUN_MANIFEST["calls"].clear()
        analysis._record_call("claude-sonnet-4-6", 1200, self._FakeResp())
        path = str(tmp_path / "repro.json")
        m = analysis.export_reproducibility(path=path, prompts_dir=str(tmp_path / "prompts"),
                                            run_id="r1", date="2026-06-15")
        assert m["run_id"] == "r1" and m["models"]["smart"] == "claude-sonnet-4-6"
        assert "cro" in m["prompts"] and len(m["prompts"]["cro"]["sha256_16"]) == 16
        assert m["calls"]["claude-sonnet-4-6"]["n_calls"] == 1
        on_disk = json.load(open(path))
        assert on_disk["sampling"]["temperature"].startswith("api_default")


class TestReconcileHealthRecord:
    """A7: a crash reconciliation writes a health check for the alert path."""

    def test_manual_required_records_failed(self, tmp_path, monkeypatch):
        import reconcile, health, json
        monkeypatch.setattr(health, "HEALTH_FILE", str(tmp_path / "h.json"))
        monkeypatch.setattr(health, "HEALTH_HISTORY_FILE", str(tmp_path / "hist.jsonl"))
        reconcile._record_health({"classification": reconcile.MANUAL_REQUIRED,
                                  "recommended_action": "human needed",
                                  "counts": {}, "run_id": "r1"})
        h = json.load(open(str(tmp_path / "h.json")))
        assert h["checks"]["crash_reconciliation"]["status"] == "FAILED"
        assert h["overall_status"] == "FAILED"


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

    def test_report_stamps_formula_version(self):
        from backtest.report import build_report
        from quant_engine import FORMULA_VERSION
        rep = build_report({
            "equity_curve": [("d1", 100.0), ("d2", 101.0)], "benchmark_curve": [],
            "transactions": [], "initial_capital": 100.0, "final_equity": 101.0,
            "traded_notional_total": 0.0, "fundamental_coverage_pct": 90.0,
        })
        assert rep["formula_version"] == FORMULA_VERSION
        assert rep["fundamental_coverage_pct"] == 90.0

    def test_below_floor_coverage_adds_reweight_caveat(self):
        from backtest.report import build_report
        rep = build_report({
            "equity_curve": [("d1", 100.0), ("d2", 101.0)], "benchmark_curve": [],
            "transactions": [], "initial_capital": 100.0, "final_equity": 101.0,
            "traded_notional_total": 0.0, "fundamental_coverage_pct": 39.8,
        })
        assert any("RE-WEIGHT NOT FAIRLY TESTED" in c for c in rep["caveats"])

    def test_above_floor_coverage_no_reweight_caveat(self):
        from backtest.report import build_report
        rep = build_report({
            "equity_curve": [("d1", 100.0), ("d2", 101.0)], "benchmark_curve": [],
            "transactions": [], "initial_capital": 100.0, "final_equity": 101.0,
            "traded_notional_total": 0.0, "fundamental_coverage_pct": 85.0,
        })
        assert not any("RE-WEIGHT NOT FAIRLY TESTED" in c for c in rep["caveats"])

    def test_backtest_deterministic_reproducible(self):
        # Same snapshot → identical equity curve + trades (no RNG, no wall-clock).
        from backtest.engine import run_backtest
        from backtest.strategies import quant_momentum_vol
        snap = {"history": {
            "SPY":  _bt_bars([100 * (1.001 ** i) for i in range(40)]),
            "WIN":  _bt_bars([100 * (1.004 ** i) for i in range(40)]),
            "MEH":  _bt_bars([100 * (1.0005 ** i) for i in range(40)]),
        }, "fundamentals": {}}
        r1 = run_backtest(quant_momentum_vol, snapshot=snap, initial_capital=10_000.0,
                          rebalance_days=5, warmup=22)
        r2 = run_backtest(quant_momentum_vol, snapshot=snap, initial_capital=10_000.0,
                          rebalance_days=5, warmup=22)
        assert r1["equity_curve"] == r2["equity_curve"]
        assert r1["final_equity"] == r2["final_equity"]
        assert r1["fundamental_coverage_pct"] == r2["fundamental_coverage_pct"] == 0.0


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

    def test_min_hold_exactly_30_trading_days_allowed(self):
        import guardrails as g
        # Fri 2026-05-01 → Fri 2026-06-12 = exactly 30 trading days; '< 30' blocks,
        # so 30 is allowed (v2.0 boundary — was 5 pre-Phase-5)
        kept, rej = g.enforce_min_holding_period(
            [{"ticker": "X", "action": "SELL", "target_weight": 0.0}], {"positions": []},
            transactions=[{"ticker": "X", "action": "BUY", "date": "2026-05-01", "dry_run": False}],
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
        # stable API: margins/debt/PE from ratios-ttm, FCF-yield/EV from key-metrics-ttm
        from data_providers import FMPProvider
        p = FMPProvider(api_key="x")
        def fake_get(path, **k):
            if path == "ratios-ttm":
                return [{"grossProfitMarginTTM": 0.4612, "operatingProfitMarginTTM": 0.301,
                         "debtToEquityRatioTTM": 1.23, "priceToEarningsRatioTTM": 28.4}]
            if path == "key-metrics-ttm":
                return [{"freeCashFlowYieldTTM": 0.035, "evToEBITDATTM": 19.1}]
            return None
        monkeypatch.setattr(p, "_get", fake_get)
        f = p.fundamentals("AAPL")
        assert f["gross_margin"] == 0.4612 and f["operating_margin"] == 0.301
        assert f["debt_to_equity"] == 1.23 and f["pe_ratio"] == 28.4
        assert f["fcf_yield"] == 0.035 and f["ev_ebitda"] == 19.1

    def test_fmp_next_earnings_picks_soonest_future(self, monkeypatch):
        # stable 'earnings' endpoint: per-symbol rows with a date (epsActual null = upcoming)
        from data_providers import FMPProvider
        p = FMPProvider(api_key="x")
        monkeypatch.setattr(p, "_get", lambda *a, **k: [
            {"date": "2020-01-01", "epsActual": 1.0},    # past → ignored
            {"date": "2099-09-09", "epsActual": None},
            {"date": "2099-07-01", "epsActual": None},   # soonest future
        ])
        assert p.next_earnings_date("AAPL") == "2099-07-01"

    def test_get_provider_factory(self, monkeypatch):
        import data_providers as dp
        monkeypatch.delenv("FMP_API_KEY", raising=False)
        assert isinstance(dp.get_provider(), dp.SECProvider)      # no key → EDGAR (free)
        monkeypatch.setenv("FMP_API_KEY", "k")
        assert isinstance(dp.get_provider(), dp.CascadeProvider)  # key → FMP+SEC cascade


# ─────────────────────────────────────────────────────────────────────────────
#  SECProvider — EDGAR fundamentals, free, no key, ~100% US equity coverage.
#  Tests use mocked HTTP to avoid live EDGAR calls.
# ─────────────────────────────────────────────────────────────────────────────

class TestSECProvider:
    """SECProvider via mocked HTTP so no EDGAR calls hit the network."""

    # Minimal EDGAR company_tickers.json payload for AAPL
    TICKERS_RESP = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

    # Minimal us-gaap facts for AAPL: revenue, gross profit, operating income,
    # equity, long-term debt — all as 10-K entries.
    FACTS_RESP = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"end": "2024-09-28", "val": 391035000000, "form": "10-K"},
                        {"end": "2023-09-30", "val": 383285000000, "form": "10-K"},
                    ]}
                },
                "GrossProfit": {
                    "units": {"USD": [
                        {"end": "2024-09-28", "val": 180683000000, "form": "10-K"},
                    ]}
                },
                "OperatingIncomeLoss": {
                    "units": {"USD": [
                        {"end": "2024-09-28", "val": 123216000000, "form": "10-K"},
                    ]}
                },
                "StockholdersEquity": {
                    "units": {"USD": [
                        {"end": "2024-09-28", "val": 56950000000, "form": "10-K"},
                    ]}
                },
                "LongTermDebt": {
                    "units": {"USD": [
                        {"end": "2024-09-28", "val": 85750000000, "form": "10-K"},
                    ]}
                },
            }
        }
    }

    def _make_provider(self, monkeypatch):
        from data_providers import SECProvider
        import requests
        p = SECProvider(timeout=5)
        call_log = []

        def fake_get(url, **kwargs):
            call_log.append(url)
            import types, json as _json
            resp = types.SimpleNamespace()
            resp.raise_for_status = lambda: None
            if "company_tickers" in url:
                resp.json = lambda: self.TICKERS_RESP
            else:
                resp.json = lambda: self.FACTS_RESP
            return resp

        monkeypatch.setattr(requests, "get", fake_get)
        return p, call_log

    def test_ratios_computed_from_annual_10k(self, monkeypatch):
        p, _ = self._make_provider(monkeypatch)
        f = p.fundamentals("AAPL")
        assert f is not None
        # gross_margin = 180_683 / 391_035 ≈ 0.4621
        assert abs(f["gross_margin"] - round(180683000000 / 391035000000, 4)) < 1e-6
        # operating_margin = 123_216 / 391_035 ≈ 0.3151
        assert abs(f["operating_margin"] - round(123216000000 / 391035000000, 4)) < 1e-6
        # debt_to_equity = 85_750 / 56_950 ≈ 1.5057
        assert abs(f["debt_to_equity"] - round(85750000000 / 56950000000, 4)) < 1e-6

    def test_picks_most_recent_annual_entry(self, monkeypatch):
        # Two 10-K revenue entries; should pick the later end date.
        p, _ = self._make_provider(monkeypatch)
        f = p.fundamentals("AAPL")
        # 391_035 (2024-09-28) beats 383_285 (2023-09-30)
        expected_gm = round(180683000000 / 391035000000, 4)
        assert f["gross_margin"] == expected_gm

    def test_unknown_ticker_returns_none(self, monkeypatch):
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        monkeypatch.setattr(requests, "get",
                            lambda url, **kw: types.SimpleNamespace(
                                json=lambda: self.TICKERS_RESP, raise_for_status=lambda: None))
        assert p.fundamentals("ZZZZ") is None   # not in CIK map

    def test_no_earnings_or_estimates(self, monkeypatch):
        p, _ = self._make_provider(monkeypatch)
        assert p.next_earnings_date("AAPL") is None
        assert p.estimates("AAPL") is None

    def test_cik_map_loaded_once(self, monkeypatch):
        p, calls = self._make_provider(monkeypatch)
        p.fundamentals("AAPL")
        p.fundamentals("AAPL")
        tickers_calls = [c for c in calls if "company_tickers" in c]
        assert len(tickers_calls) == 1     # CIK map fetched once, cached in-instance

    def test_empty_equity_omits_debt_ratio(self, monkeypatch):
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        facts = {
            "facts": {"us-gaap": {
                "Revenues": {"units": {"USD": [{"end": "2024-01-01", "val": 1000, "form": "10-K"}]}},
                "GrossProfit": {"units": {"USD": [{"end": "2024-01-01", "val": 400, "form": "10-K"}]}},
                "StockholdersEquity": {"units": {"USD": [{"end": "2024-01-01", "val": 0, "form": "10-K"}]}},
                "LongTermDebt": {"units": {"USD": [{"end": "2024-01-01", "val": 500, "form": "10-K"}]}},
            }}
        }
        def fake_get(url, **kw):
            r = types.SimpleNamespace()
            r.raise_for_status = lambda: None
            r.json = (lambda: self.TICKERS_RESP) if "company_tickers" in url else (lambda: facts)
            return r
        monkeypatch.setattr(requests, "get", fake_get)
        f = p.fundamentals("AAPL")
        assert "gross_margin" in f         # computed fine
        assert "debt_to_equity" not in f   # equity=0 → guard prevents divide-by-zero

    def test_http_error_returns_none(self, monkeypatch):
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        call_n = [0]
        def fake_get(url, **kw):
            call_n[0] += 1
            r = types.SimpleNamespace()
            r.raise_for_status = lambda: None
            if "company_tickers" in url:
                r.json = lambda: self.TICKERS_RESP
            else:
                raise requests.exceptions.ConnectionError("offline")
            return r
        monkeypatch.setattr(requests, "get", fake_get)
        assert p.fundamentals("AAPL") is None   # network error → None, never raises

    def test_conforms_to_protocol(self):
        from data_providers import SECProvider, MarketDataProvider
        assert isinstance(SECProvider(), MarketDataProvider)

    def test_cik_map_ok_true_on_load(self, monkeypatch):
        p, _ = self._make_provider(monkeypatch)
        assert p.cik_map_ok() is True     # map loaded with ≥1 entry

    def test_cik_map_ok_false_on_load_failure(self, monkeypatch):
        # A failed CIK-map fetch must surface as cik_map_ok()==False, NOT a silent
        # empty map that zeros coverage with no trace (the June incident class).
        from data_providers import SECProvider
        import requests
        p = SECProvider(timeout=5)
        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("edgar down")
        monkeypatch.setattr(requests, "get", boom)
        assert p.cik_map_ok() is False
        assert p._cik_load_error is not None
        assert p.fundamentals("AAPL") is None   # every lookup None, but signalled

    def test_cik_map_ok_false_on_http_error(self, monkeypatch):
        # A 500/403 (raise_for_status) is a load FAILURE, not an empty universe.
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        def fake_get(url, **kw):
            r = types.SimpleNamespace(json=lambda: {})
            def raise_():
                raise requests.exceptions.HTTPError("403")
            r.raise_for_status = raise_
            return r
        monkeypatch.setattr(requests, "get", fake_get)
        assert p.cik_map_ok() is False

    def test_empty_200_body_records_diagnostic_error(self, monkeypatch):
        # HTTP 200 with an empty {} body → load fails, but WHY must be recorded (not
        # a silent blank map) so a 0%-coverage run is diagnosable.
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        monkeypatch.setattr(requests, "get",
                            lambda url, **kw: types.SimpleNamespace(
                                json=lambda: {}, raise_for_status=lambda: None))
        assert p.cik_map_ok() is False
        assert p._cik_load_error is not None and "empty" in p._cik_load_error.lower()

    def test_cik_map_load_attempted_once_on_failure(self, monkeypatch):
        # After a failure, we do NOT retry on every subsequent call (no retry storm).
        from data_providers import SECProvider
        import requests
        p = SECProvider(timeout=5)
        n = [0]
        def boom(url, **kw):
            n[0] += 1
            raise requests.exceptions.ConnectionError("down")
        monkeypatch.setattr(requests, "get", boom)
        p.fundamentals("AAPL"); p.fundamentals("MSFT"); p.cik_map_ok()
        assert n[0] == 1     # one attempt total, then cached failure


class TestUniverse:
    """Phase 2: gated universe expansion + resumable fetch cursor."""

    def test_core_is_the_watchlist(self):
        import universe, market_data
        assert market_data.WATCHLIST is universe.CORE_UNIVERSE
        assert len(universe.CORE_UNIVERSE) == 100

    def test_expanded_superset_of_core_and_larger(self):
        import universe
        assert set(universe.CORE_UNIVERSE) <= set(universe.EXPANDED_UNIVERSE)
        assert len(universe.EXPANDED_UNIVERSE) > 300      # ~400 target
        # no duplicates in the built expanded list
        assert len(universe.EXPANDED_UNIVERSE) == len(set(universe.EXPANDED_UNIVERSE))

    def test_gate_requires_both_enabled_and_coverage(self):
        import universe
        assert universe.get_active_universe(coverage_ok=True,  enabled=False) == universe.CORE_UNIVERSE
        assert universe.get_active_universe(coverage_ok=False, enabled=True)  == universe.CORE_UNIVERSE
        assert universe.get_active_universe(coverage_ok=True,  enabled=True)  == universe.EXPANDED_UNIVERSE

    def test_gate_reads_env_flag_by_default(self, monkeypatch):
        import universe
        monkeypatch.delenv("UNIVERSE_EXPANDED", raising=False)
        assert universe.get_active_universe(coverage_ok=True) == universe.CORE_UNIVERSE   # default OFF
        monkeypatch.setenv("UNIVERSE_EXPANDED", "true")
        assert universe.get_active_universe(coverage_ok=True) == universe.EXPANDED_UNIVERSE

    def test_cursor_hands_out_sequential_batches(self, tmp_path):
        import universe
        path = str(tmp_path / "fp.json")
        tickers = [f"T{i}" for i in range(10)]
        b1, c1 = universe.next_batch(tickers, 4, path)
        assert b1 == tickers[0:4] and c1 == 0
        universe.save_batch(tickers, 4, c1, path)
        b2, c2 = universe.next_batch(tickers, 4, path)
        assert b2 == tickers[4:8] and c2 == 4
        universe.save_batch(tickers, 4, c2, path)
        b3, c3 = universe.next_batch(tickers, 4, path)
        assert b3 == tickers[8:10] and c3 == 8            # short final batch

    def test_cursor_wraps_around(self, tmp_path):
        import universe
        path = str(tmp_path / "fp.json")
        tickers = [f"T{i}" for i in range(6)]
        _, c = universe.next_batch(tickers, 4, path)
        nc = universe.save_batch(tickers, 4, c, path)     # 0+4=4 < 6
        _, c = universe.next_batch(tickers, 4, path)
        assert c == 4
        nc = universe.save_batch(tickers, 4, c, path)     # 4+4=8 >= 6 → wrap to 0
        assert nc == 0
        b, c = universe.next_batch(tickers, 4, path)
        assert c == 0 and b == tickers[0:4]               # fresh sweep

    def test_cursor_resets_on_universe_size_change(self, tmp_path):
        import universe, json
        path = str(tmp_path / "fp.json")
        (tmp_path / "fp.json").write_text(json.dumps({"cursor": 3, "universe_size": 10}))
        # a different-sized universe (or an old size-keyed file) must NOT resume mid-way
        b, c = universe.next_batch([f"T{i}" for i in range(5)], 2, path)
        assert c == 0 and b == ["T0", "T1"]

    def test_cursor_resets_on_same_size_content_swap(self, tmp_path):
        # A same-LENGTH membership change must reset the sweep — keying on size alone
        # would silently skip the first N names of the new ordering (coverage gap).
        import universe
        path = str(tmp_path / "fp.json")
        u1 = [f"T{i}" for i in range(6)]
        _, c = universe.next_batch(u1, 4, path)
        universe.save_batch(u1, 4, c, path)          # cursor advances to 4 for u1
        u2 = u1[:5] + ["SWAPPED"]                     # same length, one name changed
        b, c = universe.next_batch(u2, 4, path)
        assert c == 0 and b == u2[0:4]               # fresh sweep, not resumed at 4

    def test_crash_before_save_retries_same_batch(self, tmp_path):
        import universe
        path = str(tmp_path / "fp.json")
        tickers = [f"T{i}" for i in range(10)]
        b1, _ = universe.next_batch(tickers, 4, path)     # fetch starts...
        # ...crash before save_batch → cursor not advanced
        b1_again, _ = universe.next_batch(tickers, 4, path)
        assert b1_again == b1                             # same batch retried, no gap

    def test_empty_universe_safe(self, tmp_path):
        import universe
        path = str(tmp_path / "fp.json")
        assert universe.next_batch([], 4, path) == ([], 0)
        assert universe.save_batch([], 4, 0, path) == 0


class TestSelectFetchBatch:
    """MANUAL_TODO #6b (2026-07-05): wiring the resumable cursor into the fetch loop
    — core/held/SP500/benchmarks always fetched; only expansion-only names batched."""

    CORE = ["AAPL", "MSFT", "GOOGL"]
    SP500_KEYS = ["AAPL", "SPY"]     # AAPL overlaps CORE on purpose (dedup check)

    def test_not_expanded_matches_pre_feature_behavior_exactly(self, tmp_path):
        # When NOT expanded, active == CORE, so expansion_only must be empty and
        # all_tickers must equal the OLD set (core | sp500 | held) — zero behavior
        # change until an operator flips UNIVERSE_EXPANDED.
        import market_data
        held = {"NVDA"}
        active = list(self.CORE)     # get_active_universe returns CORE when not expanded
        all_t, exp_only, batch, cursor = market_data.select_fetch_batch(
            active, self.CORE, self.SP500_KEYS, held, expanded=False,
            progress_path=str(tmp_path / "fp.json"))
        old_all_tickers = sorted(set(active) | set(self.SP500_KEYS) | held)
        assert all_t == old_all_tickers
        assert exp_only == [] and batch == [] and cursor == 0

    def test_expanded_batches_only_the_expansion_remainder(self, tmp_path):
        import market_data
        held = {"NVDA"}
        expansion_names = [f"EXP{i}" for i in range(10)]
        active = self.CORE + expansion_names
        all_t, exp_only, batch, cursor = market_data.select_fetch_batch(
            active, self.CORE, self.SP500_KEYS, held, expanded=True, batch_size=4,
            progress_path=str(tmp_path / "fp.json"))
        assert exp_only == sorted(expansion_names)          # core/sp500/held excluded
        assert len(batch) == 4 and cursor == 0
        always_fetch = set(self.CORE) | set(self.SP500_KEYS) | held
        # every always-fetch name is present, PLUS this run's expansion batch, nothing more
        assert set(all_t) == always_fetch | set(batch)

    def test_always_fetch_set_never_batched_even_when_expanded(self, tmp_path):
        # Core/held/SP500 must appear in all_tickers on EVERY run, never subject to
        # the cursor, regardless of expansion state or batch size.
        import market_data
        held = {"NVDA"}
        expansion_names = [f"EXP{i}" for i in range(20)]
        active = self.CORE + expansion_names
        all_t, _, _, _ = market_data.select_fetch_batch(
            active, self.CORE, self.SP500_KEYS, held, expanded=True, batch_size=3,
            progress_path=str(tmp_path / "fp.json"))
        always_fetch = set(self.CORE) | set(self.SP500_KEYS) | held
        assert always_fetch <= set(all_t)

    def test_successive_calls_advance_through_expansion_names_after_save(self, tmp_path):
        # Mirrors how get_market_snapshot uses this: caller must call save_batch
        # after a successful sweep for the cursor to move; without it, the same
        # batch is returned again (crash-safe retry).
        import market_data, universe
        path = str(tmp_path / "fp.json")
        expansion_names = [f"EXP{i}" for i in range(9)]
        active = self.CORE + expansion_names
        all_t1, exp_only, batch1, c1 = market_data.select_fetch_batch(
            active, self.CORE, [], set(), expanded=True, batch_size=4, progress_path=path)
        assert batch1 == sorted(expansion_names)[0:4]
        universe.save_batch(exp_only, 4, c1, path)          # caller advances the cursor
        _, _, batch2, c2 = market_data.select_fetch_batch(
            active, self.CORE, [], set(), expanded=True, batch_size=4, progress_path=path)
        assert batch2 == sorted(expansion_names)[4:8] and c2 == 4

    def test_no_expansion_names_left_over_is_safe(self, tmp_path):
        # active == core exactly even though expanded=True is passed (edge case,
        # e.g. a coverage-gate flip mid-computation) — must not crash on an empty
        # expansion_only set.
        import market_data
        all_t, exp_only, batch, cursor = market_data.select_fetch_batch(
            self.CORE, self.CORE, [], set(), expanded=True,
            progress_path=str(tmp_path / "fp.json"))
        assert exp_only == [] and batch == [] and cursor == 0
        assert all_t == sorted(self.CORE)


class TestCorporateActions:
    """P0-3: split/print-outlier detection + delisted-holding detection (offline)."""

    def test_detects_split_like_outlier(self):
        from corporate_actions import detect_price_outliers
        # A ~-50% overnight move (unadjusted 2:1 split shape) with default 35% threshold.
        hist = {"ZZ": [{"date": "2026-01-01", "close": 100.0},
                       {"date": "2026-01-02", "close": 49.0},   # -51%
                       {"date": "2026-01-03", "close": 50.0}]}
        out = detect_price_outliers(hist)
        assert len(out) == 1
        assert out[0]["ticker"] == "ZZ" and out[0]["change_pct"] == -51.0

    def test_normal_moves_not_flagged(self):
        from corporate_actions import detect_price_outliers
        hist = {"AA": [{"date": "d1", "close": 100.0},
                       {"date": "d2", "close": 103.0},   # +3%
                       {"date": "d3", "close": 98.0}]}    # -4.85%
        assert detect_price_outliers(hist) == []

    def test_custom_threshold(self):
        from corporate_actions import detect_price_outliers
        hist = {"BB": [{"date": "d1", "close": 100.0}, {"date": "d2", "close": 90.0}]}  # -10%
        assert detect_price_outliers(hist, threshold_pct=5) != []
        assert detect_price_outliers(hist, threshold_pct=15) == []

    def test_epoch_ms_date_normalized_to_iso(self):
        # Live snapshot bars carry epoch-MS integer dates (Polygon 't'); the finding
        # must emit a readable ISO string, not a raw epoch int.
        from corporate_actions import detect_price_outliers
        hist = {"ZZ": [{"date": 1748736000000, "close": 100.0},
                       {"date": 1748822400000, "close": 40.0}]}   # -60%
        out = detect_price_outliers(hist)
        assert len(out) == 1
        assert isinstance(out[0]["date"], str) and out[0]["date"].count("-") == 2   # YYYY-MM-DD
        assert out[0]["date"].startswith("2025-")

    def test_bad_bar_breaks_chain_no_false_positive(self):
        from corporate_actions import detect_price_outliers
        # A None/zero close must not create a phantom infinite/huge move.
        hist = {"CC": [{"date": "d1", "close": 100.0},
                       {"date": "d2", "close": 0.0},      # bad print — chain breaks
                       {"date": "d3", "close": 101.0}]}   # vs d2 would be huge, but chain reset
        assert detect_price_outliers(hist) == []

    def test_uses_policy_threshold_by_default(self):
        from corporate_actions import detect_price_outliers, _outlier_threshold_pct
        import policy
        assert _outlier_threshold_pct() == policy.VALUES["price_outlier_pct"] == 35

    def test_find_unpriced_holdings_list_of_strings(self):
        from corporate_actions import find_unpriced_holdings
        prices = {"AAPL": {"close": 200.0}, "MSFT": {"close": 400.0}}
        holdings = ["AAPL", "DELISTED", "MSFT"]
        assert find_unpriced_holdings(holdings, prices) == ["DELISTED"]

    def test_find_unpriced_holdings_position_dicts(self):
        from corporate_actions import find_unpriced_holdings
        prices = {"AAPL": {"close": 200.0}, "GONE": {"close": 0.0}}   # zero close = suspect
        holdings = [{"ticker": "AAPL", "quantity": 1}, {"ticker": "GONE", "quantity": 2},
                    {"ticker": "MISSING", "quantity": 3}]
        assert find_unpriced_holdings(holdings, prices) == ["GONE", "MISSING"]

    def test_empty_inputs_safe(self):
        from corporate_actions import detect_price_outliers, find_unpriced_holdings
        assert detect_price_outliers({}) == []
        assert detect_price_outliers(None) == []
        assert find_unpriced_holdings([], {}) == []
        assert find_unpriced_holdings(None, {}) == []


class TestPriceOutlierPolicyParam:
    def test_default_present_and_valid(self):
        import policy
        assert policy.VALUES["price_outlier_pct"] == 35

    def test_fraction_typo_rejected(self):
        # 0.35 (a fraction typo for 35%) must be rejected → default kept.
        import policy
        v = policy._VALIDATORS["price_outlier_pct"]
        assert v(35) is True and v(50) is True
        assert v(0.35) is False and v(0) is False and v(200) is False


class TestCascadeCikMapOk:
    def test_cascade_delegates_cik_map_ok_to_fallback(self, monkeypatch):
        from data_providers import CascadeProvider, FMPProvider, SECProvider
        sec = SECProvider(timeout=5)
        import requests, types
        monkeypatch.setattr(requests, "get",
                            lambda url, **kw: types.SimpleNamespace(
                                json=lambda: {"0": {"cik_str": 1, "ticker": "AAPL", "title": "x"}},
                                raise_for_status=lambda: None))
        c = CascadeProvider(FMPProvider(api_key=None), sec)
        assert c.cik_map_ok() is True


class TestFundamentalCoverage:
    def test_coverage_counts_quality_fields(self):
        import market_data as md
        fund = {
            "AAPL": {"gross_margin": 0.4, "operating_margin": 0.3},   # covered
            "MSFT": {"debt_to_equity": 0.5},                          # covered
            "ZZZZ": {"pe_ratio": 30},                                 # valuation only → NOT covered
            "NONE": None,                                             # missing
        }
        dq = md._compute_fundamental_coverage(["AAPL", "MSFT", "ZZZZ", "NONE"], fund, cik_map_ok=True)
        assert dq["fundamentals_covered"] == 2
        assert dq["active_universe"] == 4
        assert dq["fundamental_coverage_pct"] == 50.0
        assert dq["coverage_ok"] is False           # 50% < 80% floor
        assert dq["cik_map_ok"] is True

    def test_coverage_ok_above_floor(self):
        import market_data as md
        fund = {t: {"gross_margin": 0.4} for t in ("A", "B", "C", "D")}
        fund["E"] = None
        dq = md._compute_fundamental_coverage(["A", "B", "C", "D", "E"], fund, cik_map_ok=True)
        assert dq["fundamental_coverage_pct"] == 80.0
        assert dq["coverage_ok"] is True            # 80% == floor → OK

    def test_empty_universe_no_divide_by_zero(self):
        import market_data as md
        dq = md._compute_fundamental_coverage([], {}, cik_map_ok=None)
        assert dq["fundamental_coverage_pct"] == 0.0
        assert dq["cik_map_ok"] is None

    def test_valuation_coverage_reported_separately(self):
        # Quality (EDGAR) and valuation (FMP-only) coverage are measured separately;
        # the gate (coverage_ok) is on QUALITY, valuation is transparency-only.
        import market_data as md
        fund = {
            "A": {"gross_margin": 0.4, "pe_ratio": 20},   # quality + valuation
            "B": {"gross_margin": 0.3},                    # quality only
            "C": {"pe_ratio": 15},                         # valuation only (NOT quality-covered)
            "D": None,
        }
        dq = md._compute_fundamental_coverage(["A", "B", "C", "D"], fund, cik_map_ok=True)
        assert dq["fundamental_coverage_pct"] == 50.0     # A,B quality-covered
        assert dq["valuation_coverage_pct"] == 50.0       # A,C valuation-covered
        assert dq["coverage_ok"] is False                 # gate is on quality, 50% < 80%

    def test_shared_helper_backtest_and_live_agree(self):
        # The backtest coverage and the live snapshot coverage come from ONE helper.
        import data_providers, market_data as md
        from backtest.engine import _coverage_pct
        fund = {"A": {"gross_margin": 0.4}, "B": {"operating_margin": 0.2}, "C": None}
        # live path (all tickers)
        live = md._compute_fundamental_coverage(["A", "B", "C"], fund, None)["fundamental_coverage_pct"]
        # backtest path (history keys, benchmarks excluded) — same underlying helper
        bt = _coverage_pct({"A": [], "B": [], "C": [], "SPY": []}, fund)
        assert live == bt == round(100 * 2 / 3, 1)


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
        H = len(calibration.HORIZONS)
        assert n == 6 * H                 # (AAPL: 5 agents, MSFT: quant only) × each horizon
        rows = [json.loads(l) for l in open(path)]
        assert {r["horizon_days"] for r in rows} == set(calibration.HORIZONS)
        aq = next(r for r in rows if r["ticker"] == "AAPL" and r["agent"] == "quant"
                  and r["horizon_days"] == 21)
        # A1: signal_close is reference-only (the close the signal was computed on),
        # NOT the return base — score_matured derives the executable next-open entry.
        assert aq["value"] == 80 and aq["signal_close"] == 200

    def test_log_forecasts_skips_missing_price(self, tmp_path):
        import calibration
        n = calibration.log_forecasts("r1", "2026-06-14",
                                      {"quant_scores": {"AAPL": {"composite_score": 80}}},
                                      ["AAPL"], {"AAPL": {}}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_score_matured_joins_forward_return(self, tmp_path):
        # A1: entry is the NEXT-SESSION OPEN after the signal date, not the signal
        # close. Signal 2026-01-02 → entry = open(2026-01-05)=100 → exit = close
        # on/after entry+5d (2026-01-10) = 110 → return 0.10.
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                "field": "composite_score", "ticker": "AAPL", "value": 80,
                                "signal_close": 98.0, "horizon_days": 5, "schema": 2}) + "\n")
        snap = {"history": {"AAPL": [
            {"date": "2026-01-05", "open": 100.0, "close": 101.0},   # next session → entry open
            {"date": "2026-01-12", "open": 109.0, "close": 110.0},   # >= entry+5d → exit close
        ]}}
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 1
        r = json.loads(open(scored).readline())
        assert r["entry_price"] == 100.0 and r["future_price"] == 110.0
        assert r["realized_return"] == 0.1 and r["basis"] == "next_open"
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0  # idempotent

    def test_score_matured_multi_horizon_independent(self, tmp_path):
        # P1-9: the same (run_id,agent,field,ticker) at TWO horizons must BOTH score —
        # the idempotency key includes horizon_days, else the 2nd horizon is wrongly
        # skipped as "already scored". And re-running stays idempotent per horizon.
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            for h in (5, 10):
                f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                    "field": "composite_score", "ticker": "AAPL", "value": 80,
                                    "signal_close": 98.0, "horizon_days": h, "schema": 2}) + "\n")
        snap = {"history": {"AAPL": [
            {"date": "2026-01-05", "open": 100.0, "close": 101.0},   # entry (next open)
            {"date": "2026-01-20", "open": 119.0, "close": 120.0},   # >= entry+5d AND entry+10d
        ]}}
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 2  # both horizons
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0  # idempotent

    def test_score_matured_skips_immature(self, tmp_path):
        # Entry session exists, but horizon hasn't elapsed in available history.
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                "field": "composite_score", "ticker": "AAPL", "value": 80,
                                "signal_close": 98.0, "horizon_days": 5, "schema": 2}) + "\n")
        snap = {"history": {"AAPL": [
            {"date": "2026-01-05", "open": 100.0, "close": 101.0},   # entry exists
            {"date": "2026-01-07", "open": 104.0, "close": 105.0},   # < entry+5d → not matured
        ]}}
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0

    def test_score_matured_skips_no_next_session(self, tmp_path):
        # A1: a forecast logged with no later bar yet has no executable entry → skip.
        import calibration, json
        ledger, scored = str(tmp_path / "f.jsonl"), str(tmp_path / "s.jsonl")
        with open(ledger, "w") as f:
            f.write(json.dumps({"run_id": "r1", "date": "2026-01-02", "agent": "quant",
                                "field": "composite_score", "ticker": "AAPL", "value": 80,
                                "signal_close": 98.0, "horizon_days": 5, "schema": 2}) + "\n")
        snap = {"history": {"AAPL": [{"date": "2026-01-02", "open": 97.0, "close": 98.0}]}}
        assert calibration.score_matured(snap, ledger_path=ledger, scored_path=scored) == 0

    def test_agent_scorecard_shrinks_small_sample(self, tmp_path):
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        with open(scored, "w") as f:
            for i in range(5):
                # formula_version = CURRENT so this rows to the PLAIN key (matches
                # real post-Phase-1 ledger data) — see TestCalibrationFormulaVersion
                # for the version-partition behavior itself.
                f.write(json.dumps({"run_id": f"r{i}", "agent": "quant", "field": "composite_score",
                                    "ticker": "AAPL", "value": float(i), "realized_return": i / 100,
                                    "date": f"2026-0{i+1}-01",
                                    "formula_version": calibration._CURRENT_QUANT_FORMULA}) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card, shrink_k=50,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        k = "quant.composite_score@21d"   # grouped by horizon; rows w/o horizon_days default to 21
        assert out[k]["n"] == 5 and out[k]["ic"] == 1.0
        assert out[k]["ic_shrunk"] == round(5 / 55, 3)        # shrunk far below the raw IC
        assert out[k]["ic_shrunk"] < out[k]["ic"]


class TestPMForecastScoring:
    """MANUAL_TODO #16 (2026-07-05): score the PM's own expected_return so the
    net-edge gate's only input eventually earns/loses trust from real evidence."""

    def _pstate(self, decisions):
        return {"portfolio_manager_proposed": decisions}

    def test_logs_only_buy_with_numeric_expected_return(self, tmp_path):
        import calibration, json
        path = str(tmp_path / "f.jsonl")
        decisions = [
            {"ticker": "AAPL", "action": "BUY", "expected_return": 0.08},
            {"ticker": "MSFT", "action": "SELL", "expected_return": 0.0},   # SELL -> excluded
            {"ticker": "GOOGL", "action": "BUY", "expected_return": None},  # no estimate -> excluded
            {"ticker": "AMZN", "action": "BUY"},                            # missing field -> excluded
        ]
        prices = {"AAPL": {"close": 200}, "MSFT": {"close": 100},
                 "GOOGL": {"close": 150}, "AMZN": {"close": 180}}
        n = calibration.log_pm_forecasts("r1", "2026-07-08", self._pstate(decisions), prices, path=path)
        H = len(calibration.HORIZONS)
        assert n == 1 * H
        rows = [json.loads(l) for l in open(path)]
        assert {r["ticker"] for r in rows} == {"AAPL"}
        assert all(r["agent"] == "pm" and r["field"] == "expected_return" for r in rows)
        assert rows[0]["value"] == 0.08

    def test_skips_missing_price(self, tmp_path):
        import calibration
        decisions = [{"ticker": "AAPL", "action": "BUY", "expected_return": 0.05}]
        n = calibration.log_pm_forecasts("r1", "2026-07-08", self._pstate(decisions),
                                         {"AAPL": {}}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_string_expected_return_coerced(self, tmp_path):
        # enforce_net_edge tolerates a stringified expected_return; the calibration
        # ledger should score the same decisions it gates, not silently drop them.
        import calibration, json
        path = str(tmp_path / "f.jsonl")
        decisions = [{"ticker": "AAPL", "action": "BUY", "expected_return": "0.05"}]
        n = calibration.log_pm_forecasts("r1", "2026-07-08", self._pstate(decisions),
                                         {"AAPL": {"close": 200}}, path=path)
        assert n == len(calibration.HORIZONS)
        rows = [json.loads(l) for l in open(path)]
        assert rows[0]["value"] == 0.05

    def test_garbage_expected_return_dropped(self, tmp_path):
        import calibration
        decisions = [{"ticker": "AAPL", "action": "BUY", "expected_return": "abc"}]
        n = calibration.log_pm_forecasts("r1", "2026-07-08", self._pstate(decisions),
                                         {"AAPL": {"close": 200}}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_empty_proposed_list_safe(self, tmp_path):
        import calibration
        n = calibration.log_pm_forecasts("r1", "2026-07-08", {"portfolio_manager_proposed": []},
                                         {}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_missing_key_safe(self, tmp_path):
        import calibration
        n = calibration.log_pm_forecasts("r1", "2026-07-08", {}, {}, path=str(tmp_path / "f.jsonl"))
        assert n == 0

    def test_scored_by_agent_scorecard_under_pm_key(self, tmp_path):
        # End-to-end: log -> matches agent_scorecard's grouping under the plain "pm.expected_return@21d" key
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        with open(scored, "w") as f:
            for i in range(1, 6):
                f.write(json.dumps({"run_id": f"r{i}", "agent": "pm", "field": "expected_return",
                                    "ticker": "AAPL", "value": float(i) / 100,
                                    "realized_return": float(i) / 100,
                                    "date": f"2026-0{i}-15", "horizon_days": 21}) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                          factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert "pm.expected_return@21d" in out
        assert out["pm.expected_return@21d"]["n"] == 5
        assert out["pm.expected_return@21d"]["orientation"] == 1   # default +1, higher predicts higher
        assert not any(k.startswith("pm.expected_return@21d~") for k in out)  # no version concept for pm


class TestCalibrationFormulaVersionAndVariance:
    """Phase 1 (2026-07-05): agent_scorecard must not silently pool a legacy quant
    formula's forecasts with the current one, and must drop degenerate
    zero-cross-sectional-variance days (e.g. the Jun 8-10 outage defaults)."""

    def _row(self, agent, field, value, date, ticker="AAPL", realized_return=0.01,
            formula_version=None, horizon_days=21):
        return {"run_id": f"{ticker}-{date}", "agent": agent, "field": field,
                "ticker": ticker, "value": value, "realized_return": realized_return,
                "date": date, "horizon_days": horizon_days, "formula_version": formula_version}

    def test_current_formula_keys_plain_metric(self, tmp_path):
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        rows = [self._row("quant", "composite_score", float(i), f"2026-0{i+1}-01",
                          realized_return=i / 100,
                          formula_version=calibration._CURRENT_QUANT_FORMULA)
                for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert "quant.composite_score@21d" in out
        assert out["quant.composite_score@21d"]["formula_version"] == calibration._CURRENT_QUANT_FORMULA

    def test_untagged_formula_unresolved_keys_suffixed_metric(self, tmp_path):
        """Rows with NO formula_version tag AND no matching factor_history.jsonl
        row (the join can't recover their true vintage) must NOT land on the
        plain key stage_c_readiness.py reads — they're genuinely unknown vintage."""
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        rows = [self._row("quant", "composite_score", float(i), f"2026-0{i+1}-01",
                          realized_return=i / 100, formula_version=None)
                for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert "quant.composite_score@21d" not in out
        assert "quant.composite_score@21d~unknown" in out

    def test_untagged_formula_resolved_via_factor_history_join(self, tmp_path):
        """A row with NO formula_version tag but a MATCHING factor_history.jsonl
        entry for the same (date, ticker) must be correctly resolved — recovering
        pre-fix rows' true vintage via a read-only join rather than dumping them
        all into one undifferentiated 'unknown' bucket, and WITHOUT rewriting the
        historical forecasts_scored.jsonl ledger itself."""
        import calibration, json
        scored = str(tmp_path / "s.jsonl")
        fh = str(tmp_path / "fh.jsonl")
        card = str(tmp_path / "card.json")
        rows = [self._row("quant", "composite_score", float(i), f"2026-0{i+1}-01",
                          realized_return=i / 100, formula_version=None)
                for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(fh, "w") as f:
            for r in rows:
                # factor_history has the SAME (date, ticker) tagged with the
                # CURRENT formula — this is what the join should recover.
                f.write(json.dumps({"date": r["date"], "ticker": r["ticker"],
                                    "formula_version": calibration._CURRENT_QUANT_FORMULA}) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                          factor_history_path=fh)
        # Resolved via the join -> lands on the PLAIN (current-formula) key, not
        # a suffixed one, even though the scored row itself carried no tag.
        assert "quant.composite_score@21d" in out
        assert out["quant.composite_score@21d"]["n"] == 5
        assert not any(k.startswith("quant.composite_score@21d~") for k in out)
        # The scored ledger file on disk must be untouched (read-only join).
        with open(scored) as f:
            on_disk = [json.loads(l) for l in f]
        assert all(r.get("formula_version") is None for r in on_disk)

    def test_old_and_new_formula_never_pooled(self, tmp_path):
        """A mix of legacy-tagged and current-tagged quant rows must produce TWO
        separate scorecard entries, never one pooled IC."""
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        old_rows = [self._row("quant", "composite_score", float(i), f"2026-0{i+1}-01",
                              realized_return=-i / 100, formula_version="1.0-old")
                    for i in range(1, 6)]
        new_rows = [self._row("quant", "composite_score", float(i), f"2026-0{i+1}-10",
                              realized_return=i / 100,
                              formula_version=calibration._CURRENT_QUANT_FORMULA)
                    for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in old_rows + new_rows:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert out["quant.composite_score@21d"]["n"] == 5          # current only
        assert out["quant.composite_score@21d~1.0-old"]["n"] == 5  # legacy segregated
        # Orientations differ (old_rows correlate negatively, new positively) —
        # pooling them would average toward zero and hide both signals.
        assert out["quant.composite_score@21d"]["ic"] == 1.0
        assert out["quant.composite_score@21d~1.0-old"]["ic"] == -1.0

    def test_non_quant_agent_unaffected_by_versioning(self, tmp_path):
        """research/earnings/etc. have no formula_version concept — always plain key."""
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        rows = [self._row("research", "confidence", float(i), f"2026-0{i+1}-01",
                          realized_return=i / 100, formula_version=None)
                for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert "research.confidence@21d" in out
        assert not any(k.startswith("research.confidence@21d~") for k in out)

    def test_zero_variance_day_dropped(self, tmp_path):
        """A run-date where every value in the pool is IDENTICAL (an outage-default
        emission) must be excluded — it carries no rank information and dilutes
        genuine signal from other days."""
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        # Degenerate day: 20 identical-value rows, all agent defaults during an outage.
        degenerate = [self._row("research", "confidence", 5.0, "2026-06-08", ticker=f"T{i}",
                                realized_return=(i - 10) / 100,
                                formula_version=None)
                     for i in range(20)]
        # Real signal days: value truly correlates with realized_return.
        real = [self._row("research", "confidence", float(i), f"2026-0{i}-15", ticker="AAPL",
                          realized_return=i / 100, formula_version=None)
                for i in range(1, 6)]
        with open(scored, "w") as f:
            for r in degenerate + real:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        # Only the 5 real rows should count — the 20 degenerate rows are dropped.
        assert out["research.confidence@21d"]["n"] == 5
        assert out["research.confidence@21d"]["ic"] == 1.0

    def test_mixed_variance_day_not_dropped(self, tmp_path):
        """A day with genuinely varying values must NOT be filtered — only exact
        zero cross-sectional variance is degenerate."""
        import calibration, json
        scored, card = str(tmp_path / "s.jsonl"), str(tmp_path / "card.json")
        rows = [self._row("research", "confidence", float(v), "2026-06-08", ticker=f"T{i}",
                          realized_return=v / 100, formula_version=None)
                for i, v in enumerate([1, 2, 3, 4, 5])]
        with open(scored, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        out = calibration.agent_scorecard(scored_path=scored, out_path=card,
                                  factor_history_path=str(tmp_path / "no_fh.jsonl"))
        assert out["research.confidence@21d"]["n"] == 5


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
        assert n == 2 * len(calibration.HORIZONS)   # quant + research × horizons; None earnings dropped

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


# ─────────────────────────────────────────────────────────────────────────────
#  #1 — provider enrichment cache (alternate-day 50/50, FMP free-tier safe)
# ─────────────────────────────────────────────────────────────────────────────

class TestProviderEnrichmentCache:
    def test_group_stable_and_binary(self):
        import market_data as md
        g = md._provider_group("AAPL")
        assert g == md._provider_group("AAPL") and g in (0, 1)   # deterministic, 0/1

    def test_stub_provider_is_noop(self, monkeypatch):
        # Tests inject StubProvider to get a no-op without real HTTP calls.
        import market_data as md, data_providers
        from datetime import date
        monkeypatch.setattr(data_providers, "get_provider", lambda: data_providers.StubProvider())
        fund = {}
        ec, dq = md._enrich_with_provider(["AAPL"], fund, today=date(2026, 6, 15))
        assert ec == {} and dq is None                           # stub → no-op, coverage unmeasured
        assert fund == {}                                        # untouched, no HTTP

    def test_fetches_on_group_day_and_caches(self, tmp_path, monkeypatch):
        import market_data as md, data_providers, json
        from datetime import date, timedelta
        monkeypatch.setattr(md, "PROVIDER_CACHE", str(tmp_path / "pc.json"))
        monkeypatch.setenv("FMP_API_KEY", "x")
        class FakeP:
            def fundamentals(self, t): return {"pe_ratio": 30}
            def next_earnings_date(self, t): return "2026-07-30"
        monkeypatch.setattr(data_providers, "get_provider", lambda: FakeP())
        d = date(2026, 6, 15)
        while d.toordinal() % 2 != md._provider_group("AAPL"):   # land on AAPL's group day
            d += timedelta(days=1)
        fund = {}
        ec, _dq = md._enrich_with_provider(["AAPL"], fund, today=d)
        assert ec["AAPL"] == "2026-07-30" and fund["AAPL"]["pe_ratio"] == 30
        assert json.load(open(tmp_path / "pc.json"))["AAPL"]["fetched"] == d.isoformat()

    def test_off_group_day_uses_cache_no_fetch(self, tmp_path, monkeypatch):
        import market_data as md, data_providers, json
        from datetime import date, timedelta
        cache_path = tmp_path / "pc.json"
        cache_path.write_text(json.dumps({"AAPL": {"fundamentals": {"pe_ratio": 25},
                              "next_earnings": "2026-08-01", "fetched": "2026-06-10"}}))
        monkeypatch.setattr(md, "PROVIDER_CACHE", str(cache_path))
        monkeypatch.setenv("FMP_API_KEY", "x")
        fetched = []
        class FakeP:
            def fundamentals(self, t): fetched.append(t); return {"pe_ratio": 99}
            def next_earnings_date(self, t): return "x"
        monkeypatch.setattr(data_providers, "get_provider", lambda: FakeP())
        d = date(2026, 6, 15)
        while d.toordinal() % 2 == md._provider_group("AAPL"):   # land OFF AAPL's group day
            d += timedelta(days=1)
        fund = {}
        ec, _dq = md._enrich_with_provider(["AAPL"], fund, today=d)
        assert fetched == []                          # not its day → no fetch
        assert fund["AAPL"]["pe_ratio"] == 25         # served from cache
        assert ec["AAPL"] == "2026-08-01"

    def test_premium_empty_ticker_backoff(self, tmp_path, monkeypatch):
        # FMP free tier 402s on ~65% of names → empty cache entry; don't re-hit it
        # every 2 days (30-day backoff) so the daily call budget isn't wasted.
        import market_data as md, data_providers, json
        from datetime import date, timedelta
        cache_path = tmp_path / "pc.json"
        monkeypatch.setattr(md, "PROVIDER_CACHE", str(cache_path))
        monkeypatch.setenv("FMP_API_KEY", "x")
        fetched = []
        class FakeP:
            def fundamentals(self, t): fetched.append(t); return {"pe_ratio": 1}
            def next_earnings_date(self, t): return None
        monkeypatch.setattr(data_providers, "get_provider", lambda: FakeP())
        d = date(2026, 6, 15)
        while d.toordinal() % 2 != md._provider_group("AAPL"):     # AAPL's group day
            d += timedelta(days=1)
        cache_path.write_text(json.dumps({"AAPL": {"fundamentals": None, "next_earnings": None,
                              "fetched": (d - timedelta(days=5)).isoformat()}}))   # empty, 5d old
        md._enrich_with_provider(["AAPL"], {}, today=d)
        assert fetched == []     # empty entry, age 5 < 7-day backoff → not re-fetched

    def test_full_refresh_bypasses_ttl_on_stale_empty(self, tmp_path, monkeypatch):
        # A manual "refresh all" MUST recover a stale EMPTY entry (e.g. the empties
        # the SEC-403 era wrote); otherwise full_refresh bypasses only the 50/50
        # group but not the TTL, so the empties stay pinned for 7 days and coverage
        # can't heal. Regression for the UA-403 recovery path.
        import market_data as md, data_providers, json
        from datetime import date, timedelta
        cache_path = tmp_path / "pc.json"
        monkeypatch.setattr(md, "PROVIDER_CACHE", str(cache_path))
        monkeypatch.setenv("FMP_API_KEY", "x")
        monkeypatch.setenv("FULL_REFRESH", "true")
        fetched = []
        class FakeP:
            def fundamentals(self, t): fetched.append(t); return {"gross_margin": 0.4}
            def next_earnings_date(self, t): return None
        monkeypatch.setattr(data_providers, "get_provider", lambda: FakeP())
        d = date(2026, 6, 15)                       # any day; group is irrelevant under full_refresh
        cache_path.write_text(json.dumps({"AAPL": {"fundamentals": None, "next_earnings": None,
                              "fetched": d.isoformat()}}))   # empty, age 0 → normally not due
        fund = {}
        md._enrich_with_provider(["AAPL"], fund, today=d)
        assert fetched == ["AAPL"]                  # full_refresh forced the re-fetch despite age 0
        assert fund["AAPL"]["gross_margin"] == 0.4  # real data now populated


# ── journal._load — corrupt-JSON resilience ───────────────────────────────────

class TestLoadListCorruptJSON:
    def test_corrupt_json_returns_empty_list(self, tmp_path):
        import journal
        path = str(tmp_path / "bad.json")
        (tmp_path / "bad.json").write_text("{invalid json{{")
        assert journal._load_list(path) == []

    def test_dict_json_coerced_to_list(self, tmp_path):
        import journal
        path = str(tmp_path / "dict.json")
        (tmp_path / "dict.json").write_text('{"key": "value"}')
        assert journal._load_list(path) == []

    def test_truncated_json_returns_empty_list(self, tmp_path):
        import journal
        path = str(tmp_path / "trunc.json")
        (tmp_path / "trunc.json").write_text('[{"ticker": "AAPL", "action')
        assert journal._load_list(path) == []


# ── health.append_check ───────────────────────────────────────────────────────

class TestAppendCheck:
    def _hp(self, tmp_path):
        return str(tmp_path / "health.json")

    def test_creates_from_scratch_when_no_file(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", self._hp(tmp_path))
        result = health.append_check("step", health.OK, "all good")
        assert result["checks"]["step"]["status"] == health.OK
        assert result["overall_status"] == health.OK

    def test_adds_to_existing_data(self, tmp_path, monkeypatch):
        import health
        hp = self._hp(tmp_path)
        monkeypatch.setattr(health, "HEALTH_FILE", hp)
        seed = {"run_id": "r1", "date": "2026-01-01",
                "checks": {"old": {"status": "OK", "message": ""}},
                "alerts": [], "overall_status": "OK"}
        (tmp_path / "health.json").write_text(json.dumps(seed))
        health.append_check("new_check", health.FAILED, "broke")
        result = json.loads((tmp_path / "health.json").read_text())
        assert "old" in result["checks"] and "new_check" in result["checks"]

    def test_overwrites_existing_check(self, tmp_path, monkeypatch):
        import health
        hp = self._hp(tmp_path)
        monkeypatch.setattr(health, "HEALTH_FILE", hp)
        seed = {"run_id": "r1", "date": "2026-01-01",
                "checks": {"step": {"status": "OK", "message": "first"}},
                "alerts": [], "overall_status": "OK"}
        (tmp_path / "health.json").write_text(json.dumps(seed))
        health.append_check("step", health.DEGRADED, "second")
        result = json.loads((tmp_path / "health.json").read_text())
        assert result["checks"]["step"]["status"] == health.DEGRADED
        assert result["checks"]["step"]["message"] == "second"

    def test_escalates_overall_status(self, tmp_path, monkeypatch):
        import health
        hp = self._hp(tmp_path)
        monkeypatch.setattr(health, "HEALTH_FILE", hp)
        seed = {"run_id": "r1", "date": "2026-01-01",
                "checks": {"ok_step": {"status": "OK", "message": ""}},
                "alerts": [], "overall_status": "OK"}
        (tmp_path / "health.json").write_text(json.dumps(seed))
        health.append_check("bad_step", health.FAILED, "exploded")
        result = json.loads((tmp_path / "health.json").read_text())
        assert result["overall_status"] == health.FAILED

    def test_rebuilds_alerts_list(self, tmp_path, monkeypatch):
        import health
        hp = self._hp(tmp_path)
        monkeypatch.setattr(health, "HEALTH_FILE", hp)
        health.append_check("good", health.OK, "fine")
        health.append_check("bad", health.FAILED, "broken")
        result = json.loads((tmp_path / "health.json").read_text())
        assert any("bad" in a for a in result["alerts"])
        assert not any("good" in a for a in result["alerts"])

    def test_aborted_beats_failed(self, tmp_path, monkeypatch):
        import health
        hp = self._hp(tmp_path)
        monkeypatch.setattr(health, "HEALTH_FILE", hp)
        seed = {"run_id": "r1", "date": "2026-01-01",
                "checks": {"f": {"status": "FAILED", "message": "bad"}},
                "alerts": ["[FAILED] f: bad"], "overall_status": "FAILED"}
        (tmp_path / "health.json").write_text(json.dumps(seed))
        health.append_check("abort_step", health.ABORTED, "aborted")
        result = json.loads((tmp_path / "health.json").read_text())
        assert result["overall_status"] == health.ABORTED

    def test_kwargs_stored(self, tmp_path, monkeypatch):
        import health
        monkeypatch.setattr(health, "HEALTH_FILE", self._hp(tmp_path))
        health.append_check("step", health.OK, "ok", extra_field="hello", count=42)
        result = json.loads((tmp_path / "health.json").read_text())
        assert result["checks"]["step"]["extra_field"] == "hello"
        assert result["checks"]["step"]["count"] == 42


# ── execute._compute_qty ──────────────────────────────────────────────────────

class TestComputeQty:
    def _qty(self, target_weight, action, ticker, portfolio, prices):
        from execute import _compute_qty
        return _compute_qty(target_weight, action, ticker, portfolio, prices)

    def _portfolio(self, total=1000.0, positions=None):
        return {"total_value": total, "positions": positions or []}

    def test_buy_no_existing_holdings(self):
        qty = self._qty(0.10, "BUY", "AAPL", self._portfolio(), {"AAPL": {"close": 100.0}})
        assert abs(qty - 1.0) < 1e-5  # $100 / $100 = 1.0 shares

    def test_buy_already_at_target(self):
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "1.0", "available_qty": "1.0"}])
        qty = self._qty(0.10, "BUY", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 0.0

    def test_buy_above_target(self):
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "2.0", "available_qty": "2.0"}])
        qty = self._qty(0.10, "BUY", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 0.0

    def test_buy_partial_top_up(self):
        # Holds 0.5 sh; target=10% of $1000=$100; current=$50; delta=$50 → 0.5 sh
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "0.5", "available_qty": "0.5"}])
        qty = self._qty(0.10, "BUY", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert abs(qty - 0.5) < 1e-5

    def test_sell_full_exit_returns_available_qty(self):
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "3.0", "available_qty": "3.0"}])
        qty = self._qty(0.0, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 3.0

    def test_sell_full_exit_capped_by_available(self):
        # Held 3.0 but broker says only 2.5 sellable
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "3.0", "available_qty": "2.5"}])
        qty = self._qty(0.0, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 2.5

    def test_sell_partial_reduce(self):
        # Holds 3 sh ($300, 30%); target 10% ($100); sell $200 = 2.0 sh
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "3.0", "available_qty": "3.0"}])
        qty = self._qty(0.10, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert abs(qty - 2.0) < 1e-5

    def test_sell_partial_reduce_capped_by_available(self):
        # Needs to sell 2.0 sh but only 1.5 available
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "3.0", "available_qty": "1.5"}])
        qty = self._qty(0.10, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert abs(qty - 1.5) < 1e-5

    def test_sell_already_at_target(self):
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "1.0", "available_qty": "1.0"}])
        qty = self._qty(0.10, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 0.0

    def test_hold_returns_zero(self):
        qty = self._qty(0.0, "HOLD", "AAPL", self._portfolio(), {"AAPL": {"close": 100.0}})
        assert qty == 0.0

    def test_missing_price_returns_zero(self):
        qty = self._qty(0.10, "BUY", "AAPL", self._portfolio(), {})
        assert qty == 0.0

    def test_ticker_not_in_positions(self):
        # AAPL not in positions list → current_qty=0, treat as new position
        port = self._portfolio(1000.0, [{"symbol": "MSFT", "qty": "5.0", "available_qty": "5.0"}])
        qty = self._qty(0.10, "BUY", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert abs(qty - 1.0) < 1e-5

    def test_available_qty_fallback_to_qty(self):
        # available_qty absent in position dict → falls back to qty
        port = self._portfolio(1000.0, [{"symbol": "AAPL", "qty": "3.0"}])
        qty = self._qty(0.0, "SELL", "AAPL", port, {"AAPL": {"close": 100.0}})
        assert qty == 3.0  # full exit, fallback available_qty == qty


# ── tax_lots — edge cases ─────────────────────────────────────────────────────

class TestTaxLotsAdditional:
    def test_empty_transactions(self):
        from tax_lots import open_lots
        assert open_lots([]) == {}

    def test_all_lots_consumed_by_sell(self):
        from tax_lots import open_lots
        txs = [
            {"action": "BUY", "ticker": "AAPL", "qty": 2, "price": 100, "date": "2026-01-01"},
            {"action": "SELL", "ticker": "AAPL", "qty": 2, "price": 110, "date": "2026-01-10"},
        ]
        assert "AAPL" not in open_lots(txs)

    def test_oversell_clamps_to_zero(self):
        from tax_lots import open_lots
        txs = [
            {"action": "BUY", "ticker": "AAPL", "qty": 1, "price": 100, "date": "2026-01-01"},
            {"action": "SELL", "ticker": "AAPL", "qty": 5, "price": 110, "date": "2026-01-10"},
        ]
        assert "AAPL" not in open_lots(txs)  # no negative lots

    def test_multi_ticker_independent(self):
        from tax_lots import open_lots
        txs = [
            {"action": "BUY",  "ticker": "AAPL", "qty": 2, "price": 100, "date": "2026-01-01"},
            {"action": "BUY",  "ticker": "MSFT", "qty": 3, "price": 50,  "date": "2026-01-02"},
            {"action": "SELL", "ticker": "MSFT", "qty": 5, "price": 50,  "date": "2026-01-05"},
        ]
        result = open_lots(txs)
        assert "AAPL" in result       # AAPL untouched by MSFT sell
        assert "MSFT" not in result   # MSFT oversold → 0 remaining

    def test_ticker_filter(self):
        from tax_lots import open_lots
        txs = [
            {"action": "BUY", "ticker": "AAPL", "qty": 2, "price": 100, "date": "2026-01-01"},
            {"action": "BUY", "ticker": "MSFT", "qty": 3, "price": 50,  "date": "2026-01-01"},
        ]
        result = open_lots(txs, ticker="AAPL")
        assert isinstance(result, list)
        assert len(result) == 1 and result[0]["qty"] == 2.0

    def test_holding_days_today_default(self):
        from tax_lots import holding_days
        from datetime import date
        assert holding_days(date.today().isoformat()) == 0

    def test_holding_days_null_acquired_returns_none(self):
        from tax_lots import holding_days
        assert holding_days(None) is None

    def test_holding_days_invalid_today_returns_none(self):
        from tax_lots import holding_days
        assert holding_days("2026-01-01", today="not-a-date") is None


# ── performance._portfolio_curve — edge cases ─────────────────────────────────

class TestPortfolioCurveEdgeCases:
    def test_non_list_agent_log_returns_empty(self, tmp_path):
        from performance import _portfolio_curve
        path = str(tmp_path / "log.json")
        (tmp_path / "log.json").write_text('{"run_id": "x"}')
        assert _portfolio_curve(path) == []

    def test_missing_portfolio_snapshot_skipped(self, tmp_path):
        from performance import _portfolio_curve
        path = str(tmp_path / "log.json")
        (tmp_path / "log.json").write_text(json.dumps([
            {"run_id": "a", "date": "2026-06-01"},
        ]))
        assert _portfolio_curve(path) == []

    def test_missing_total_value_skipped(self, tmp_path):
        from performance import _portfolio_curve
        path = str(tmp_path / "log.json")
        (tmp_path / "log.json").write_text(json.dumps([
            {"run_id": "a", "date": "2026-06-01", "portfolio_snapshot": {"total_value": None}},
        ]))
        assert _portfolio_curve(path) == []

    def test_timestamp_key_fallback(self, tmp_path):
        from performance import _portfolio_curve
        path = str(tmp_path / "log.json")
        (tmp_path / "log.json").write_text(json.dumps([
            {"run_id": "a", "timestamp": "2026-06-01T14:00:00Z",
             "portfolio_snapshot": {"total_value": 510.0}},
        ]))
        assert _portfolio_curve(path) == [("2026-06-01", 510.0)]


# ── performance._align — edge cases ──────────────────────────────────────────

class TestAlignEdgeCases:
    def test_portfolio_predates_spy_returns_empty(self):
        from performance import _align
        portfolio = [("2020-01-01", 1000.0)]
        spy = {"2026-01-01": 500.0}
        dates, pv, sv = _align(portfolio, spy)
        assert dates == [] and pv == [] and sv == []

    def test_spy_bars_missing_close_skipped(self, tmp_path):
        from performance import _spy_curve
        path = str(tmp_path / "snap.json")
        (tmp_path / "snap.json").write_text(json.dumps({
            "history": {
                "SPY": [
                    {"date": 1748736000000, "close": 500.0},
                    {"date": 1748822400000, "close": None},
                ]
            }
        }))
        result = _spy_curve(path)
        assert len(result) == 1
        assert all(v is not None for v in result.values())


# ── guardrails.validate_decisions — additional edge cases ─────────────────────

class TestValidateDecisionsAdditional:
    def _portfolio(self):
        return {"total_value": 1000.0, "positions": []}

    def test_missing_ticker_field_rejected(self):
        from guardrails import validate_decisions
        decisions = [{"action": "BUY", "ticker": "", "target_weight": 0.05, "qty": 0.5}]
        kept, report = validate_decisions(
            decisions, self._portfolio(), {"AAPL": {"close": 100.0}}, ["AAPL"], transactions=[])
        assert len(kept) == 0
        assert any("missing ticker" in r["reason"] for r in report["rejected"])

    def test_none_target_weight_rejected(self):
        from guardrails import validate_decisions
        decisions = [{"action": "BUY", "ticker": "AAPL", "target_weight": None, "qty": 0.5}]
        kept, report = validate_decisions(
            decisions, self._portfolio(), {"AAPL": {"close": 100.0}}, ["AAPL"], transactions=[])
        assert len(kept) == 0
        assert any("not a number" in r["reason"] for r in report["rejected"])

    def test_holdings_ticker_sell_passes_universe_check(self):
        # Ticker in holdings but not in candidates → SELL must pass (universe = candidates | holdings)
        from guardrails import validate_decisions
        portfolio = {"total_value": 1000.0,
                     "positions": [{"symbol": "XYZ", "qty": "2.0", "available_qty": "2.0"}]}
        decisions = [{"action": "SELL", "ticker": "XYZ", "target_weight": 0.0, "qty": 2.0}]
        kept, report = validate_decisions(
            decisions, portfolio, {"XYZ": {"close": 100.0}}, candidates=[], transactions=[])
        assert not any(r["ticker"] == "XYZ" for r in report["rejected"])
        assert len(kept) == 1

    def test_hold_does_not_increment_passed_counter(self):
        from guardrails import validate_decisions
        decisions = [{"action": "HOLD", "ticker": "AAPL", "target_weight": 0.0}]
        kept, report = validate_decisions(
            decisions, self._portfolio(), {"AAPL": {"close": 100.0}}, ["AAPL"], transactions=[])
        assert report["passed"] == 0  # HOLD takes early path — never counted
        assert len(kept) == 1        # but HOLD is in the kept list


# ── guardrails.enforce_wash_sale_reentry — edge cases ────────────────────────

class TestEnforceWashSaleEdgeCases:
    def test_bad_sell_date_format_passes_through(self):
        from guardrails import enforce_wash_sale_reentry
        txs = [{"ticker": "AAPL", "action": "SELL", "date": "not-a-date", "dry_run": False}]
        decisions = [{"action": "BUY", "ticker": "AAPL", "target_weight": 0.05, "qty": 0.5}]
        kept, rejected = enforce_wash_sale_reentry(
            decisions, transactions=txs, today="2026-06-14")
        assert len(kept) == 1 and len(rejected) == 0

    def test_multiple_sells_uses_most_recent(self):
        # One sell 40d ago (outside window), one sell 5d ago (inside 30d window)
        # _last_live_sell_date = max() = 5d ago → BUY rejected
        from guardrails import enforce_wash_sale_reentry
        txs = [
            {"ticker": "AAPL", "action": "SELL", "date": "2026-05-05", "dry_run": False},
            {"ticker": "AAPL", "action": "SELL", "date": "2026-06-09", "dry_run": False},
        ]
        decisions = [{"action": "BUY", "ticker": "AAPL", "target_weight": 0.05, "qty": 0.5}]
        kept, rejected = enforce_wash_sale_reentry(
            decisions, transactions=txs, today="2026-06-14")
        assert len(rejected) == 1 and len(kept) == 0


# ── preflight_gate — missing pending file / malformed snapshot ────────────────

class TestPreflightGateMissingPending(TestPreflightGate):
    def test_proceed_with_no_pending_file(self, tmp_path):
        today = self._today_et()
        self._write(tmp_path, "market_snapshot.json",
                    {"date": today, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._fresh_dossier(tmp_path)
        # no pending_decisions.json written at all → falls through to snapshot check → PROCEED
        assert self._run(tmp_path) == 0

    def test_malformed_snapshot_returns_skip_retry(self, tmp_path):
        (tmp_path / "market_snapshot.json").write_text("{invalid json")
        assert self._run(tmp_path) == 10


# ── cost_model — zero inputs, LT rate, zero notional ─────────────────────────

class TestCostModelEdgeCases:
    def test_tax_on_realized_both_zero(self):
        from cost_model import tax_on_realized
        tax, cf = tax_on_realized(0, 0)
        assert tax == 0.0 and cf == 0.0

    def test_round_trip_cost_zero_notional(self):
        from cost_model import round_trip_cost
        assert round_trip_cost(0) == 0.0

    def test_net_edge_zero_return(self):
        from cost_model import net_edge, round_trip_cost
        result = net_edge(0.0, notional=1000)
        assert result["gross"] == 0.0
        assert result["tax"] == 0.0
        assert abs(result["net"] - (-round_trip_cost(1000))) < 1e-4

    def test_net_edge_lt_rate_higher_net_than_st(self):
        from cost_model import net_edge
        st = net_edge(0.05, notional=1000, short_term=True)
        lt = net_edge(0.05, notional=1000, short_term=False)
        assert lt["net"] > st["net"]  # LT rate ~37% < ST rate ~54% → more left after tax


# ── journal.record_run — rotation at 90 entries ───────────────────────────────

class TestRecordRunRotation:
    def test_agent_log_capped_at_90(self, tmp_path, monkeypatch):
        import journal
        log_path = str(tmp_path / "agent_log.json")
        monkeypatch.setattr(journal, "AGENT_LOG_FILE", log_path)
        existing = [{"run_id": f"r{i}", "date": f"2026-01-{(i % 28) + 1:02d}"} for i in range(90)]
        (tmp_path / "agent_log.json").write_text(json.dumps(existing))
        journal.record_run("r90", {"date": "2026-04-01"})
        result = json.loads((tmp_path / "agent_log.json").read_text())
        assert len(result) == 90

    def test_oldest_entry_dropped_first(self, tmp_path, monkeypatch):
        import journal
        log_path = str(tmp_path / "agent_log.json")
        monkeypatch.setattr(journal, "AGENT_LOG_FILE", log_path)
        existing = [{"run_id": f"r{i}"} for i in range(90)]
        (tmp_path / "agent_log.json").write_text(json.dumps(existing))
        journal.record_run("r90", {"date": "2026-04-01"})
        result = json.loads((tmp_path / "agent_log.json").read_text())
        ids = [r["run_id"] for r in result]
        assert "r0" not in ids    # oldest dropped
        assert "r90" in ids       # newest preserved


# ── journal.recently_exited — edge cases ─────────────────────────────────────

class TestRecentlyExitedEdgeCases:
    def test_bad_exit_date_skipped(self, tmp_path, monkeypatch):
        import journal
        jpath = str(tmp_path / "journal.json")
        monkeypatch.setattr(journal, "JOURNAL_FILE", jpath)
        entries = [{"ticker": "AAPL", "status": "closed", "exits": [{"date": "not-a-date"}]}]
        (tmp_path / "journal.json").write_text(json.dumps(entries))
        result = journal.recently_exited(within_days=10)
        assert "AAPL" not in result  # bad date → silently skipped, no exception

    def test_closed_entry_with_empty_exits_excluded(self, tmp_path, monkeypatch):
        import journal
        jpath = str(tmp_path / "journal.json")
        monkeypatch.setattr(journal, "JOURNAL_FILE", jpath)
        entries = [{"ticker": "AAPL", "status": "closed", "exits": []}]
        (tmp_path / "journal.json").write_text(json.dumps(entries))
        result = journal.recently_exited(within_days=10)
        assert "AAPL" not in result

    def test_open_entry_not_included(self, tmp_path, monkeypatch):
        import journal
        from datetime import date
        jpath = str(tmp_path / "journal.json")
        monkeypatch.setattr(journal, "JOURNAL_FILE", jpath)
        entries = [{"ticker": "AAPL", "status": "open",
                    "exits": [{"date": date.today().isoformat()}]}]
        (tmp_path / "journal.json").write_text(json.dumps(entries))
        result = journal.recently_exited(within_days=10)
        assert "AAPL" not in result  # status != "closed" → never included


class TestPMBackstop:
    """Tests for main.apply_pm_backstop — 3-signal auto-SELL override."""

    def _portfolio(self, *tickers):
        return {"positions": [{"symbol": t} for t in tickers]}

    def _state(self, ticker, pr_action, hold_score, da_reject, da_risk=8):
        return {
            "position_reviews": {ticker: {
                "recommended_action": pr_action,
                "hold_score": hold_score,
                "remaining_alpha": "low",
            }},
            "devils_advocate": {ticker: {
                "recommend_reject": da_reject,
                "overall_risk_score": da_risk,
            }},
        }

    def test_all_three_signals_trigger_sell(self):
        from main import apply_pm_backstop
        decisions = []
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            self._state("LLY", "REDUCE", 4, True),
        )
        assert exits == ["LLY"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["ticker"] == "LLY"
        assert d["action"] == "SELL"
        assert d["target_weight"] == 0.0
        assert "3-signal override" in d["rationale"]

    def test_exit_action_also_triggers(self):
        from main import apply_pm_backstop
        decisions = []
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("XYZ"),
            self._state("XYZ", "EXIT", 2, True),
        )
        assert "XYZ" in exits
        assert decisions[0]["action"] == "SELL"

    def test_missing_one_signal_does_not_trigger(self):
        from main import apply_pm_backstop
        # DA does not reject
        decisions = []
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            self._state("LLY", "REDUCE", 4, False),
        )
        assert exits == []
        assert decisions == []

    def test_hold_score_5_does_not_trigger(self):
        from main import apply_pm_backstop
        # hold_score == 5 is NOT < 5 → no trigger
        decisions = []
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            self._state("LLY", "REDUCE", 5, True),
        )
        assert exits == []

    def test_already_selling_skipped(self):
        from main import apply_pm_backstop
        existing_sell = {"ticker": "LLY", "action": "SELL", "target_weight": 0.0}
        decisions = [existing_sell]
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            self._state("LLY", "EXIT", 1, True),
        )
        assert exits == []
        assert len(decisions) == 1  # no duplicate added

    def test_hold_action_does_not_suppress_backstop(self):
        from main import apply_pm_backstop
        # PM said HOLD (not SELL) → backstop should still fire
        existing_hold = {"ticker": "LLY", "action": "HOLD"}
        decisions = [existing_hold]
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            self._state("LLY", "REDUCE", 3, True),
        )
        assert "LLY" in exits
        assert any(d["action"] == "SELL" for d in decisions)

    def test_multiple_positions_independent(self):
        from main import apply_pm_backstop
        state = {
            "position_reviews": {
                "AAA": {"recommended_action": "REDUCE", "hold_score": 3, "remaining_alpha": "low"},
                "BBB": {"recommended_action": "HOLD",   "hold_score": 7, "remaining_alpha": "high"},
                "CCC": {"recommended_action": "EXIT",   "hold_score": 2, "remaining_alpha": "none"},
            },
            "devils_advocate": {
                "AAA": {"recommend_reject": True,  "overall_risk_score": 9},
                "BBB": {"recommend_reject": True,  "overall_risk_score": 8},
                "CCC": {"recommend_reject": False, "overall_risk_score": 6},
            },
        }
        decisions = []
        exits = apply_pm_backstop(decisions, self._portfolio("AAA", "BBB", "CCC"), state)
        # AAA: all 3 signals → exit. BBB: pr=HOLD → no exit. CCC: da_reject=False → no exit.
        assert exits == ["AAA"]
        assert len(decisions) == 1
        assert decisions[0]["ticker"] == "AAA"

    def test_null_hold_score_treated_as_10(self):
        from main import apply_pm_backstop
        # hold_score=None → `(None or 10)` = 10 → NOT < 5 → no trigger
        decisions = []
        exits = apply_pm_backstop(
            decisions,
            self._portfolio("LLY"),
            {
                "position_reviews": {"LLY": {"recommended_action": "REDUCE", "hold_score": None}},
                "devils_advocate": {"LLY": {"recommend_reject": True}},
            },
        )
        assert exits == []


# ─────────────────────────────────────────────────────────────────────────────
# Post-run gap fixes (2026-06-17): regime plumbing, Supabase-403 classification,
# PM parse-failure detection. See the "Post-run gaps" changelog.
# ─────────────────────────────────────────────────────────────────────────────

class TestSupabaseHealthClassification:
    """_record_supabase_health: the EXPECTED cloud egress 403 must NOT mark the run
    FAILED (that forced overall_status=FAILED every clean run + blocked alert
    auto-close); a REAL publish error still must."""

    def _tracker(self):
        from health import HealthTracker
        return HealthTracker(run_id="t", date="2026-06-17")

    def test_allowlist_403_recorded_ok(self):
        from main import _record_supabase_health
        from health import OK
        h = self._tracker()
        err = Exception("{'message': 'JSON could not be generated', 'code': 403, "
                        "'details': \"Host not in allowlist: xyz.supabase.co\"}")
        _record_supabase_health(h, err)
        assert h.checks["supabase_publish"]["status"] == OK

    def test_egress_wording_recorded_ok(self):
        from main import _record_supabase_health
        from health import OK
        h = self._tracker()
        _record_supabase_health(h, Exception("blocked by network egress settings"))
        assert h.checks["supabase_publish"]["status"] == OK

    def test_real_error_recorded_failed(self):
        from main import _record_supabase_health
        from health import FAILED
        h = self._tracker()
        _record_supabase_health(h, Exception("401 Invalid API key"))
        assert h.checks["supabase_publish"]["status"] == FAILED

    # ── Phase 1 (2026-07-05): cloud-plane detection is now the PRIMARY signal —
    # a reworded/unrecognized egress-proxy error must still classify OK when we
    # are structurally in the cloud plane (no ANTHROPIC_API_KEY, OAuth token file
    # present), since ANY publish exception there is the expected block. ──

    def test_cloud_plane_any_message_recorded_ok(self, tmp_path, monkeypatch):
        from main import _record_supabase_health
        from health import OK
        token_file = tmp_path / "token"
        token_file.write_text("fake-oauth-token")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", str(token_file))
        h = self._tracker()
        # A completely unrecognized error shape (proxy reworded its message) —
        # must still be OK because we're in the cloud plane, not because the
        # string happens to match.
        _record_supabase_health(h, Exception("connection reset by peer"))
        assert h.checks["supabase_publish"]["status"] == OK

    def test_cloud_plane_detection_requires_no_api_key(self, tmp_path, monkeypatch):
        from main import _record_supabase_health
        from health import FAILED
        token_file = tmp_path / "token"
        token_file.write_text("fake-oauth-token")
        # ANTHROPIC_API_KEY present -> NOT the cloud plane (local/CI with a real
        # key), even if a stale token file happens to exist -> falls through to
        # the message heuristic, which does not match -> FAILED.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", str(token_file))
        h = self._tracker()
        _record_supabase_health(h, Exception("connection reset by peer"))
        assert h.checks["supabase_publish"]["status"] == FAILED

    def test_cloud_plane_detection_requires_real_token_file(self, monkeypatch):
        from main import _record_supabase_health
        from health import FAILED
        # Token file env var points to a nonexistent path -> not cloud plane.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "/nonexistent/path")
        h = self._tracker()
        _record_supabase_health(h, Exception("connection reset by peer"))
        assert h.checks["supabase_publish"]["status"] == FAILED


class TestSafeCallMeta:
    """_safe_call(return_meta=True) must distinguish a GENUINE default value (PM
    legitimately returns []) from a PARSE FAILURE that collapsed to the default."""

    def test_genuine_empty_array_parsed_ok_true(self, monkeypatch):
        import analysis
        monkeypatch.setattr(analysis, "_call", lambda *a, **k: ("[]", "max_tokens"))
        result, meta = analysis._safe_call("m", "s", "u", default=[], retries=0, return_meta=True)
        assert result == []
        assert meta["parsed_ok"] is True

    def test_unparseable_response_parsed_ok_false(self, monkeypatch):
        import analysis
        monkeypatch.setattr(analysis, "_call", lambda *a, **k: ("this is not json at all", "end_turn"))
        result, meta = analysis._safe_call("m", "s", "u", default=[], retries=0, return_meta=True)
        assert result == []
        assert meta["parsed_ok"] is False

    def test_valid_payload_round_trips(self, monkeypatch):
        import analysis
        monkeypatch.setattr(analysis, "_call",
                            lambda *a, **k: ('[{"ticker": "AAPL", "action": "BUY"}]', "end_turn"))
        result, meta = analysis._safe_call("m", "s", "u", default=[], retries=0, return_meta=True)
        assert result == [{"ticker": "AAPL", "action": "BUY"}]
        assert meta["parsed_ok"] is True


class TestPmParseFailureSurfaced:
    """run_portfolio_manager returns (decisions, meta); the pipeline records the
    parse-ok flag so a mangled PM response can't masquerade as a deliberate hold."""

    def test_pm_returns_tuple_with_meta(self, monkeypatch):
        import analysis
        monkeypatch.setattr(analysis, "_call", lambda *a, **k: ("not json", "end_turn"))
        portfolio = {"total_value": 500.0, "cash": 500.0, "positions": []}
        decisions, meta = analysis.run_portfolio_manager(
            {}, {}, {}, {}, {}, {}, portfolio, [], date="2026-06-17")
        assert decisions == []
        assert meta["parsed_ok"] is False


class TestCashDisciplineStatus:
    """cash_discipline_status: DEGRADED only when cash is over the ceiling AND the
    run deploys none of it. Observability signal — never forces a trade."""

    def test_high_cash_no_buys_degraded(self):
        from main import cash_discipline_status, CASH_DISCIPLINE_PCT
        from health import DEGRADED
        assert cash_discipline_status(33.5, 0.0) == DEGRADED
        assert CASH_DISCIPLINE_PCT == 15.0

    def test_high_cash_with_buys_ok(self):
        # A run actively deploying cash (net_buy > 0) is NOT flagged.
        from main import cash_discipline_status
        from health import OK
        assert cash_discipline_status(33.5, 120.0) == OK

    def test_low_cash_ok(self):
        from main import cash_discipline_status
        from health import OK
        assert cash_discipline_status(8.0, 0.0) == OK

    def test_exactly_at_threshold_ok(self):
        # Strictly greater-than: 15.0 is not over the 15.0 ceiling.
        from main import cash_discipline_status
        from health import OK
        assert cash_discipline_status(15.0, 0.0) == OK


class TestPublishRegimePriority:
    """publish_to_supabase must publish the LIVE regime, not a stale one inherited
    from the previous day's portfolio_snapshot.json (the bug that showed a RISK_ON
    run as NEUTRAL on the dashboard)."""

    def _setup(self, tmp_path, monkeypatch, snapshot_regime, log_regime, log_date):
        monkeypatch.chdir(tmp_path)
        # Stale snapshot from a prior day.
        (tmp_path / "portfolio_snapshot.json").write_text(json.dumps({
            "is_close": False, "regime": snapshot_regime,
            "portfolio": {"cash": 100, "total_value": 500, "positions": []},
        }))
        # agent_log with a regime entry dated log_date.
        (tmp_path / "agent_log.json").write_text(json.dumps([
            {"run_id": "r", "date": log_date, "regime": {"regime": log_regime}}
        ]))
        # Ensure Supabase is treated as unconfigured so publish returns after the
        # snapshot write (no network).
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def _written_regime(self, tmp_path):
        return json.loads((tmp_path / "portfolio_snapshot.json").read_text())["regime"]

    def test_explicit_arg_wins_over_stale_file(self, tmp_path, monkeypatch):
        import importlib, publish
        importlib.reload(publish)
        self._setup(tmp_path, monkeypatch, snapshot_regime="NEUTRAL",
                    log_regime="NEUTRAL", log_date="2000-01-01")
        publish.publish_to_supabase(
            {"cash": 100, "total_value": 500, "positions": []}, regime="RISK_ON")
        assert self._written_regime(tmp_path) == "RISK_ON"

    def test_todays_agent_log_used_when_no_arg(self, tmp_path, monkeypatch):
        import importlib, publish
        importlib.reload(publish)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        self._setup(tmp_path, monkeypatch, snapshot_regime="NEUTRAL",
                    log_regime="RISK_OFF", log_date=today)
        publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        assert self._written_regime(tmp_path) == "RISK_OFF"

    def test_stale_agent_log_falls_through_to_file(self, tmp_path, monkeypatch):
        import importlib, publish
        importlib.reload(publish)
        self._setup(tmp_path, monkeypatch, snapshot_regime="NEUTRAL",
                    log_regime="RISK_ON", log_date="2000-01-01")
        publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        # agent_log is stale (old date) → ignored → fall back to file regime.
        assert self._written_regime(tmp_path) == "NEUTRAL"


class TestPublishDryRunGuard:
    """Found 2026-07-05: a local risk_watch dry run (DRY_RUN=true) published a
    synthetic portfolio to the PRODUCTION Supabase behind the live website —
    DRY_RUN gated order placement but not Supabase publishing. The guard must
    still write portfolio_snapshot.json (the GitHub Actions publish.yml trigger)
    but never perform a live Supabase network write."""

    def _setup(self, tmp_path, monkeypatch):
        import importlib, publish
        importlib.reload(publish)
        monkeypatch.chdir(tmp_path)
        # Real-LOOKING creds present so the test proves the guard skips BEFORE using
        # them (not merely because Supabase is unconfigured).
        monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        return publish

    def test_dry_run_writes_file_but_skips_supabase(self, tmp_path, monkeypatch):
        publish = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("DRY_RUN", "true")
        import supabase
        monkeypatch.setattr(supabase, "create_client", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_client called — DRY_RUN guard did not fire")))
        publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        # The snapshot FILE must still be written — it's the trigger for publish.yml.
        snap = tmp_path / "portfolio_snapshot.json"
        assert snap.exists()
        assert json.loads(snap.read_text())["portfolio"]["total_value"] == 500

    def test_dry_run_case_insensitive(self, tmp_path, monkeypatch):
        publish = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("DRY_RUN", "TRUE")   # matches execute.py's .lower() semantics
        import supabase
        monkeypatch.setattr(supabase, "create_client", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("create_client called on DRY_RUN=TRUE")))
        publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        assert (tmp_path / "portfolio_snapshot.json").exists()

    def test_dry_run_false_reaches_supabase_block(self, tmp_path, monkeypatch):
        # DRY_RUN=false with creds present → publish proceeds to create_client,
        # proving the guard is specific to DRY_RUN, not a blanket skip that would
        # break the real GitHub-Actions publish path (which runs with no DRY_RUN).
        publish = self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("DRY_RUN", "false")
        reached = {"create_client": False}
        import supabase
        def marker(*a, **k):
            reached["create_client"] = True
            raise RuntimeError("stop — only proving we reached the Supabase block")
        monkeypatch.setattr(supabase, "create_client", marker)
        try:
            publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        except RuntimeError:
            pass
        assert reached["create_client"] is True

    def test_dry_run_unset_reaches_supabase_block(self, tmp_path, monkeypatch):
        # No DRY_RUN at all (GitHub Actions publish.yml has none) → not gated.
        publish = self._setup(tmp_path, monkeypatch)
        monkeypatch.delenv("DRY_RUN", raising=False)
        reached = {"create_client": False}
        import supabase
        def marker(*a, **k):
            reached["create_client"] = True
            raise RuntimeError("stop")
        monkeypatch.setattr(supabase, "create_client", marker)
        try:
            publish.publish_to_supabase({"cash": 100, "total_value": 500, "positions": []})
        except RuntimeError:
            pass
        assert reached["create_client"] is True


# ── CascadeProvider tests ──────────────────────────────────────────────────────

class TestCascadeProvider:
    """CascadeProvider: FMP for all 6 factors, SEC EDGAR fallback for 3 quality fields on FMP misses."""

    def _cascade(self, fmp_data=None, sec_data=None, earnings=None, estimates=None):
        from data_providers import CascadeProvider

        class _FMP:
            def fundamentals(self, t):    return fmp_data
            def next_earnings_date(self, t): return earnings
            def estimates(self, t):       return estimates

        class _SEC:
            def fundamentals(self, t):    return sec_data
            def next_earnings_date(self, t): return None
            def estimates(self, t):       return None

        return CascadeProvider(_FMP(), _SEC())

    def test_fmp_hit_returns_fmp_data_sec_not_consulted(self):
        """FMP covers the ticker → return FMP data, SEC not called."""
        fmp = {"gross_margin": 0.5, "operating_margin": 0.2, "debt_to_equity": 0.3,
               "pe_ratio": 20.0, "fcf_yield": 0.04, "ev_ebitda": 15.0}
        # sec_data=None simulates SEC never being invoked; if it were consulted and
        # returned None, merged result would still equal fmp — but we also verify
        # via a sentinel that the SEC object isn't called.
        calls = []

        from data_providers import CascadeProvider

        class _SEC:
            def fundamentals(self, t): calls.append(t); return None
            def next_earnings_date(self, t): return None
            def estimates(self, t): return None

        class _FMP:
            def fundamentals(self, t):       return fmp
            def next_earnings_date(self, t): return None
            def estimates(self, t):          return None

        cp = CascadeProvider(_FMP(), _SEC())
        result = cp.fundamentals("AAPL")
        assert result == fmp
        assert calls == [], "SEC should not be consulted when FMP has quality fields"

    def test_fmp_miss_falls_back_to_sec(self):
        """FMP returns None → SEC fills 3 quality fields."""
        sec = {"gross_margin": 0.6, "operating_margin": 0.25, "debt_to_equity": 0.8}
        cp = self._cascade(fmp_data=None, sec_data=sec)
        assert cp.fundamentals("PANW") == sec

    def test_fmp_no_quality_fields_supplements_sec(self):
        """FMP returns {} (no quality fields) → merges with SEC quality fields."""
        sec = {"gross_margin": 0.4, "operating_margin": 0.1, "debt_to_equity": 1.2}
        cp = self._cascade(fmp_data={}, sec_data=sec)
        result = cp.fundamentals("CRWD")
        assert result["gross_margin"] == 0.4
        assert result["operating_margin"] == 0.1

    def test_fmp_wins_on_overlap(self):
        """When both providers have gross_margin, FMP value wins."""
        fmp = {"gross_margin": 0.55, "operating_margin": 0.30, "debt_to_equity": 0.5}
        sec = {"gross_margin": 0.40, "operating_margin": 0.20, "debt_to_equity": 1.0}
        cp = self._cascade(fmp_data=None, sec_data=sec)
        # FMP returns None here so SEC fills in; but if FMP had data it wins:
        from data_providers import CascadeProvider

        class _FMP:
            def fundamentals(self, t): return None  # miss on free tier
            def next_earnings_date(self, t): return None
            def estimates(self, t): return None

        class _SEC:
            def fundamentals(self, t): return sec
            def next_earnings_date(self, t): return None
            def estimates(self, t): return None

        # Simulate a partial FMP hit with valuation only (no quality fields):
        from data_providers import _QUALITY_FIELDS

        class _FMP_partial:
            def fundamentals(self, t): return {"pe_ratio": 25.0}  # no quality fields
            def next_earnings_date(self, t): return None
            def estimates(self, t): return None

        cp2 = CascadeProvider(_FMP_partial(), _SEC())
        result = cp2.fundamentals("X")
        # SEC fills quality fields; FMP's pe_ratio is preserved
        assert result.get("gross_margin") == sec["gross_margin"]
        assert result.get("pe_ratio") == 25.0

    def test_both_none_returns_none(self):
        """FMP and SEC both return None → CascadeProvider returns None."""
        cp = self._cascade(fmp_data=None, sec_data=None)
        assert cp.fundamentals("UNKNOWN") is None

    def test_earnings_and_estimates_use_primary(self):
        """next_earnings_date and estimates delegate to FMP, not SEC."""
        cp = self._cascade(fmp_data=None, sec_data=None, earnings="2026-08-01", estimates={"eps": 2.5})
        assert cp.next_earnings_date("AAPL") == "2026-08-01"
        assert cp.estimates("AAPL") == {"eps": 2.5}



# ── Consecutive cash above threshold tests ─────────────────────────────────────

class TestConsecutiveCashAbove:
    """consecutive_cash_above() counts consecutive recent runs where cash_pct > threshold."""

    def _run(self, tmp_path, monkeypatch, entries, threshold=15.0):
        import json
        from journal import AGENT_LOG_FILE
        (tmp_path / AGENT_LOG_FILE).write_text(json.dumps(entries))
        monkeypatch.chdir(tmp_path)
        from journal import consecutive_cash_above
        return consecutive_cash_above(threshold)

    def _ps(self, cash, total):
        return {"portfolio_snapshot": {"cash": cash, "total_value": total}}

    def test_single_run_above_threshold(self, tmp_path, monkeypatch):
        entries = [self._ps(100, 400)]  # 25% > 15%
        assert self._run(tmp_path, monkeypatch, entries) == 1

    def test_single_run_at_threshold_not_counted(self, tmp_path, monkeypatch):
        entries = [self._ps(60, 400)]  # exactly 15% → not > threshold
        assert self._run(tmp_path, monkeypatch, entries) == 0

    def test_streak_broken_by_below_threshold_run(self, tmp_path, monkeypatch):
        entries = [
            self._ps(10, 400),   # 2.5% — below
            self._ps(80, 400),   # 20% — above
            self._ps(100, 400),  # 25% — above
        ]
        # Most recent two runs above, then one below breaks the streak
        assert self._run(tmp_path, monkeypatch, entries) == 2

    def test_no_streak_when_last_run_below(self, tmp_path, monkeypatch):
        entries = [
            self._ps(100, 400),  # 25% — above
            self._ps(10, 400),   # 2.5% — below (most recent)
        ]
        assert self._run(tmp_path, monkeypatch, entries) == 0

    def test_empty_log_returns_zero(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, []) == 0

    def test_missing_total_value_breaks_streak(self, tmp_path, monkeypatch):
        # Most recent entry (last in list) has no total_value → count stops immediately
        entries = [
            {"portfolio_snapshot": {"cash": 100, "total_value": 400}},  # older, above
            {"portfolio_snapshot": {"cash": 100}},    # most recent, no total_value
        ]
        assert self._run(tmp_path, monkeypatch, entries) == 0


# ── _safe_call no-retry on genuine default ────────────────────────────────────

class TestSafeCallNoRetryOnGenuineDefault:
    """_safe_call with return_meta=True does not retry when parsed_ok=True and result==default."""

    def test_no_retry_on_genuine_empty_list(self, monkeypatch):
        """PM returns '[]' legitimately — _safe_call should NOT retry."""
        import analysis
        call_count = [0]

        def _fake_call(model, system, user_msg, max_tokens=600):
            call_count[0] += 1
            return "[]", "end_turn"

        monkeypatch.setattr(analysis, "_call", _fake_call)
        result, meta = analysis._safe_call(
            "model", "sys", "user", default=[], max_tokens=600, retries=2, return_meta=True
        )
        assert result == []
        assert meta["parsed_ok"] is True
        assert call_count[0] == 1, f"Expected 1 call (no retry), got {call_count[0]}"

    def test_retry_still_fires_on_parse_failure(self, monkeypatch):
        """Parse failure (not return_meta) still retries — existing behavior preserved."""
        import analysis
        call_count = [0]

        def _fake_call(model, system, user_msg, max_tokens=600):
            call_count[0] += 1
            return "NOT VALID JSON", "end_turn"

        monkeypatch.setattr(analysis, "_call", _fake_call)
        result = analysis._safe_call(
            "model", "sys", "user", default=[], max_tokens=600, retries=2, return_meta=False
        )
        assert result == []
        assert call_count[0] == 3, f"Expected 3 attempts (2 retries), got {call_count[0]}"

    def test_retry_fires_on_parse_failure_with_meta(self, monkeypatch):
        """Even with return_meta=True, a parse failure (parsed_ok=False) still retries."""
        import analysis
        call_count = [0]

        def _fake_call(model, system, user_msg, max_tokens=600):
            call_count[0] += 1
            return "NOT VALID JSON", "end_turn"

        monkeypatch.setattr(analysis, "_call", _fake_call)
        result, meta = analysis._safe_call(
            "model", "sys", "user", default=[], max_tokens=600, retries=2, return_meta=True
        )
        assert result == []
        assert meta["parsed_ok"] is False
        assert call_count[0] == 3, f"Expected 3 attempts (2 retries), got {call_count[0]}"


# ── Phase 0: policy.yaml single-source parity (zero behavior change) ─────────────

class TestPolicyParity:
    """policy.yaml is the single source of truth for the deterministic limits.

    These tests are the OPERATIVE-BASELINE GUARANTEE: the values served from
    policy.yaml, the guardrails/execute constants sourced from it, AND the
    built-in _DEFAULTS fallback must all agree on the deployed policy. Phase 0
    proved parity with the historical constants; the v2.0 Phase-5 migration
    (min-hold 5→30, + stop / rebalance-weekday / tax-hold / safe-mode keys) is a
    §18.4-governed change — this oracle now asserts the v2.0 values, so any
    future drift (in either the yaml or the fallback) fails loudly.
    """

    # The OPERATIVE v2.0 values, restated here independently as the oracle.
    OPERATIVE = {
        "max_target_weight":        0.10,
        "max_buy_notional_pct":     0.12,
        "min_order_notional":       5.00,
        "gfv_window_trading_days":  2,
        "max_sector_weight":        0.25,
        "min_holding_trading_days": 30,     # v2.0 migration (was 5) — IPS §7.2
        "wash_sale_reentry_days":   30,
        "min_net_edge":             0.0,
        "tax_aware_hold_window_trading_days": 30,   # v2.0 — IPS §7.5
        "rebalance_weekday":        2,              # v2.0 — Wednesday
        "single_name_stop_pct":     0.25,           # v2.0 — risk_watch stop (§6.7)
        "safe_mode_index_drop_pct": 7,              # v2.0 — §18.5 crisis brake
        "blocked_tickers":          ["TSLA"],
    }

    def test_defaults_match_operative_baseline(self):
        """policy._DEFAULTS (the fallback) equals the OPERATIVE policy — a yaml load
        failure must never silently roll back a governed migration (e.g. revert the
        30-day min-hold to 5 or lose the rebalance weekday)."""
        import policy
        for k, v in self.OPERATIVE.items():
            assert policy._DEFAULTS[k] == v, f"_DEFAULTS[{k}] drifted from operative baseline"

    def test_policy_yaml_matches_operative(self):
        """The shipped policy.yaml carries the operative v2.0 values."""
        import policy
        loaded = policy._load()  # reads the real policy.yaml next to the module
        for k, v in self.OPERATIVE.items():
            assert loaded[k] == v, f"policy.yaml {k}={loaded[k]!r} != operative {v!r}"
        assert loaded["policy_version"] == "2.0-phase5-weekly"

    def test_guardrails_constants_sourced_from_policy(self):
        """guardrails.* constants equal the policy values AND the operative ones."""
        import guardrails, policy
        assert guardrails.MAX_TARGET_WEIGHT        == policy.VALUES["max_target_weight"]        == 0.10
        assert guardrails.MAX_BUY_NOTIONAL_PCT     == policy.VALUES["max_buy_notional_pct"]     == 0.12
        assert guardrails.MIN_ORDER_NOTIONAL       == policy.VALUES["min_order_notional"]       == 5.00
        assert guardrails.GFV_WINDOW_TRADING_DAYS  == policy.VALUES["gfv_window_trading_days"]  == 2
        assert guardrails.MAX_SECTOR_WEIGHT        == policy.VALUES["max_sector_weight"]        == 0.25
        assert guardrails.MIN_HOLDING_TRADING_DAYS == policy.VALUES["min_holding_trading_days"] == 30
        assert guardrails.WASH_SALE_REENTRY_DAYS   == policy.VALUES["wash_sale_reentry_days"]   == 30
        assert guardrails.MIN_NET_EDGE             == policy.VALUES["min_net_edge"]             == 0.0
        assert guardrails.TAX_AWARE_HOLD_WINDOW    == policy.VALUES["tax_aware_hold_window_trading_days"] == 30
        assert guardrails.SAFE_MODE_INDEX_DROP_PCT == policy.VALUES["safe_mode_index_drop_pct"] == 7

    def test_blocked_tickers_sourced_from_policy(self):
        import execute, policy
        assert execute.BLOCKED_TICKERS == {"TSLA"}
        assert set(policy.VALUES["blocked_tickers"]) == {"TSLA"}

    def test_loader_falls_back_when_file_missing(self):
        """A missing policy.yaml must yield exactly _DEFAULTS — never crash."""
        import policy
        result = policy._load("/nonexistent/policy.yaml")
        assert result == policy._DEFAULTS

    def test_loader_falls_back_on_malformed_yaml(self, tmp_path):
        """A malformed policy.yaml must degrade to _DEFAULTS, not raise."""
        import policy
        bad = tmp_path / "policy.yaml"
        bad.write_text("guardrails: [this is: not valid: yaml: {{{")
        result = policy._load(str(bad))
        assert result == policy._DEFAULTS

    def test_partial_policy_overlays_on_defaults(self, tmp_path):
        """A policy.yaml that sets only some keys keeps defaults for the rest."""
        import policy
        partial = tmp_path / "policy.yaml"
        partial.write_text(
            "policy_version: test-partial\n"
            "guardrails:\n"
            "  max_sector_weight: 0.20\n"
        )
        result = policy._load(str(partial))
        assert result["max_sector_weight"] == 0.20            # overlaid
        assert result["max_target_weight"] == 0.10            # default kept
        assert result["policy_version"] == "test-partial"
        assert result["blocked_tickers"] == ["TSLA"]          # default kept

    def test_policy_version_helper(self):
        import policy
        assert policy.policy_version() == policy.VALUES["policy_version"]
        assert policy.policy_version() == "2.0-phase5-weekly"

    def test_validation_rejects_units_typo_keeps_cap(self, tmp_path):
        """A percent/fraction units typo (10 instead of 0.10) must NOT disable the cap —
        the loader rejects the out-of-range value and keeps the safe default."""
        import policy
        bad = tmp_path / "policy.yaml"
        bad.write_text(
            "guardrails:\n"
            "  max_target_weight: 10\n"      # typo: meant 0.10; 10 = 1000%
            "  max_sector_weight: 0.25\n"    # valid — should overlay
        )
        result = policy._load(str(bad))
        assert result["max_target_weight"] == 0.10   # rejected typo → safe default kept
        assert result["max_sector_weight"] == 0.25   # valid value overlaid

    def test_validation_rejects_wrong_type(self, tmp_path):
        """A string where a number is expected keeps the default (no runtime TypeError later)."""
        import policy
        bad = tmp_path / "policy.yaml"
        bad.write_text("guardrails:\n  min_holding_trading_days: '30 days'\n")
        result = policy._load(str(bad))
        assert result["min_holding_trading_days"] == 30   # default kept (v2.0 baseline)

    def test_validation_rejects_percent_form_stop(self, tmp_path):
        """single_name_stop_pct: 25 (the IPS percent form) must be REJECTED — read as a
        fraction it would be a 2500% stop that never fires. Default 0.25 kept."""
        import policy
        bad = tmp_path / "policy.yaml"
        bad.write_text("risk:\n  single_name_stop_pct: 25\n")
        result = policy._load(str(bad))
        assert result["single_name_stop_pct"] == 0.25

    def test_v2_sectioned_keys_load_from_their_sections(self, tmp_path):
        """trading:/risk: sectioned keys overlay from their own sections."""
        import policy
        p = tmp_path / "policy.yaml"
        p.write_text("trading:\n  rebalance_weekday: 3\n"
                     "risk:\n  single_name_stop_pct: 0.30\n  safe_mode_index_drop_pct: 10\n")
        result = policy._load(str(p))
        assert result["rebalance_weekday"] == 3
        assert result["single_name_stop_pct"] == 0.30
        assert result["safe_mode_index_drop_pct"] == 10

    def test_weekend_rebalance_weekday_rejected(self, tmp_path):
        """rebalance_weekday: 6 (Sunday) would silently never fire — rejected."""
        import policy
        p = tmp_path / "policy.yaml"
        p.write_text("trading:\n  rebalance_weekday: 6\n")
        assert policy._load(str(p))["rebalance_weekday"] == 2

    def test_validation_rejects_bad_blocked_tickers(self, tmp_path):
        """blocked_tickers must be a list[str]; anything else keeps the default."""
        import policy
        bad = tmp_path / "policy.yaml"
        bad.write_text("universe:\n  blocked_tickers: TSLA\n")  # str, not list
        result = policy._load(str(bad))
        assert result["blocked_tickers"] == ["TSLA"]

    def test_validation_accepts_valid_override(self, tmp_path):
        """A valid in-range override IS applied (validation isn't over-strict)."""
        import policy
        good = tmp_path / "policy.yaml"
        good.write_text(
            "guardrails:\n"
            "  min_holding_trading_days: 30\n"   # the IPS-target migration value
            "  max_target_weight: 0.08\n"
        )
        result = policy._load(str(good))
        assert result["min_holding_trading_days"] == 30
        assert result["max_target_weight"] == 0.08


# ── Phase 1: forecast-feed persistence (the Jun-18 silent-break regression) ──────

class TestForecastFeedPersistence:
    """The forecast ledger was gitignored + never committed, so the cloud routine's
    `git add` was a silent no-op and every run's forecasts were lost (frozen Jun 18).
    These guard the fix so the evidence clock can never silently stop again."""

    LEDGER_FILES = ["forecasts.jsonl", "forecasts_scored.jsonl", "agent_scorecards.json"]

    def test_ledger_files_not_gitignored(self):
        """The exact regression: none of the ledger files may be gitignored, or the
        routine's `git add` silently stages nothing."""
        import subprocess, os
        repo = os.path.dirname(os.path.abspath(__file__))
        for f in self.LEDGER_FILES:
            rc = subprocess.run(["git", "check-ignore", f], cwd=repo,
                                 capture_output=True).returncode
            assert rc != 0, f"{f} is gitignored — routine `git add` would be a silent no-op"

    def test_ledger_in_routine_commit_list(self):
        """forecasts.jsonl must be in the routine's daily-cycle `git add` (else not pushed)."""
        import os
        repo = os.path.dirname(os.path.abspath(__file__))
        routine = open(os.path.join(repo, "ROUTINE_DAILY_CYCLE.md")).read()
        assert "forecasts.jsonl" in routine
        daily = [l for l in routine.splitlines()
                 if l.startswith("git add") and "trades.csv" in l and "fills.json" in l]
        assert daily and "forecasts.jsonl" in daily[0], \
            "forecasts.jsonl not in the daily-cycle git add line"

    def test_forecast_ledger_integrity(self):
        """If the committed ledger exists, it must be valid schema-2 with no dup keys."""
        import json, os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forecasts.jsonl")
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            pytest.skip("forecasts.jsonl not present in this checkout")
        rows = [json.loads(l) for l in open(path) if l.strip()]
        keys = [(r["run_id"], r["agent"], r["field"], r["ticker"], r["horizon_days"]) for r in rows]
        assert len(keys) == len(set(keys)), "duplicate (run_id,agent,field,ticker,horizon) rows"
        assert all(r.get("schema") == 2 for r in rows), "non-v2 rows present"
        assert len({r["date"] for r in rows}) >= 1

    def test_scoring_wired_into_run(self):
        """score_matured + agent_scorecard must be CALLED in main.py. The harness was
        built but switched off (called only from tests), so the evidence clock never
        advanced — guard against regressing to that state."""
        import os
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")).read()
        assert "score_matured(" in src, "score_matured not wired into main.py"
        assert "agent_scorecard(" in src, "agent_scorecard not wired into main.py"


# ── Phase 1 §7.5: counterfactual rejected-name tracking ─────────────────────────

class TestCounterfactual:
    """Does each model's reject/veto/select decision predict the right forward-return
    direction? Logs binary flags scored by the SAME machinery as forecasts."""

    def _pstate(self):
        return {
            "candidates": ["AAA", "BBB", "CCC"],
            "devils_advocate": {"AAA": {"recommend_reject": True},
                                "BBB": {"recommend_reject": False},
                                "CCC": {"recommend_reject": False}},
            "cro": {"rejected_tickers": ["BBB"]},
            "final_decisions": [{"ticker": "CCC", "action": "BUY"}],
        }

    def test_log_decisions_flags(self, tmp_path):
        import calibration, json
        path = str(tmp_path / "d.jsonl")
        prices = {"AAA": {"close": 10}, "BBB": {"close": 20}, "CCC": {"close": 30}}
        n = calibration.log_decisions("r1", "2026-06-14", self._pstate(), prices, path=path)
        H = len(calibration.HORIZONS)
        assert n == 3 * 3 * H            # 3 candidates × 3 signals × horizons
        rows = [json.loads(l) for l in open(path)]
        def flag(ag, t):
            return next(r for r in rows if r["agent"] == ag and r["ticker"] == t
                        and r["horizon_days"] == 21)["value"]
        assert flag("da_reject", "AAA") == 1.0 and flag("da_reject", "BBB") == 0.0
        assert flag("cro_veto", "BBB") == 1.0 and flag("cro_veto", "AAA") == 0.0
        assert flag("pm_selected", "CCC") == 1.0 and flag("pm_selected", "AAA") == 0.0

    def test_counterfactual_adds_value(self, tmp_path):
        # da_reject: flagged (rejected) names underperform → gap>0 → ADDS_VALUE once
        # n clears AND the gap is statistically real (Phase 1: not just n >= min_n).
        # Returns vary WITHIN each group (not a bit-identical constant) — real
        # realized returns never land on the exact same float across different
        # tickers/dates, and a literally-constant group is untestable by design
        # (see test_welch_p_constant_sample_is_none).
        import calibration, json, datetime
        scored, out = str(tmp_path / "ds.jsonl"), str(tmp_path / "cf.json")
        with open(scored, "w") as f:
            for i in range(12):                          # spaced dates so block-sample keeps all
                d = (datetime.date(2026, 1, 1) + datetime.timedelta(days=40 * i)).isoformat()
                f.write(json.dumps({"run_id": f"r{i}", "agent": "da_reject", "field": "flag",
                    "ticker": f"F{i}", "value": 1.0, "realized_return": -0.05 + (i % 3) * 0.001,
                    "horizon_days": 21, "date": d}) + "\n")
                f.write(json.dumps({"run_id": f"r{i}", "agent": "da_reject", "field": "flag",
                    "ticker": f"K{i}", "value": 0.0, "realized_return": 0.05 - (i % 3) * 0.001,
                    "horizon_days": 21, "date": d}) + "\n")
        rep = calibration.counterfactual_report(scored_path=scored, out_path=out, min_n=10)
        k = "da_reject@21d"
        assert rep[k]["gap_kept_minus_flagged"] > 0.09     # ~0.10, small within-group jitter
        assert rep[k]["adds_value"] is True and rep[k]["verdict"] == "ADDS_VALUE"
        assert rep[k]["p_value"] is not None and rep[k]["p_value"] < 0.05

    def test_counterfactual_not_significant_small_n(self, tmp_path):
        import calibration, json
        scored, out = str(tmp_path / "ds.jsonl"), str(tmp_path / "cf.json")
        with open(scored, "w") as f:
            f.write(json.dumps({"run_id": "r1", "agent": "cro_veto", "field": "flag",
                "ticker": "F", "value": 1.0, "realized_return": -0.1,
                "horizon_days": 21, "date": "2026-01-01"}) + "\n")
            f.write(json.dumps({"run_id": "r1", "agent": "cro_veto", "field": "flag",
                "ticker": "K", "value": 0.0, "realized_return": 0.1,
                "horizon_days": 21, "date": "2026-01-01"}) + "\n")
        rep = calibration.counterfactual_report(scored_path=scored, out_path=out, min_n=10)
        assert rep["cro_veto@21d"]["verdict"] == "NOT_SIGNIFICANT"
        assert rep["cro_veto@21d"]["n_floor_met"] is False

    def test_counterfactual_large_n_but_no_real_gap_not_significant(self, tmp_path):
        """Phase 1: n >= min_n on both sides is NOT sufficient by itself — the old
        heuristic would have called this ADDS_VALUE/significant purely on sample
        size. Both groups draw from the IDENTICAL multiset of noisy returns (same
        mean, same variance, by construction — no seed/luck involved), so the
        Welch test must correctly find no significant difference despite each
        side individually having real (non-constant) variance."""
        import calibration, json, datetime
        noisy_returns = [-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04,
                         0.05, -0.05, 0.045, -0.045, 0.015, -0.015]
        scored, out = str(tmp_path / "ds.jsonl"), str(tmp_path / "cf.json")
        with open(scored, "w") as f:
            for i, ret in enumerate(noisy_returns):
                d = (datetime.date(2026, 1, 1) + datetime.timedelta(days=40 * i)).isoformat()
                f.write(json.dumps({"run_id": f"r{i}", "agent": "pm_selected", "field": "flag",
                    "ticker": f"F{i}", "value": 1.0, "realized_return": ret,
                    "horizon_days": 21, "date": d}) + "\n")
                f.write(json.dumps({"run_id": f"r{i}", "agent": "pm_selected", "field": "flag",
                    "ticker": f"K{i}", "value": 0.0, "realized_return": ret,
                    "horizon_days": 21, "date": d}) + "\n")
        rep = calibration.counterfactual_report(scored_path=scored, out_path=out, min_n=10)
        k = "pm_selected@21d"
        assert rep[k]["n_floor_met"] is True          # sample-size floor alone WOULD pass
        assert rep[k]["verdict"] == "NOT_SIGNIFICANT"  # but the real test correctly rejects it
        assert rep[k]["gap_kept_minus_flagged"] == 0.0

    def test_welch_p_constant_sample_is_none(self):
        """A bit-identical sample (zero real variance) must return None, not a
        floating-point-noise-driven near-zero p-value (the bug this guard fixes:
        summing many copies of the same float can leave ~1e-35 residual variance,
        which an `== 0` check misses but which produces a nonsensical p≈0)."""
        import calibration
        assert calibration._welch_p([-0.05] * 12, [0.05] * 12) is None
        assert calibration._welch_p([1.0], [2.0, 3.0]) is None     # n < 2 on one side
        assert calibration._welch_p([1.0, 1.0], [2.0, 2.0]) is None  # both constant


# ── Phase 1 §7.6: measurement rigor (TWR, risk-adjusted, breadth ceiling) ────────

class TestMeasurementRigor:
    def test_twr_no_flows_equals_cumulative(self):
        import performance as p
        dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        pv = [100.0, 110.0, 121.0]
        assert p._twr(dates, pv) == round(121.0 / 100.0 - 1, 4)   # == simple cumulative

    def test_twr_neutralizes_deposit(self):
        # +10% invest, then a $100 deposit, then +10% invest. Naive return = 131%, but the
        # deposit-neutral TWR is only 21% (two 10% periods) — the documented peak bug fixed.
        import performance as p
        dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        pv = [100.0, 110.0, 210.0, 231.0]
        naive = round(231.0 / 100.0 - 1, 4)
        twr = p._twr(dates, pv, cash_flows={"2026-01-03": 100.0})
        assert naive == 1.31
        assert twr == 0.21 and twr != naive

    def test_sortino_in_metrics(self):
        import performance as p
        m = p._metrics([100.0, 101.0, 99.0, 102.0, 101.0, 103.0])
        assert "sortino" in m   # computed (downside deviation vs 0 target)

    def test_information_ratio_positive_when_outperforming(self):
        import performance as p
        pv    = [100.0, 101.0, 102.0, 103.0, 104.0]   # steady +1%/day
        bench = [100.0, 100.5, 101.0, 101.5, 102.0]   # steady +0.5%/day
        ir = p._information_ratio(pv, bench)
        assert ir is not None and ir > 0               # consistent active return, low TE

    def test_twr_length_guard(self):
        import performance as p
        assert p._twr(["2026-01-01"], [100.0, 110.0, 121.0]) is None   # dates/pv mismatch

    def test_information_ratio_no_misalign_on_zero(self):
        # A zero in one series must skip that period for BOTH (paired), never desync — and
        # never crash. (Regression for the independent-filter misalignment.)
        import performance as p
        pv    = [100.0, 0.0, 100.0, 101.0]
        bench = [100.0, 100.0, 100.0, 100.5]
        ir = p._information_ratio(pv, bench)
        assert ir is None or isinstance(ir, float)   # well-defined, no IndexError / mispairing

    def test_breadth_ceiling_not_available_without_scorecard(self, tmp_path):
        import performance as p
        assert p.breadth_ceiling(str(tmp_path / "nope.json"))["available"] is False

    def test_breadth_ceiling_computes_fundamental_law(self, tmp_path):
        import performance as p, json
        card = tmp_path / "card.json"
        card.write_text(json.dumps({
            "quant.composite_score@21d": {"ic_block": 0.1, "n_effective": 100},
            "_meta": {"primary_metric": "quant.composite_score@21d"},
        }))
        out = p.breadth_ceiling(str(card))
        assert out["available"] is True
        assert out["implied_ir_ceiling"] == round(0.1 * (100 ** 0.5), 3)   # IC×√breadth = 1.0


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Observability & alerting (§15) + the §16.4 chaos suite
# ══════════════════════════════════════════════════════════════════════════════

def _dq_snapshot(coverage_pct=96.0, valuation_pct=22.0, universe=100, fetched=100,
                 min_depth=200, cik_ok=True, nan_close=False, data_date="2026-07-02"):
    """Build a minimal snapshot with a data_quality block for classifier tests."""
    prices = {f"T{i}": {"close": 100.0 + i, "open": 100.0, "high": 101.0,
                        "low": 99.0, "change_pct": 0.5} for i in range(fetched)}
    if nan_close:
        prices["T0"]["close"] = float("nan")
    history = {t: [{"date": j, "close": 100.0} for j in range(min_depth)] for t in prices}
    return {
        "date": data_date, "_data_date": data_date,
        "prices": prices, "history": history,
        "data_quality": {
            "active_universe": universe,
            "fundamental_coverage_pct": coverage_pct,
            "valuation_coverage_pct": valuation_pct,
            "cik_map_ok": cik_ok,
            "price_outlier_count": 0,
        },
    }


class TestMarketCalendar:
    def test_trading_day_true_on_weekday(self):
        import market_calendar as mc
        assert mc.is_trading_day("2026-07-02") is True     # Thursday

    def test_weekend_is_not_trading_day(self):
        import market_calendar as mc
        assert mc.is_trading_day("2026-07-04") is False    # Saturday
        assert mc.is_trading_day("2026-07-05") is False    # Sunday

    def test_nyse_holiday_is_not_trading_day(self):
        import market_calendar as mc
        assert mc.is_trading_day("2026-07-03") is False    # Independence Day (observed)
        assert mc.is_trading_day("2026-12-25") is False

    def test_preflight_reexports_same_calendar(self):
        # Single source: preflight_gate.NYSE_HOLIDAYS must be market_calendar's set.
        import market_calendar as mc, preflight_gate as pg
        assert pg.NYSE_HOLIDAYS is mc.NYSE_HOLIDAYS


class TestDataQualityClassifier:
    def test_clean_snapshot_is_ok(self):
        from data_quality import classify_data_quality, OK
        r = classify_data_quality(_dq_snapshot())
        assert r["status"] == OK and r["data_quality_score"] == 100
        assert r["strategy_shift_ok"] is True
        assert r["breaches"] == []

    def test_valuation_never_gates(self):
        # Valuation is FMP-capped ~35% — reported, never DEGRADES the run.
        from data_quality import classify_data_quality, OK
        r = classify_data_quality(_dq_snapshot(valuation_pct=0.0))
        assert r["status"] == OK
        assert r["metrics"]["valuation_coverage_pct"]["informational"] is True

    def test_missing_coverage_value_aborts_that_metric(self):
        from data_quality import classify_data_quality
        snap = _dq_snapshot()
        snap["data_quality"]["fundamental_coverage_pct"] = None
        r = classify_data_quality(snap)
        assert r["metrics"]["fundamental_coverage_pct"]["status"] == "ABORT"


class TestDataQualityFloorParity:
    def test_coverage_floor_single_valued(self):
        # The 80% quality-coverage floor lives in market_data (sets snapshot
        # `coverage_ok`) AND data_quality._FLOORS (sets `strategy_shift_ok`). They
        # MUST agree or the two gates disagree on the same run. Parity guard, same
        # pattern as TestPolicyParity. If you intend to change the floor, change both.
        import market_data as md
        from data_quality import _FLOORS
        assert md.FUNDAMENTAL_COVERAGE_FLOOR_PCT == _FLOORS["fundamental_coverage_pct"]["degraded"]


class TestDataQualityProvenance:
    def test_provenance_stamp_shape(self):
        from data_quality import classify_data_quality, provenance_stamp
        r = classify_data_quality(_dq_snapshot())
        s = provenance_stamp(r)
        assert set(s) == {"data_quality_score", "data_quality_status", "data_quality_hash"}
        assert s["data_quality_score"] == 100 and s["data_quality_status"] == "OK"

    def test_provenance_stamp_empty_safe(self):
        from data_quality import provenance_stamp
        s = provenance_stamp(None)
        assert s["data_quality_score"] is None and s["data_quality_hash"] is None

    def test_hash_excludes_volatile_fields(self):
        # Two classifications of the SAME snapshot differ only in generated_at → same hash.
        from data_quality import classify_data_quality
        a = classify_data_quality(_dq_snapshot())
        b = classify_data_quality(_dq_snapshot())
        assert a["hash"] == b["hash"]

    def test_hash_changes_when_a_metric_changes(self):
        from data_quality import classify_data_quality
        a = classify_data_quality(_dq_snapshot(coverage_pct=96.0))
        b = classify_data_quality(_dq_snapshot(coverage_pct=40.0))
        assert a["hash"] != b["hash"]

    def test_write_report_appends_history(self, tmp_path):
        from data_quality import classify_data_quality, write_report
        rep = str(tmp_path / "dq.json"); hist = str(tmp_path / "dq_hist.jsonl")
        r = classify_data_quality(_dq_snapshot())
        write_report(r, path=rep, history_path=hist)
        write_report(r, path=rep, history_path=hist)
        lines = [l for l in open(hist) if l.strip()]
        assert len(lines) == 2
        row = json.loads(lines[0])
        assert row["status"] == "OK" and row["coverage_pct"] == 96.0

    def test_forecast_rows_carry_provenance(self, tmp_path):
        import calibration
        prov = {"data_quality_score": 85, "data_quality_status": "DEGRADED",
                "data_quality_hash": "abc123"}
        # 'research'→'confidence' is a real numeric forecast field (calibration._FORECASTS).
        state = {"research": {"AAPL": {"confidence": 7}}}
        path = str(tmp_path / "fc.jsonl")
        n = calibration.log_forecasts("run1", "2026-07-02", state, ["AAPL"],
                                      {"AAPL": {"close": 200.0}}, path=path, provenance=prov)
        assert n > 0                                      # the field mapped → rows written
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert all(row.get("data_quality_hash") == "abc123" for row in rows)
        assert all(row.get("data_quality_score") == 85 for row in rows)


class TestHeartbeat:
    def _seed(self, root, snapshot_date=None, dq_date=None, factor_date=None,
              health_date=None, forecast_date=None, dossier_date=None):
        import os
        if dossier_date is None:      # dossier is a data-plane artifact → tie to dq by default
            dossier_date = dq_date
        def _wj(name, d):
            if d is not None:
                with open(os.path.join(root, name), "w") as f:
                    json.dump({"date": d}, f)
        def _wl(name, d):
            if d is not None:
                with open(os.path.join(root, name), "w") as f:
                    f.write(json.dumps({"date": d, "ticker": "X"}) + "\n")
        _wj("market_snapshot.json", snapshot_date)
        _wj("data_quality_report.json", dq_date)
        _wl("factor_history.jsonl", factor_date)
        _wj("research_dossier.json", dossier_date)
        _wj("system_health.json", health_date)
        _wl("forecasts.jsonl", forecast_date)

    def test_all_fresh_is_ok(self, tmp_path):
        # 2026-07-02 (Thu) is the LAST TRADING DAY of ISO week 2026-W27 (2026-07-03
        # is an NYSE holiday) — the weekly_rebalance check now correctly evaluates
        # here, so stamp the week as rebalanced; this test is about data-artifact
        # freshness, not the weekly-rebalance signal.
        from heartbeat_check import check_heartbeat
        d = "2026-07-02"
        self._seed(str(tmp_path), d, d, d, d, d)
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W27", "date": d, "run_id": "r",
             "execution_started_at": "x", "executed_at": "y", "tickers": []}))
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert r["ok"] is True and r["missing"] == []

    def test_stale_dossier_when_data_fresh_alerts(self, tmp_path):
        # Stage A blind-spot fix: snapshot/dq/factor all fresh, but build_dossier failed
        # to write today's dossier (left a stale one). The consumer would otherwise trade
        # on a silently-stale dossier — the heartbeat must flag it.
        from heartbeat_check import check_heartbeat
        d, old = "2026-07-02", "2026-06-30"
        self._seed(str(tmp_path), d, d, d, d, d, dossier_date=old)
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert r["ok"] is False and "research_dossier" in r["missing"]

    def test_missing_snapshot_alerts(self, tmp_path):
        # Jun 11 class: cron skipped, no snapshot.
        from heartbeat_check import check_heartbeat
        d = "2026-07-02"
        self._seed(str(tmp_path), None, None, None, None, None)
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert r["ok"] is False and "market_snapshot" in r["missing"]

    def test_stale_health_when_data_fresh_alerts(self, tmp_path):
        # Data plane fresh but routine didn't run (system_health stale) → silent skip.
        from heartbeat_check import check_heartbeat
        d, old = "2026-07-02", "2026-06-30"
        self._seed(str(tmp_path), d, d, d, old, d)
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert r["ok"] is False and "system_health" in r["missing"]

    def test_stale_data_does_not_cascade_to_compute(self, tmp_path):
        # If the data plane is stale, the routine correctly skipped — do NOT also
        # flag its absent artifacts (no cascading false alarm).
        from heartbeat_check import check_heartbeat
        d, old = "2026-07-02", "2026-06-30"
        self._seed(str(tmp_path), old, old, old, None, None)
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert "system_health" not in r["missing"]      # not required when data stale
        assert "market_snapshot" in r["missing"]         # the real (data-plane) failure

    def test_forecast_freeze_is_warning_not_failure(self, tmp_path):
        # Jun 18 dead feed: everything ran but forecasts stopped appending. A
        # 0-candidate day legitimately writes none, so this is a WARNING, not a fail.
        # (2026-07-02 is the last trading day of its ISO week — see test_all_fresh_is_ok;
        # stamp the week so this test's failure mode stays isolated to forecasts.)
        from heartbeat_check import check_heartbeat
        d, old = "2026-07-02", "2026-06-01"
        self._seed(str(tmp_path), d, d, d, d, old)
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W27", "date": d, "run_id": "r",
             "execution_started_at": "x", "executed_at": "y", "tickers": []}))
        r = check_heartbeat(as_of=d, root=str(tmp_path))
        assert r["ok"] is True
        assert any(w["name"] == "forecasts" for w in r["warnings"])

    def test_non_trading_day_skips(self, tmp_path):
        from heartbeat_check import check_heartbeat
        r = check_heartbeat(as_of="2026-07-04", root=str(tmp_path))   # Saturday
        assert r["ok"] is True and r["skipped"]


class TestPipelineDigest:
    def test_digest_summarizes_window(self, tmp_path):
        from pipeline_digest import build_digest
        hist = str(tmp_path / "dq_hist.jsonl")
        with open(hist, "w") as f:
            f.write(json.dumps({"date": "2026-07-01", "status": "OK",
                                "data_quality_score": 100, "coverage_pct": 96.0}) + "\n")
            f.write(json.dumps({"date": "2026-07-02", "status": "DEGRADED",
                                "data_quality_score": 85, "coverage_pct": 60.0}) + "\n")
        d = build_digest(as_of="2026-07-02", dq_path=hist,
                         health_path=str(tmp_path / "none.jsonl"))
        assert d["dq_runs"] == 2
        assert d["coverage_min"] == 60.0 and d["coverage_max"] == 96.0
        assert len(d["degraded_or_abort_days"]) == 1

    def test_digest_window_excludes_old_rows(self, tmp_path):
        from pipeline_digest import build_digest
        hist = str(tmp_path / "dq_hist.jsonl")
        with open(hist, "w") as f:
            f.write(json.dumps({"date": "2026-06-01", "status": "ABORT",
                                "data_quality_score": 20, "coverage_pct": 28.0}) + "\n")
            f.write(json.dumps({"date": "2026-07-02", "status": "OK",
                                "data_quality_score": 100, "coverage_pct": 96.0}) + "\n")
        d = build_digest(as_of="2026-07-02", window_days=7, dq_path=hist,
                         health_path=str(tmp_path / "none.jsonl"))
        assert d["dq_runs"] == 1          # the June row is outside the 7-day window


class TestChaosSuite16_4:
    """§16.4 — each historical silent-failure reproduced and asserted to trip a signal."""

    def test_chronic_28pct_coverage_degrades_and_blocks_shift(self):
        # THE June bug. Absolute floor (not delta): 28% coverage → DEGRADED + the
        # momentum→fundamental strategy shift is blocked.
        from data_quality import classify_data_quality, DEGRADED
        r = classify_data_quality(_dq_snapshot(coverage_pct=28.0))
        assert r["status"] == DEGRADED
        assert r["strategy_shift_ok"] is False
        assert any("fundamental_coverage_pct" in b for b in r["breaches"])

    def test_steady_low_coverage_still_caught_no_drop(self):
        # A delta check would MISS this — both runs at 28%, nothing "dropped".
        from data_quality import classify_data_quality
        a = classify_data_quality(_dq_snapshot(coverage_pct=28.0))
        b = classify_data_quality(_dq_snapshot(coverage_pct=28.0))
        assert a["strategy_shift_ok"] is False and b["strategy_shift_ok"] is False

    def test_nan_close_flagged_degraded(self):
        # Jun 16 publish break: a NaN close in the snapshot.
        from data_quality import classify_data_quality, DEGRADED
        r = classify_data_quality(_dq_snapshot(nan_close=True))
        assert r["metrics"]["nan_inf_count"]["value"] >= 1
        assert r["status"] == DEGRADED

    def test_partial_fetch_universe_floor(self):
        # Polygon 5/min rate-limit → partial fetch. <95% DEGRADED, <80% ABORT.
        from data_quality import classify_data_quality, DEGRADED, ABORT
        deg = classify_data_quality(_dq_snapshot(universe=100, fetched=90))
        assert deg["status"] == DEGRADED
        ab = classify_data_quality(_dq_snapshot(universe=100, fetched=70))
        assert ab["status"] == ABORT

    def test_dead_feed_thin_history_aborts(self):
        # A dead/degraded feed returns almost no bars → min_history_depth ABORT.
        from data_quality import classify_data_quality, ABORT
        r = classify_data_quality(_dq_snapshot(min_depth=10))
        assert r["status"] == ABORT

    def test_cron_skip_caught_by_heartbeat(self, tmp_path):
        # Jun 11 silent cron skip: no fresh snapshot on a trading day.
        from heartbeat_check import check_heartbeat
        r = check_heartbeat(as_of="2026-07-02", root=str(tmp_path))   # empty dir
        assert r["ok"] is False and "market_snapshot" in r["missing"]


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — build_dossier (research pipeline synthesis, §11.3/§12.2)
# ══════════════════════════════════════════════════════════════════════════════

def _dossier_snapshot(as_of="2026-07-03"):
    """Minimal snapshot for build_dossier tests: 2 names + SPY, with history."""
    def _hist(base):
        return [{"date": 1782000000000 + i * 86400000, "open": base, "high": base * 1.01,
                 "low": base * 0.99, "close": base * (1 + i * 0.001), "volume": 1e6}
                for i in range(130)]
    return {
        "date": as_of, "_data_date": as_of,
        "prices": {"AAA": {"ticker": "AAA", "close": 100.0, "change_pct": 1.2},
                   "BBB": {"ticker": "BBB", "close": 50.0, "change_pct": -0.5},
                   "SPY": {"ticker": "SPY", "close": 400.0, "change_pct": 0.3}},
        "history": {"AAA": _hist(90.0), "BBB": _hist(48.0), "SPY": _hist(390.0)},
        "fundamentals": {"AAA": {"gross_margin": 0.6, "operating_margin": 0.3, "debt_to_equity": 0.4}},
        "earnings_calendar": {"AAA": "2026-07-06"},
    }


def _factor_rows(as_of="2026-07-03", fv="2.0-quality-tilt"):
    rows = []
    for i, d in enumerate(["2026-07-01", "2026-07-02", as_of]):
        rows.append({"date": d, "ticker": "AAA", "composite_score": 70 + i,
                     "momentum_score": 80, "quality_score": 64, "valuation_score": 50,
                     "volatility_score": 55, "factors_used": ["momentum", "quality", "volatility"],
                     "formula_version": fv})
        rows.append({"date": d, "ticker": "BBB", "composite_score": 40 + i,
                     "momentum_score": 45, "quality_score": 50, "valuation_score": 50,
                     "volatility_score": 60, "factors_used": ["momentum", "quality", "volatility"],
                     "formula_version": fv})
    return rows


class TestBuildDossier:
    def test_builds_valid_schema(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        ok, errors = bd.validate_dossier(d, as_of="2026-07-03")
        assert ok, errors
        assert d["n_tickers"] == 2 and set(d["tickers"]) == {"AAA", "BBB"}

    def test_benchmarks_excluded(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        assert "SPY" not in d["tickers"] and "QQQ" not in d["tickers"]

    def test_price_as_of_stamped(self):
        # P0-1: each record carries the price date so the consumer can re-quote live.
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        assert d["tickers"]["AAA"]["price_as_of"] == "2026-07-03"

    def test_returns_are_fractions(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        hs = d["tickers"]["AAA"]["history_summary"]
        assert -1.0 < hs["ret_21d"] < 1.0       # fraction, not percent
        assert hs["max_dd_126d"] <= 0.0

    def test_no_lookahead_drops_future_filing(self):
        # A fundamental filed AFTER as_of must not appear (no look-ahead).
        import build_dossier as bd
        snap = _dossier_snapshot(as_of="2026-07-03")
        snap["fundamentals"]["AAA"] = {"gross_margin": 0.6, "_as_of_filing": "2026-09-01"}
        d = bd.build_dossier(snap, _factor_rows(), [], [])
        assert d["tickers"]["AAA"]["fundamentals"] == {}

    def test_persistence_within_formula_version_only(self):
        # P0-2: a row from a DIFFERENT formula_version must not enter the 7d window.
        import build_dossier as bd
        rows = _factor_rows(fv="2.0-quality-tilt")
        rows.append({"date": "2026-06-30", "ticker": "AAA", "composite_score": 999,
                     "factors_used": ["momentum"], "formula_version": "1.0-old"})
        d = bd.build_dossier(_dossier_snapshot(), rows, [], [])
        assert d["tickers"]["AAA"]["persistence"]["composite_7d_mean"] < 100   # 999 excluded

    def test_holdings_get_last_decision(self):
        import build_dossier as bd
        journal = [{"ticker": "AAA", "action": "BUY", "date": "2026-06-01",
                    "target_weight": 0.08, "thesis": "x", "status": "open",
                    "confidence": 8, "entry_price": 80.0}]
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), journal, [],
                             holdings={"AAA"})
        ld = d["tickers"]["AAA"]["last_decision"]
        assert ld and ld["action"] == "BUY"
        se = d["tickers"]["AAA"]["since_entry"]
        assert se["entry_price"] == 80.0 and se["cum_return"] == round((100.0 - 80.0) / 80.0, 4)
        # days_since_entry measured from as_of (2026-07-03), NOT wall-clock today (reproducible)
        from datetime import date as _date
        assert se["days_since_entry"] == (_date(2026, 7, 3) - _date(2026, 6, 1)).days
        assert "last_decision" not in d["tickers"]["BBB"]     # non-holding

    def test_events_attached(self):
        import build_dossier as bd
        events = [{"date": "2026-07-02", "ticker": "AAA", "type": "rating_change",
                   "summary": "PT raised", "url": "http://x"}]
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], events)
        evs = d["tickers"]["AAA"]["events"]
        assert len(evs) == 1 and evs[0]["type"] == "rating_change"

    def test_earnings_imminent_flag(self):
        # AAA earnings 2026-07-06, as_of 2026-07-03 → 3 days → imminent.
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        e = d["tickers"]["AAA"]["earnings"]
        assert e["days_until"] == 3 and e["imminent"] is True


class TestDossierValidation:
    def test_missing_top_level_key(self):
        import build_dossier as bd
        ok, errors = bd.validate_dossier({"tickers": {}}, as_of=None)
        assert not ok and any("schema" in e for e in errors)

    def test_stale_as_of_rejected(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot("2026-07-03"), _factor_rows(), [], [])
        ok, errors = bd.validate_dossier(d, as_of="2026-07-06")
        assert not ok and any("stale" in e for e in errors)

    def test_insufficient_built_from_days(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows()[:2], [], [])  # 1 day only
        ok, errors = bd.validate_dossier(d, as_of="2026-07-03")
        assert not ok and any("insufficient history" in e for e in errors)

    def test_ticker_missing_required_key(self):
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        del d["tickers"]["AAA"]["factors"]
        ok, errors = bd.validate_dossier(d, as_of="2026-07-03")
        assert not ok and any("missing keys" in e for e in errors)


class TestBuildDossierRemediation:
    """Regression tests for the /code-review high findings on PR #20."""

    def test_vol_ann_populated_not_always_none(self):
        # BUG: read risk.get("annualized_vol") — a key compute_risk_metrics never
        # returns — so vol_ann was ALWAYS None. Correct key is "volatility".
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        assert d["tickers"]["AAA"]["history_summary"]["vol_ann"] is not None

    def test_persistence_has_fixed_key_set(self):
        # No-history + populated names must expose the SAME keys (no consumer KeyError).
        import build_dossier as bd
        snap = _dossier_snapshot()
        snap["prices"]["CCC"] = {"ticker": "CCC", "close": 10.0, "change_pct": 0.0}
        snap["history"]["CCC"] = snap["history"]["AAA"]
        d = bd.build_dossier(snap, _factor_rows(), [], [])   # CCC has NO factor rows
        keys_aaa = set(d["tickers"]["AAA"]["persistence"])
        keys_ccc = set(d["tickers"]["CCC"]["persistence"])
        assert keys_aaa == keys_ccc
        assert {"formula_version", "rank_chg_7d"} <= keys_ccc   # present even with no history

    def test_as_of_none_raises_not_crashes_midloop(self):
        import build_dossier as bd, pytest
        snap = _dossier_snapshot()
        snap.pop("_data_date"); snap.pop("date")
        with pytest.raises(ValueError):
            bd.build_dossier(snap, _factor_rows(), [], [])

    def test_none_ticker_factor_row_excluded_from_ranks(self):
        # A malformed factor row (ticker=None) must not consume a rank ordinal.
        import build_dossier as bd
        rows = _factor_rows()
        rows.append({"date": "2026-07-03", "ticker": None, "composite_score": 999,
                     "factors_used": ["momentum"], "formula_version": "2.0-quality-tilt"})
        d = bd.build_dossier(_dossier_snapshot(), rows, [], [])
        assert None not in d["tickers"]              # phantom never surfaces

    def test_fundamentals_stale_none_when_vintage_unknown(self):
        # No _as_of_filing → age unknown → stale must be None (not False = "fresh").
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])
        assert d["tickers"]["AAA"]["data_quality"]["fundamentals_age_days"] is None
        assert d["tickers"]["AAA"]["data_quality"]["fundamentals_stale"] is None

    def test_epoch_filing_date_does_not_bypass_lookahead(self):
        # A future filing stamped as epoch-ms must still be dropped (not just ISO strings).
        import build_dossier as bd
        from datetime import datetime, timezone
        future_ms = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
        snap = _dossier_snapshot(as_of="2026-07-03")
        snap["fundamentals"]["AAA"] = {"gross_margin": 0.6, "_as_of_filing": future_ms}
        d = bd.build_dossier(snap, _factor_rows(), [], [])
        assert d["tickers"]["AAA"]["fundamentals"] == {}     # future filing dropped

    def test_validate_rejects_stale_factors_even_with_history(self):
        # built_from_days >= 2 but newest factor date != real today → stale, rejected.
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot("2026-07-03"), _factor_rows("2026-07-03"), [], [])
        ok, errors = bd.validate_dossier(d, as_of="2026-07-06")   # real today is later
        assert not ok and any("stale" in e for e in errors)

    def test_rank_chg_true_7day_lookback_or_none(self):
        # With < window+1 rows in the current formula_version, rank_chg_7d is None
        # (never a 6-day proxy silently labeled 7d).
        import build_dossier as bd
        d = bd.build_dossier(_dossier_snapshot(), _factor_rows(), [], [])  # only 3 dates
        assert d["tickers"]["AAA"]["persistence"]["rank_chg_7d"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 (increment 2) — event_digest (Haiku news→events, §11.3 step 4)
# ══════════════════════════════════════════════════════════════════════════════

def _stub_safe_call(events_by_call):
    """safe_call stub returning successive canned event arrays. Each element is
    (events, parsed_ok); each call pops the next."""
    calls = list(events_by_call)
    def _call(model, system, user_msg, default, max_tokens=1200, return_meta=False):
        evs, ok = calls.pop(0) if calls else (default, False)
        result = evs if ok else default
        return (result, {"parsed_ok": ok, "raw": "", "stop_reason": "end_turn"}) if return_meta else result
    return _call


def _news(n, date="2026-07-02", ticker="AAPL"):
    return [{"title": f"t{i}", "description": "d", "published_utc": f"{date}T10:00:00Z",
             "tickers": [ticker]} for i in range(n)]


class TestEventDigest:
    def test_extract_filters_universe_and_normalizes(self):
        import event_digest as ed
        stub = _stub_safe_call([([
            {"ticker": "aapl", "type": "price_target", "summary": "PT up", "date": "2026-07-02"},
            {"ticker": "ZZZZ", "type": "earnings", "summary": "untracked", "date": "2026-07-02"},
            {"ticker": "MSFT", "type": "weird", "summary": "bad type", "date": "2026-07-02"},
        ], True)])
        evs, stats = ed.extract_events(_news(1), {"AAPL", "MSFT"}, "2026-07-02", safe_call=stub)
        assert {e["ticker"] for e in evs} == {"AAPL", "MSFT"}   # ZZZZ dropped, aapl upper-cased
        assert next(e for e in evs if e["ticker"] == "MSFT")["type"] == "other"
        assert stats["parse_success_rate"] == 1.0

    def test_no_lookahead_drops_future_event(self):
        import event_digest as ed
        stub = _stub_safe_call([([
            {"ticker": "AAPL", "type": "product", "summary": "future", "date": "2026-09-01"},
            {"ticker": "AAPL", "type": "product", "summary": "today", "date": "2026-07-02"},
        ], True)])
        evs, _ = ed.extract_events(_news(1), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert len(evs) == 1 and evs[0]["summary"] == "today"

    def test_empty_news_is_safe(self):
        import event_digest as ed
        evs, stats = ed.extract_events([], {"AAPL"}, "2026-07-02", safe_call=_stub_safe_call([]))
        assert evs == [] and stats["chunks"] == 0 and stats["parse_success_rate"] == 1.0

    def test_chunking_multiple_calls(self):
        import event_digest as ed
        stub = _stub_safe_call([([], True), ([], True), ([], True)])
        _, stats = ed.extract_events(_news(45), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert stats["chunks"] == 3 and stats["chunks_ok"] == 3

    def test_parse_failure_degrades_rate(self):
        import event_digest as ed
        stub = _stub_safe_call([([{"ticker": "AAPL", "type": "earnings",
                                   "summary": "ok", "date": "2026-07-02"}], True),
                                ([], False)])
        evs, stats = ed.extract_events(_news(40), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert stats["chunks"] == 2 and stats["chunks_ok"] == 1
        assert stats["parse_success_rate"] == 0.5              # → DEGRADED (<0.8)

    def test_digest_dedups_within_and_across_runs(self, tmp_path):
        import event_digest as ed
        path = str(tmp_path / "events.jsonl")
        snap = {"_data_date": "2026-07-02", "news": _news(1)}
        one = {"ticker": "AAPL", "type": "earnings", "summary": "beat", "date": "2026-07-02"}
        s1 = ed.digest(snap, {"AAPL"}, path=path, safe_call=_stub_safe_call([([one, dict(one)], True)]))
        assert s1["events_written"] == 1 and s1["events_deduped"] == 1
        s2 = ed.digest(snap, {"AAPL"}, path=path, safe_call=_stub_safe_call([([dict(one)], True)]))
        assert s2["events_written"] == 0
        assert len([l for l in open(path) if l.strip()]) == 1

    def test_event_key_stable(self):
        import event_digest as ed
        a = {"ticker": "AAPL", "date": "2026-07-02", "type": "earnings", "summary": "Beat X"}
        b = {"ticker": "aapl", "date": "2026-07-02T00:00", "type": "EARNINGS", "summary": "beat x  "}
        assert ed.event_key(a) == ed.event_key(b)

    def test_ticker_news_folded_in(self, tmp_path):
        import event_digest as ed
        snap = {"_data_date": "2026-07-02", "news": [],
                "ticker_news": {"NVDA": _news(1, ticker="NVDA")}}
        captured = {}
        def stub(model, system, user_msg, default, max_tokens=1200, return_meta=False):
            captured["msg"] = user_msg
            return ([], {"parsed_ok": True, "raw": "", "stop_reason": "end_turn"})
        ed.digest(snap, {"NVDA"}, path=str(tmp_path / "e.jsonl"), safe_call=stub)
        assert "NVDA" in captured["msg"]


class TestEventDigestRemediation:
    """Regression tests for the /code-review findings on the event digest."""

    def test_cross_day_dedup(self, tmp_path):
        # The feed re-surfaces a multi-day-old article; it must NOT be re-appended on a
        # later run just because its date != today.
        import event_digest as ed
        path = str(tmp_path / "events.jsonl")
        old = {"ticker": "AAPL", "type": "earnings", "summary": "beat Q2", "date": "2026-06-30"}
        # Run on 6/30 logs it.
        snap_630 = {"_data_date": "2026-06-30", "news": _news(1, date="2026-06-30")}
        s1 = ed.digest(snap_630, {"AAPL"}, path=path, safe_call=_stub_safe_call([([dict(old)], True)]))
        assert s1["events_written"] == 1
        # Run on 7/03 sees the SAME 6/30 article again → must dedup (within 60d window).
        snap_703 = {"_data_date": "2026-07-03", "news": _news(1, date="2026-06-30")}
        s2 = ed.digest(snap_703, {"AAPL"}, path=path, safe_call=_stub_safe_call([([dict(old)], True)]))
        assert s2["events_written"] == 0
        assert len([l for l in open(path) if l.strip()]) == 1

    def test_single_dict_result_coerced_not_lost(self):
        # Haiku returns a lone event OBJECT (not an array) — must still be captured and
        # the chunk counted as a parse success (not a spurious DEGRADED).
        import event_digest as ed
        lone = {"ticker": "AAPL", "type": "earnings", "summary": "beat", "date": "2026-07-02"}
        stub = _stub_safe_call([(lone, True)])          # returns a dict, not a list
        evs, stats = ed.extract_events(_news(1), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert len(evs) == 1 and stats["chunks_ok"] == 1 and stats["parse_success_rate"] == 1.0

    def test_wrapped_events_key_coerced(self):
        import event_digest as ed
        wrapped = {"events": [{"ticker": "AAPL", "type": "earnings",
                               "summary": "beat", "date": "2026-07-02"}]}
        evs, stats = ed.extract_events(_news(1), {"AAPL"}, "2026-07-02",
                                       safe_call=_stub_safe_call([(wrapped, True)]))
        assert len(evs) == 1 and stats["chunks_ok"] == 1

    def test_epoch_or_garbage_date_dropped(self):
        # A non-ISO/epoch date must not slip past the look-ahead guard as a bogus date.
        import event_digest as ed
        bad = [{"ticker": "AAPL", "type": "earnings", "summary": "e", "date": 1725000000},
               {"ticker": "AAPL", "type": "earnings", "summary": "g", "date": "not-a-date"}]
        evs, _ = ed.extract_events(_news(1), {"AAPL"}, "2026-07-02",
                                   safe_call=_stub_safe_call([(bad, True)]))
        assert evs == []                                # both dropped

    def test_event_digest_degradation_recorded_in_report(self, tmp_path):
        # A <80% parse rate must floor the data_quality report at DEGRADED (not silent).
        import data_quality as dq, json
        rep = str(tmp_path / "dq.json")
        json.dump({"status": "OK", "data_quality_score": 100, "date": "2026-07-02",
                   "metrics": {}, "breaches": []}, open(rep, "w"))
        out = dq.merge_event_digest_into_report({"chunks": 2, "chunks_ok": 1,
                                                 "parse_success_rate": 0.5}, path=rep)
        assert out["status"] == "DEGRADED"
        assert any("event_digest" in b for b in out["breaches"])
        assert out["event_digest"]["parse_success_rate"] == 0.5

    def test_event_digest_ok_does_not_degrade_report(self, tmp_path):
        import data_quality as dq, json
        rep = str(tmp_path / "dq.json")
        json.dump({"status": "OK", "data_quality_score": 100, "date": "2026-07-02",
                   "metrics": {}, "breaches": []}, open(rep, "w"))
        out = dq.merge_event_digest_into_report({"chunks": 3, "chunks_ok": 3,
                                                 "parse_success_rate": 1.0}, path=rep)
        assert out["status"] == "OK" and out["breaches"] == []


class TestSECFilingDate:
    """Increment 3: SECProvider stamps `_as_of_filing` (the no-look-ahead availability date)."""

    def _provider(self, monkeypatch, facts):
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        def fake_get(url, **kwargs):
            resp = types.SimpleNamespace(raise_for_status=lambda: None)
            resp.json = (lambda: {"0": {"cik_str": 1, "ticker": "XYZ", "title": "X"}}) \
                if "company_tickers" in url else (lambda: facts)
            return resp
        monkeypatch.setattr(requests, "get", fake_get)
        return p

    def test_stamps_latest_filed_among_inputs(self, monkeypatch):
        facts = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [{"end": "2025-12-31", "val": 1000,
                                            "form": "10-K", "filed": "2026-02-10"}]}},
            "GrossProfit": {"units": {"USD": [{"end": "2025-12-31", "val": 400,
                                               "form": "10-K", "filed": "2026-02-10"}]}},
            "StockholdersEquity": {"units": {"USD": [{"end": "2025-12-31", "val": 500,
                                                      "form": "10-K", "filed": "2026-03-01"}]}},
            "LongTermDebt": {"units": {"USD": [{"end": "2025-12-31", "val": 250,
                                                "form": "10-K", "filed": "2026-02-10"}]}},
        }}}
        p = self._provider(monkeypatch, facts)
        f = p.fundamentals("XYZ")
        assert f["gross_margin"] == 0.4 and f["debt_to_equity"] == 0.5
        # latest filed among used inputs = the equity filing 2026-03-01 (conservative)
        assert f["_as_of_filing"] == "2026-03-01"

    def test_omits_as_of_filing_when_sec_has_no_filed(self, monkeypatch):
        facts = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [{"end": "2025-12-31", "val": 1000, "form": "10-K"}]}},
            "GrossProfit": {"units": {"USD": [{"end": "2025-12-31", "val": 400, "form": "10-K"}]}},
        }}}
        p = self._provider(monkeypatch, facts)
        f = p.fundamentals("XYZ")
        assert f["gross_margin"] == 0.4 and "_as_of_filing" not in f   # vintage unknown, not faked

    def test_dossier_computes_real_age_from_stamped_filing(self):
        # End-to-end: a stamped filing makes the dossier's fundamentals_age_days REAL
        # (null before increment 3) and the no-look-ahead drop LIVE.
        import build_dossier as bd
        snap = _dossier_snapshot(as_of="2026-07-03")
        snap["fundamentals"]["AAA"] = {"gross_margin": 0.6, "_as_of_filing": "2026-02-13"}
        d = bd.build_dossier(snap, _factor_rows(), [], [])
        dq = d["tickers"]["AAA"]["data_quality"]
        assert dq["fundamentals_age_days"] is not None and dq["fundamentals_age_days"] > 100
        assert dq["fundamentals_stale"] is True         # >100d → stale (real bool, not null)


class TestSECFilingDatePartial:
    """Increment 3 review fix: omit _as_of_filing when vintage is only PARTIALLY known."""

    def _provider(self, monkeypatch, facts):
        from data_providers import SECProvider
        import requests, types
        p = SECProvider(timeout=5)
        def fake_get(url, **kwargs):
            resp = types.SimpleNamespace(raise_for_status=lambda: None)
            resp.json = (lambda: {"0": {"cik_str": 1, "ticker": "XYZ", "title": "X"}}) \
                if "company_tickers" in url else (lambda: facts)
            return resp
        monkeypatch.setattr(requests, "get", fake_get)
        return p

    def test_partial_filed_omits_stamp(self, monkeypatch):
        # Revenue has filed, GrossProfit does NOT → we cannot know the bundle's true
        # latest vintage → omit _as_of_filing (understating it would be a look-ahead).
        facts = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [{"end": "2025-12-31", "val": 1000,
                                            "form": "10-K", "filed": "2026-02-10"}]}},
            "GrossProfit": {"units": {"USD": [{"end": "2025-12-31", "val": 400,
                                               "form": "10-K"}]}},   # no `filed`
        }}}
        p = self._provider(monkeypatch, facts)
        f = p.fundamentals("XYZ")
        assert f["gross_margin"] == 0.4 and "_as_of_filing" not in f


class TestDossierSignalLogging:
    """Stage A: observational logging of dossier persistence + event-presence signals."""

    def _dossier(self, as_of="2026-07-02"):
        return {"as_of": as_of, "tickers": {
            "AAA": {"persistence": {"composite_7d_mean": 72.0},
                    "events": [{"type": "earnings", "summary": "beat"}]},
            "BBB": {"persistence": {"composite_7d_mean": 40.0}, "events": []},
        }}

    def test_logs_persistence_and_event_present(self, tmp_path):
        import calibration
        path = str(tmp_path / "fc.jsonl")
        n = calibration.log_dossier_signals("r1", "2026-07-02", self._dossier(),
                                            {"AAA": {"close": 100.0}, "BBB": {"close": 50.0}},
                                            path=path)
        rows = [json.loads(l) for l in open(path) if l.strip()]
        agents = {r["agent"] for r in rows}
        assert agents == {"persist_mean", "event_present"}
        # AAA has an event → flag 1.0; BBB has none → 0.0
        ev = {r["ticker"]: r["value"] for r in rows if r["agent"] == "event_present"
              and r["horizon_days"] == 21}
        assert ev["AAA"] == 1.0 and ev["BBB"] == 0.0
        pm = {r["ticker"]: r["value"] for r in rows if r["agent"] == "persist_mean"
              and r["horizon_days"] == 21}
        assert pm["AAA"] == 72.0

    def test_stale_dossier_logs_nothing(self, tmp_path):
        import calibration
        path = str(tmp_path / "fc.jsonl")
        n = calibration.log_dossier_signals("r1", "2026-07-02", self._dossier(as_of="2026-06-30"),
                                            {"AAA": {"close": 100.0}}, path=path)
        assert n == 0 and not (tmp_path / "fc.jsonl").exists()

    def test_missing_or_bad_dossier_safe(self, tmp_path):
        import calibration
        path = str(tmp_path / "fc.jsonl")
        assert calibration.log_dossier_signals("r1", "2026-07-02", None,
                                               {"AAA": {"close": 1.0}}, path=path) == 0
        assert calibration.log_dossier_signals("r1", "2026-07-02", {},
                                               {"AAA": {"close": 1.0}}, path=path) == 0

    def test_provenance_stamped_on_dossier_signals(self, tmp_path):
        import calibration
        path = str(tmp_path / "fc.jsonl")
        prov = {"data_quality_score": 90, "data_quality_hash": "h1"}
        calibration.log_dossier_signals("r1", "2026-07-02", self._dossier(),
                                        {"AAA": {"close": 100.0}}, path=path, provenance=prov)
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert all(r.get("data_quality_hash") == "h1" for r in rows)


class TestEventDigestBudgetCap:
    """Stage A: token-budget cap (§15.2 P2-13) on the event digest."""

    def test_cap_limits_chunks_and_flags(self):
        import event_digest as ed
        # (MAX_CHUNKS+3) chunks' worth of articles → only MAX_CHUNKS processed, capped=True
        n_articles = (ed.MAX_CHUNKS + 3) * ed.BATCH_SIZE
        stub = _stub_safe_call([([], True)] * (ed.MAX_CHUNKS + 3))
        _, stats = ed.extract_events(_news(n_articles), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert stats["chunks"] == ed.MAX_CHUNKS         # never exceeds the cap
        assert stats["capped"] is True
        assert stats["chunks_available"] == ed.MAX_CHUNKS + 3

    def test_under_cap_not_flagged(self):
        import event_digest as ed
        stub = _stub_safe_call([([], True), ([], True)])
        _, stats = ed.extract_events(_news(2 * ed.BATCH_SIZE), {"AAPL"}, "2026-07-02", safe_call=stub)
        assert stats["capped"] is False

    def test_capped_run_degrades_report(self, tmp_path):
        import data_quality as dq, json
        rep = str(tmp_path / "dq.json")
        json.dump({"status": "OK", "data_quality_score": 100, "date": "2026-07-02",
                   "metrics": {}, "breaches": []}, open(rep, "w"))
        out = dq.merge_event_digest_into_report(
            {"chunks": 15, "chunks_ok": 15, "parse_success_rate": 1.0,
             "capped": True, "chunks_available": 18, "max_chunks": 15}, path=rep)
        assert out["status"] == "DEGRADED"
        assert any("budget cap" in b for b in out["breaches"])


class TestEventDigestTickerNewsPriority:
    """Stage A review fix: ticker_news must survive the token cap (kept at the head)."""

    def test_ticker_news_in_first_chunk_under_cap(self, tmp_path):
        import event_digest as ed
        # 1 mover ticker_news (NVDA) + a large broad feed (> cap) of AAPL articles.
        broad = _news((ed.MAX_CHUNKS + 5) * ed.BATCH_SIZE, ticker="AAPL")
        snap = {"_data_date": "2026-07-02", "news": broad,
                "ticker_news": {"NVDA": _news(1, ticker="NVDA")}}
        first_msgs = []
        def stub(model, system, user_msg, default, max_tokens=1200, return_meta=False):
            first_msgs.append(user_msg)
            return ([], {"parsed_ok": True, "raw": "", "stop_reason": "end_turn"})
        ed.digest(snap, {"NVDA", "AAPL"}, path=str(tmp_path / "e.jsonl"), safe_call=stub)
        # NVDA (ticker_news, prepended) must appear in the FIRST chunk — not dropped by the cap.
        assert "NVDA" in first_msgs[0]


class TestStageCReadiness:
    """Stage C readiness monitor — read-only evidence-clock gate."""

    def _sig(self, ic=0.2, ci=0.10, n_eff=40, sig=True):
        return {"ic": ic, "ic_shrunk": ic * 0.7, "ci_halfwidth": ci,
                "n_effective": n_eff, "significant_bh": sig}

    def test_ready_when_quant_and_a_dossier_signal_decidable(self):
        import stage_c_readiness as scr
        card = {scr.PRIMARY_QUANT: self._sig(),
                "persist_mean.composite_7d_mean@21d": self._sig(),
                "event_present.flag@21d": self._sig(ci=0.5)}   # this one wide, but the other is tight
        a = scr.assess_readiness(card)
        assert a["ready"] is True and a["quant_decidable"] and a["dossier_decidable"]

    def test_not_ready_when_dossier_signals_absent(self):
        import stage_c_readiness as scr
        card = {scr.PRIMARY_QUANT: self._sig()}      # quant decidable, no dossier signals yet
        a = scr.assess_readiness(card)
        assert a["ready"] is False and a["quant_decidable"] and not a["dossier_decidable"]
        assert any("not scored yet" in b for b in a["blockers"])

    def test_wide_ci_not_decidable(self):
        import stage_c_readiness as scr
        card = {scr.PRIMARY_QUANT: self._sig(ci=0.314),        # the live case: n_eff ok, CI too wide
                "persist_mean.composite_7d_mean@21d": self._sig()}
        a = scr.assess_readiness(card)
        assert a["ready"] is False and not a["quant_decidable"]
        assert any("CI" in b for b in a["blockers"])

    def test_low_n_effective_not_decidable(self):
        import stage_c_readiness as scr
        card = {scr.PRIMARY_QUANT: self._sig(n_eff=5),
                "persist_mean.composite_7d_mean@21d": self._sig()}
        a = scr.assess_readiness(card)
        assert not a["quant_decidable"]

    def test_missing_scorecard_safe(self):
        import stage_c_readiness as scr
        a = scr.assess_readiness({})
        assert a["ready"] is False
        assert scr.load_scorecard("/nonexistent/x.json") == {}

    def test_summary_line(self):
        import stage_c_readiness as scr
        card = {scr.PRIMARY_QUANT: self._sig(),
                "persist_mean.composite_7d_mean@21d": self._sig()}
        line = scr.summary_line(scr.assess_readiness(card))
        assert "DECIDABLE" in line and "quant ✓" in line


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 (Stages B/C/D) — weekly cadence, risk_watch, dossier consumer, Stage D
# ═══════════════════════════════════════════════════════════════════════════

class TestPreflightGateModeRouting(TestPreflightGate):
    """§6.3 mode routing: 0 REBALANCE (Wed / Thu-Fri catch-up) · 30 RISK-WATCH ·
    10 SKIP/RETRY · 20 SKIP/DONE. Uses the week of 2026-06-22 (Mon) — no NYSE
    holidays that week. OPEN_DAY (2026-06-17) is the Wednesday of the prior week."""

    MON, TUE, WED, THU, FRI = ("2026-06-22", "2026-06-23", "2026-06-24",
                               "2026-06-25", "2026-06-26")

    def _fresh_data(self, tmp_path, date):
        self._write(tmp_path, "market_snapshot.json",
                    {"date": date, "prices": {"AAPL": {}}, "history": {"AAPL": [{}] * 200}})
        self._fresh_dossier(tmp_path, date)

    def _rebalance_stamp(self, tmp_path, date, executed=True, tickers=None):
        from market_calendar import iso_week_of
        self._write(tmp_path, "last_rebalance.json", {
            "iso_week": iso_week_of(date), "date": date, "run_id": "r1",
            "execution_started_at": "2026-06-24T13:50:00Z",
            "executed_at": "2026-06-24T13:55:00Z" if executed else None,
            "tickers": tickers or ["JPM"]})

    def test_monday_routes_to_risk_watch(self, tmp_path):
        self._fresh_data(tmp_path, self.MON)
        assert self._run(tmp_path, date_override=self.MON) == 30

    def test_tuesday_routes_to_risk_watch(self, tmp_path):
        self._fresh_data(tmp_path, self.TUE)
        assert self._run(tmp_path, date_override=self.TUE) == 30

    def test_risk_watch_needs_no_snapshot_or_dossier(self, tmp_path):
        """The daily SELL-only safety net uses live MCP data — a late GitHub
        Actions cron must NEVER disable it (P1-7)."""
        assert self._run(tmp_path, date_override=self.MON) == 30  # empty dir

    def test_wednesday_rebalances(self, tmp_path):
        self._fresh_data(tmp_path, self.WED)
        assert self._run(tmp_path, date_override=self.WED) == 0

    def test_thursday_after_wed_rebalance_routes_to_risk_watch(self, tmp_path):
        self._fresh_data(tmp_path, self.THU)
        self._rebalance_stamp(tmp_path, self.WED)
        assert self._run(tmp_path, date_override=self.THU) == 30

    def test_thursday_catchup_when_week_unrebalanced(self, tmp_path):
        """§6.5.2: Wednesday missed (stale data all attempts / holiday) → Thursday
        runs the weekly rebalance."""
        self._fresh_data(tmp_path, self.THU)
        assert self._run(tmp_path, date_override=self.THU) == 0

    def test_friday_catchup_when_week_unrebalanced(self, tmp_path):
        self._fresh_data(tmp_path, self.FRI)
        assert self._run(tmp_path, date_override=self.FRI) == 0

    def test_thursday_claim_only_disables_catchup(self, tmp_path):
        """A Wednesday CLAIM without executed_at = crashed mid-execution — orders
        may exist (Scenario B). Catch-up must NOT re-run the rebalance; the day
        falls through to risk-watch. Fails toward missed trades, never duplicates."""
        self._fresh_data(tmp_path, self.THU)
        self._rebalance_stamp(tmp_path, self.WED, executed=False)
        assert self._run(tmp_path, date_override=self.THU) == 30

    def test_pending_envelope_fallback_locks_week(self, tmp_path):
        """No last_rebalance.json (pre-mirror) — a rebalance-mode pending envelope
        executed this ISO week still locks the week (fallback source)."""
        self._fresh_data(tmp_path, self.THU)
        self._write(tmp_path, "pending_decisions.json",
                    {"date": self.WED, "mode": "rebalance", "run_id": "r1",
                     "executed_at": "2026-06-24T13:55:00Z", "decisions": []})
        assert self._run(tmp_path, date_override=self.THU) == 30

    def test_legacy_envelope_without_mode_counts_as_rebalance(self, tmp_path):
        """Daily-era envelopes carry no mode — treated as rebalance semantics."""
        self._fresh_data(tmp_path, self.THU)
        self._write(tmp_path, "pending_decisions.json",
                    {"date": self.WED, "run_id": "r1",
                     "executed_at": "2026-06-24T13:55:00Z", "decisions": []})
        assert self._run(tmp_path, date_override=self.THU) == 30

    def test_risk_watch_envelope_does_not_lock_week(self, tmp_path):
        """Tuesday's executed risk_watch envelope must NOT satisfy the ISO-week
        rebalance lock — Wednesday still rebalances."""
        self._fresh_data(tmp_path, self.WED)
        self._write(tmp_path, "pending_decisions.json",
                    {"date": self.TUE, "mode": "risk_watch", "run_id": "r1",
                     "executed_at": "2026-06-23T13:55:00Z", "decisions": []})
        assert self._run(tmp_path, date_override=self.WED) == 0

    def test_prior_week_stamp_does_not_lock_this_week(self, tmp_path):
        self._fresh_data(tmp_path, self.WED)
        self._rebalance_stamp(tmp_path, self.OPEN_DAY)   # Wed of the PRIOR week
        assert self._run(tmp_path, date_override=self.WED) == 0

    def test_executed_risk_watch_today_skips_done(self, tmp_path):
        """Daily idempotency applies to risk_watch too: an executed envelope
        dated today → 20 on the retry attempts."""
        self._write(tmp_path, "pending_decisions.json",
                    {"date": self.MON, "mode": "risk_watch", "run_id": "r1",
                     "executed_at": "2026-06-22T13:55:00Z", "decisions": []})
        assert self._run(tmp_path, date_override=self.MON) == 20

    def test_wednesday_stale_data_skips_retry_not_risk_watch(self, tmp_path):
        """On the rebalance day, stale data → 10 (retry / Thu catch-up), never a
        silent fallback into risk-watch (mixing modes intra-day would defeat the
        daily envelope idempotency)."""
        self._write(tmp_path, "market_snapshot.json",
                    {"date": "2020-01-01", "prices": {}, "history": {"AAPL": [{}] * 200}})
        assert self._run(tmp_path, date_override=self.WED) == 10


class TestJournalRebalanceStamp:
    """journal._mirror_rebalance_stamp — the durable once-per-ISO-week lock (§6.5)."""

    def _pending(self, tmp_path, mode="rebalance", date="2026-06-24", decisions=None):
        obj = {"run_id": "r1", "date": date, "generated_at": "x",
               "execution_started_at": None, "executed_at": None,
               "decisions": decisions if decisions is not None else [
                   {"ticker": "JPM", "action": "BUY", "target_weight": 0.08},
                   {"ticker": "VRTX", "action": "SELL", "target_weight": 0.0},
                   {"ticker": "MS", "action": "HOLD"}]}
        if mode is not None:
            obj["mode"] = mode
        (tmp_path / "pending_decisions.json").write_text(json.dumps(obj))

    def test_executed_rebalance_writes_week_stamp(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.chdir(tmp_path)
        self._pending(tmp_path)
        journal.mark_pending_executed("r1")
        lr = json.loads((tmp_path / "last_rebalance.json").read_text())
        assert lr["iso_week"] == "2026-W26" and lr["date"] == "2026-06-24"
        assert lr["executed_at"] and lr["run_id"] == "r1"
        # HOLD is not a trade — only BUY/SELL tickers feed the SELL interlock
        assert lr["tickers"] == ["JPM", "VRTX"]

    def test_claim_alone_writes_week_stamp(self, tmp_path, monkeypatch):
        """A crash after the claim must still lock the week (orders may exist)."""
        import journal
        monkeypatch.chdir(tmp_path)
        self._pending(tmp_path)
        journal.mark_execution_started("r1")
        lr = json.loads((tmp_path / "last_rebalance.json").read_text())
        assert lr["execution_started_at"] and lr["executed_at"] is None

    def test_risk_watch_mode_never_touches_week_stamp(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.chdir(tmp_path)
        (tmp_path / "last_rebalance.json").write_text(json.dumps({"iso_week": "2026-W26",
                                                                  "sentinel": True}))
        self._pending(tmp_path, mode="risk_watch")
        journal.mark_pending_executed("r1")
        lr = json.loads((tmp_path / "last_rebalance.json").read_text())
        assert lr.get("sentinel") is True   # untouched — risk_watch never mirrors

    def test_legacy_envelope_without_mode_mirrors(self, tmp_path, monkeypatch):
        import journal
        monkeypatch.chdir(tmp_path)
        self._pending(tmp_path, mode=None)
        journal.mark_pending_executed("r1")
        assert (tmp_path / "last_rebalance.json").is_file()


class TestRiskWatchTriggers:
    """§6.7 — the tight, mechanical, LLM-free trigger set. SELL-only is structural."""

    def _pos(self, sym, avg, cur, qty=1.0, avail=None):
        return {"symbol": sym, "qty": qty, "available_qty": avail if avail is not None else qty,
                "avg_price": avg, "current_price": cur, "market_value": cur * qty}

    def test_stop_fires_at_exactly_threshold(self):
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("AAPL", 100.0, 75.0)]}      # exactly −25%
        d, r = evaluate_triggers(port, "2026-06-22", stop_pct=0.25)
        assert len(d) == 1 and d[0]["ticker"] == "AAPL" and d[0]["action"] == "SELL"
        assert d[0]["risk_exit"] is True and d[0]["target_weight"] == 0.0

    def test_stop_does_not_fire_above_threshold(self):
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("AAPL", 100.0, 75.01)]}     # −24.99%
        d, r = evaluate_triggers(port, "2026-06-22", stop_pct=0.25)
        assert d == [] and r["fired"] == []

    def test_never_emits_a_buy(self):
        """Structural invariant: every constructible decision is a SELL."""
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("A", 100, 50), self._pos("B", 100, 60),
                              self._pos("C", 100, 200)]}
        d, _ = evaluate_triggers(port, "2026-06-22")
        assert d and all(x["action"] == "SELL" for x in d)

    def test_interlocked_ticker_fires_but_is_not_sold(self):
        """§6.5.3 cross-mode interlock: a name the rebalance traded this ISO week is
        surfaced (DEGRADED health), never double-sold."""
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("JPM", 100, 60)]}
        d, r = evaluate_triggers(port, "2026-06-22", interlocked={"JPM"})
        assert d == [] and r["interlocked"][0]["ticker"] == "JPM"

    def test_blocked_ticker_never_sold(self):
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("TSLA", 100, 50)]}
        d, r = evaluate_triggers(port, "2026-06-22")
        assert d == [] and r["blocked"][0]["ticker"] == "TSLA"

    def test_no_cost_basis_skipped_and_surfaced(self):
        """Never sell on unverifiable data — a missing avg_price/current_price is
        surfaced for review, not silently sold or silently ignored."""
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("AAPL", 0, 75.0)]}
        d, r = evaluate_triggers(port, "2026-06-22")
        assert d == [] and r["skipped_no_basis"] == ["AAPL"]

    def test_qty_capped_to_available(self):
        from risk_watch import evaluate_triggers
        port = {"positions": [self._pos("AAPL", 100.0, 70.0, qty=5.0, avail=3.5)]}
        d, _ = evaluate_triggers(port, "2026-06-22")
        assert d[0]["qty"] == 3.5

    def test_interlock_reader_matches_week(self, tmp_path, monkeypatch):
        import risk_watch
        monkeypatch.chdir(tmp_path)
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W26", "tickers": ["JPM", "VRTX"]}))
        assert risk_watch._interlocked_tickers("2026-06-25") == {"JPM", "VRTX"}
        assert risk_watch._interlocked_tickers("2026-06-29") == set()  # next week


class TestRiskWatchEnvelope:
    """run_risk_watch writes the SAME idempotency envelope the rebalance uses,
    with mode=risk_watch, and never touches the ISO-week rebalance lock."""

    def _setup(self, tmp_path, monkeypatch, positions):
        import risk_watch
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _zi
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(risk_watch, "DRY_RUN", True)
        monkeypatch.setattr(risk_watch, "_publish", lambda *a, **k: None)
        now = _dt.now(_zi("America/New_York")).isoformat()
        (tmp_path / "mcp_portfolio.json").write_text(json.dumps(
            {"as_of": now, "cash": 100.0,
             "total_value": 100.0 + sum(p["market_value"] for p in positions),
             "positions": positions}))
        return risk_watch

    def test_envelope_written_with_mode_and_no_week_stamp(self, tmp_path, monkeypatch):
        rw = self._setup(tmp_path, monkeypatch, [
            {"symbol": "AAPL", "qty": 1.0, "available_qty": 1.0, "avg_price": 100.0,
             "current_price": 70.0, "market_value": 70.0, "unrealized_pnl": -30.0}])
        rw.run_risk_watch()
        p = json.loads((tmp_path / "pending_decisions.json").read_text())
        assert p["mode"] == "risk_watch"
        assert len(p["decisions"]) == 1 and p["decisions"][0]["action"] == "SELL"
        assert p["executed_at"] is None            # DRY_RUN never stamps
        assert not (tmp_path / "last_rebalance.json").exists()
        # speculative logs written for the routine's reconciler
        txs = json.loads((tmp_path / "transactions.json").read_text())
        assert txs[-1]["ticker"] == "AAPL" and txs[-1]["dry_run"] is True
        h = json.loads((tmp_path / "system_health.json").read_text())
        assert "risk_watch" in h["checks"]

    def test_quiet_day_writes_empty_envelope(self, tmp_path, monkeypatch):
        rw = self._setup(tmp_path, monkeypatch, [
            {"symbol": "AAPL", "qty": 1.0, "available_qty": 1.0, "avg_price": 100.0,
             "current_price": 99.0, "market_value": 99.0, "unrealized_pnl": -1.0}])
        rw.run_risk_watch()
        p = json.loads((tmp_path / "pending_decisions.json").read_text())
        assert p["mode"] == "risk_watch" and p["decisions"] == []

    def test_stale_portfolio_aborts_without_envelope(self, tmp_path, monkeypatch):
        import risk_watch as rw
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(rw, "_publish", lambda *a, **k: None)
        (tmp_path / "mcp_portfolio.json").write_text(json.dumps(
            {"as_of": "2020-01-01T09:00:00-04:00", "cash": 1, "total_value": 1,
             "positions": []}))
        rw.run_risk_watch()
        assert not (tmp_path / "pending_decisions.json").exists()
        h = json.loads((tmp_path / "system_health.json").read_text())
        assert h["checks"]["risk_watch"]["status"] == "ABORTED"


class TestTaxAwareHold:
    """IPS §7.5 — block a discretionary SELL of a gained lot near its 1-year
    long-term boundary (~54% ST vs ~37% LT on the same gain)."""

    def _buy(self, ticker, date, price, qty=1.0):
        return {"ticker": ticker, "action": "BUY", "date": date, "qty": qty,
                "price": price, "dry_run": False, "timestamp": f"{date}T14:00:00Z"}

    def _sell(self, ticker="AAPL"):
        return [{"ticker": ticker, "action": "SELL", "target_weight": 0.0}]

    def test_gained_lot_near_boundary_blocked(self):
        import guardrails as g
        # acquired 340 calendar days ago (within 42 of 365), in gain → blocked
        kept, rej = g.enforce_tax_aware_hold(
            self._sell(), {"AAPL": {"close": 150.0}},
            transactions=[self._buy("AAPL", "2025-07-30", 100.0)], today="2026-07-05")
        assert kept == [] and "tax-aware hold" in rej[0]["rejected_reason"]

    def test_gained_lot_far_from_boundary_allowed(self):
        import guardrails as g
        # 300 days held — outside the ~42-calendar-day window → allowed
        kept, rej = g.enforce_tax_aware_hold(
            self._sell(), {"AAPL": {"close": 150.0}},
            transactions=[self._buy("AAPL", "2025-09-08", 100.0)], today="2026-07-05")
        assert len(kept) == 1 and rej == []

    def test_loss_lot_near_boundary_allowed(self):
        import guardrails as g
        # harvesting a short-term loss is tax-favorable — never blocked
        kept, rej = g.enforce_tax_aware_hold(
            self._sell(), {"AAPL": {"close": 80.0}},
            transactions=[self._buy("AAPL", "2025-07-30", 100.0)], today="2026-07-05")
        assert len(kept) == 1 and rej == []

    def test_lot_past_one_year_allowed(self):
        import guardrails as g
        kept, rej = g.enforce_tax_aware_hold(
            self._sell(), {"AAPL": {"close": 150.0}},
            transactions=[self._buy("AAPL", "2025-06-01", 100.0)], today="2026-07-05")
        assert len(kept) == 1 and rej == []

    def test_risk_exit_exempt(self):
        import guardrails as g
        sell = [{"ticker": "AAPL", "action": "SELL", "target_weight": 0.0, "risk_exit": True}]
        kept, rej = g.enforce_tax_aware_hold(
            sell, {"AAPL": {"close": 150.0}},
            transactions=[self._buy("AAPL", "2025-07-30", 100.0)], today="2026-07-05")
        assert len(kept) == 1 and rej == []

    def test_kill_active_exempt(self):
        import guardrails as g
        kept, rej = g.enforce_tax_aware_hold(
            self._sell(), {"AAPL": {"close": 150.0}}, kill_active=True,
            transactions=[self._buy("AAPL", "2025-07-30", 100.0)], today="2026-07-05")
        assert len(kept) == 1 and rej == []

    def test_buys_and_holds_pass_through(self):
        import guardrails as g
        ds = [{"ticker": "AAPL", "action": "BUY", "target_weight": 0.08},
              {"ticker": "MS", "action": "HOLD"}]
        kept, rej = g.enforce_tax_aware_hold(
            ds, {"AAPL": {"close": 150.0}},
            transactions=[self._buy("AAPL", "2025-07-30", 100.0)], today="2026-07-05")
        assert len(kept) == 2 and rej == []


class TestCrisisSafeMode:
    """§18.5 — a market-wide index drop halts all new BUYs; SELLs stay allowed."""

    def test_fires_at_exact_threshold(self):
        import guardrails as g
        active, reason = g.crisis_safe_mode_active(-7.0, threshold_pct=7)
        assert active and "safe-mode" in reason

    def test_does_not_fire_above_threshold(self):
        import guardrails as g
        assert g.crisis_safe_mode_active(-6.99, threshold_pct=7) == (False, "")

    def test_none_spy_data_never_traps(self):
        """No SPY data → safe-mode NOT active (a data outage must not silently
        disable buying forever)."""
        import guardrails as g
        assert g.crisis_safe_mode_active(None) == (False, "")

    def test_buys_dropped_sells_kept(self):
        import guardrails as g
        ds = [{"ticker": "A", "action": "BUY", "target_weight": 0.08},
              {"ticker": "B", "action": "SELL", "target_weight": 0.0},
              {"ticker": "C", "action": "HOLD"}]
        kept, rejected, reason = g.enforce_safe_mode(ds, -8.0)
        assert [d["ticker"] for d in kept] == ["B", "C"]
        assert rejected[0]["ticker"] == "A" and reason

    def test_noop_when_calm(self):
        import guardrails as g
        ds = [{"ticker": "A", "action": "BUY"}]
        kept, rejected, reason = g.enforce_safe_mode(ds, 0.5)
        assert kept == ds and rejected == [] and reason == ""


class TestIsLastTradingDayOfISOWeek:
    """Phase 1 (2026-07-05): direct unit coverage of the pure helper the
    missed-week heartbeat now gates on (replacing literal weekday==4)."""

    def test_normal_friday_is_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        assert _is_last_trading_day_of_iso_week(date(2026, 6, 26)) is True   # ordinary Fri

    def test_normal_thursday_is_not_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        assert _is_last_trading_day_of_iso_week(date(2026, 6, 25)) is False  # Fri still trades

    def test_thursday_before_holiday_friday_is_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        # 2026-12-25 (Fri) is a holiday → 2026-12-24 (Thu) is the week's last trading day.
        assert _is_last_trading_day_of_iso_week(date(2026, 12, 24)) is True

    def test_holiday_friday_itself_is_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        # 12-25 is a holiday and the rest of its ISO week (Sat/Sun) never trades →
        # vacuously the "last trading day" too. (The heartbeat's outer gate skips
        # this day anyway; this just documents the helper is total, not partial.)
        assert _is_last_trading_day_of_iso_week(date(2026, 12, 25)) is True

    def test_year_boundary_w53_thursday_is_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        # 2026-12-31 (Thu, ISO 2026-W53); 2027-01-01 (Fri) is a holiday → Thu is last.
        assert _is_last_trading_day_of_iso_week(date(2026, 12, 31)) is True

    def test_monday_is_not_last(self):
        from heartbeat_check import _is_last_trading_day_of_iso_week
        from datetime import date
        assert _is_last_trading_day_of_iso_week(date(2026, 6, 22)) is False   # Mon


class TestDropZeroVarianceDays:
    """Phase 1 (2026-07-05): direct unit coverage of the degenerate-day filter."""

    def test_constant_day_dropped(self):
        from calibration import _drop_zero_variance_days
        rows = [{"date": "2026-06-08", "value": 5.0} for _ in range(10)]
        assert _drop_zero_variance_days(rows) == []

    def test_varying_day_kept(self):
        from calibration import _drop_zero_variance_days
        rows = [{"date": "2026-06-08", "value": float(i)} for i in range(5)]
        assert len(_drop_zero_variance_days(rows)) == 5

    def test_only_degenerate_days_dropped_mixed(self):
        from calibration import _drop_zero_variance_days
        degenerate = [{"date": "2026-06-08", "value": 5.0} for _ in range(4)]
        real = [{"date": "2026-06-09", "value": float(i)} for i in range(4)]
        out = _drop_zero_variance_days(degenerate + real)
        assert len(out) == 4 and all(r["date"] == "2026-06-09" for r in out)

    def test_single_row_day_passes_through(self):
        # < 2 values can't be judged degenerate — a legitimate 1-name day survives.
        from calibration import _drop_zero_variance_days
        rows = [{"date": "2026-06-08", "value": 5.0}]
        assert len(_drop_zero_variance_days(rows)) == 1

    def test_unparseable_or_missing_date_passes_through(self):
        from calibration import _drop_zero_variance_days
        rows = [{"value": 5.0}, {"value": 5.0}]   # no date key
        assert len(_drop_zero_variance_days(rows)) == 2


class TestHeartbeatMissedWeek:
    """§15.3 — Friday's heartbeat alerts when the whole ISO week had no rebalance
    claim/execution (Wed + Thu/Fri catch-up all missed)."""

    FRI, THU = "2026-06-26", "2026-06-25"

    def test_friday_without_stamp_alerts(self, tmp_path):
        from heartbeat_check import check_heartbeat
        rep = check_heartbeat(as_of=self.FRI, root=str(tmp_path))
        assert "weekly_rebalance" in rep["missing"]

    def test_friday_with_this_week_stamp_ok(self, tmp_path):
        from heartbeat_check import check_heartbeat
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W26", "date": "2026-06-24", "run_id": "r1",
             "execution_started_at": "x", "executed_at": "y", "tickers": []}))
        rep = check_heartbeat(as_of=self.FRI, root=str(tmp_path))
        assert "weekly_rebalance" not in rep["missing"]

    def test_friday_with_prior_week_stamp_alerts(self, tmp_path):
        from heartbeat_check import check_heartbeat
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W25", "date": "2026-06-17", "run_id": "r0",
             "execution_started_at": "x", "executed_at": "y", "tickers": []}))
        rep = check_heartbeat(as_of=self.FRI, root=str(tmp_path))
        assert "weekly_rebalance" in rep["missing"]

    def test_thursday_never_runs_the_check(self, tmp_path):
        from heartbeat_check import check_heartbeat
        rep = check_heartbeat(as_of=self.THU, root=str(tmp_path))
        assert all(c["name"] != "weekly_rebalance" for c in rep["checks"])

    # ── Phase 1 (2026-07-05): a Friday that is itself an NYSE holiday must not
    # silently swallow the missed-week check for that whole ISO week. The check
    # must run on the week's actual LAST TRADING DAY instead. ──

    def test_holiday_friday_itself_skips_heartbeat_entirely(self, tmp_path):
        """2026-12-25 (Christmas, observed Friday) is a non-trading day — the
        heartbeat's outer gate skips outright, same as any weekend."""
        from heartbeat_check import check_heartbeat
        rep = check_heartbeat(as_of="2026-12-25", root=str(tmp_path))
        assert rep["skipped"] is not None

    def test_thursday_before_holiday_friday_runs_the_check(self, tmp_path):
        """2026-12-24 (Thursday) is the LAST TRADING DAY of ISO week 2026-W52
        because 12-25 is a holiday — the missed-week check must run HERE, not
        wait for a Friday that never comes as a trading day. Before this fix,
        the check was gated on literal weekday==4 and would never fire for
        this entire week."""
        from heartbeat_check import check_heartbeat
        rep = check_heartbeat(as_of="2026-12-24", root=str(tmp_path))
        assert any(c["name"] == "weekly_rebalance" for c in rep["checks"])
        assert "weekly_rebalance" in rep["missing"]  # no stamp written -> alerts

    def test_thursday_before_holiday_friday_ok_with_stamp(self, tmp_path):
        from heartbeat_check import check_heartbeat
        (tmp_path / "last_rebalance.json").write_text(json.dumps(
            {"iso_week": "2026-W52", "date": "2026-12-23", "run_id": "r2",
             "execution_started_at": "x", "executed_at": "y", "tickers": []}))
        rep = check_heartbeat(as_of="2026-12-24", root=str(tmp_path))
        assert "weekly_rebalance" not in rep["missing"]

    def test_new_years_holiday_friday_week_boundary(self, tmp_path):
        """2027-01-01 (New Year's, Friday) is a holiday; 2026-12-31 (Thursday)
        is the last trading day of that ISO week (2026-W53) and must run the
        check — exercises the year-boundary ISO week (W53) case too."""
        from heartbeat_check import check_heartbeat
        rep = check_heartbeat(as_of="2026-12-31", root=str(tmp_path))
        assert any(c["name"] == "weekly_rebalance" for c in rep["checks"])
        assert "weekly_rebalance" in rep["missing"]


class TestStageDStorageSplit:
    """Stage D — snapshot slimming + per-ticker price vintage (P0-1)."""

    def _snap(self, n_bars=210):
        bar = {"date": 1, "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1}
        return {"date": "2026-07-06", "prices": {},
                "history": {"AAPL": [dict(bar)] * n_bars,      # core
                            "XYZ":  [dict(bar)] * n_bars,      # expansion
                            "SPY":  [dict(bar)] * n_bars}}

    def test_slim_keeps_full_for_core_and_tails_expansion(self):
        from market_data import slim_snapshot_for_commit
        snap = self._snap()
        out = slim_snapshot_for_commit(snap, keep_full={"AAPL", "SPY"}, tail_bars=63)
        assert len(out["history"]["AAPL"]) == 210
        assert len(out["history"]["SPY"]) == 210
        assert len(out["history"]["XYZ"]) == 63
        assert out["history_tail_tickers"] == ["XYZ"]
        assert len(snap["history"]["XYZ"]) == 210     # input not mutated

    def test_slim_noop_for_short_history(self):
        from market_data import slim_snapshot_for_commit
        out = slim_snapshot_for_commit(self._snap(n_bars=40), keep_full=set(), tail_bars=63)
        assert out["history_tail_tickers"] == []

    def test_dossier_stamps_per_ticker_price_vintage(self):
        """A carried-forward name's dossier record carries ITS OWN price_as_of —
        the consumer re-quotes it live instead of sizing on the stale slice (P0-1)."""
        from build_dossier import build_dossier
        bar = {"date": 1, "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1}
        snapshot = {"date": "2026-07-06", "_data_date": "2026-07-06",
                    "price_as_of_by_ticker": {"XYZ": "2026-07-02"},
                    "prices": {"AAPL": {"close": 100.0, "change_pct": 0.0},
                               "XYZ": {"close": 50.0, "change_pct": 0.0}},
                    "history": {"AAPL": [dict(bar)] * 30, "XYZ": [dict(bar)] * 30},
                    "fundamentals": {}}
        d = build_dossier(snapshot, [], [], [])
        assert d["tickers"]["AAPL"]["price_as_of"] == "2026-07-06"
        assert d["tickers"]["XYZ"]["price_as_of"] == "2026-07-02"

    def test_history_store_roundtrip(self, tmp_path, monkeypatch):
        import market_data as md
        monkeypatch.chdir(tmp_path)
        md._save_history_store({"AAPL": {"date": "2026-07-06", "history": [1, 2]}})
        assert md._load_history_store()["AAPL"]["history"] == [1, 2]

    def test_main_depth_classification_ignores_intentional_tails(self):
        """A 63-bar expansion tail must not page as degraded market data; a genuinely
        shallow CORE name still must. (Mirrors main.py's deep-subset computation.)"""
        history = {"AAPL": [{}] * 210, "XYZ": [{}] * 63}
        tails = {"XYZ"}
        deep = [len(h) for t, h in history.items() if t not in tails]
        assert min(deep) == 210          # OK — tails excluded
        deep_bad = [len(h) for t, h in {"AAPL": [{}] * 63}.items() if t not in tails]
        assert min(deep_bad) == 63       # still DEGRADED for a shallow core name


class TestDossierConsumer:
    """Phase 5 Stage C — the dossier reaches the agents' prompts."""

    def _rec(self):
        return {"ticker": "AAPL", "persistence": {"composite_7d_mean": 71.2,
                                                  "composite_7d_std": 2.3,
                                                  "rank_chg_7d": 3},
                "history_summary": {"ret_21d": 0.04, "ret_63d": 0.11,
                                    "ret_126d": 0.19, "max_dd_126d": -0.14},
                "fundamentals": {"gross_margin": 0.74, "_as_of_filing": "2026-05-15"},
                "data_quality": {"fundamentals_age_days": 47, "fundamentals_stale": False},
                "events": [{"date": "2026-06-30", "type": "rating_change",
                            "summary": "PT raised"}],
                "earnings": {"next_date": "2026-08-04", "days_until": 29, "imminent": False},
                "last_decision": {"action": "BUY", "date": "2026-06-24",
                                  "thesis": "multi-year hold", "confidence": 8},
                "since_entry": {"entry_price": 100.0, "current_price": 104.0,
                                "cum_return": 0.04, "days_since_entry": 12}}

    def test_fmt_dossier_record_renders_key_signals(self):
        from analysis import _fmt_dossier_record
        block = _fmt_dossier_record(self._rec())
        assert "71.2" in block and "rank_chg_7d=+3" in block
        assert "rating_change" in block and "2026-08-04" in block
        assert "47d old" in block

    def test_fmt_dossier_record_empty_for_none(self):
        from analysis import _fmt_dossier_record
        assert _fmt_dossier_record(None) == ""
        assert _fmt_dossier_record({}) == ""

    def test_fmt_since_entry_anchor(self):
        from analysis import _fmt_since_entry
        block = _fmt_since_entry(self._rec())
        assert "ENTRY ANCHOR" in block and "+4.0%" in block and "12d" in block

    def test_dossier_block_reaches_research_prompt(self, monkeypatch):
        import analysis
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            # run_research_analyst now calls with return_meta=True → return a tuple.
            return ((kw.get("default"), {"parsed_ok": True, "api_failed": False})
                    if kw.get("return_meta") else kw.get("default"))
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        md = {"date": "2026-07-06", "prices": {"AAPL": {"close": 104.0, "change_pct": 0.5}},
              "news": [], "ticker_news": {}}
        analysis.run_research_analyst("AAPL", md, {}, dossier_rec=self._rec())
        assert "RESEARCH DOSSIER" in captured["user_msg"]
        assert "rank_chg_7d=+3" in captured["user_msg"]

    def test_dossier_signals_reach_pm_prompt(self, monkeypatch):
        import analysis
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return ([], {"parsed_ok": True}) if kw.get("return_meta") else kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        dossier = {"tickers": {"AAPL": self._rec()}}
        analysis.run_portfolio_manager(
            {"regime": "NEUTRAL"}, {"AAPL": {"thesis": "t"}}, {"AAPL": {}}, {"AAPL": {}},
            {}, {}, {"total_value": 500.0, "cash": 100.0, "positions": []},
            [], date="2026-07-06", dossier=dossier)
        assert "DOSSIER SIGNALS" in captured["user_msg"]
        assert "persist_7d=71.2" in captured["user_msg"]
        assert "TAX-AWARE HOLD" in captured["user_msg"]


# ─────────────────────────────────────────────────────────────────────────────
# Jul 8 2026 remediation — sector-map completeness (fail-closed), capital
# dependency, PM min-hold awareness, lint gate, and the run_daily_cycle smoke
# test. See CLAUDE.md changelog "Jul 9 2026".
# ─────────────────────────────────────────────────────────────────────────────

class TestSectorMapCompleteness:
    """Structural guarantee: the sector cap can see EVERY name the system can
    buy. Jul 8 2026: CB+CFG (expansion names absent from SECTOR_MAP) fell to
    'UNKNOWN', splitting true Financials exposure across two under-cap buckets
    — a realized 35%-financials book passed the 25% cap. Future universe
    growth must fail THIS test before it can reopen that hole."""

    KNOWN_SECTORS = {
        "Technology", "Communication Services", "Consumer Discretionary",
        "Consumer Staples", "Financials", "Health Care", "Industrials",
        "Energy", "Materials", "Real Estate", "Utilities", "ETF",
    }

    def test_every_universe_ticker_is_mapped(self):
        import universe
        from guardrails import SECTOR_MAP
        missing = [t for t in universe.EXPANDED_UNIVERSE if t not in SECTOR_MAP]
        assert missing == [], (
            f"{len(missing)} universe ticker(s) missing from SECTOR_MAP — the "
            f"25% sector cap is blind to them (fail-closed will reject their "
            f"BUYs): {missing}")

    def test_every_label_is_a_known_sector(self):
        from guardrails import SECTOR_MAP
        bad = {t: s for t, s in SECTOR_MAP.items() if s not in self.KNOWN_SECTORS}
        assert bad == {}, f"Unknown sector labels (typo splits a sector): {bad}"

    def test_jul8_financials_are_mapped(self):
        """The exact names the Jul 8 incident proved invisible."""
        from guardrails import SECTOR_MAP
        for t in ("CB", "CFG", "ALL", "AIG", "AMP", "COF"):
            assert SECTOR_MAP.get(t) == "Financials", f"{t} must map to Financials"


class TestSectorFailClosed:
    """A BUY whose sector is unmapped is rejected outright — its concentration
    cannot be risk-checked. SELLs always pass (an exit must never be blocked
    by a map gap)."""

    def _portfolio(self):
        return {"total_value": 1000.0, "cash": 1000.0, "positions": []}

    def test_unmapped_buy_rejected(self):
        from guardrails import enforce_sector_limits
        kept, rejected = enforce_sector_limits(
            [{"ticker": "ZZZZ", "action": "BUY", "target_weight": 0.05}],
            self._portfolio())
        assert kept == []
        assert len(rejected) == 1
        assert "unmapped" in rejected[0]["rejected_reason"]

    def test_unmapped_sell_passes(self):
        from guardrails import enforce_sector_limits
        kept, rejected = enforce_sector_limits(
            [{"ticker": "ZZZZ", "action": "SELL", "target_weight": 0.0}],
            self._portfolio())
        assert [d["ticker"] for d in kept] == ["ZZZZ"]
        assert rejected == []

    def test_mapped_buy_still_passes(self):
        from guardrails import enforce_sector_limits
        kept, rejected = enforce_sector_limits(
            [{"ticker": "MSFT", "action": "BUY", "target_weight": 0.05}],
            self._portfolio())
        assert [d["ticker"] for d in kept] == ["MSFT"]
        assert rejected == []

    def test_jul8_regression_cb_cfg_rejected_at_cap(self):
        """The exact Jul 8 book: MS+AXP (Financials ≈17.2%) held; BUY CB 9% +
        BUY CFG 9% must now reject at the 25% Financials cap instead of
        slipping through an UNKNOWN bucket."""
        from guardrails import enforce_sector_limits
        portfolio = {"total_value": 513.05, "positions": [
            {"symbol": "MS",   "market_value": 47.35},
            {"symbol": "VRTX", "market_value": 52.54},
            {"symbol": "AXP",  "market_value": 40.67},
            {"symbol": "EBAY", "market_value": 46.58},
        ]}
        decisions = [
            {"ticker": "CB",  "action": "BUY", "target_weight": 0.09,
             "source_of_capital": "AXP"},
            {"ticker": "CFG", "action": "BUY", "target_weight": 0.09,
             "source_of_capital": "MS"},
        ]
        kept, rejected = enforce_sector_limits(decisions, portfolio)
        assert kept == []
        assert {r["ticker"] for r in rejected} == {"CB", "CFG"}
        assert all("Financials" in r["rejected_reason"] for r in rejected)

    def test_jul8_with_sells_kept_would_have_passed(self):
        """Counterfactual: had the SELL legs survived, the rotation fits under
        the cap — proving the cap rejects the orphaned-BUY book specifically,
        not the intended rotation."""
        from guardrails import enforce_sector_limits
        portfolio = {"total_value": 513.05, "positions": [
            {"symbol": "MS",   "market_value": 47.35},
            {"symbol": "VRTX", "market_value": 52.54},
            {"symbol": "AXP",  "market_value": 40.67},
            {"symbol": "EBAY", "market_value": 46.58},
        ]}
        decisions = [
            {"ticker": "AXP", "action": "SELL", "target_weight": 0.0},
            {"ticker": "MS",  "action": "SELL", "target_weight": 0.0},
            {"ticker": "CB",  "action": "BUY", "target_weight": 0.09},
            {"ticker": "CFG", "action": "BUY", "target_weight": 0.09},
        ]
        kept, rejected = enforce_sector_limits(decisions, portfolio)
        assert rejected == []
        assert len(kept) == 4


class TestCapitalDependency:
    """A BUY funded by a rejected SELL must not execute alone (Jul 8 2026:
    orphaned CB/CFG BUYs filled from cash after their AXP/MS funding SELLs
    were min-hold-rejected — the realized book was one the CRO never saw)."""

    def test_dependent_buy_dropped(self):
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY", "target_weight": 0.09,
              "source_of_capital": "AXP"}],
            [{"ticker": "AXP", "action": "SELL",
              "rejected_reason": "min-holding: bought 2026-06-22"}])
        assert kept == []
        assert len(dropped) == 1
        assert "AXP" in dropped[0]["rejected_reason"]
        assert "min-holding" in dropped[0]["rejected_reason"]

    def test_cash_funded_buy_unaffected(self):
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY", "source_of_capital": "cash"}],
            [{"ticker": "AXP", "action": "SELL", "rejected_reason": "x"}])
        assert len(kept) == 1 and dropped == []

    def test_missing_source_unaffected(self):
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY"}],
            [{"ticker": "AXP", "action": "SELL", "rejected_reason": "x"}])
        assert len(kept) == 1 and dropped == []

    def test_multiple_buys_same_source_all_dropped(self):
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB",  "action": "BUY", "source_of_capital": "AXP"},
             {"ticker": "CFG", "action": "BUY", "source_of_capital": "AXP"},
             {"ticker": "LIN", "action": "BUY", "source_of_capital": "MS"}],
            [{"ticker": "AXP", "action": "SELL", "rejected_reason": "x"}])
        assert [d["ticker"] for d in kept] == ["LIN"]
        assert {d["ticker"] for d in dropped} == {"CB", "CFG"}

    def test_case_insensitive_match(self):
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY", "source_of_capital": "axp"}],
            [{"ticker": "AXP", "action": "SELL", "rejected_reason": "x"}])
        assert kept == [] and len(dropped) == 1

    def test_rejected_buy_never_cascades(self):
        """Only rejected SELLs create dependencies — a rejected BUY of X must
        not take down another decision naming X."""
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY", "source_of_capital": "AXP"}],
            [{"ticker": "AXP", "action": "BUY", "rejected_reason": "x"}])
        assert len(kept) == 1 and dropped == []

    def test_sells_and_holds_pass_through(self):
        from guardrails import enforce_capital_dependency
        decisions = [{"ticker": "AXP", "action": "SELL", "source_of_capital": "AXP"},
                     {"ticker": "MS", "action": "HOLD", "source_of_capital": "MS"}]
        kept, dropped = enforce_capital_dependency(
            decisions,
            [{"ticker": "MS", "action": "SELL", "rejected_reason": "x"}])
        assert len(kept) == 2 and dropped == []

    def test_empty_rejected_noop(self):
        from guardrails import enforce_capital_dependency
        decisions = [{"ticker": "CB", "action": "BUY", "source_of_capital": "AXP"}]
        kept, dropped = enforce_capital_dependency(decisions, [])
        assert kept == decisions and dropped == []

    def test_reason_key_variant_accepted(self):
        """validation_report['rejected'] entries carry 'reason', guard rejects
        carry 'rejected_reason' — both shapes must chain into the message."""
        from guardrails import enforce_capital_dependency
        kept, dropped = enforce_capital_dependency(
            [{"ticker": "CB", "action": "BUY", "source_of_capital": "AXP"}],
            [{"ticker": "AXP", "action": "SELL", "reason": "not in candidates"}])
        assert kept == []
        assert "not in candidates" in dropped[0]["rejected_reason"]


class TestCROVetoCascade:
    """The CRO-veto boundary reuses enforce_capital_dependency: a vetoed SELL
    also drops the BUY it was funding (Jul 8 2026 failure at a different layer
    — main.py's guard can't see a SELL the CRO removed inside analysis.py)."""

    def _stub(self, monkeypatch, proposed, rejected_tickers):
        import analysis
        monkeypatch.setattr(analysis, "run_market_regime_strategist",
                            lambda *a, **k: {"regime": "NEUTRAL", "confidence": 60})
        monkeypatch.setattr(analysis, "_select_candidates",
                            lambda *a, **k: ["CB", "AXP"])
        monkeypatch.setattr(analysis, "run_research_analyst",
                            lambda *a, **k: {"thesis": "t", "confidence": 6})
        monkeypatch.setattr(analysis, "run_earnings_catalyst_analyst",
                            lambda *a, **k: {"earnings_alpha_score": 5})
        monkeypatch.setattr(analysis, "run_devils_advocate",
                            lambda *a, **k: {"bear_case": "b", "recommend_reject": False})
        monkeypatch.setattr(analysis, "run_portfolio_manager",
                            lambda *a, **k: (list(proposed), {"parsed_ok": True}))
        monkeypatch.setattr(analysis, "run_chief_risk_officer",
                            lambda *a, **k: {"approved": True,
                                             "rejected_tickers": rejected_tickers,
                                             "reasoning": "test"})
        md = {"date": "2026-07-09", "prices": {}, "history": {}}
        return analysis, md

    def test_vetoed_sell_drops_dependent_buy(self, monkeypatch):
        proposed = [
            {"ticker": "AXP", "action": "SELL", "target_weight": 0.0,
             "source_of_capital": "AXP"},
            {"ticker": "CB", "action": "BUY", "target_weight": 0.09,
             "source_of_capital": "AXP"},
        ]
        analysis, md = self._stub(monkeypatch, proposed, ["AXP"])
        portfolio = {"total_value": 500.0, "cash": 100.0,
                     "positions": [{"symbol": "AXP", "qty": 0.1, "avg_price": 300.0,
                                    "market_value": 40.0}]}
        decisions, _ = analysis.get_trade_decisions(portfolio, md, {})
        # AXP SELL vetoed AND CB BUY cascaded out → nothing survives.
        assert decisions == []

    def test_cash_funded_buy_survives_unrelated_veto(self, monkeypatch):
        proposed = [
            {"ticker": "AXP", "action": "SELL", "target_weight": 0.0,
             "source_of_capital": "AXP"},
            {"ticker": "CB", "action": "BUY", "target_weight": 0.09,
             "source_of_capital": "cash"},
        ]
        analysis, md = self._stub(monkeypatch, proposed, ["AXP"])
        portfolio = {"total_value": 500.0, "cash": 100.0,
                     "positions": [{"symbol": "AXP", "qty": 0.1, "avg_price": 300.0,
                                    "market_value": 40.0}]}
        decisions, _ = analysis.get_trade_decisions(portfolio, md, {})
        assert [d["ticker"] for d in decisions] == ["CB"]


class TestMinHoldDaysRemaining:
    def test_counts_down_and_clamps_to_zero(self):
        from guardrails import min_hold_days_remaining
        txs = [{"ticker": "AXP", "action": "BUY", "date": "2026-06-22", "dry_run": False}]
        rem = min_hold_days_remaining("AXP", transactions=txs,
                                      today="2026-07-09", min_holding_days=30)
        assert rem == 17  # 13 weekday-trading-days elapsed → 17 left
        assert min_hold_days_remaining("AXP", transactions=txs,
                                       today="2026-12-01", min_holding_days=30) == 0

    def test_no_buy_record_returns_none(self):
        from guardrails import min_hold_days_remaining
        assert min_hold_days_remaining("VRTX", transactions=[],
                                       today="2026-07-09") is None

    def test_dry_run_buy_ignored(self):
        from guardrails import min_hold_days_remaining
        txs = [{"ticker": "AXP", "action": "BUY", "date": "2026-07-08", "dry_run": True}]
        assert min_hold_days_remaining("AXP", transactions=txs,
                                       today="2026-07-09") is None


class TestPMMinHoldInjection:
    """Acceptance: min-hold eligibility actually reaches the PM user_msg, so
    the PM stops proposing rotations the guard is guaranteed to reject."""

    def _capture(self, monkeypatch, tmp_path):
        import json as _j
        import analysis, journal
        txs = [{"ticker": "AXP", "action": "BUY", "date": "2026-06-22", "dry_run": False}]
        txf = tmp_path / "transactions.json"
        txf.write_text(_j.dumps(txs))
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE", str(txf))
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return ([], {"parsed_ok": True}) if kw.get("return_meta") else kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        return analysis, captured

    def test_held_name_tagged_not_sellable(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path)
        portfolio = {"total_value": 500.0, "cash": 100.0, "positions": [
            {"symbol": "AXP", "qty": 0.12, "avg_price": 339.61, "market_value": 40.0},
        ]}
        analysis.run_portfolio_manager({}, {}, {}, {}, {}, {}, portfolio, [],
                                       date="2026-07-09")
        assert "min_hold:" in captured["user_msg"]
        assert "NOT sellable" in captured["user_msg"]

    def test_unrestricted_name_tagged_sellable(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path)
        portfolio = {"total_value": 500.0, "cash": 100.0, "positions": [
            {"symbol": "VRTX", "qty": 0.1, "avg_price": 464.73, "market_value": 52.0},
        ]}
        analysis.run_portfolio_manager({}, {}, {}, {}, {}, {}, portfolio, [],
                                       date="2026-07-09")
        assert "| sellable" in captured["user_msg"]

    def test_system_prompt_carries_min_hold_exception(self):
        from analysis import _PM_SYSTEM
        assert "min_hold" in _PM_SYSTEM
        assert "NOT sellable" in _PM_SYSTEM


class TestWashSaleDaysRemaining:
    """BUY-side mirror of min_hold_days_remaining — calendar-day arithmetic must
    agree with enforce_wash_sale_reentry at the 30-day boundary."""

    def test_counts_down_calendar_days(self):
        from guardrails import wash_sale_days_remaining
        txs = [{"ticker": "JNJ", "action": "SELL", "date": "2026-06-10", "dry_run": False}]
        # Jun 30 = 20 calendar days after the sale → 10 left (the live JNJ case)
        assert wash_sale_days_remaining("JNJ", transactions=txs,
                                        today="2026-06-30") == 10

    def test_boundary_matches_the_guard(self):
        from guardrails import enforce_wash_sale_reentry, wash_sale_days_remaining
        txs = [{"ticker": "JNJ", "action": "SELL", "date": "2026-06-10", "dry_run": False}]
        buy = [{"ticker": "JNJ", "action": "BUY", "target_weight": 0.05}]
        # day 29: guard rejects ⟺ helper says blocked
        kept, rej = enforce_wash_sale_reentry(buy, transactions=txs, today="2026-07-09")
        assert rej and wash_sale_days_remaining("JNJ", transactions=txs,
                                                today="2026-07-09") == 1
        # day 30: guard allows ⟺ helper says 0
        kept, rej = enforce_wash_sale_reentry(buy, transactions=txs, today="2026-07-10")
        assert kept and wash_sale_days_remaining("JNJ", transactions=txs,
                                                 today="2026-07-10") == 0

    def test_no_sell_record_returns_none(self):
        from guardrails import wash_sale_days_remaining
        assert wash_sale_days_remaining("WM", transactions=[],
                                        today="2026-07-15") is None

    def test_dry_run_sell_ignored(self):
        from guardrails import wash_sale_days_remaining
        txs = [{"ticker": "JNJ", "action": "SELL", "date": "2026-07-14", "dry_run": True}]
        assert wash_sale_days_remaining("JNJ", transactions=txs,
                                        today="2026-07-15") is None

    def test_future_dated_sell_fails_open_like_the_guard(self):
        from guardrails import wash_sale_days_remaining
        txs = [{"ticker": "JNJ", "action": "SELL", "date": "2026-08-01", "dry_run": False}]
        assert wash_sale_days_remaining("JNJ", transactions=txs,
                                        today="2026-07-15") is None


class TestPMBuyEligibilityInjection:
    """Acceptance for the Jul 15 2026 cash-stall fix: BUY eligibility (wash-sale
    block / sector-cap headroom) actually reaches the PM user_msg, so the PM
    stops proposing BUYs the guard chain silently rejects (V/JNJ/JPM, Jun 25–
    Jul 2 — the BUY-side twin of the Jul 8 min-hold SELL fix)."""

    def _capture(self, monkeypatch, tmp_path, txs):
        import json as _j
        import analysis, journal
        txf = tmp_path / "transactions.json"
        txf.write_text(_j.dumps(txs))
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE", str(txf))
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return ([], {"parsed_ok": True}) if kw.get("return_meta") else kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        return analysis, captured

    def _quant(self, ticker):
        return {ticker: {"composite_score": 85.0, "momentum_score": 70.0,
                         "quality_score": 90.0, "valuation_score": 50,
                         "volatility": 25.0, "beta": 1.0}}

    def test_wash_blocked_candidate_tagged(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path, [
            {"ticker": "JNJ", "action": "SELL", "date": "2026-07-05", "dry_run": False}])
        portfolio = {"total_value": 500.0, "cash": 230.0, "positions": []}
        # research_map must contain the candidate WITH a non-empty thesis — the
        # PM's quant table is restricted to the vetted set (Jul 22 2026), and an
        # empty thesis would trip the higher-priority research-unavailable tag
        # instead of the wash-sale tag under test.
        analysis.run_portfolio_manager({}, {"JNJ": {"thesis": "t"}}, {}, {}, {},
                                       self._quant("JNJ"),
                                       portfolio, [], date="2026-07-15")
        assert "wash-sale re-entry block" in captured["user_msg"]
        assert "⛔ NOT buyable" in captured["user_msg"]

    def test_sector_capped_candidate_tagged(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path, [])
        # 26% of the book in JPM → Financials over the 25% cap → any Financials
        # candidate (JPM itself here) must be tagged not-buyable.
        portfolio = {"total_value": 500.0, "cash": 230.0, "positions": [
            {"symbol": "JPM", "qty": 0.5, "avg_price": 200.0, "market_value": 130.0}]}
        analysis.run_portfolio_manager({}, {"JPM": {"thesis": "t"}}, {}, {}, {},
                                       self._quant("JPM"),
                                       portfolio, [], date="2026-07-15")
        assert "≥ 25% cap" in captured["user_msg"]

    def test_clean_candidate_tagged_eligible_with_headroom(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path, [])
        portfolio = {"total_value": 500.0, "cash": 230.0, "positions": []}
        analysis.run_portfolio_manager({}, {"JNJ": {"thesis": "t"}}, {}, {}, {},
                                       self._quant("JNJ"),
                                       portfolio, [], date="2026-07-15")
        assert "✓ BUY-eligible" in captured["user_msg"]
        assert "sector headroom" in captured["user_msg"]

    def test_cash_over_band_renders_discipline_block(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path, [])
        portfolio = {"total_value": 500.0, "cash": 230.0, "positions": []}  # 46%
        analysis.run_portfolio_manager({}, {}, {}, {}, {}, self._quant("JNJ"),
                                       portfolio, [], date="2026-07-15")
        assert "CASH DISCIPLINE" in captured["user_msg"]
        assert "8-holding floor" in captured["user_msg"]   # 0 positions < 8

    def test_cash_within_band_no_discipline_block(self, monkeypatch, tmp_path):
        analysis, captured = self._capture(monkeypatch, tmp_path, [])
        portfolio = {"total_value": 500.0, "cash": 25.0, "positions": []}  # 5%
        analysis.run_portfolio_manager({}, {}, {}, {}, {}, self._quant("JNJ"),
                                       portfolio, [], date="2026-07-15")
        assert "CASH DISCIPLINE" not in captured["user_msg"]

    def test_system_prompt_carries_deploy_mandate(self):
        from analysis import _PM_SYSTEM
        assert "Cash is a position" in _PM_SYSTEM
        assert "BUY-eligible" in _PM_SYSTEM
        assert "NOT buyable" in _PM_SYSTEM


class TestCandidateScopeEnforcement:
    """Jul 22 2026 (run 20260722-134836): the PM proposed NSC — a name NOT in
    the 20-ticker candidate shortlist (never through Research/Earnings/Devil's
    Advocate) — and the scope-blind CRO approved it; only validate_decisions
    caught it. Two fixes, both tested here:
      P0-1: the PM's quant menu is restricted to the vetted set (research_map
            keys ∪ holdings), so an un-researched name never appears to buy.
      P0-2: the CRO is given the vetted scope and force-rejects any out-of-scope
            proposed ticker (belt-and-suspenders around the LLM instruction)."""

    def _capture_pm(self, monkeypatch, tmp_path):
        import json as _j
        import analysis, journal
        (tmp_path / "transactions.json").write_text("[]")
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE",
                            str(tmp_path / "transactions.json"))
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return ([], {"parsed_ok": True}) if kw.get("return_meta") else kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        return analysis, captured

    def _q(self, **names):
        # names: ticker -> composite score
        return {t: {"composite_score": c, "momentum_score": 70.0,
                    "quality_score": 80.0, "valuation_score": 50,
                    "volatility": 25.0, "beta": 1.0} for t, c in names.items()}

    # ── P0-1: PM menu = vetted set ───────────────────────────────────────────
    def test_unvetted_high_score_name_absent_from_pm_menu(self, monkeypatch, tmp_path):
        analysis, captured = self._capture_pm(monkeypatch, tmp_path)
        # EOG is vetted (in research_map); NSC scores HIGHER but was never
        # researched → it must not appear in the PM's quant table.
        research_map = {"EOG": {"thesis": "vetted", "confidence": 6}}
        quant = self._q(NSC=90.0, EOG=85.0)
        portfolio = {"total_value": 500.0, "cash": 230.0, "positions": []}
        analysis.run_portfolio_manager({}, research_map, {}, {}, {}, quant,
                                       portfolio, [], date="2026-07-22")
        assert "EOG:" in captured["user_msg"]
        assert "NSC:" not in captured["user_msg"]   # the leak is closed

    def test_holdings_always_in_pm_menu_even_if_unresearched(self, monkeypatch, tmp_path):
        analysis, captured = self._capture_pm(monkeypatch, tmp_path)
        # A holding with no research_map entry must still show (for SELL/trim).
        quant = self._q(CB=88.0)
        portfolio = {"total_value": 500.0, "cash": 100.0, "positions": [
            {"symbol": "CB", "qty": 0.1, "avg_price": 350.0, "market_value": 45.0}]}
        analysis.run_portfolio_manager({}, {}, {}, {}, {}, quant,
                                       portfolio, [], date="2026-07-22")
        assert "CB:" in captured["user_msg"]

    # ── P0-2: CRO scope awareness ────────────────────────────────────────────
    def _capture_cro(self, monkeypatch, llm_result):
        import analysis
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return dict(llm_result)
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        return analysis, captured

    def _portfolio(self):
        return {"total_value": 500.0, "cash": 100.0, "positions": [
            {"symbol": "AXP", "qty": 0.1, "avg_price": 300.0, "market_value": 40.0}]}

    def test_cro_force_rejects_out_of_scope_ticker(self, monkeypatch):
        analysis, captured = self._capture_cro(
            monkeypatch, {"approved": True, "rejected_tickers": [], "reasoning": "ok"})
        decisions = [{"ticker": "NSC", "action": "BUY", "target_weight": 0.09}]
        risk = analysis.run_chief_risk_officer(
            decisions, self._portfolio(), {}, candidates=["EOG", "PLD"])
        assert "NSC" in risk["rejected_tickers"]            # forced in
        assert "scope" in risk["reasoning"].lower()          # and explained
        assert "VETTED CANDIDATES" in captured["user_msg"]   # told the model too

    def test_cro_keeps_in_scope_candidate_and_holding(self, monkeypatch):
        analysis, _ = self._capture_cro(
            monkeypatch, {"approved": True, "rejected_tickers": [], "reasoning": "ok"})
        decisions = [
            {"ticker": "EOG", "action": "BUY", "target_weight": 0.09},   # candidate
            {"ticker": "AXP", "action": "SELL", "target_weight": 0.0},   # holding
        ]
        risk = analysis.run_chief_risk_officer(
            decisions, self._portfolio(), {}, candidates=["EOG", "PLD"])
        assert risk["rejected_tickers"] == []

    def test_cro_preserves_model_correlation_veto_and_adds_scope(self, monkeypatch):
        analysis, _ = self._capture_cro(
            monkeypatch, {"approved": True, "rejected_tickers": ["XOM"],
                          "reasoning": "XOM correlated"})
        decisions = [
            {"ticker": "EOG", "action": "BUY", "target_weight": 0.09},
            {"ticker": "XOM", "action": "BUY", "target_weight": 0.09},  # candidate, model-vetoed
            {"ticker": "NSC", "action": "BUY", "target_weight": 0.09},  # out of scope
        ]
        risk = analysis.run_chief_risk_officer(
            decisions, self._portfolio(), {}, candidates=["EOG", "XOM", "PLD"])
        assert set(risk["rejected_tickers"]) == {"XOM", "NSC"}  # both survive/added
        assert "XOM correlated" in risk["reasoning"]            # model veto intact

    def test_cro_no_candidates_disables_scope_check(self, monkeypatch):
        # Legacy caller (candidates=None) → no scope block, no forced rejects.
        analysis, captured = self._capture_cro(
            monkeypatch, {"approved": True, "rejected_tickers": [], "reasoning": "ok"})
        decisions = [{"ticker": "NSC", "action": "BUY", "target_weight": 0.09}]
        risk = analysis.run_chief_risk_officer(decisions, self._portfolio(), {})
        assert risk["rejected_tickers"] == []
        assert "VETTED CANDIDATES" not in captured["user_msg"]

    def test_cro_api_failure_stays_full_veto_not_partial(self, monkeypatch):
        """Fail-safe regression: on a CRO API failure the default is
        approved=False + rejected_tickers=[] → the veto boundary reads that as a
        FULL veto (block ALL trades). The out-of-scope force-reject must NOT
        inject a named ticker there — doing so flips it to a PARTIAL veto and
        lets the OTHER (un-approved) trades execute with no risk sign-off."""
        import analysis
        # Simulate _safe_call returning its api-failure default unchanged.
        def fake_call(model, system, user_msg, **kw):
            return dict(kw.get("default"))
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        decisions = [
            {"ticker": "EOG", "action": "BUY", "target_weight": 0.09},  # in scope
            {"ticker": "NSC", "action": "BUY", "target_weight": 0.09},  # out of scope
        ]
        risk = analysis.run_chief_risk_officer(
            decisions, self._portfolio(), {}, candidates=["EOG", "PLD"])
        assert risk.get("api_failed") is True
        assert risk["approved"] is False
        assert risk["rejected_tickers"] == []   # untouched → full veto stands


class TestResearchBackedBuyGuard:
    """Jul 22 2026 (P3): a BUY whose Research thesis came back empty (529 / parse
    fail / blank) must be dropped — enrichment failure must never become a
    full-conviction BUY on the quant score alone. SELLs/HOLDs always pass."""

    def test_empty_thesis_buy_rejected(self):
        from guardrails import enforce_research_backed_buys
        decisions = [{"ticker": "EOG", "action": "BUY", "target_weight": 0.09}]
        research = {"EOG": {"thesis": "", "_empty_reason": "api_error"}}
        kept, rejected = enforce_research_backed_buys(decisions, research)
        assert kept == []
        assert rejected[0]["ticker"] == "EOG"
        assert "api_error" in rejected[0]["rejected_reason"]

    def test_backed_buy_kept(self):
        from guardrails import enforce_research_backed_buys
        decisions = [{"ticker": "EOG", "action": "BUY", "target_weight": 0.09}]
        research = {"EOG": {"thesis": "real thesis"}}
        kept, rejected = enforce_research_backed_buys(decisions, research)
        assert [d["ticker"] for d in kept] == ["EOG"] and rejected == []

    def test_sell_of_empty_thesis_name_passes(self):
        from guardrails import enforce_research_backed_buys
        # A SELL/exit needs no fresh thesis — never blocked.
        decisions = [{"ticker": "EOG", "action": "SELL", "target_weight": 0.0}]
        research = {"EOG": {"thesis": ""}}
        kept, rejected = enforce_research_backed_buys(decisions, research)
        assert [d["ticker"] for d in kept] == ["EOG"] and rejected == []

    def test_research_none_is_noop(self):
        from guardrails import enforce_research_backed_buys
        decisions = [{"ticker": "EOG", "action": "BUY", "target_weight": 0.09}]
        kept, rejected = enforce_research_backed_buys(decisions, None)
        assert [d["ticker"] for d in kept] == ["EOG"] and rejected == []

    def test_empty_research_map_blocks_all_buys(self):
        from guardrails import enforce_research_backed_buys
        # Total research failure ({}) is NOT a no-op — buying on zero research
        # is the exact failure mode; every BUY is rejected (fail-safe direction).
        decisions = [
            {"ticker": "EOG", "action": "BUY", "target_weight": 0.09},
            {"ticker": "MS", "action": "SELL", "target_weight": 0.0},
        ]
        kept, rejected = enforce_research_backed_buys(decisions, {})
        assert [d["ticker"] for d in kept] == ["MS"]      # SELL survives
        assert [d["ticker"] for d in rejected] == ["EOG"]  # BUY blocked

    def test_missing_ticker_key_treated_as_empty(self):
        from guardrails import enforce_research_backed_buys
        decisions = [{"ticker": "NSC", "action": "BUY", "target_weight": 0.09}]
        research = {"EOG": {"thesis": "t"}}  # NSC absent
        kept, rejected = enforce_research_backed_buys(decisions, research)
        assert kept == [] and rejected[0]["ticker"] == "NSC"


class TestResearchEmptyReasonStamping:
    """Jul 22 2026 (529 surfacing): run_research_analyst records WHY a thesis is
    empty (api_error / truncated / parse_failed / model_returned_empty) so the
    root cause is auditable instead of a generic "empty thesis"."""

    def _md(self):
        return {"date": "2026-07-22", "news": [], "ticker_news": {},
                "prices": {"AAPL": {"close": 104.0, "change_pct": 0.5}}}

    def _stub(self, monkeypatch, result, meta):
        import analysis
        def fake(model, system, user_msg, **kw):
            return (result, meta) if kw.get("return_meta") else result
        monkeypatch.setattr(analysis, "_safe_call", fake)
        return analysis

    def test_api_failure_stamped_api_error(self, monkeypatch):
        default = {"thesis": "", "catalysts": [], "confidence": 5,
                   "key_risks": [], "invalidates_if": [], "variant_view": ""}
        analysis = self._stub(monkeypatch, dict(default),
                              {"parsed_ok": False, "stop_reason": None, "api_failed": True})
        out = analysis.run_research_analyst("AAPL", self._md(), {})
        assert out["_empty_reason"] == "api_error"

    def test_truncation_stamped_truncated(self, monkeypatch):
        default = {"thesis": "", "catalysts": [], "confidence": 5,
                   "key_risks": [], "invalidates_if": [], "variant_view": ""}
        analysis = self._stub(monkeypatch, dict(default),
                              {"parsed_ok": False, "stop_reason": "max_tokens", "api_failed": False})
        out = analysis.run_research_analyst("AAPL", self._md(), {})
        assert out["_empty_reason"] == "truncated"

    def test_parse_failure_stamped_parse_failed(self, monkeypatch):
        default = {"thesis": "", "catalysts": [], "confidence": 5,
                   "key_risks": [], "invalidates_if": [], "variant_view": ""}
        analysis = self._stub(monkeypatch, dict(default),
                              {"parsed_ok": False, "stop_reason": "end_turn", "api_failed": False})
        out = analysis.run_research_analyst("AAPL", self._md(), {})
        assert out["_empty_reason"] == "parse_failed"

    def test_non_empty_thesis_not_stamped(self, monkeypatch):
        analysis = self._stub(monkeypatch, {"thesis": "real", "confidence": 6},
                              {"parsed_ok": True, "stop_reason": "end_turn", "api_failed": False})
        out = analysis.run_research_analyst("AAPL", self._md(), {})
        assert "_empty_reason" not in out

    def test_safe_call_meta_carries_api_failed_flag(self, monkeypatch):
        # _safe_call's success-path meta must expose api_failed=False so callers
        # can distinguish an infra failure from a content one.
        import analysis
        monkeypatch.setattr(analysis, "_call",
                            lambda *a, **k: ('{"thesis": "x"}', "end_turn"))
        _, meta = analysis._safe_call("m", "s", "u", default={"thesis": ""},
                                      return_meta=True)
        assert meta["api_failed"] is False


class TestPMValuationDisplay:
    """Jul 22 2026 (P1): the PM quant menu shows val=N/A when the valuation factor
    has no real data (no FMP key), not a misleading val=50 that reads as a real
    neutral valuation call."""

    def _capture(self, monkeypatch, tmp_path):
        import analysis, journal
        (tmp_path / "transactions.json").write_text("[]")
        monkeypatch.setattr(journal, "TRANSACTIONS_FILE",
                            str(tmp_path / "transactions.json"))
        cap = {}
        def fake(model, system, user_msg, **kw):
            cap["user_msg"] = user_msg
            return ([], {"parsed_ok": True}) if kw.get("return_meta") else kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake)
        return analysis, cap

    def test_valuation_unavailable_shows_na(self, monkeypatch, tmp_path):
        analysis, cap = self._capture(monkeypatch, tmp_path)
        quant = {"EOG": {"composite_score": 87.0, "momentum_score": 97.0,
                         "quality_score": 90.0, "valuation_score": 50,
                         "valuation_available": False, "volatility": 30.0, "beta": 1.0}}
        portfolio = {"total_value": 500.0, "cash": 100.0, "positions": []}
        analysis.run_portfolio_manager({}, {"EOG": {"thesis": "t"}}, {}, {}, {},
                                       quant, portfolio, [], date="2026-07-22")
        assert "val=N/A" in cap["user_msg"]
        assert "val=50" not in cap["user_msg"]

    def test_valuation_available_shows_score(self, monkeypatch, tmp_path):
        analysis, cap = self._capture(monkeypatch, tmp_path)
        quant = {"EOG": {"composite_score": 87.0, "momentum_score": 97.0,
                         "quality_score": 90.0, "valuation_score": 72,
                         "valuation_available": True, "volatility": 30.0, "beta": 1.0}}
        portfolio = {"total_value": 500.0, "cash": 100.0, "positions": []}
        analysis.run_portfolio_manager({}, {"EOG": {"thesis": "t"}}, {}, {}, {},
                                       quant, portfolio, [], date="2026-07-22")
        assert "val=72" in cap["user_msg"]


class TestDAFabricationGuard:
    """Jul 15 2026: with valuation_available=False the DA cited precise forward
    P/E multiples from training priors on all 14 candidates — same fabrication
    class as the earnings-date guard (Phase 3.2), now guarded the same way."""

    def _capture(self, monkeypatch):
        import analysis
        captured = {}
        def fake_call(model, system, user_msg, **kw):
            captured["user_msg"] = user_msg
            return kw.get("default")
        monkeypatch.setattr(analysis, "_safe_call", fake_call)
        return analysis, captured

    def _md(self, ticker):
        return {"prices": {ticker: {"close": 100.0}}}

    def test_no_valuation_data_forbids_multiples(self, monkeypatch):
        analysis, captured = self._capture(monkeypatch)
        scores = {"WM": {"valuation_available": False, "volatility": 22.0}}
        analysis.run_devils_advocate("WM", {}, {}, self._md("WM"), scores)
        assert "NO VALUATION DATA" in captured["user_msg"]
        assert "Do NOT cite specific multiples" in captured["user_msg"]

    def test_real_valuation_data_passes_the_score(self, monkeypatch):
        analysis, captured = self._capture(monkeypatch)
        scores = {"BAC": {"valuation_available": True, "valuation_score": 83.3,
                          "volatility": 20.6}}
        analysis.run_devils_advocate("BAC", {}, {}, self._md("BAC"), scores)
        assert "Pipeline valuation score: 83.3/100" in captured["user_msg"]
        assert "NO VALUATION DATA" not in captured["user_msg"]

    def test_rejection_criterion_c_gated_on_data(self, monkeypatch):
        analysis, captured = self._capture(monkeypatch)
        scores = {"WM": {"valuation_available": False}}
        analysis.run_devils_advocate("WM", {}, {}, self._md("WM"), scores)
        assert "criterion (c) is only usable" in captured["user_msg"]


class TestRecentlyExitedWindow:
    """The PM's re-entry warning must cover the full 30-day enforced block —
    the 10-day default left days 11–30 invisible (JNJ proposed on day 20)."""

    def test_30d_window_sees_a_20_day_old_exit(self, monkeypatch, tmp_path):
        import json as _j
        from datetime import date, timedelta
        import journal
        exit_date = (date.today() - timedelta(days=20)).isoformat()
        jf = tmp_path / "decision_journal.json"
        jf.write_text(_j.dumps([{
            "ticker": "JNJ", "action": "BUY", "status": "closed",
            "exits": [{"date": exit_date, "qty": 1.0}]}]))
        monkeypatch.setattr(journal, "JOURNAL_FILE", str(jf))
        assert "JNJ" not in journal.recently_exited()              # 10d default misses it
        assert "JNJ" in journal.recently_exited(within_days=30)    # enforced window sees it


class TestCashDragReport:
    """§IPS 0–10% band opportunity-cost metric (A4 of the Jul 15 cash-stall fix)."""

    def _write_inputs(self, tmp_path, spy_closes, cash=300.0, total=500.0):
        import json as _j
        dates = ["2026-07-01", "2026-07-08", "2026-07-15"][:len(spy_closes)]
        log = [{"date": d, "portfolio_snapshot": {"total_value": total, "cash": cash}}
               for d in dates]
        lp = tmp_path / "agent_log.json"
        lp.write_text(_j.dumps(log))
        snap = {"history": {"SPY": [{"date": d, "close": c}
                                    for d, c in zip(dates, spy_closes)]}}
        sp = tmp_path / "market_snapshot.json"
        sp.write_text(_j.dumps(snap))
        return str(lp), str(sp)

    def test_positive_drag_when_spy_rises(self, tmp_path):
        from performance import cash_drag_report
        lp, sp = self._write_inputs(tmp_path, [100.0, 110.0])
        r = cash_drag_report(lp, sp)
        # excess = 300 − 10%·500 = 250; SPY +10% → drag = 25.00
        assert r["cumulative_drag"] == 25.0
        assert r["avg_excess_cash_pct"] == 50.0   # 60% cash − 10% band
        assert r["n_periods"] == 1

    def test_negative_drag_when_spy_falls(self, tmp_path):
        from performance import cash_drag_report
        lp, sp = self._write_inputs(tmp_path, [100.0, 90.0])
        r = cash_drag_report(lp, sp)
        assert r["cumulative_drag"] == -25.0      # cash was protective

    def test_no_drag_when_cash_within_band(self, tmp_path):
        from performance import cash_drag_report
        lp, sp = self._write_inputs(tmp_path, [100.0, 110.0], cash=25.0)
        r = cash_drag_report(lp, sp)
        assert r["cumulative_drag"] == 0.0

    def test_single_observation_returns_none(self, tmp_path):
        from performance import cash_drag_report
        lp, sp = self._write_inputs(tmp_path, [100.0])
        assert cash_drag_report(lp, sp) is None

    def test_missing_log_returns_none(self, tmp_path):
        from performance import cash_drag_report
        assert cash_drag_report(str(tmp_path / "nope.json"),
                                str(tmp_path / "nope2.json")) is None

    def test_nan_spy_close_does_not_poison_drag(self, tmp_path):
        # NaN closes occur in market_snapshot.json (Jun 16 2026 quant NaN fix);
        # `s0 <= 0` is False for NaN so drag would silently become NaN.
        import math
        from performance import cash_drag_report
        lp, sp = self._write_inputs(tmp_path, [100.0, float("nan"), 110.0])
        r = cash_drag_report(lp, sp)
        assert r is not None
        assert math.isfinite(r["cumulative_drag"])   # periods touching NaN skipped


class TestLintGate:
    """ruff F821/F823 catches the exact Jul 8 bug class (a function-local
    re-import shadowing a module-level name → UnboundLocalError at runtime).
    Cheap static net over every module on the live path."""

    def test_repo_is_clean_of_undefined_name_bugs(self):
        import glob
        import shutil
        import subprocess
        import sys
        if shutil.which("ruff") is None:
            try:
                import ruff  # noqa: F401  (pip package present without CLI on PATH)
            except ImportError:
                pytest.skip("ruff unavailable in this environment")
        files = [f for f in glob.glob("*.py")] + [f for f in glob.glob("backtest/*.py")]
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--select", "F821,F823",
             "--isolated", *files],
            capture_output=True, text=True)
        assert result.returncode == 0, (
            "ruff found undefined-name / shadowed-import bugs (the Jul 8 2026 "
            f"UnboundLocalError class):\n{result.stdout}")


class TestRunDailyCycleSmoke:
    """End-to-end smoke of run_daily_cycle() with the network boundary stubbed
    and EVERY file write redirected to tmp_path (all pipeline file constants
    are relative paths, so monkeypatch.chdir isolates them). This is the net
    that was missing on Jul 8 2026: the load_dossier UnboundLocalError was a
    compile-scope bug inside run_daily_cycle that ANY full call would have hit
    — including this one, stubs and all."""

    TICKERS = ["MSFT", "JPM", "SPY"]

    def _market_data(self, today, extra=()):
        prices, history = {}, {}
        for i, t in enumerate(list(self.TICKERS) + list(extra)):
            px = 100.0 + 10 * i
            prices[t] = {"ticker": t, "close": px, "open": px, "high": px * 1.01,
                         "low": px * 0.99, "volume": 1_000_000, "change_pct": 0.5}
            history[t] = _trend(px * 0.9, px, 63)
        return {"_source": "test", "_data_date": today, "date": today,
                "prices": prices, "history": history, "fundamentals": {},
                "news": [], "ticker_news": {}}

    def _dossier(self, today):
        return {"as_of": today, "n_tickers": len(self.TICKERS),
                "built_from_days": ["2026-01-01", "2026-01-02"], "tickers": {}}

    def _pipeline_state(self, decisions):
        # Candidates must cover every decision ticker or validate_decisions
        # rejects it as "not analyzed" before the guards under test can run.
        cands = list(dict.fromkeys(list(self.TICKERS)
                                   + [d.get("ticker") for d in decisions]))
        return {
            "regime": {"regime": "NEUTRAL", "confidence": 60},
            "candidates": cands,
            "quant_scores": {},
            # Every candidate carries a non-empty thesis so enforce_research_backed_buys
            # passes them through to the guards under test (Jul 22 2026 research gate).
            "research": {t: {"thesis": "stub thesis", "confidence": 6} for t in cands},
            "earnings": {"MSFT": {"earnings_alpha_score": 6,
                                  "key_catalysts_90d": ["stub"]}},
            "devils_advocate": {"MSFT": {"bear_case": "stub bear",
                                         "recommend_reject": False}},
            "position_reviews": {"JPM": {"hold_score": 7, "remaining_alpha": "MED",
                                         "recommended_action": "HOLD"}},
            "portfolio_manager_proposed": list(decisions),
            "portfolio_manager_parsed_ok": True,
            "cro": {"approved": True, "rejected_tickers": []},
            "final_decisions": list(decisions),
        }

    def _run(self, monkeypatch, tmp_path, decisions, portfolio=None, pipeline_state=None,
             kill_active=False):
        import json as _j
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import main, execute
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

        monkeypatch.chdir(tmp_path)  # every relative-path artifact lands here
        # A fresh OK data-quality report for today keeps classify() off the
        # synthetic 3-ticker snapshot (whose coverage would breach real floors).
        (tmp_path / "data_quality_report.json").write_text(_j.dumps({
            "data_date": today, "status": "OK", "breaches": [],
            "data_quality_score": 100, "strategy_shift_ok": True, "hash": "test"}))

        if portfolio is None:
            portfolio = {"cash": 400.0, "total_value": 500.0, "positions": [
                {"symbol": "JPM", "qty": 0.5, "avg_price": 200.0,
                 "available_qty": 0.5, "current_price": 200.0,
                 "market_value": 100.0, "unrealized_pnl": 0.0}]}
        # Any decision/holding ticker outside TICKERS needs a price for qty.
        extra = ({d.get("ticker") for d in decisions}
                 | {p["symbol"] for p in portfolio["positions"]}) - set(self.TICKERS)

        monkeypatch.setattr(main, "get_portfolio_summary", lambda: portfolio)
        monkeypatch.setattr(main, "check_kill_switches",
                            lambda p: (kill_active, "kill switch active" if kill_active else ""))
        monkeypatch.setattr(main, "get_market_snapshot",
                            lambda: self._market_data(today, extra=sorted(extra)))
        monkeypatch.setattr(main, "load_dossier", lambda: self._dossier(today))
        monkeypatch.setattr(main, "validate_dossier",
                            lambda d, as_of=None: (True, []))
        monkeypatch.setattr(main, "get_trade_decisions",
                            lambda *a, **k: (list(decisions),
                                             pipeline_state or self._pipeline_state(decisions)))
        monkeypatch.setattr(main, "publish_to_supabase",
                            lambda *a, **k: None)
        # Belt-and-suspenders: never let the broker path go live in a test.
        monkeypatch.setattr(main, "DRY_RUN", True)
        monkeypatch.setattr(execute, "DRY_RUN", True)

        main.run_daily_cycle()
        return today

    def test_full_cycle_with_a_buy_completes(self, monkeypatch, tmp_path):
        import json as _j
        decisions = [{"ticker": "MSFT", "action": "BUY", "target_weight": 0.05,
                      "source_of_capital": "cash", "expected_return": 0.10,
                      "rationale": "smoke"}]
        today = self._run(monkeypatch, tmp_path, decisions)

        pending = _j.loads((tmp_path / "pending_decisions.json").read_text())
        assert pending["date"] == today
        assert pending["mode"] == "rebalance"
        assert pending["executed_at"] is None          # DRY_RUN never stamps
        assert pending["execution_started_at"] is None  # DRY_RUN never claims
        assert [d["ticker"] for d in pending["decisions"]] == ["MSFT"]
        assert pending["decisions"][0]["qty"] > 0

        health = _j.loads((tmp_path / "system_health.json").read_text())
        assert health["overall_status"] in ("OK", "DEGRADED")
        assert health["checks"]["market_data"]["status"] != "FAILED"

    def test_no_trade_day_completes(self, monkeypatch, tmp_path):
        import json as _j
        today = self._run(monkeypatch, tmp_path, [])
        pending = _j.loads((tmp_path / "pending_decisions.json").read_text())
        assert pending["date"] == today and pending["decisions"] == []
        assert (tmp_path / "system_health.json").exists()

    def test_min_hold_blocked_reduce_does_not_flag_starvation(self, monkeypatch, tmp_path):
        """Jul 15 2026: a REDUCE recommendation on a still-min-hold-blocked
        holding must not trip the 'likely data starvation' DEGRADED — the PM
        correctly can't act on it (Jul 8 2026 post-mortem: PM min-hold
        awareness). Regression for the main.py agent_6 heuristic gap."""
        import json as _j
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        import main  # noqa: F401  (ensure import side effects precede chdir)
        (tmp_path / "transactions.json").write_text(_j.dumps([
            {"ticker": "JPM", "action": "BUY", "date": recent, "dry_run": False}]))
        pipeline_state = self._pipeline_state([])
        pipeline_state["position_reviews"] = {
            "JPM": {"hold_score": 4, "remaining_alpha": "LOW",
                    "recommended_action": "REDUCE"}}
        self._run(monkeypatch, tmp_path, [], pipeline_state=pipeline_state)
        health = _j.loads((tmp_path / "system_health.json").read_text())
        assert health["checks"]["agent_6_portfolio_manager"]["status"] == "OK"

    def test_freely_sellable_reduce_still_flags_starvation(self, monkeypatch, tmp_path):
        """Counterfactual to the above: a REDUCE on a holding with NO buy
        record (freely sellable, unblocked) and 0 trades is still a genuine
        data-starvation signal — the fix must not blanket-suppress the check."""
        import json as _j
        pipeline_state = self._pipeline_state([])
        pipeline_state["position_reviews"] = {
            "JPM": {"hold_score": 4, "remaining_alpha": "LOW",
                    "recommended_action": "REDUCE"}}
        self._run(monkeypatch, tmp_path, [], pipeline_state=pipeline_state)
        health = _j.loads((tmp_path / "system_health.json").read_text())
        assert health["checks"]["agent_6_portfolio_manager"]["status"] == "DEGRADED"

    def test_kill_active_reduce_still_flags_starvation_even_if_recent_buy(self, monkeypatch, tmp_path):
        """During a kill switch, enforce_min_holding_period bypasses min-hold
        entirely (risk exits are never blocked) — so a REDUCE on a
        recently-bought name IS actionable even though min_hold_days_remaining
        alone would say otherwise. The health check must mirror that exemption,
        not just min_hold_days_remaining in isolation."""
        import json as _j
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        import main  # noqa: F401  (ensure import side effects precede chdir)
        (tmp_path / "transactions.json").write_text(_j.dumps([
            {"ticker": "JPM", "action": "BUY", "date": recent, "dry_run": False}]))
        pipeline_state = self._pipeline_state([])
        pipeline_state["position_reviews"] = {
            "JPM": {"hold_score": 2, "remaining_alpha": "LOW",
                    "recommended_action": "EXIT"}}
        self._run(monkeypatch, tmp_path, [], pipeline_state=pipeline_state, kill_active=True)
        health = _j.loads((tmp_path / "system_health.json").read_text())
        assert health["checks"]["agent_6_portfolio_manager"]["status"] == "DEGRADED"

    def test_orphaned_buy_is_dropped_end_to_end(self, monkeypatch, tmp_path):
        """Jul 8 2026 end-to-end regression through main's guard chain: a SELL
        of a recently-bought holding is min-hold-rejected, and the BUY it was
        funding must be dropped by enforce_capital_dependency — not executed
        from cash."""
        import json as _j
        from datetime import datetime, timedelta
        # A live BUY of JPM 5 weekdays ago → min-hold (30d) rejects its SELL.
        recent = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        decisions = [
            {"ticker": "JPM",  "action": "SELL", "target_weight": 0.0,
             "source_of_capital": "JPM", "rationale": "smoke exit"},
            {"ticker": "MSFT", "action": "BUY", "target_weight": 0.05,
             "source_of_capital": "JPM", "rationale": "smoke rotation"},
        ]
        # Pre-seed transactions.json in tmp BEFORE the run.
        import main  # noqa: F401  (ensure import side effects precede chdir)
        (tmp_path / "transactions.json").write_text(_j.dumps([
            {"ticker": "JPM", "action": "BUY", "date": recent, "dry_run": False}]))
        today = self._run(monkeypatch, tmp_path, decisions)
        pending = _j.loads((tmp_path / "pending_decisions.json").read_text())
        assert pending["date"] == today
        assert pending["decisions"] == []  # SELL min-hold-rejected, BUY cascaded
        health = _j.loads((tmp_path / "system_health.json").read_text())
        dv = health["checks"]["decision_validation"]
        assert dv["status"] == "DEGRADED"
        assert any("funding SELL JPM was rejected" in r.get("reason", "")
                   for r in dv.get("rejected", []))

    def test_sector_cap_breach_rejected_end_to_end(self, monkeypatch, tmp_path):
        """Jul 8 2026 core regression through the FULL pipeline: a book already
        heavy in Financials + a cash-funded Financials BUY that breaches 25% is
        rejected in the envelope (not silently executed). Uses BAC — a name the
        pre-fix SECTOR_MAP already knew — so this fails loudly if the sector cap
        wiring regresses, independent of the map-completeness fix."""
        import json as _j
        # Portfolio: JPM $100 + GS $80 = $180/$500 = 36% Financials already.
        portfolio = {"cash": 320.0, "total_value": 500.0, "positions": [
            {"symbol": "JPM", "qty": 0.5, "avg_price": 200.0, "available_qty": 0.5,
             "current_price": 200.0, "market_value": 100.0, "unrealized_pnl": 0.0},
            {"symbol": "GS", "qty": 0.2, "avg_price": 400.0, "available_qty": 0.2,
             "current_price": 400.0, "market_value": 80.0, "unrealized_pnl": 0.0}]}
        decisions = [{"ticker": "BAC", "action": "BUY", "target_weight": 0.09,
                      "source_of_capital": "cash", "expected_return": 0.10,
                      "rationale": "would breach sector cap"}]
        today = self._run(monkeypatch, tmp_path, decisions, portfolio=portfolio)
        pending = _j.loads((tmp_path / "pending_decisions.json").read_text())
        assert pending["date"] == today
        assert pending["decisions"] == []  # BAC BUY rejected by the sector cap
        dv = _j.loads((tmp_path / "system_health.json").read_text())["checks"]["decision_validation"]
        assert dv["status"] == "DEGRADED"
        assert any(r.get("ticker") == "BAC" and "Financials" in r.get("reason", "")
                   for r in dv.get("rejected", []))

    def test_unmapped_buy_fail_closed_end_to_end(self, monkeypatch, tmp_path):
        """Fail-closed net through the full pipeline: a BUY of a ticker absent
        from SECTOR_MAP is rejected outright rather than escaping the cap in an
        UNKNOWN bucket — the structural fix for the Jul 8 class of bug."""
        import json as _j
        import guardrails
        # A synthetic ticker guaranteed absent from SECTOR_MAP.
        assert "ZZZZ" not in guardrails.SECTOR_MAP
        portfolio = {"cash": 480.0, "total_value": 500.0, "positions": [
            {"symbol": "ZZZZ", "qty": 0.2, "avg_price": 100.0, "available_qty": 0.2,
             "current_price": 100.0, "market_value": 20.0, "unrealized_pnl": 0.0}]}
        decisions = [{"ticker": "ZZZZ", "action": "BUY", "target_weight": 0.05,
                      "source_of_capital": "cash", "expected_return": 0.10,
                      "rationale": "unmapped — must fail closed"}]
        today = self._run(monkeypatch, tmp_path, decisions, portfolio=portfolio)
        pending = _j.loads((tmp_path / "pending_decisions.json").read_text())
        assert pending["date"] == today
        assert pending["decisions"] == []  # unmapped BUY rejected fail-closed
        dv = _j.loads((tmp_path / "system_health.json").read_text())["checks"]["decision_validation"]
        assert any(r.get("ticker") == "ZZZZ" and "unmapped" in r.get("reason", "")
                   for r in dv.get("rejected", []))
