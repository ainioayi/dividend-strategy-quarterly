import pytest

from build_historical_filtered_replay import (
    _require_inputs,
    convert_listed_dividends,
    convert_listed_price_rows,
)
from build_historical_universe import canonical_sha256


def test_convert_listed_dividends_keeps_only_visible_events_and_sums_year():
    stock = {"records": [
        {"year": 2024, "ex_date": "2025-05-01", "dps": 0.3,
         "bonus_ratio": 0.0, "transfer_ratio": 0.0},
        {"year": 2024, "ex_date": "2025-08-01", "dps": 0.2,
         "bonus_ratio": 1.0, "transfer_ratio": 0.0},
        {"year": 2025, "ex_date": "2026-09-01", "dps": 0.5,
         "bonus_ratio": 0.0, "transfer_ratio": 0.0},
    ]}
    summaries, details = convert_listed_dividends(stock, "2026-08-25")
    assert summaries == [{
        "year": 2024, "dps": 0.5,
        "bonus_ratio": 1.0, "transfer_ratio": 0.0,
    }]
    assert [row["ex_date"] for row in details] == ["2025-05-01", "2025-08-01"]


def test_convert_listed_price_rows_recomputes_hash_and_bounds_date():
    rows = [
        {"date": "2026-08-25", "close": 10.0},
        {"date": "2026-08-26", "close": 11.0},
    ]
    stock = {"code": "600001", "rows": rows, "rows_sha256": canonical_sha256(rows)}
    assert convert_listed_price_rows(stock, "2026-08-25") == {"2026-08-25": 10.0}
    stock["rows_sha256"] = "bad"
    with pytest.raises(ValueError, match="哈希无效"):
        convert_listed_price_rows(stock, "2026-08-25")


def test_filtered_replay_requires_each_gate_but_not_all_stock_verification():
    security = {"independently_verified": True}
    delisted_dividends = {"manual_data_gate_complete": True}
    delisted_prices = {"independently_verified": True}
    listed_dividends = {"manual_data_gate_complete": True}
    listed_prices = {
        "manual_data_gate_complete": True,
        "eligible_scope_independently_verified": True,
    }
    _require_inputs(
        security, delisted_dividends, delisted_prices,
        listed_dividends, listed_prices,
    )
    listed_dividends["manual_data_gate_complete"] = False
    with pytest.raises(RuntimeError, match="在市分红"):
        _require_inputs(
            security, delisted_dividends, delisted_prices,
            listed_dividends, listed_prices,
        )
    listed_dividends["manual_data_gate_complete"] = True
    listed_prices["eligible_scope_independently_verified"] = False
    with pytest.raises(RuntimeError, match="没有放行"):
        _require_inputs(
            security, delisted_dividends, delisted_prices,
            listed_dividends, listed_prices,
        )
