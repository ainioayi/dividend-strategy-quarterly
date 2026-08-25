"""季度持仓决策的安全边界测试。"""
import pytest

from quarterly_strategy import evaluate_holding, rebalance_quarter


@pytest.mark.parametrize("sustainability", [None, "", "未评估", "数据缺失"])
def test_unverified_sustainability_never_triggers_sale(sustainability):
    row = {"code": "600000", "price": 10, "yield": 0, "pr": 99,
           "sustainability": sustainability}
    result = evaluate_holding(row, soft_exit_streak=1)
    assert result["action"] == "hold"
    assert result["kind"] == "data_gap"
    assert result["soft_exit_streak"] == 1


def test_missing_quote_never_triggers_sale_or_fallback_execution():
    state = {
        "initial_capital": 100000,
        "cash": 1000,
        "holdings": {
            "600000": {"code": "600000", "name": "测试", "shares": 100,
                       "entry_price": 10, "entry_date": "2025-01-02",
                       "soft_exit_streak": 1, "sector": "银行", "bank": True},
        },
        "events": [],
    }
    row = {"code": "600000", "price": None, "yield": 1, "pr": 2,
           "sustainability": "不可持续", "industry": "银行"}
    result = rebalance_quarter(state, [row], "2026-04-01")
    assert "600000" in result["holdings"]
    assert result["events"] == []
    assert result["actions"][0]["kind"] == "data_gap"


def test_soft_exit_requires_two_consecutive_quarters():
    row = {"code": "600000", "price": 10, "yield": 4.0, "pr": 1.1,
           "sustainability": "可持续", "industry": "银行"}
    first = evaluate_holding(row, 0)
    second = evaluate_holding(row, first["soft_exit_streak"])
    assert first["action"] == "hold"
    assert first["kind"] == "soft_pending"
    assert second["action"] == "sell"
    assert second["kind"] == "soft_confirmed"
