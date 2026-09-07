"""发布门禁必须拦住错误账本和未证实的缺价，并保留停牌例外。"""
import copy
import json
import sys
from unittest.mock import Mock

import pytest

from forward_performance import _journal_codes, build_strategy_suite, fetch_suspension_evidence, update_market_snapshot
from forward_quality import required_price_dates, validate_publication
from monthly_forward import FORWARD_STRATEGIES, _hash, build_metadata, strategy_profile


def _seal(market):
    for security in [market["benchmark"], *market["securities"].values()]:
        security["hashes"] = {f"{field}_sha256": _hash(security[field]) for field in ("prices", "dividends")}
    market["hashes"] = {}
    market["hashes"]["content_sha256"] = _hash({k: v for k, v in market.items() if k != "retrieved_at"})


@pytest.fixture
def inputs():
    metadata = {key: build_metadata(key) for key in FORWARD_STRATEGIES}
    journals = {
        key: [json.loads(line) for line in strategy_profile(key)["journal_path"].read_text(encoding="utf-8").splitlines()][:2]
        for key in FORWARD_STRATEGIES
    }
    calendar = ["2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28", "2026-08-31",
                "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    securities = {}
    for rows in journals.values():
        for holding in rows[-1]["holdings"]:
            code = holding["code"]
            close = rows[-1].get("closing_prices", {}).get(code, holding["entry_price"])
            securities[code] = {"code": code, "name": code, "selected_provider": "测试行情",
                                "prices": [{"date": day, "close": close} for day in calendar], "dividends": []}
    market = {"as_of": calendar[-1], "price_format": "unadjusted_close",
              "trading_calendar": {"provider": "baostock_trade_dates", "dates": calendar},
              "benchmark": {"prices": [{"date": day, "close": 4.0} for day in calendar], "dividends": []},
              "securities": securities, "hashes": {}}
    _seal(market)
    return metadata, journals, market


def _validate(inputs, previous=None):
    metadata, journals, market = inputs
    result = build_strategy_suite(metadata, journals, market)
    return validate_publication(metadata, journals, market, result, previous)


def test五套当前持仓都必须刷新且卖出的证券不再算当前持仓():
    rows = [
        {"strategy_id": "v1", "event_type": "execution", "execution_date": "2026-09-01", "holdings": [{"code": "600329", "shares": 100}]},
        {"strategy_id": "ma_v22", "event_type": "execution", "execution_date": "2026-09-01", "holdings": [{"code": "511010", "shares": 100}]},
        {"strategy_id": "v1", "event_type": "execution", "execution_date": "2026-10-09", "holdings": []},
    ]
    assert _journal_codes(rows[:2])[1] == {"600329", "511010"}
    assert _journal_codes(rows)[1] == {"511010"}
    assert required_price_dates(rows, ["2026-09-01", "2026-10-09"]) == {
        "600329": {"2026-09-01"}, "511010": {"2026-09-01", "2026-10-09"},
    }


@pytest.mark.parametrize("status", ["0", "1"])
def test缺价只能由真实停牌状态放行(monkeypatch, status):
    bs = Mock()
    bs.login.return_value.error_code = "0"
    result = Mock(error_code="0", fields=["date", "tradestatus"])
    result.next.side_effect = [True, False]
    result.get_row_data.return_value = ["2026-09-04", status]
    bs.query_history_k_data_plus.return_value = result
    monkeypatch.setitem(sys.modules, "baostock", bs)
    if status == "0":
        assert fetch_suspension_evidence("600329", ["2026-09-04"])["dates"] == ["2026-09-04"]
    else:
        with pytest.raises(RuntimeError, match="停牌未确认"):
            fetch_suspension_evidence("600329", ["2026-09-04"])
    assert bs.query_history_k_data_plus.call_args.kwargs["adjustflag"] == "3"
    bs.logout.assert_called_once()


def test五策略完整数据通过并报告真实价格日(inputs):
    report = _validate(inputs)
    assert report["status"] == "passed"
    assert len(report["strategies"]) == 5
    assert all(row["as_of"] == "2026-09-04" and row["price_status"] == "current" for row in report["strategies"].values())


@pytest.mark.parametrize("missing_day", ["2026-09-02", "2026-09-04"])
def test当日或历史持仓缺价不能冒充停牌(inputs, missing_day):
    security = inputs[2]["securities"]["600329"]
    security["prices"] = [row for row in security["prices"] if row["date"] != missing_day]
    _seal(inputs[2])
    with pytest.raises(ValueError, match="行情缺失且停牌未确认"):
        _validate(inputs)
    security["suspension_evidence"] = {"provider": "baostock_tradestatus", "dates": [missing_day]}
    _seal(inputs[2])
    report = _validate(inputs)
    assert report["warnings"]
    if missing_day == "2026-09-04":
        assert report["strategies"]["v1"]["price_status"] == "confirmed_suspension"


def test指纹不符不得发布(inputs):
    inputs[2]["securities"]["600329"]["prices"][-1]["close"] += 1
    with pytest.raises(ValueError, match="快照指纹"):
        _validate(inputs)


def test共同日历或策略日期缺失不得发布(inputs):
    metadata, journals, market = inputs
    market["trading_calendar"]["dates"] = market["trading_calendar"]["dates"][:-1]
    _seal(market)
    with pytest.raises(ValueError, match="核验交易日历"):
        _validate(inputs)


def test历史收益更正需要明确原因且保留逐项差异(inputs):
    from forward_quality import history_changes
    metadata, journals, market = inputs
    old = build_strategy_suite(metadata, journals, market)
    market["securities"]["600329"]["prices"][-1]["close"] += 1
    _seal(market)
    new = build_strategy_suite(metadata, journals, market)
    with pytest.raises(ValueError, match="修复原因"):
        validate_publication(metadata, journals, market, new, old)
    report = validate_publication(metadata, journals, market, new, old, correction_reason="测试历史行情更正")
    assert report["status"] == "passed"
    changes = history_changes(old, new)
    assert {row["field"] for row in changes} == {"v1_nav", "v2_nav", "v3_nav"}
    assert all(row["date"] == "2026-09-04" and row["after"] > row["before"] for row in changes)


def test账本内容或历史前缀漂移不得发布(inputs):
    with pytest.raises(ValueError, match="历史前缀"):
        _validate(inputs, {"audit": {"strategy_journals": {"v1": "0" * 64}}})
    inputs[1]["v1"][-1]["cash"] += 1
    with pytest.raises(ValueError, match="账本内容指纹"):
        _validate(inputs)


def test规则漂移和逾期未执行分别拦截(inputs):
    changed = copy.deepcopy(inputs)
    changed[0]["v1"]["rules"]["hold_yield"] += 1
    with pytest.raises(ValueError, match="冻结合同"):
        _validate(changed)
    inputs[1]["v1"].pop()
    with pytest.raises(ValueError, match="逾期待执行"):
        _validate(inputs)


def test执行日期跳过真实交易日即使指纹重算也拒绝(inputs):
    event = inputs[1]["v1"][-1]
    event["execution_date"] = "2026-09-02"
    event["execution_input"]["data_cutoff"] = "2026-09-02"
    event["content_sha256"] = _hash({k: v for k, v in event.items() if k not in ("recorded_at", "content_sha256")})
    with pytest.raises(ValueError, match="下一真实交易日"):
        _validate(inputs)


def test费用先合计原精度再四舍五入(inputs):
    metadata, journals, market = inputs
    result = build_strategy_suite(metadata, journals, market)
    assert result["strategies"]["v1"]["summary"]["fees"] == round(journals["v1"][-1]["fees"], 2)


def test更新快照覆盖所有账户而不是只有最后一个(inputs):
    metadata, journals, market = inputs
    combined = [row for rows in journals.values() for row in rows]
    calls = []

    def prices(code, start, as_of):
        calls.append(code)
        security = market["benchmark"] if code == "510300" else market["securities"][code]
        return {"prices": security["prices"], "prices_sha256": _hash(security["prices"]),
                "selected_provider": "测试行情", "validated_providers": ["测试行情"]}

    dividends = lambda *args: {"provider": "测试分红", "url": "https://example.test", "dividends": []}
    result = update_market_snapshot(metadata["v1"], combined, "2026-09-04", market,
                                    price_fetcher=prices, benchmark_dividend_fetcher=dividends,
                                    fund_dividend_fetcher=dividends, stock_dividend_fetcher=lambda *args: [],
                                    sleep_seconds=0, names={})
    assert set(calls) == {"510300", *market["securities"]}
    assert set(result["active_codes"]) == set(market["securities"])
