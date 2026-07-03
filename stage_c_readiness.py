"""
stage_c_readiness.py — is the evidence clock ready to DECIDE Stage C?

Stage C (the dossier consumer that trades real capital) must not be built on faith. The
ml_ai review showed the signals are indistinguishable from noise at the current sample —
quant composite IC 0.046, p=0.30, n_eff≈5, CI ±0.31 — where an IC of 0.05 is statistically
the same as 0.35. Building the machine to exploit a signal before knowing the signal exists
is the trap this tool exists to prevent.

A signal is DECIDABLE when its forward-IC confidence interval is tight enough to tell a real
edge from zero: enough independent observations (`n_effective`) AND a small `ci_halfwidth`.
"Decidable" is deliberately symmetric — it does NOT mean "the signal works," it means "the
evidence can now answer yes or no." A tight CI around zero is just as decisive (→ the signal
has no edge, act on that) as a tight CI around a positive IC.

Read-only, zero order code. Reads agent_scorecards.json; prints a verdict + per-signal status.
Also surfaced weekly in pipeline_digest.py so you don't have to eyeball the scorecard.
"""

from __future__ import annotations

import json
from pathlib import Path

SCORECARD_FILE = "agent_scorecards.json"

# A signal's IC is decidable once we have this many effective (autocorrelation-adjusted)
# observations AND the ±CI is this tight. At CI ±0.31 (today) nothing is decidable; ±0.15
# means an IC estimate is distinguishable from a null by a meaningful margin.
MIN_N_EFFECTIVE  = 30
MAX_CI_HALFWIDTH = 0.15

# The signals whose forward IC gates the Stage C go/no-go: the primary quant composite +
# the Stage-A dossier signals (logged full-universe, maturing at 21d). Keyed as the
# scorecard names them: "{agent}.{field}@{horizon}d".
PRIMARY_QUANT   = "quant.composite_score@21d"
DOSSIER_SIGNALS = ["persist_mean.composite_7d_mean@21d", "event_present.flag@21d"]


def _assess_signal(entry: dict) -> dict:
    n_eff = entry.get("n_effective")
    ci    = entry.get("ci_halfwidth")
    decidable = (isinstance(n_eff, (int, float)) and n_eff >= MIN_N_EFFECTIVE
                 and isinstance(ci, (int, float)) and ci <= MAX_CI_HALFWIDTH)
    return {
        "present":        True,
        "n_effective":    n_eff,
        "ic":             entry.get("ic"),
        "ic_shrunk":      entry.get("ic_shrunk"),
        "ci_halfwidth":   ci,
        "significant_bh": entry.get("significant_bh"),
        "decidable":      bool(decidable),
    }


def assess_readiness(scorecard: dict) -> dict:
    """Evaluate whether the Stage C decision is now supportable by evidence.

    `ready` is True when the primary quant signal AND at least one dossier signal are each
    decidable — i.e. the evidence can answer the go/no-go, in either direction."""
    all_signals = [PRIMARY_QUANT] + DOSSIER_SIGNALS
    per: dict = {}
    for s in all_signals:
        e = scorecard.get(s)
        per[s] = _assess_signal(e) if isinstance(e, dict) else {"present": False, "decidable": False}

    quant_ok   = per[PRIMARY_QUANT].get("decidable", False)
    dossier_ok = any(per[s].get("decidable") for s in DOSSIER_SIGNALS)
    ready = bool(quant_ok and dossier_ok)

    # Human-readable "why not yet" — the nearest gap on each not-yet-decidable signal.
    blockers = []
    for s in all_signals:
        p = per[s]
        if p.get("decidable"):
            continue
        if not p.get("present"):
            blockers.append(f"{s}: not scored yet (needs ~21d of matured forecasts)")
        else:
            bits = []
            n_eff, ci = p.get("n_effective"), p.get("ci_halfwidth")
            if not (isinstance(n_eff, (int, float)) and n_eff >= MIN_N_EFFECTIVE):
                bits.append(f"n_eff {n_eff} < {MIN_N_EFFECTIVE}")
            if not (isinstance(ci, (int, float)) and ci <= MAX_CI_HALFWIDTH):
                bits.append(f"CI ±{ci} > ±{MAX_CI_HALFWIDTH}")
            blockers.append(f"{s}: " + ", ".join(bits))

    return {
        "ready":     ready,
        "verdict":   "DECIDABLE — the scorecard can now answer Stage C's go/no-go"
                     if ready else "ACCUMULATING — not enough evidence to decide yet",
        "quant_decidable":   quant_ok,
        "dossier_decidable": dossier_ok,
        "signals":   per,
        "blockers":  blockers,
        "thresholds": {"min_n_effective": MIN_N_EFFECTIVE, "max_ci_halfwidth": MAX_CI_HALFWIDTH},
    }


def load_scorecard(path: str = SCORECARD_FILE) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def summary_line(assessment: dict) -> str:
    """One-line summary for the weekly digest."""
    s = assessment
    return (f"Stage C: {'✅ DECIDABLE' if s['ready'] else '⏳ accumulating'} "
            f"(quant {'✓' if s['quant_decidable'] else '✗'} · "
            f"dossier {'✓' if s['dossier_decidable'] else '✗'})")


def main() -> int:
    a = assess_readiness(load_scorecard())
    print(f"=== Stage C readiness ===\n{a['verdict']}")
    print(f"thresholds: n_effective ≥ {MIN_N_EFFECTIVE}, CI halfwidth ≤ {MAX_CI_HALFWIDTH}\n")
    for name, p in a["signals"].items():
        if not p.get("present"):
            print(f"  [—] {name}: not scored yet")
        else:
            mark = "✅" if p["decidable"] else "⏳"
            print(f"  [{mark}] {name}: ic={p['ic']} ci=±{p['ci_halfwidth']} "
                  f"n_eff={p['n_effective']} sig_bh={p['significant_bh']}")
    if not a["ready"]:
        print("\nBlockers:")
        for b in a["blockers"]:
            print(f"  - {b}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
