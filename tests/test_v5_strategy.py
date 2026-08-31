import math
from datetime import date

import pytest

from v5_strategy import (annualized_volatility, cash_interest, dividend_cut_exit,
                         downside_semideviation, new_buy_budget_multiplier,
                         payout_covered, rebalance_band, risk_multiplier,
                         round_lot_shares, select_candidates, transaction_fees,
                         backtest_metrics)


def test_risk_windows_and_multiplier():
    returns = [-0.01] * 50
    assert downside_semideviation(returns) == pytest.approx(math.sqrt(252) * 0.01)
    assert downside_semideviation(returns[:49]) is None
    assert risk_multiplier([-0.010248] * 50, [-0.005] * 50) == pytest.approx(0.615, abs=0.001)
    prices = [100 * (1.01 ** index) for index in range(61)]
    assert annualized_volatility(prices) == pytest.approx(0.0, abs=1e-12)
    assert annualized_volatility(prices[:60]) is None


def test附件2015年末阀门值可由锁定公式反算():
    attachment_m = 0.615  # 附录“月度决策”F6，决策日 2015-12-31。
    controlling_daily_loss = 0.10 / attachment_m / math.sqrt(252)
    assert risk_multiplier([-controlling_daily_loss] * 50, [0.0] * 50) == pytest.approx(
        attachment_m
    )


def test_entry_gate_finance_dividend_and_index():
    assert payout_covered(1.2, 1.0, "2026-04-01", "2026-06-30")
    assert not payout_covered(1.2, 1.0, "2026-07-01", "2026-06-30")
    assert not payout_covered(0.8, 1.0, "2026-04-01", "2026-06-30")
    assert dividend_cut_exit(1.0, 0.69, "2026-06-30")
    assert not dividend_cut_exit(1.0, 0.69, "2026-07-31")
    assert new_buy_budget_multiplier([100.0] + [99.0] * 240) == 0.5
    assert new_buy_budget_multiplier([100.0] * 240) is None
    assert rebalance_band(0.80, 0.779) == 0.05
    assert rebalance_band(0.80, 0.78) == 0.20


def test_selection_cost_interest_and_lot():
    rows = [
        {"code": "1", "industry": "银行", "yield": 9, "momentum": 1,
         "volatility": 0.2, "payout_covered": True},
        {"code": "2", "industry": "银行", "yield": 10, "momentum": 1,
         "volatility": 0.3, "payout_covered": True},
        {"code": "3", "industry": "煤炭", "yield": 8, "momentum": 1,
         "volatility": 0.6, "payout_covered": True},
    ]
    assert [row["code"] for row in select_candidates(rows)] == ["2", "3"]
    before = transaction_fees(10000, "sell", "2023-08-27")
    after = transaction_fees(10000, "sell", "2023-08-28")
    assert before["total"] - after["total"] == pytest.approx(5)
    assert cash_interest(100000, 2026) == pytest.approx(100000 * 0.014 / 252)
    assert round_lot_shares(9999, 9.99) == 1000


def test_rolling_metrics_require_complete_months():
    rows = []
    year, month = 2020, 1
    for index in range(49):
        rows.append({"date": date(year, month, 28).isoformat(), "nav": 100 * (1.01 ** index)})
        month += 1
        if month == 13:
            year, month = year + 1, 1
    metrics = backtest_metrics(rows, 100, 0)
    assert metrics["rolling_36m_worst_cagr"] == pytest.approx(1.01 ** 12 - 1)
    assert metrics["rolling_48m_worst_cagr"] == pytest.approx(1.01 ** 12 - 1)
    rows.pop(20)
    metrics = backtest_metrics(rows, 100, 0)
    assert metrics["rolling_48m_worst_cagr"] is None
