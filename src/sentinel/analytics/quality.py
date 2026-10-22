"""Accounting-quality scorecards.

Every score here has an original sample, an era and a set of industries it was never
meant for. Those are printed next to the number rather than buried, because a Z-score
without its caveat is a rumour with a decimal point.

Implemented: Piotroski F, Altman Z and Z-double-prime, Sloan accruals, cash conversion.
Not implemented: Beneish M and Ohlson O, which need lagged SG&A and receivables
intensities that the current dictionary cannot supply reliably. They are listed as
missing in LIMITATIONS.md rather than approximated.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BANDS = {
    "piotroski_f": [(8, 9, "strong"), (5, 7, "neutral"), (0, 4, "weak")],
    "altman_z": [(3.0, 99.0, "safe"), (1.8, 3.0, "grey"), (-99.0, 1.8, "distress")],
}

CAVEATS = {
    "piotroski_f": "Piotroski (2000), 9 signals, calibrated on high book-to-market US firms. "
                   "A low score is not a prediction; it describes last year's fundamentals.",
    "altman_z": "Altman (1968), US manufacturers. Not meaningful for banks, insurers or REITs, "
                "where leverage is structural. Z-double-prime is the variant for other issuers.",
    "accruals": "Sloan (1996). Accruals above zero mean earnings ran ahead of cash.",
    "cash_conversion": "Descriptive ratio. Below 1 means profit is not turning into cash yet.",
}


def _val(row, name):
    v = row.get(name)
    return float(v) if v is not None and pd.notna(v) else np.nan


def piotroski(row) -> tuple[float, list[str]]:
    """Nine binary signals. Signals needing data we do not hold are skipped, not faked."""
    signals = []
    roa, cfo, assets = _val(row, "roa"), _val(row, "cfo"), _val(row, "assets")
    if not np.isnan(roa):
        signals.append(roa > 0)                      # 1 profitability
    if not np.isnan(cfo):
        signals.append(cfo > 0)                      # 2 operating cash
    prev_roa = _val(row, "roa_prev")
    if not np.isnan(roa) and not np.isnan(prev_roa):
        signals.append(roa > prev_roa)               # 3 improving ROA
    if not np.isnan(cfo) and not np.isnan(assets) and assets:
        signals.append((cfo / assets) > roa)         # 4 accrual quality
    lev, prev_lev = _val(row, "leverage"), _val(row, "leverage_prev")
    if not np.isnan(lev) and not np.isnan(prev_lev):
        signals.append(lev < prev_lev)               # 5 falling leverage
    gm, prev_gm = _val(row, "gross_margin"), _val(row, "gross_margin_prev")
    if not np.isnan(gm) and not np.isnan(prev_gm):
        signals.append(gm > prev_gm)                 # 6 improving margin
    at, prev_at = _val(row, "asset_turnover"), _val(row, "asset_turnover_prev")
    if not np.isnan(at) and not np.isnan(prev_at):
        signals.append(at > prev_at)                 # 7 improving turnover
    return (float(sum(signals)) if signals else np.nan), [str(s) for s in signals]


def altman(row) -> tuple[float, float]:
    """Z and Z-double-prime. NaN where the inputs do not exist."""
    assets = _val(row, "assets")
    liabilities = _val(row, "liabilities")
    if np.isnan(assets) or assets <= 0 or np.isnan(liabilities) or liabilities <= 0:
        return np.nan, np.nan
    ca, cl = _val(row, "assets_current"), _val(row, "liabilities_current")
    wc = (ca - cl) if not (np.isnan(ca) or np.isnan(cl)) else np.nan
    retained, ebit, equity = _val(row, "retained_earnings"), _val(row, "operating_income"), _val(row, "equity")
    z = np.nan
    if not np.isnan(wc) and not np.isnan(retained) and not np.isnan(ebit) and not np.isnan(equity):
        z = (1.2 * wc / assets + 1.4 * retained / assets + 3.3 * ebit / assets + 0.6 * equity / liabilities)
    # Z-double-prime also needs working capital. Substituting zero when it is missing
    # would produce a number that looks like a score and is not one, so the whole score
    # is withheld instead.
    z2 = np.nan
    if not (np.isnan(wc) or np.isnan(retained) or np.isnan(ebit) or np.isnan(equity)):
        z2 = (6.56 * wc / assets + 3.26 * retained / assets
              + 6.72 * ebit / assets + 1.05 * equity / liabilities)
    return z, z2


def band(metric: str, value: float) -> str:
    if value is None or np.isnan(value):
        return "n/a"
    for lo, hi, label in BANDS.get(metric, []):
        if lo <= value <= hi:
            return label
    return "n/a"


def score(panel: pd.DataFrame) -> pd.DataFrame:
    """Attach scores to a derived panel (one row per company-year)."""
    frame = panel.sort_values(["cik", "fiscal_year"]).copy()
    for col in ("roa", "leverage", "gross_margin", "asset_turnover"):
        if col in frame.columns:
            frame[col + "_prev"] = frame.groupby("cik")[col].shift(1)

    scores, bands, z2, notes = [], [], [], []
    for _, row in frame.iterrows():
        f, _signals = piotroski(row)
        z, zz = altman(row)
        scores.append(f)
        bands.append(band("piotroski_f", f))
        z2.append(zz)
        notes.append("")
    frame["piotroski_f"] = scores
    frame["piotroski_band"] = bands
    frame["altman_z2"] = z2
    frame["altman_band"] = [band("altman_z", v) for v in frame["altman_z2"]]
    return frame
