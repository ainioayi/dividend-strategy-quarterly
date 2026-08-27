import pytest

from build_historical_universe import canonical_sha256
from build_historical_v1_replay import convert_dividends, convert_price_rows


def test_convert_price_rows_keeps_replayable_unadjusted_close_and_bounds_date():
    rows = [
        {"date": "2015-01-05", "close": "10.50"},
        {"date": "2015-01-06", "close": "11.00"},
    ]
    stock = {
        "code": "600001", "provider_complete": True, "rows": rows,
        "row_count": 2, "rows_sha256": canonical_sha256(rows),
        "start_date": "2015-01-05", "end_date": "2015-01-06",
        "delist_date": "2015-01-06", "adjustflag": "3",
        "stored_fields": ["date", "close"], "trade_status_filtered": True,
    }
    assert convert_price_rows(stock, "2015-01-05") == {"2015-01-05": 10.5}
    stock["rows_sha256"] = "bad"
    with pytest.raises(ValueError, match="校验失败"):
        convert_price_rows(stock, "2015-01-05")


def test_convert_dividends_converts_per_share_stock_ratio_to_per_ten():
    stock = {"records": [{
        "report_year": 2014, "ex_date": "2015-06-01",
        "cash_per_share_before_tax": 0.5,
        "stock_dividend_per_share": 0.1,
        "reserve_to_stock_per_share": 0.2,
    }]}
    summaries, details = convert_dividends(stock, "2015-12-31")
    assert summaries == [{"year": 2014, "dps": 0.5,
                          "bonus_ratio": 1.0, "transfer_ratio": 2.0}]
    assert details[0]["ex_date"] == "2015-06-01"
