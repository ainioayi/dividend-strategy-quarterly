from build_historical_universe import canonical_sha256, write_json_atomic
from verify_delisted_prices import (
    arbitrate_value,
    build_artifact,
    build_verified_prices,
    checkpoint_is_complete,
    collect,
    collect_tencent_arbitration,
    validate_rows,
)


def _stock(code="600001", rows=None):
    rows = [{"date": "2015-01-05", "close": "10.0000"}] if rows is None else rows
    return {
        "code": code, "name": "测试股份", "start_date": "2015-01-05",
        "end_date": "2015-01-05", "row_count": len(rows), "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }


def _checkpoint(code="600001", rows=None):
    rows = [{"date": "2015-01-05", "close": 10.0}] if rows is None else rows
    return {
        "schema_version": 1, "provider": "sina", "code": code,
        "start_date": "2015-01-05", "end_date": "2015-01-05",
        "price_format": "unadjusted_close", "provider_complete": True,
        "raw_row_count": 1, "row_count": len(rows), "rows": rows,
        "rows_sha256": canonical_sha256(rows),
    }


def test_rows_fail_closed_on_duplicate_or_nonpositive_close():
    assert validate_rows([], allow_empty=True)
    row = {"date": "2015-01-05", "close": 10.0}
    assert not validate_rows([row, row], allow_empty=True)
    assert not validate_rows([dict(row, close=0)], allow_empty=True)


def test_empty_target_range_requires_nonempty_raw_response():
    checkpoint = _checkpoint("000562", [])
    checkpoint["raw_row_count"] = 4777
    assert checkpoint_is_complete(checkpoint, "000562", "2015-01-05", "2015-01-05")
    checkpoint["raw_row_count"] = 0
    assert not checkpoint_is_complete(checkpoint, "000562", "2015-01-05", "2015-01-05")


def test_collect_writes_atomic_resumable_checkpoint(tmp_path):
    calls = []
    stock = _stock()

    def fetcher(code, start, end):
        calls.append(code)
        return {"provider_complete": True, "raw_row_count": 2, "rows": [
            {"date": "2015-01-05", "close": 10.0}
        ]}

    assert collect([stock], tmp_path, fetcher)["succeeded"] == 1
    assert collect([stock], tmp_path, fetcher)["skipped"] == 1
    assert calls == ["600001"]


def test_collect_clears_transient_error_after_successful_retry(tmp_path):
    attempts = 0

    def flaky_fetcher(code, start, end):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("临时限流")
        return {"provider_complete": True, "raw_row_count": 1, "rows": [
            {"date": "2015-01-05", "close": 10.0}
        ]}

    result = collect([_stock()], tmp_path, flaky_fetcher, retry=1)
    assert result["succeeded"] == 1
    assert result["failed"] == 0


def test_artifact_true_only_for_all_exact_matches(tmp_path):
    source = {
        "source_snapshot_date": "2026-08-27", "target_count": 1,
        "provider_complete": True, "stocks": [_stock()],
    }
    write_json_atomic(tmp_path / "sina" / "600001.json", _checkpoint())
    artifact = build_artifact(source, tmp_path)
    assert artifact["independently_verified"] is True

    changed = _checkpoint(rows=[{"date": "2015-01-05", "close": 10.01}])
    write_json_atomic(tmp_path / "sina" / "600001.json", changed)
    mismatch = build_artifact(source, tmp_path)
    assert mismatch["independently_verified"] is False
    assert mismatch["mismatched_stock_count"] == 1
    assert mismatch["stocks"][0]["different_close_dates"] == ["2015-01-05"]


def test_three_source_majority_can_resolve_price_or_no_trade():
    price = arbitrate_value(10.0, 10.01, 10.0)
    assert price["resolved"] is True
    assert price["accepted_value"] == 10.0
    assert price["majority_sources"] == ["baostock", "tencent"]
    no_trade = arbitrate_value(None, 0.16, None)
    assert no_trade["resolved"] is True
    assert no_trade["accepted_value"] is None
    assert arbitrate_value(10.0, 10.01, 10.02)["resolved"] is False


def test_artifact_accepts_only_complete_tencent_majority(tmp_path):
    source = {
        "source_snapshot_date": "2026-08-27", "target_count": 1,
        "provider_complete": True, "stocks": [_stock()],
    }
    write_json_atomic(
        tmp_path / "sina" / "600001.json",
        _checkpoint(rows=[{"date": "2015-01-05", "close": 10.01}]),
    )

    def fetcher(code, dates):
        return {"provider_complete": True, "values": {"2015-01-05": 10.0}}

    result = collect_tencent_arbitration(source["stocks"], tmp_path, fetcher)
    assert result["succeeded"] == 1
    artifact = build_artifact(source, tmp_path)
    assert artifact["independently_verified"] is True
    decision = artifact["stocks"][0]["arbitration"][0]
    assert decision["baostock_close"] == 10.0
    assert decision["sina_close"] == 10.01
    assert decision["tencent_close"] == 10.0


def test_verified_prices_only_apply_resolved_arbitration():
    source = {
        "source_snapshot_date": "2026-08-27", "scope": "测试", "target_count": 1,
        "stocks": [_stock(rows=[
            {"date": "2015-01-05", "close": "10.0000"},
            {"date": "2015-01-06", "close": "11.0000"},
        ])],
    }
    verification = {
        "provider_complete": True, "independently_verified": True,
        "stocks": [{
            "code": "600001", "independently_verified": True,
            "arbitration": [{
                "date": "2015-01-05", "baostock_close": 10.0,
                "sina_close": 10.1, "tencent_close": 10.1,
                "accepted_value": 10.1, "majority_sources": ["sina", "tencent"],
                "resolved": True,
            }],
        }],
    }
    result = build_verified_prices(
        source, verification, source_file_sha256="a" * 64,
        verification_file_sha256="b" * 64,
    )
    assert result["independently_verified"] is True
    assert result["changed_date_count"] == 1
    assert result["arbitration_date_count"] == 1
    assert result["stocks"][0]["rows"] == [
        {"date": "2015-01-05", "close": "10.1000"},
        {"date": "2015-01-06", "close": "11.0000"},
    ]
    assert result["stocks"][0]["row_count"] == 2
    assert result["source_file_sha256"] == "a" * 64


def test_verified_prices_can_insert_and_delete_only_arbitrated_dates():
    source = {
        "source_snapshot_date": "2026-08-27", "target_count": 1,
        "stocks": [_stock(rows=[{"date": "2015-01-05", "close": "10.0000"}])],
    }
    verification = {
        "provider_complete": True, "independently_verified": True,
        "stocks": [{
            "code": "600001", "independently_verified": True,
            "arbitration": [
                {"date": "2015-01-05", "baostock_close": 10.0, "accepted_value": None,
                 "majority_sources": ["sina", "tencent"], "resolved": True},
                {"date": "2015-01-06", "baostock_close": None, "accepted_value": 9.9,
                 "majority_sources": ["sina", "tencent"], "resolved": True},
            ],
        }],
    }
    result = build_verified_prices(
        source, verification, source_file_sha256="a", verification_file_sha256="b"
    )
    assert result["stocks"][0]["rows"] == [
        {"date": "2015-01-06", "close": "9.9000"}
    ]
    assert {change["action"] for change in result["changes"]} == {"delete", "insert"}
