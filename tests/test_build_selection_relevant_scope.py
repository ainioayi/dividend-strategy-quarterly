from build_historical_universe import write_json_atomic
from build_selection_relevant_scope import build_selection_scope


def test_selection_scope_uses_only_signal_time_rules(tmp_path):
    records = [
        {"year": year, "ex_date": f"{year + 1}-06-01", "dps": 1.0,
         "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        for year in range(2015, 2021)
    ]
    dividends = {"stocks": [{
        "code": "600001", "verification_required": True, "records": records,
    }]}
    rows = [
        {"date": day, "close": close}
        for day, close in zip(
            ["2020-01-31", "2020-02-28", "2020-03-31", "2020-04-30", "2020-05-29"],
            [11.0, 12.0, 12.0, 12.0, 10.0],
        )
    ]
    checkpoint = {
        "schema_version": 1, "provider": "baostock", "code": "600001",
        "list_date": "2000-01-01", "start_date": "2015-01-01", "as_of": "2026-08-25",
        "price_format": "unadjusted_close", "provider_complete": True,
        "row_count": len(rows), "rows": rows,
    }
    from build_historical_universe import canonical_sha256
    checkpoint["rows_sha256"] = canonical_sha256(rows)
    write_json_atomic(tmp_path / "prices" / "baostock" / "600001.json", checkpoint)
    result = build_selection_scope(
        dividends,
        ["2020-01-31", "2020-02-28", "2020-03-31", "2020-04-30", "2020-05-29"],
        tmp_path / "prices", tmp_path / "cache", "2026-08-25", "baostock",
    )
    assert result["selection_relevant_codes"] == ["600001"]
    assert result["status"] == "complete"


def test_selection_scope_records_missing_prices_without_using_performance(tmp_path):
    dividends = {"stocks": [{
        "code": "600001", "verification_required": True,
        "records": [{"year": year, "ex_date": f"{year + 1}-06-01", "dps": 1.0}
                    for year in range(2015, 2021)],
    }]}
    result = build_selection_scope(
        dividends, ["2020-05-29"], tmp_path, tmp_path, "2026-08-25",
    )
    assert result["status"] == "complete_with_exclusions"
    assert result["manual_data_gate_complete"] is True
    assert result["filtered_price_unverifiable_codes"] == ["600001"]
    assert result["selection_relevant_codes"] == []
    assert result["missing_observations"] == [{
        "code": "600001", "date": "2020-05-29", "reason": "missing_checkpoint",
    }]
