"""
factor_attribution.py — C6 / T3.3

Fama-French 5-factor attribution (Mkt-RF, SMB, HML, Mom/UMD, BAB-proxy) for the
portfolio equity curve.  Fetches free daily factor data from Ken French's data
library (requests + stdlib zipfile/io); caches to factor_cache.json so the
network is only hit when the cache is stale (> 7 days old or missing).

The regression needs a return series.  Today's live book has ~30 trading-day
points.  The module runs but labels results PRELIMINARY and suppresses t-stat
claims when n_obs < MIN_OBS.  Build the harness now; results firm up with time.

Usage:
    python factor_attribution.py          # print report
    from factor_attribution import run    # call from analysis / paper scripts
"""

from __future__ import annotations

import io
import json
import math
import os
import zipfile
from datetime import date

# ── paths ─────────────────────────────────────────────────────────────────────
AGENT_LOG    = "agent_log.json"
CACHE_FILE   = "factor_cache.json"
CACHE_TTL    = 7   # days before re-fetching
MIN_OBS      = 30  # below this, report data but suppress alpha claim

# ── Ken French daily data URLs ────────────────────────────────────────────────
FF3_URL  = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Research_Data_Factors_daily_CSV.zip")
MOM_URL  = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Momentum_Factor_daily_CSV.zip")


# ── Ken French data loader ────────────────────────────────────────────────────

def _parse_ff_csv(text: str) -> dict[str, dict[str, float]]:
    """Parse a Ken French daily CSV into {YYYYMMDD: {col: pct_return}}.

    French CSVs have a text header of variable length before the data block,
    which starts with a line whose first token is an 8-digit date.  Percentages
    are already in pct form (divide by 100 to get decimal returns).
    """
    rows: dict[str, dict[str, float]] = {}
    header: list[str] = []
    in_data = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if not in_data:
            if len(parts[0]) == 8 and parts[0].isdigit():
                in_data = True
            else:
                if all(p and not p[0].isdigit() for p in parts if p):
                    # Skip the first field — it is always the date position label
                    # ("Date", empty, or similar); only factor names follow it.
                    header = [p for p in parts[1:] if p]
                continue
        if not (len(parts[0]) == 8 and parts[0].isdigit()):
            break  # end of daily block (annual summary follows)
        date_str = parts[0]
        try:
            vals = [float(p) / 100.0 for p in parts[1:len(header) + 1]]
        except ValueError:
            continue
        rows[date_str] = dict(zip(header, vals))
    return rows


def _fetch_zip_csv(url: str, timeout: int = 30) -> str:
    """Download a French zip and return the sole CSV text inside."""
    import requests
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        name = next(n for n in z.namelist() if n.endswith(".CSV") or n.endswith(".csv"))
        return z.read(name).decode("latin-1")


def _load_cache() -> dict:
    if not os.path.isfile(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)


def load_factors(force_refresh: bool = False) -> dict[str, dict[str, float]]:
    """Return {YYYY-MM-DD: {Mkt-RF, SMB, HML, Mom, RF}} (decimal returns).

    Merges the FF3 and Momentum daily files.  Caches to CACHE_FILE for
    CACHE_TTL days.  Returns {} and prints a warning on network failure if the
    cache is cold.
    """
    cache = _load_cache()
    fetched_at = cache.get("fetched_at", "")
    stale = True
    if fetched_at:
        try:
            age = (date.today() - date.fromisoformat(fetched_at)).days
            stale = age > CACHE_TTL
        except ValueError:
            pass

    if not force_refresh and not stale and cache.get("factors"):
        return cache["factors"]

    print("   📥 Fetching Ken French daily factor data ...")
    try:
        ff3_raw  = _fetch_zip_csv(FF3_URL)
        mom_raw  = _fetch_zip_csv(MOM_URL)
    except Exception as e:
        if cache.get("factors"):
            print(f"   ⚠ Network fetch failed ({e}); using cached factors.")
            return cache["factors"]
        print(f"   ⚠ Factor fetch failed and cache is cold: {e}")
        return {}

    ff3 = _parse_ff_csv(ff3_raw)
    mom = _parse_ff_csv(mom_raw)

    merged: dict[str, dict[str, float]] = {}
    for raw_date, vals in ff3.items():
        iso = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        row = {k: vals[k] for k in vals}
        if raw_date in mom:
            for k, v in mom[raw_date].items():
                row[k] = v
        merged[iso] = row

    cache["factors"] = merged
    cache["fetched_at"] = date.today().isoformat()
    _save_cache(cache)
    print(f"   ✅ Loaded {len(merged)} daily factor observations.")
    return merged


# ── portfolio curve ───────────────────────────────────────────────────────────

def _portfolio_returns(agent_log_path: str = AGENT_LOG) -> list[tuple[str, float]]:
    """(date, daily_return) pairs from agent_log.json total_value curve.

    Only calendar-adjacent pairs (gap ≤ 7 days) produce a return observation;
    larger gaps are skipped so a missed run doesn't create a spurious multi-day
    return that distorts the regression.
    """
    if not os.path.isfile(agent_log_path):
        return []
    with open(agent_log_path) as f:
        log = json.load(f)

    # Last point per date (multiple intra-day runs → take the last).
    by_date: dict[str, float] = {}
    for run in log:
        d = (run.get("date") or (run.get("timestamp") or "")[:10])
        val = (run.get("portfolio_snapshot") or {}).get("total_value")
        if d and val:
            by_date[d] = float(val)

    sorted_dates = sorted(by_date)
    returns = []
    for i in range(1, len(sorted_dates)):
        d_prev, d_curr = sorted_dates[i - 1], sorted_dates[i]
        gap = (date.fromisoformat(d_curr) - date.fromisoformat(d_prev)).days
        if gap > 7:
            continue  # skip; more than a week gap is likely a missed run
        v_prev, v_curr = by_date[d_prev], by_date[d_curr]
        if v_prev and v_curr:
            returns.append((d_curr, v_curr / v_prev - 1))
    return returns


# ── OLS regression ────────────────────────────────────────────────────────────

def _ols(y: list[float], X: list[list[float]]) -> dict:
    """OLS with t-stats via numpy.  X must include the intercept column."""
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy not installed — run `pip install numpy` in the project venv"}

    Y = np.array(y)
    A = np.array(X)
    try:
        coeffs, _, _, _ = np.linalg.lstsq(A, Y, rcond=None)
        e = Y - A @ coeffs
        n, k = A.shape
        sigma2 = (e @ e) / max(n - k, 1)
        ATA_inv = np.linalg.inv(A.T @ A)
        se = np.sqrt(np.diag(ATA_inv) * sigma2)
        t_stats = coeffs / np.where(se > 0, se, np.nan)
        return {
            "coeffs":  coeffs.tolist(),
            "t_stats": t_stats.tolist(),
            "se":      se.tolist(),
            "n":       int(n),
            "k":       int(k),
            "r2":      float(1 - (e @ e) / max((Y - Y.mean()) @ (Y - Y.mean()), 1e-15)),
        }
    except (np.linalg.LinAlgError, ValueError) as exc:
        return {"error": str(exc)}


# ── main attribution ──────────────────────────────────────────────────────────

def run(agent_log_path: str = AGENT_LOG,
        force_refresh: bool = False) -> dict:
    """Run factor attribution.  Returns a structured result dict.

    Factors used (all from Ken French daily):
        Mkt-RF  market excess return
        SMB     small-minus-big
        HML     high-minus-low (value)
        Mom     Carhart momentum (UMD)

    BAB (betting-against-beta) is from AQR and requires a separate fetch not
    yet implemented; it is noted as missing in the output.

    Regression:
        (Rp - Rf) = α + β_mkt·(Mkt-RF) + β_smb·SMB + β_hml·HML + β_mom·Mom + ε
    """
    factors = load_factors(force_refresh=force_refresh)
    port_rets = _portfolio_returns(agent_log_path)

    if not port_rets:
        return {"error": "No portfolio return observations in agent_log.json"}
    if not factors:
        return {"error": "Factor data unavailable (network failure, cold cache)"}

    # Align observations — inner join on date.
    aligned_dates, y_exc, X_rows, factor_names = [], [], [], ["Mkt-RF", "SMB", "HML", "Mom"]
    for d, rp in port_rets:
        row = factors.get(d)
        if not row:
            continue
        rf   = row.get("RF", 0.0)
        mkt  = row.get("Mkt-RF", row.get("Mkt.RF", 0.0))
        smb  = row.get("SMB", 0.0)
        hml  = row.get("HML", 0.0)
        mom  = row.get("Mom", 0.0)
        if any(v == 0.0 and k in ("Mkt-RF", "Mkt.RF") for k, v in row.items() if k in ("Mkt-RF", "Mkt.RF")):
            pass  # zero market return is valid; only skip if key is missing
        aligned_dates.append(d)
        y_exc.append(rp - rf)
        X_rows.append([1.0, mkt, smb, hml, mom])

    n_obs = len(y_exc)
    preliminary = n_obs < MIN_OBS

    result: dict = {
        "n_obs":        n_obs,
        "date_range":   {"first": aligned_dates[0] if aligned_dates else None,
                         "last":  aligned_dates[-1] if aligned_dates else None},
        "preliminary":  preliminary,
        "min_obs_threshold": MIN_OBS,
        "factors_used": factor_names,
        "bab_note":     ("BAB (betting-against-beta) not yet implemented — requires "
                         "AQR data fetch.  Add when the base regression is stable."),
    }

    if n_obs < 3:
        result["error"] = f"Insufficient observations for regression ({n_obs} < 3)"
        return result

    ols = _ols(y_exc, X_rows)
    if "error" in ols:
        result["regression_error"] = ols["error"]
        return result

    labels = ["alpha"] + factor_names
    coeffs  = ols["coeffs"]
    t_stats = ols["t_stats"]
    se_vals = ols["se"]

    result["regression"] = {
        lbl: {
            "coeff":  round(c, 6),
            "t_stat": round(t, 3) if not math.isnan(t) else None,
            "se":     round(s, 6),
        }
        for lbl, c, t, s in zip(labels, coeffs, t_stats, se_vals)
    }
    result["r_squared"] = round(ols["r2"], 4)
    result["annualized_alpha_bps"] = round(coeffs[0] * 252 * 10_000, 1)

    if preliminary:
        result["caution"] = (
            f"n={n_obs} < {MIN_OBS} — t-stats are not meaningful at this sample size.  "
            "Report the regression once the series is longer; do not cite alpha."
        )

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt(val, decimals=3) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{decimals}f}"


if __name__ == "__main__":
    r = run()
    print("\n" + "=" * 64)
    print("📐  FACTOR ATTRIBUTION (FF3 + Momentum)")
    print("=" * 64)

    if "error" in r:
        print(f"   ⚠ {r['error']}")
    else:
        print(f"   Observations : {r['n_obs']}  ({r['date_range']['first']} → {r['date_range']['last']})")
        print(f"   R²           : {r.get('r_squared', 'n/a')}")
        print(f"   Ann. alpha   : {r.get('annualized_alpha_bps', 'n/a')} bps/yr")
        if r.get("preliminary"):
            print(f"\n   ⚠ PRELIMINARY — {r.get('caution', '')}\n")
        reg = r.get("regression", {})
        for name, vals in reg.items():
            print(f"   {name:<10}  coeff={_fmt(vals['coeff'], 5)}  "
                  f"t={_fmt(vals['t_stat'], 2)}  se={_fmt(vals['se'], 5)}")
        print(f"\n   {r['bab_note']}")

    print("=" * 64 + "\n")
