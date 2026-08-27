"""分红信息可得性延迟的时点边界测试。"""
from __future__ import annotations

import backtest


def _run_with_lag(monkeypatch, lag: int):
    captured = []

    monkeypatch.setattr(
        backtest,
        "fetch_kline",
        lambda code, cutoff=None: {"2024-01-31": 10.0},
    )
    monkeypatch.setattr(backtest, "fetch_dividends", lambda code: [])
    monkeypatch.setattr(
        backtest,
        "_fetch_dividends_detail",
        lambda code: [
            {"year": 2022, "ex_date": "2023-12-01", "dps": 1.0},
            {"year": 2023, "ex_date": "2024-01-15", "dps": 1.0},
        ],
    )

    def snapshot(code, price, history, as_of, detail=None):
        captured.append((as_of, [item["ex_date"] for item in (detail or [])]))
        return {
            "code": code,
            "name": code,
            "price": price,
            "yield": 10.0,
            "real_yield": 10.0,
            "pr": 0.5,
            "dps": 1.0,
            "sustainability": "可持续",
            "industry": "未知行业",
            "sector": "未知行业",
            "bank": False,
        }

    monkeypatch.setattr(backtest, "build_snapshot", snapshot)
    backtest.run_backtest(
        rules={
            "initial_capital": 100000,
            "pool_mode": "curated",
            "momentum_months": 0,
            "execution_lag_days": 0,
            "reinvest_dividends": False,
            "dividend_information_lag_days": lag,
            "max_holdings": 1,
        },
        dynamic_pool=False,
        codes=["000001"],
        rebalance_dates=["2024-01-31"],
        verbose=False,
    )
    return captured


def test_information_lag_zero_keeps_all_known_ex_dates(monkeypatch):
    captured = _run_with_lag(monkeypatch, 0)
    assert captured == [("2024-01-31", ["2023-12-01", "2024-01-15"])]


def test_information_lag_filters_signal_only_and_does_not_change_default(monkeypatch):
    captured = _run_with_lag(monkeypatch, 30)
    # 30 日前为 2024-01-01，1 月 15 日的分红不能参与该信号。
    assert captured == [("2024-01-31", ["2023-12-01"])]


def test_information_lag_rejects_negative_value():
    try:
        backtest.run_backtest(
            rules={"dividend_information_lag_days": -1},
            dynamic_pool=False,
            codes=[],
            rebalance_dates=[],
            verbose=False,
        )
    except ValueError as exc:
        assert "dividend_information_lag_days" in str(exc)
    else:
        raise AssertionError("负的信息延迟必须被拒绝")


def test_backtest_rejects_invalid_pool_switch_month():
    try:
        backtest.run_backtest(
            rules={"pool_switch_month": 13},
            dynamic_pool=False,
            codes=[],
            rebalance_dates=[],
            verbose=False,
        )
    except ValueError as exc:
        assert "pool_switch_month" in str(exc)
    else:
        raise AssertionError("非法候选池切换月份必须被拒绝")
