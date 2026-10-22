"""Peer fingerprints and quality scores: the arithmetic that has a right answer."""

from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.analytics import quality


def test_piotroski_counts_only_available_signals():
    row = {"roa": 0.10, "cfo": 50.0, "assets": 1000.0, "leverage": 0.4, "leverage_prev": 0.5,
           "gross_margin": 0.4, "gross_margin_prev": 0.35, "asset_turnover": 1.1,
           "asset_turnover_prev": 1.0, "roa_prev": 0.08}
    score, signals = quality.piotroski(row)
    # Seven signals are computable here; six pass. CFO/assets (0.05) is below ROA (0.10),
    # so the accrual-quality signal fails.
    assert len(signals) == 7
    assert score == 6.0


def test_piotroski_is_nan_without_inputs():
    score, signals = quality.piotroski({})
    assert np.isnan(score)
    assert signals == []


def test_altman_needs_liabilities_and_assets():
    assert np.isnan(quality.altman({"assets": 100.0})[0])
    assert np.isnan(quality.altman({"assets": 0.0, "liabilities": 50.0})[0])


def test_altman_z_increases_with_profitability():
    base = {"assets": 1000.0, "liabilities": 400.0, "retained_earnings": 100.0,
            "operating_income": 50.0, "equity": 600.0, "assets_current": 300.0,
            "liabilities_current": 200.0}
    better = dict(base, operating_income=150.0)
    assert quality.altman(better)[0] > quality.altman(base)[0]


def test_bands_label_correctly():
    assert quality.band("piotroski_f", 8) == "strong"
    assert quality.band("piotroski_f", 3) == "weak"
    assert quality.band("altman_z", 3.5) == "safe"
    assert quality.band("altman_z", 1.0) == "distress"
    assert quality.band("altman_z", float("nan")) == "n/a"
