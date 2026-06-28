"""
policy.py — single source of truth loader for the operative deterministic limits.

Reads policy.yaml (the machine mirror of IPS.md Appendix A) into a flat dict of
limits consumed by guardrails.py and execute.py. Before this, every limit was a
hard-coded constant scattered across modules and prompts — a drift-bug class the
system has already hit ("the sector limit lived only in the PM prompt"). Now there
is exactly one place a limit is defined: policy.yaml.

TOLERANT BY DESIGN — capital-integrity posture:
  _DEFAULTS below ARE the historical hard-coded constants. If policy.yaml or PyYAML
  is unavailable, or the file is malformed, the loader logs a warning and returns
  _DEFAULTS. Behavior is therefore byte-identical to the pre-Phase-0 system in the
  worst case — this module can never change behavior silently or break the live
  trade path. The parity test (TestPolicyParity) asserts policy.yaml == _DEFAULTS.

policy.yaml is the OPERATIVE (currently-deployed) policy; IPS.md Appendix A is the
TARGET mandate. Where they differ, the delta is a tracked migration applied in a
later rollout phase via §18.4 change-control. See policy.yaml's header.
"""

from __future__ import annotations

import os

# _DEFAULTS — the historical guardrails.py / execute.py constants, verbatim. These
# are the PARITY BASELINE: if policy.yaml cannot be loaded, the system behaves
# exactly as it did before Phase 0.
_DEFAULTS: dict = {
    "policy_version":           "0.0-builtin-defaults",
    "max_target_weight":        0.10,
    "max_buy_notional_pct":     0.12,
    "min_order_notional":       5.00,
    "gfv_window_trading_days":  2,
    "max_sector_weight":        0.25,
    "min_holding_trading_days": 5,
    "wash_sale_reentry_days":   30,
    "min_net_edge":             0.0,
    "blocked_tickers":          ["TSLA"],
}

# Per-key validators for the guardrails scalars. A value that fails validation is
# REJECTED (the default is kept) with a loud warning — this is the capital-safety
# net for the single source of truth: a plausible units typo (e.g. `max_target_weight:
# 10` instead of `0.10` — IPS Appendix A states limits in PERCENTS while policy.yaml
# uses FRACTIONS) must NEVER silently disable a cap. `bool` is excluded from the
# numeric checks because `isinstance(True, int)` is True in Python.
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)

_VALIDATORS = {
    # fractions of the portfolio — must be in (0, 1]; catches the percent/fraction typo
    "max_target_weight":        lambda v: _is_num(v) and 0 < v <= 1,
    "max_buy_notional_pct":     lambda v: _is_num(v) and 0 < v <= 1,
    "max_sector_weight":        lambda v: _is_num(v) and 0 < v <= 1,
    # USD floors — non-negative
    "min_order_notional":       lambda v: _is_num(v) and v >= 0,
    "min_net_edge":             lambda v: _is_num(v) and v >= 0,
    # trading/calendar day counts — non-negative ints
    "gfv_window_trading_days":  lambda v: _is_int(v) and v >= 0,
    "min_holding_trading_days": lambda v: _is_int(v) and v >= 0,
    "wash_sale_reentry_days":   lambda v: _is_int(v) and v >= 0,
}
_GUARDRAIL_KEYS = tuple(_VALIDATORS)

_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.yaml")


def _load(path: str | None = None) -> dict:
    """Merge a VALIDATED policy.yaml over _DEFAULTS. On ANY failure, return _DEFAULTS.

    Each overlaid guardrail value must pass its validator; an out-of-range or
    wrong-type value keeps the default and warns loudly (a units typo must never
    silently disable a capital control). `path` is injectable for tests.
    """
    merged = dict(_DEFAULTS)
    path = path or _POLICY_PATH
    try:
        import yaml  # local import so a missing PyYAML degrades to defaults, not ImportError
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        guardrails = raw.get("guardrails", {}) or {}
        universe = raw.get("universe", {}) or {}
        for k in _GUARDRAIL_KEYS:
            if k in guardrails and guardrails[k] is not None:
                v = guardrails[k]
                if _VALIDATORS[k](v):
                    merged[k] = v
                else:
                    print(f"⚠ policy.yaml {k}={v!r} failed validation; "
                          f"keeping default {_DEFAULTS[k]!r} (capital-safety guard)")
        if raw.get("policy_version"):
            merged["policy_version"] = raw["policy_version"]
        bt = universe.get("blocked_tickers")
        if bt is not None:
            if isinstance(bt, list) and all(isinstance(t, str) for t in bt):
                merged["blocked_tickers"] = list(bt)
            else:
                print(f"⚠ policy.yaml blocked_tickers={bt!r} not a list[str]; "
                      f"keeping default {_DEFAULTS['blocked_tickers']!r}")
    except Exception as e:  # missing file, missing yaml, parse error — never break the live path
        print(f"⚠ policy.yaml not loaded ({e!r}); using built-in defaults (no behavior change)")
    return merged


# Loaded once at import. Module-level so default-argument binding in guardrails.py
# (e.g. `min_holding_days: int = MIN_HOLDING_TRADING_DAYS`) captures the policy value.
VALUES: dict = _load()


def get(key: str, default=None):
    """Read a single operative limit."""
    return VALUES.get(key, default)


def policy_version() -> str:
    """The operative policy_version (stamped onto pending_decisions for change-control)."""
    return VALUES.get("policy_version", "unknown")
