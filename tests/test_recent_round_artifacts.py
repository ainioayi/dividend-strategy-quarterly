"""第 22–24 轮冻结产物的口径与决策回归测试。"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SHA256 = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"
DATES_SHA256 = "f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e"
CUTOFF = "2026-08-25"


def _load(name: str) -> dict:
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


def _assert_frozen_inputs(payload: dict) -> None:
    assert payload["manifest_records_sha256"] == MANIFEST_SHA256
    assert payload["dates_sha256"] == DATES_SHA256
    assert payload["data_cutoff"] == CUTOFF


def test_round22_rebalance_windows_use_normalized_warmup_state():
    payload = _load("round22_simple_controls.json")
    _assert_frozen_inputs(payload)
    windows = payload["reset_windows"]
    assert len(windows) == 9
    assert all(item["warmup_count"] == 4 for item in windows)
    assert all(item["warmup_state_preserved"] is True for item in windows)
    assert all(item["nav_end"] == CUTOFF for item in windows)
    assert all(item["start_nav_before_normalization"] != 100000 for item in windows)
    by_key = {(item["rebalance_threshold"], item["start"]): item for item in windows}
    assert by_key[(2.0, "2023-01-01")]["metrics"]["cagr"] == -6.73
    assert by_key[(2.0, "2025-01-01")]["metrics"]["cagr"] == 3.10


def test_round23_two_holdings_still_match_the_authoritative_baseline():
    payload = _load("round23_holdings.json")
    _assert_frozen_inputs(payload)
    by_holdings = {item["max_holdings"]: item for item in payload["experiments"]}
    baseline = by_holdings[2]["metrics"]
    assert baseline["cagr"] == 41.38
    assert baseline["max_drawdown"] == 28.06
    assert baseline["sharpe"] == 1.217
    assert baseline["trade_count"] == 75
    assert by_holdings[1]["metrics"]["cagr"] < baseline["cagr"]
    assert by_holdings[3]["metrics"]["cagr"] < baseline["cagr"]


def test_round24_yield_ranking_remains_better_than_momentum_ranking():
    payload = _load("round24_rank_by.json")
    _assert_frozen_inputs(payload)
    by_rank = {item["rank_by"]: item for item in payload["experiments"]}
    yield_rank = by_rank["yield"]
    momentum_rank = by_rank["momentum"]
    assert yield_rank["metrics"]["cagr"] > momentum_rank["metrics"]["cagr"]
    assert yield_rank["metrics"]["max_drawdown"] < momentum_rank["metrics"]["max_drawdown"]
    assert yield_rank["rolling36"]["min_cagr"] > momentum_rank["rolling36"]["min_cagr"]
    assert yield_rank["oos"]["2023"]["cagr"] > momentum_rank["oos"]["2023"]["cagr"]


def test_round25_survivorship_audit_artifact():
    payload = _load("round25_survivorship_audit.json")
    assert payload["round"] == 25
    assert payload["as_of"] == CUTOFF
    assert payload["manifest_records_sha256"] == MANIFEST_SHA256
    assert payload["current_manifest_count"] == 210
    summary = payload["summary"]
    assert summary["processed"] == summary["failed_to_fetch"] + summary["zero_dividend_records"] + summary["has_dividend_records"]
    assert summary["has_dividend_records"] == 0, "东财 API 对退市股应不返回分红数据"
    assert payload["delisted_with_3yr_consecutive_dividend"] == 0
    assert len(payload["qualified"]) == 0
    assert "不完整" in payload["limitation"] or "覆盖" in payload["limitation"]
"""第 22–26 轮冻结产物的口径与决策回归测试。"""


def test_round26_daily_nav_audit_artifact():
    payload = _load("round26_daily_nav_audit.json")
    assert payload["round"] == 26
    assert payload["data_cutoff"] == CUTOFF
    assert payload["manifest_records_sha256"] == MANIFEST_SHA256
    assert payload["dates_sha256"] == DATES_SHA256
    monthly = payload["monthly"]
    daily = payload["daily"]
    # 月度基线仍然不变
    assert monthly["cagr"] == 41.38
    assert monthly["max_drawdown"] == 28.06
    # 日频回撤必须 >= 月频（月频不可能发现月内低点）
    assert daily["max_drawdown"] > monthly["max_drawdown"]
    assert daily["nav_points"] > monthly["nav_points"]
    # 低估约 4.4pp
    comparison = payload["comparison"]
    assert comparison["monthly_underestimates_daily_by_pp"] >= 3.0
"""第 22–27 轮冻结产物的口径与决策回归测试。"""


def test_round27_four_months_dominates_all_other_periods():
    payload = _load("round27_momentum_periods.json")
    _assert_frozen_inputs(payload)
    by_months = {item["momentum_months"]: item for item in payload["experiments"]}
    baseline = by_months[4]["metrics"]
    assert baseline["cagr"] == 41.38
    assert baseline["max_drawdown"] == 28.06
    for months in (3, 5, 6):
        challenger = by_months[months]["metrics"]
        assert baseline["cagr"] > challenger["cagr"]
        assert baseline["max_drawdown"] < challenger["max_drawdown"]
        assert baseline["sharpe"] > challenger["sharpe"]
        assert by_months[4]["rolling36"]["min_cagr"] > by_months[months]["rolling36"]["min_cagr"]
"""第 22–28 轮冻结产物的口径与决策回归测试。"""


def test_round28_yield_rank_beats_yield_vol_rank():
    payload = _load("round28_yield_vol_rank.json")
    _assert_frozen_inputs(payload)
    by_rank = {item["rank_by"]: item for item in payload["experiments"]}
    yield_rank = by_rank["yield"]
    yield_vol = by_rank["yield_vol"]
    assert yield_rank["metrics"]["cagr"] > yield_vol["metrics"]["cagr"]
    assert yield_rank["metrics"]["max_drawdown"] <= yield_vol["metrics"]["max_drawdown"]
    assert yield_rank["metrics"]["sharpe"] > yield_vol["metrics"]["sharpe"]
