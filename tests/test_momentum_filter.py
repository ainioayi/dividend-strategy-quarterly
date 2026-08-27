"""动量过滤功能单元测试。"""
import pytest
from quarterly_strategy import momentum_filter, merged_rules


def _row(code, price, yield_pct):
    """构建测试用快照行。"""
    return {
        "code": code, "name": code, "price": price,
        "yield": yield_pct, "real_yield": yield_pct,
        "pr": 0.5, "dps": price * yield_pct / 100,
        "sustainability": "可持续",
        "industry": "测试", "sector": "测试", "bank": False,
    }

# 足够长的调仓日列表，支持回看 3 个月
DATES = ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30", "2024-05-31", "2024-06-30"]


def test_momentum_disabled_returns_all_rows():
    """momentum_months=0 时不过滤，返回全部行。"""
    rows = [_row("000001", 10.0, 8.0), _row("000002", 20.0, 7.5)]
    result = momentum_filter(rows, set(), None, "2024-06-30", DATES)
    assert len(result) == 2


def test_momentum_filters_declining_stock():
    """价格下跌的入场候选被过滤。"""
    rows = [_row("000001", 8.0, 8.0), _row("000002", 20.0, 8.5)]
    prices = {"000001": 10.0, "000002": 19.0}
    lookup = lambda code, date: prices.get(code)
    rules = merged_rules({"momentum_months": 3, "momentum_threshold": 0.93, "entry_yield": 7.5})
    result = momentum_filter(rows, set(), lookup, "2024-06-30", DATES, rules)
    codes = [r["code"] for r in result]
    assert "000002" in codes
    assert "000001" not in codes


def test_momentum_keeps_held_stocks():
    """已持仓股票不受动量限制。"""
    rows = [_row("000001", 8.0, 8.0)]
    prices = {"000001": 10.0}
    lookup = lambda code, date: prices.get(code)
    rules = merged_rules({"momentum_months": 3, "momentum_threshold": 0.93, "entry_yield": 7.5})
    result = momentum_filter(rows, {"000001"}, lookup, "2024-06-30", DATES, rules)
    assert len(result) == 1


def test_momentum_keeps_below_entry_yield():
    """未达入场门槛的股票不受动量限制。"""
    rows = [_row("000001", 8.0, 5.0)]
    prices = {"000001": 10.0}
    lookup = lambda code, date: prices.get(code)
    rules = merged_rules({"momentum_months": 3, "momentum_threshold": 0.93, "entry_yield": 7.5})
    result = momentum_filter(rows, set(), lookup, "2024-06-30", DATES, rules)
    assert len(result) == 1


def test_momentum_exit_calculates_ratio_for_held_low_yield_stock():
    """启用持仓退出时，即使收益率低于入场线也要留下历史动量比。"""
    rows = [_row("000001", 8.0, 5.0)]
    prices = {"000001": 10.0}
    lookup = lambda code, date: prices.get(code)
    rules = merged_rules({
        "momentum_months": 3,
        "momentum_threshold": 0.93,
        "entry_yield": 7.5,
        "momentum_exit_threshold": 0.85,
    })
    result = momentum_filter(rows, {"000001"}, lookup, "2024-06-30", DATES, rules)
    assert len(result) == 1
    assert result[0]["momentum_ratio"] == pytest.approx(0.8)


def test_momentum_insufficient_history():
    """调仓历史不足 momentum_months 时不过滤。"""
    rows = [_row("000001", 8.0, 8.0)]
    lookup = lambda code, date: 10.0
    short_dates = ["2024-06-30"]
    rules = merged_rules({"momentum_months": 3, "momentum_threshold": 0.93, "entry_yield": 7.5})
    result = momentum_filter(rows, set(), lookup, "2024-06-30", short_dates, rules)
    assert len(result) == 1


def test_momentum_threshold_boundary():
    """阈值边界精确匹配时保留。"""
    rows = [_row("000001", 9.3, 8.0)]
    prices = {"000001": 10.0}
    lookup = lambda code, date: prices.get(code)
    rules = merged_rules({"momentum_months": 3, "momentum_threshold": 0.93, "entry_yield": 7.5})
    result = momentum_filter(rows, set(), lookup, "2024-06-30", DATES, rules)
    assert len(result) == 1
