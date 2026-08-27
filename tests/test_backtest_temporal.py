"""回测时点与分红入账回归测试。"""
from __future__ import annotations

from backtest import (
    _apply_precise_dividends,
    _find_price,
    _next_trading_date,
    _split_adjusted_trailing_dps,
)
from quarterly_strategy import _market_value


def test_find_price_never_uses_future_trade_day():
    prices = {"2024-06-28": 10.0, "2024-07-01": 11.0}
    # 6 月 30 日是周日，只能回填前一交易日，不能取 7 月 1 日。
    assert _find_price(prices, "2024-06-30") == 10.0
    assert _find_price(prices, "2024-06-27") is None


def test_next_trading_date_is_strictly_after_signal():
    calendar = ["2024-06-28", "2024-07-01", "2024-07-02"]
    assert _next_trading_date(calendar, "2024-06-28", 1) == "2024-07-01"
    assert _next_trading_date(calendar, "2024-06-28", 2) == "2024-07-02"
    assert _next_trading_date(calendar, "2024-07-02", 1) is None


def test_precise_dividend_only_after_entry_and_applies_split():
    state = {
        "cash": 0.0,
        "holdings": {"000001": {"shares": 100, "entry_date": "2024-06-30"}},
        "events": [],
    }
    details = {"000001": [
        {"ex_date": "2024-06-01", "dps": 1.0, "bonus_ratio": 0, "transfer_ratio": 0},
        {"ex_date": "2024-07-01", "dps": 1.0, "bonus_ratio": 1, "transfer_ratio": 0},
    ]}
    credited = set()
    result = _apply_precise_dividends(state, details, "2024-07-31", {}, credited)
    # 入场前分红不入账；税务持有期按实际除权日计算（仅 1 天，20%），
    # 且 10 送 1 增股。不能把检查日 7 月 31 日误当作除权日。
    assert result["cash"] == 80.0
    assert result["holdings"]["000001"]["shares"] == 110
    assert "000001_2024-06-01" not in credited
    assert "000001_2024-07-01" in credited


def test_precise_dividend_tax_uses_ex_date_not_check_date():
    state = {
        "cash": 0.0,
        "holdings": {"000002": {"shares": 100, "entry_date": "2024-06-01"}},
        "events": [],
    }
    details = {"000002": [{
        "ex_date": "2024-06-20", "dps": 1.0,
        "bonus_ratio": 0, "transfer_ratio": 0,
    }]}
    result = _apply_precise_dividends(state, details, "2024-08-31", {}, set())
    # 19 天持有应按 20% 税率计，不能因检查日晚于 30 天而按 10%。
    assert result["cash"] == 80.0


def test_trailing_dps_excludes_future_ex_date_in_prior_report_year():
    """报告年度早于检查日年份，但实际除息日在未来时不得入选 DPS。"""
    history = [{"year": 2023, "dps": 1.0}]
    detail = [{"year": 2023, "ex_date": "2025-07-15", "dps": 1.0}]
    assert _split_adjusted_trailing_dps(history, detail, "2025-01-31") is None


def test_precise_dividend_on_entry_date_is_not_credited():
    state = {
        "cash": 0.0,
        "holdings": {"000003": {"shares": 100, "entry_date": "2024-07-01"}},
        "events": [],
    }
    details = {"000003": [{
        "ex_date": "2024-07-01", "dps": 1.0,
        "bonus_ratio": 0, "transfer_ratio": 0,
    }]}
    result = _apply_precise_dividends(state, details, "2024-07-31", {}, set())
    assert result["cash"] == 0.0


def test_precise_dividends_apply_split_in_event_order():
    state = {
        "cash": 0.0,
        "holdings": {"000004": {"shares": 100, "entry_date": "2024-01-01"}},
        "events": [],
    }
    # 输入故意倒序。先 10 转 10，再按 200 股收取后续每股 1 元分红。
    details = {"000004": [
        {"ex_date": "2024-07-01", "dps": 1.0, "bonus_ratio": 0, "transfer_ratio": 0},
        {"ex_date": "2024-06-01", "dps": 0.0, "bonus_ratio": 0, "transfer_ratio": 10},
    ]}
    result = _apply_precise_dividends(state, details, "2024-07-31", {}, set())
    assert result["holdings"]["000004"]["shares"] == 200
    assert result["cash"] == 180.0


def test_precise_dividends_keep_distinct_same_date_payments():
    """同一除权日的不同报告年度分配不能因日期键相同而漏记。"""
    state = {
        "cash": 0.0,
        "holdings": {"000005": {"shares": 100, "entry_date": "2024-01-01"}},
        "events": [],
    }
    details = {"000005": [
        {"year": 2023, "ex_date": "2024-06-01", "dps": 1.0,
         "bonus_ratio": 0, "transfer_ratio": 0},
        {"year": 2022, "ex_date": "2024-06-01", "dps": 0.5,
         "bonus_ratio": 0, "transfer_ratio": 0},
    ]}
    result = _apply_precise_dividends(state, details, "2024-07-31", {}, set())
    # 持有超过 30 天但未满一年，两笔合计 150 元按 10% 计税。
    assert result["cash"] == 135.0
    assert len([e for e in result["events"] if e.get("side") == "dividend"]) == 2


def test_precise_dividends_drop_exact_duplicate_records_only_once():
    """接口重复返回同一记录时不能重复入账；不同记录仍应保留。"""
    state = {
        "cash": 0.0,
        "holdings": {"000006": {"shares": 100, "entry_date": "2024-01-01"}},
        "events": [],
    }
    payment = {
        "year": 2023, "ex_date": "2024-06-01", "dps": 1.0,
        "bonus_ratio": 0, "transfer_ratio": 0,
    }
    result = _apply_precise_dividends(
        state, {"000006": [dict(payment), dict(payment)]},
        "2024-07-31", {}, set(),
    )
    assert result["cash"] == 90.0
    assert len([e for e in result["events"] if e.get("side") == "dividend"]) == 1


def test_market_value_uses_stale_mark_only_for_valuation():
    holdings = {"000005": {"shares": 100, "entry_price": 10.0}}
    rows = {"000005": {"execution_price": None, "mark_price": 8.0}}
    assert _market_value(holdings, rows) == 800.0
