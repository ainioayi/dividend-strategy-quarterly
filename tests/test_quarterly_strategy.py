"""季度持仓决策的安全边界测试。"""
import pytest

from quarterly_strategy import evaluate_holding, rebalance_quarter, reinvest_cash, rebalance_rotation


# 显式规则，避免依赖默认参数变化
STRICT_RULES = {
    "hold_yield": 4.5, "hold_pr": 1.05,
    "exit_yield": 4.25, "exit_pr": 1.2,
    "exit_confirm_quarters": 2,
}


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


def test_backtest_trade_uses_separate_execution_price():
    state = {"initial_capital": 100000, "cash": 100000, "holdings": {}, "events": []}
    row = {
        "code": "600001", "price": 10.0, "execution_price": 12.0,
        "yield": 7.0, "pr": 0.5, "sustainability": "可持续",
        "industry": "电力", "bank": False,
    }
    result = rebalance_quarter(state, [row], "2026-04-01", {
        "entry_yield": 6.0, "entry_pr": 999, "max_holdings": 1,
        "max_sector": 2, "max_banks": 2, "max_position_pct": 1.0,
        "lot_size": 100, "reinvest_dividends": False,
    })
    assert result["holdings"]["600001"]["entry_price"] == 12.0
    assert result["events"][0]["price"] == 12.0


def test_missing_execution_price_blocks_sell_signal():
    state = {
        "initial_capital": 100000, "cash": 1000,
        "holdings": {"600001": {"code": "600001", "shares": 100,
                                  "entry_price": 10.0, "entry_date": "2025-01-01",
                                  "soft_exit_streak": 0}},
        "events": [],
    }
    row = {
        "code": "600001", "price": 10.0, "execution_price": None,
        "yield": 4.0, "pr": 2.0, "sustainability": "可持续",
        "industry": "电力", "bank": False,
    }
    result = rebalance_quarter(state, [row], "2026-04-01", {
        "hold_yield": 5.0, "hold_pr": 999, "exit_confirm_quarters": 1,
        "max_holdings": 1, "max_sector": 2, "max_banks": 2,
        "reinvest_dividends": False,
    })
    assert "600001" in result["holdings"]
    assert result["actions"][0]["kind"] == "data_gap"


def test_soft_exit_requires_confirmation_quarters():
    """软退出需要连续 N 个季度确认才执行（用显式规则避免依赖默认值）。"""
    row = {"code": "600000", "price": 10, "yield": 4.0, "pr": 1.1,
           "sustainability": "可持续", "industry": "银行"}
    # 用 exit_confirm_quarters=2 测试两季确认
    first = evaluate_holding(row, 0, STRICT_RULES)
    second = evaluate_holding(row, first["soft_exit_streak"], STRICT_RULES)
    assert first["action"] == "hold"
    assert first["kind"] == "soft_pending"
    assert second["action"] == "sell"
    assert second["kind"] == "soft_confirmed"


def test_fast_exit_one_quarter_confirmation():
    """exit_confirm_quarters=1 时，第一个季度即触发卖出。"""
    row = {"code": "600000", "price": 10, "yield": 4.0, "pr": 1.1,
           "sustainability": "可持续", "industry": "银行"}
    fast_rules = dict(STRICT_RULES, exit_confirm_quarters=1)
    result = evaluate_holding(row, 0, fast_rules)
    assert result["action"] == "sell"
    assert result["kind"] == "soft_confirmed"


def test_reinvest_cash_disabled_by_default_when_flag_off():
    """reinvest_dividends=False 时不再投资。"""
    rules = {"reinvest_dividends": False, "reinvest_cash_reserve": 3000,
             "lot_size": 100, "max_position_pct": 0.35}
    state = {
        "cash": 10000,
        "initial_capital": 100000,
        "holdings": {"600000": {"code": "600000", "name": "测试", "shares": 1000,
                                 "entry_price": 10, "sector": "银行", "bank": True}},
        "events": [],
    }
    rows = {"600000": {"code": "600000", "price": 10}}
    result = reinvest_cash(state, rows, "2026-04-01", rules)
    assert result["cash"] == 10000  # 现金不变
    assert len(result["events"]) == 0


def test_reinvest_cash_buys_shares_when_enabled():
    """reinvest_dividends=True 且现金超过保留额时追加买入。"""
    rules = {"reinvest_dividends": True, "reinvest_cash_reserve": 3000,
             "lot_size": 100, "max_position_pct": 0.35,
             "buy_commission_rate": 0.0003, "stamp_duty_rate": 0.0005,
             "transfer_fee_rate": 0.00001, "min_commission": 5.0}
    state = {
        "cash": 50000,
        "initial_capital": 100000,
        "holdings": {"600000": {"code": "600000", "name": "测试", "shares": 1000,
                                 "entry_price": 10, "sector": "银行", "bank": True}},
        "events": [],
    }
    rows = {"600000": {"code": "600000", "price": 10}}
    result = reinvest_cash(state, rows, "2026-04-01", rules)
    assert result["cash"] < 50000  # 现金减少
    assert result["holdings"]["600000"]["shares"] > 1000  # 股数增加
    assert len(result["events"]) == 1
    assert result["events"][0]["side"] == "买入"


def test_reinvest_cash_respects_position_cap():
    """分红再投资不超过单票仓位上限。"""
    rules = {"reinvest_dividends": True, "reinvest_cash_reserve": 1000,
             "lot_size": 100, "max_position_pct": 0.15,
             "buy_commission_rate": 0.0003, "stamp_duty_rate": 0.0005,
             "transfer_fee_rate": 0.00001, "min_commission": 5.0}
    # 持仓已达 15000（15% 上限），不应追加
    state = {
        "cash": 30000,
        "initial_capital": 100000,
        "holdings": {"600000": {"code": "600000", "name": "测试", "shares": 1500,
                                 "entry_price": 10, "sector": "银行", "bank": True}},
        "events": [],
    }
    rows = {"600000": {"code": "600000", "price": 10}}
    result = reinvest_cash(state, rows, "2026-04-01", rules)
    assert result["holdings"]["600000"]["shares"] == 1500  # 不变
    assert len(result["events"]) == 0


def test_reinvest_cash_position_cap_scales_with_current_nav():
    """仓位上限应随当前净值增长，而不是固定锁死在初始本金。"""
    rules = {"reinvest_dividends": True, "reinvest_cash_reserve": 0,
             "lot_size": 100, "max_position_pct": 0.5,
             "buy_commission_rate": 0.0003, "stamp_duty_rate": 0.0005,
             "transfer_fee_rate": 0.00001, "min_commission": 5.0}
    state = {
        "cash": 100000.0,
        "initial_capital": 100000.0,
        "holdings": {"600000": {"code": "600000", "name": "测试",
                                 "shares": 5500, "entry_price": 10,
                                 "sector": "银行", "bank": True}},
        "events": [],
    }
    # 当前净值 155000，50% 上限为 77500；按初始本金计算则会错误地
    # 认为 55000 已超过 50000，从而拒绝本次追加。
    result = reinvest_cash(state, {"600000": {"code": "600000", "price": 10}},
                           "2026-04-01", rules)
    assert result["holdings"]["600000"]["shares"] > 5500


def test_reinvest_cash_does_not_overshoot_position_cap():
    """追加买入金额不得超过剩余单票仓位上限。"""
    rules = {"reinvest_dividends": True, "reinvest_cash_reserve": 0,
             "lot_size": 100, "max_position_pct": 0.2,
             "buy_commission_rate": 0.0003, "stamp_duty_rate": 0.0005,
             "transfer_fee_rate": 0.00001, "min_commission": 5.0}
    state = {
        "cash": 100000.0,
        "initial_capital": 100000.0,
        "holdings": {"600000": {"code": "600000", "name": "测试",
                                 "shares": 1000, "entry_price": 10,
                                 "sector": "银行", "bank": True}},
        "events": [],
    }
    result = reinvest_cash(state, {"600000": {"code": "600000", "price": 10}},
                           "2026-04-01", rules)
    assert result["holdings"]["600000"]["shares"] * 10 <= 22000


def test_reinvest_cash_keeps_reserve_after_fees():
    """手续费也计入预算，交易后现金不得低于保留额。"""
    rules = {
        "reinvest_dividends": True, "reinvest_cash_reserve": 3000,
        "lot_size": 100, "max_position_pct": 1.0,
        "buy_commission_rate": 0.0003, "stamp_duty_rate": 0.0005,
        "transfer_fee_rate": 0.00001, "min_commission": 5.0,
    }
    state = {
        "cash": 4004.0, "initial_capital": 100000.0,
        "holdings": {"600000": {"code": "600000", "shares": 1000,
                                 "entry_price": 10, "sector": "银行", "bank": True}},
        "events": [],
    }
    result = reinvest_cash(state, {"600000": {"code": "600000", "price": 10}},
                           "2026-04-01", rules)
    assert result["cash"] >= 3000.0


def test_reinvest_cash_rejects_invalid_reserve_or_cap():
    state = {
        "cash": 10000.0, "initial_capital": 100000.0,
        "holdings": {"600000": {"code": "600000", "shares": 1000,
                                 "entry_price": 10}},
        "events": [],
    }
    with pytest.raises(ValueError, match="reinvest_cash_reserve"):
        reinvest_cash(state, {"600000": {"price": 10}}, "2026-04-01",
                      {"reinvest_dividends": True, "reinvest_cash_reserve": -1})
    with pytest.raises(ValueError, match="max_position_pct"):
        reinvest_cash(state, {"600000": {"price": 10}}, "2026-04-01",
                      {"reinvest_dividends": True, "max_position_pct": 0})


def test_rebalance_rotation_sells_low_rank_holdings():
    """轮换策略：收益率跌出 Top-N 且低于轮换阈值时卖出。"""
    state = {
        "initial_capital": 100000,
        "cash": 1000,
        "holdings": {
            "600000": {"code": "600000", "name": "旧股", "shares": 1000,
                       "entry_price": 10, "entry_date": "2025-01-02",
                       "soft_exit_streak": 0, "sector": "银行", "bank": True},
        },
        "events": [],
    }
    rows = [
        {"code": "600001", "name": "新股", "price": 8, "yield": 7.0,
         "sustainability": "可持续", "industry": "银行", "bank": True},
        {"code": "600000", "name": "旧股", "price": 12, "yield": 3.0,
         "sustainability": "可持续", "industry": "银行", "bank": True},
    ]
    result = rebalance_rotation(state, rows, "2026-04-01", {
        "entry_yield": 4.5, "rotate_yield": 5.0, "max_holdings": 5,
        "max_sector": 2, "max_banks": 2, "lot_size": 100,
        "max_position_pct": 1.0,
    })
    actions = result.get("actions", [])
    sells = [a for a in actions if a.get("action") == "sell"]
    assert len(sells) == 1
    assert sells[0]["code"] == "600000"


def test_rebalance_rotation_buys_top_yield():
    """轮换策略：买入收益率最高的候选。"""
    state = {
        "initial_capital": 100000,
        "cash": 100000,
        "holdings": {},
        "events": [],
    }
    rows = [
        {"code": "600001", "name": "高息股", "price": 8, "yield": 7.0,
         "sustainability": "可持续", "industry": "银行", "bank": True},
        {"code": "600002", "name": "中息股", "price": 10, "yield": 5.5,
         "sustainability": "可持续", "industry": "化工", "bank": False},
    ]
    result = rebalance_rotation(state, rows, "2026-04-01", {
        "entry_yield": 4.5, "rotate_yield": 5.0, "max_holdings": 5,
        "max_sector": 2, "max_banks": 2, "lot_size": 100,
        "max_position_pct": 1.0,
    })
    assert "600001" in result["holdings"]
    assert "600002" in result["holdings"]


def test_rebalance_rotation_keeps_top_ranked():
    """轮换策略：仍在 Top-N 的持仓不卖出。"""
    state = {
        "initial_capital": 100000,
        "cash": 1000,
        "holdings": {
            "600001": {"code": "600001", "name": "高息股", "shares": 1000,
                       "entry_price": 8, "entry_date": "2025-01-02",
                       "soft_exit_streak": 0, "sector": "银行", "bank": True},
        },
        "events": [],
    }
    rows = [
        {"code": "600001", "name": "高息股", "price": 9, "yield": 6.0,
         "sustainability": "可持续", "industry": "银行", "bank": True},
    ]
    result = rebalance_rotation(state, rows, "2026-04-01", {
        "entry_yield": 4.5, "rotate_yield": 5.0, "max_holdings": 5,
        "max_sector": 2, "max_banks": 2, "lot_size": 100,
        "max_position_pct": 1.0,
    })
    assert "600001" in result["holdings"]
    sells = [a for a in result.get("actions", []) if a.get("action") == "sell"]
    assert len(sells) == 0
