import json

import pytest

from build_historical_universe import (
    active_on,
    build_dividend_artifact,
    build_pipeline_status,
    build_price_artifact,
    build_status,
    canonical_sha256,
    load_import,
    max_consecutive_positive_dividend_years,
    normalize_dividend_row,
    price_record_is_valid,
    validate_record,
    write_json_atomic,
    write_manifest_if_complete,
)


def record(**overrides):
    value = {
        "code": "600001",
        "name": "测试股份",
        "list_date": "1998-01-22",
        "delist_date": "2009-12-29",
        "exchange": "SH",
        "source_snapshot_date": "2026-08-27",
        "price_coverage": {"status": "unverified", "source": "baostock"},
        "dividend_coverage": {"status": "unverified", "source": "baostock"},
    }
    value.update(overrides)
    return validate_record(value)


def test_active_on_uses_historical_listing_window():
    item = record()
    assert not active_on(item, "1998-01-21")
    assert active_on(item, "1998-01-22")
    assert active_on(item, "2009-12-29")
    assert not active_on(item, "2009-12-30")


def test_listed_stock_has_open_ended_window():
    assert active_on(record(delist_date=""), "2026-08-27")
    assert not active_on(record(list_date="", delist_date=""), "2026-08-27")


def test_invalid_or_incomplete_contract_is_rejected():
    with pytest.raises(ValueError, match="缺少字段"):
        validate_record({"code": "600001"})
    with pytest.raises(ValueError, match="退市日早于上市日"):
        record(delist_date="1990-01-01")
    with pytest.raises(ValueError, match="price_coverage.status"):
        record(price_coverage={"status": "大概完整", "source": "未知"})
    with pytest.raises(ValueError, match="完整覆盖"):
        record(price_coverage={"status": "complete", "source": "licensed",
                               "start": "2000-01-01", "end": "2009-12-29"})


def test_incomplete_source_fails_closed(tmp_path):
    status = build_status([record()], "2026-08-27")
    assert status["status"] == "incomplete"
    assert status["manifest_generation_allowed"] is False
    with pytest.raises(RuntimeError, match="拒绝生成"):
        write_manifest_if_complete(status, tmp_path / "manifest.json")
    assert not (tmp_path / "manifest.json").exists()


def test_valid_complete_import_can_generate_manifest(tmp_path):
    item = record(
        price_coverage={"status": "complete", "source": "licensed", "start": "1998-01-22",
                        "end": "2009-12-29"},
        dividend_coverage={"status": "complete", "source": "licensed", "start": "1998-01-22",
                           "end": "2009-12-29"},
    )
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"records": [item]}, ensure_ascii=False), encoding="utf-8")
    loaded = load_import(source)
    status = build_status(loaded, "2026-08-27")
    output = tmp_path / "manifest.json"
    write_manifest_if_complete(status, output)
    assert status["manifest_generation_allowed"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["records"][0]["code"] == "600001"


def test_duplicate_import_codes_are_rejected(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps([record(), record()]), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_import(source)


def test_snapshot_date_must_match_records():
    with pytest.raises(ValueError, match="快照日"):
        build_status([record()], "2026-08-26")


def test_dividend_normalization_and_consecutive_years():
    rows = []
    for year in (2012, 2013, 2014, 2016):
        rows.append(normalize_dividend_row({
            "dividPlanAnnounceDate": f"{year}-03-01",
            "dividRegistDate": f"{year}-06-01",
            "dividOperateDate": f"{year}-06-02",
            "dividPayDate": f"{year}-06-02",
            "dividCashPsBeforeTax": "0.5",
            "dividStocksPs": "0.1",
            "dividReserveToStockPs": "0.2",
        }, year))
    assert rows[0]["cash_per_share_before_tax"] == 0.5
    assert rows[0]["stock_dividend_per_share"] == 0.1
    assert rows[0]["reserve_to_stock_per_share"] == 0.2
    assert max_consecutive_positive_dividend_years(rows) == 3


def test_provider_complete_is_not_independent_verification():
    stock = {
        "code": "600001", "errors": [], "records": [],
        "provider_complete": True,
    }
    dividends = build_dividend_artifact({"600001": stock}, "2026-08-27", 2012, 1)
    price_rows = [{"date": "2015-01-05", "close": "10.0"}]
    prices = build_price_artifact({"600001": {
        "code": "600001", "provider_complete": True, "rows": price_rows,
            "row_count": 1, "rows_sha256": canonical_sha256(price_rows),
            "start_date": "2015-01-05", "end_date": "2015-01-05",
            "delist_date": "2015-01-06", "adjustflag": "3",
            "stored_fields": ["date", "close"], "trade_status_filtered": True,
        }}, "2026-08-27", 1)
    assert dividends["provider_complete"] is True
    assert prices["provider_complete"] is True
    assert prices["row_count"] == 1
    assert dividends["independently_verified"] is False
    assert prices["independently_verified"] is False


def test_pipeline_status_contains_only_artifact_summaries(tmp_path):
    master = {
        "provider_complete": True, "independently_verified": False,
        "record_count": 5549, "records": [{"code": "600001"}],
    }
    dividends = {
        "provider_complete": True, "independently_verified": False,
        "record_count": 10, "target_count": 2, "failed_stock_count": 0,
        "stocks": [{"code": "600001", "records": [{"report_year": 2012}]}],
    }
    prices = {
        "provider_complete": True, "independently_verified": False,
        "row_count": 100, "target_count": 1, "failed_stock_count": 0,
        "stocks": [{"code": "600001", "rows": [{"date": "2015-01-05"}]}],
    }
    write_json_atomic(tmp_path / "security_master.json", master)
    write_json_atomic(tmp_path / "delisted_dividends.json", dividends)
    write_json_atomic(tmp_path / "eligible_delisted_prices.json", prices)
    status = build_pipeline_status(tmp_path, "2026-08-27")
    assert status["provider_pipeline_complete"] is True
    assert status["manifest_generation_allowed"] is False
    assert "records" not in status["artifacts"]["security_master"]
    assert len(status["artifacts"]["security_master"]["file_sha256"]) == 64


def test_pipeline_status_uses_repo_relative_paths():
    from build_historical_universe import ROOT
    status = build_pipeline_status(ROOT / "data" / "historical", "2026-08-27")
    assert status["artifacts"]["security_master"]["path"] == (
        "data/historical/security_master.json"
    )


def test_price_resume_requires_rows_hash_and_listing_boundary():
    rows = [{"date": "2015-01-05", "close": "10.00"}]
    item = {
        "provider_complete": True, "row_count": 1, "rows": rows,
        "rows_sha256": canonical_sha256(rows), "start_date": "2015-01-05",
        "end_date": "2015-01-05", "delist_date": "2015-01-06", "adjustflag": "3",
        "stored_fields": ["date", "close"], "trade_status_filtered": True,
    }
    assert price_record_is_valid(item)
    item["trade_status_filtered"] = False
    assert not price_record_is_valid(item)
    item["trade_status_filtered"] = True
    item["rows_sha256"] = "0" * 64
    assert not price_record_is_valid(item)


def test_price_resume_accepts_explicit_empty_tradable_range():
    item = {
        "provider_complete": True,
        "row_count": 0,
        "rows": [],
        "rows_sha256": canonical_sha256([]),
        "start_date": "",
        "end_date": "",
        "delist_date": "2015-01-26",
        "adjustflag": "3",
        "stored_fields": ["date", "close"],
        "trade_status_filtered": True,
        "empty_tradable_range": True,
        "source_row_count": 16,
    }
    assert price_record_is_valid(item)
    item["trade_status_filtered"] = False
    assert not price_record_is_valid(item)
