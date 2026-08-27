import hashlib

from build_historical_universe import canonical_sha256, write_json_atomic
from collect_historical_prices import (
    build_price_artifact,
    collect_price_provider,
    decode_tonghuashun_prices,
    eligible_targets,
    max_consecutive_positive_years,
    price_checkpoint_is_complete,
    read_gzip_json,
    verified_decision_targets,
)


def _master():
    return {"records": [{
        "code": "600001", "name": "测试股份", "list_date": "2000-01-01",
        "delist_date": "", "status": "listed",
    }]}


def _price_checkpoint(provider, close=10.0):
    rows = [{"date": "2015-01-05", "close": close}]
    return {
        "schema_version": 1,
        "provider": provider,
        "code": "600001",
        "name": "测试股份",
        "list_date": "2000-01-01",
        "start_date": "2015-01-01",
        "as_of": "2026-08-25",
        "price_format": "unadjusted_close",
        "provider_complete": True,
        "error": "",
        "row_count": 1,
        "rows": rows,
        "rows_sha256": canonical_sha256(rows),
        "source_metadata": {},
    }


def test_consecutive_years_require_no_gap():
    records = [
        {"year": year, "dps": 1.0}
        for year in (2018, 2019, 2020, 2022, 2023)
    ]
    assert max_consecutive_positive_years(records) == 3


def test_targets_use_dividend_rule_and_exclude_frozen_codes():
    dividends = {
        "primary_provider_complete": True,
        "eligible_scope_independently_verified": True,
        "stocks": [{
            "code": "600001",
            "verification_required": True,
            "primary_provider_complete": True,
            "verification_provider_complete": True,
            "independently_verified": True,
            "records": [
            {"year": year, "dps": 1.0} for year in (2020, 2021, 2022)
        ]}],
    }
    assert eligible_targets(dividends, _master(), set())[0]["code"] == "600001"
    assert eligible_targets(dividends, _master(), {"600001"}) == []


def test_targets_ignore_filtered_non_candidates_without_baostock_verification():
    dividends = {
        "primary_provider_complete": True,
        "eligible_scope_independently_verified": True,
        "stocks": [{
            "code": "600001",
            "verification_required": False,
            "verification_not_required": True,
            "primary_provider_complete": True,
            "verification_provider_complete": False,
            "independently_verified": False,
            "records": [],
        }],
    }
    assert eligible_targets(dividends, _master(), set()) == []


def test_targets_reject_incomplete_candidate_verification_scope():
    dividends = {
        "primary_provider_complete": True,
        "eligible_scope_independently_verified": False,
        "stocks": [],
    }
    try:
        eligible_targets(dividends, _master(), set())
    except RuntimeError as exc:
        assert "潜在候选" in str(exc)
    else:
        raise AssertionError("候选范围未全部核验时必须拒绝价格采集")


def test_primary_only_stage_allows_provisional_baostock_price_targets():
    dividends = {
        "primary_provider_complete": True,
        "eligible_scope_independently_verified": False,
        "stocks": [{
            "code": "600001",
            "verification_required": True,
            "primary_provider_complete": True,
            "verification_provider_complete": False,
            "independently_verified": False,
            "records": [{"year": year, "dps": 1.0} for year in (2020, 2021, 2022)],
        }],
    }
    targets = eligible_targets(dividends, _master(), set(), allow_primary_only=True)
    assert [row["code"] for row in targets] == ["600001"]


def test_verified_decision_targets_only_use_dividend_gate_allowlist():
    dividends = {
        "manual_data_gate_complete": True,
        "data_quality_eligible_codes": ["600001"],
    }
    targets = verified_decision_targets(dividends, _master())
    assert [row["code"] for row in targets] == ["600001"]


def test_price_checkpoint_recomputes_hash_and_boundaries():
    payload = _price_checkpoint("baostock")
    assert price_checkpoint_is_complete(
        payload, "baostock", "600001", "2015-01-01", "2026-08-25"
    )
    payload["rows_sha256"] = "0" * 64
    assert not price_checkpoint_is_complete(
        payload, "baostock", "600001", "2015-01-01", "2026-08-25"
    )


def test_price_checkpoint_rejects_duplicate_dates_and_invalid_close():
    payload = _price_checkpoint("baostock")
    payload["rows"].append(dict(payload["rows"][0]))
    payload["row_count"] = 2
    payload["rows_sha256"] = canonical_sha256(payload["rows"])
    assert not price_checkpoint_is_complete(
        payload, "baostock", "600001", "2015-01-01", "2026-08-25"
    )
    for value in (float("nan"), 0, -1):
        invalid = _price_checkpoint("baostock", close=value)
        assert not price_checkpoint_is_complete(
            invalid, "baostock", "600001", "2015-01-01", "2026-08-25"
        )


def test_decode_tonghuashun_unadjusted_prices():
    payload = {
        "total": "2",
        "sortYear": [[2025, 2]],
        "priceFactor": 100,
        "dates": "0102,0103",
        # 每日四项依次为最低价、开盘差、高价差、收盘差。
        "price": "1000,100,300,200,1200,10,80,50",
    }
    assert decode_tonghuashun_prices(
        payload, "2025-01-01", "2025-12-31"
    ) == [
        {"date": "2025-01-02", "close": 12.0},
        {"date": "2025-01-03", "close": 12.5},
    ]


def test_decode_tonghuashun_rejects_inconsistent_counts():
    payload = {
        "total": "2",
        "sortYear": [[2025, 1]],
        "priceFactor": 100,
        "dates": "0102,0103",
        "price": "1000,100,300,200,1200,10,80,50",
    }
    try:
        decode_tonghuashun_prices(payload, "2025-01-01", "2025-12-31")
    except RuntimeError as exc:
        assert "计数" in str(exc)
    else:
        raise AssertionError("日期计数不一致时必须失败")


def test_build_price_artifact_requires_exact_independent_match(tmp_path):
    target = _master()["records"]
    write_json_atomic(
        tmp_path / "tonghuashun" / "600001.json",
        _price_checkpoint("tonghuashun"),
    )
    write_json_atomic(
        tmp_path / "eastmoney" / "600001.json", _price_checkpoint("eastmoney")
    )
    archive = tmp_path / "prices.json.gz"
    manifest = build_price_artifact(target, tmp_path, archive, "2026-08-25")
    assert manifest["provider_complete"] is True
    assert manifest["independently_verified"] is True
    assert manifest["archive"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert read_gzip_json(archive)["stocks"][0]["rows"][0]["close"] == 10.0
    assert manifest["manual_data_gate_complete"] is True
    assert manifest["data_quality_eligible_codes"] == ["600001"]

    changed = _price_checkpoint("eastmoney", close=10.1)
    write_json_atomic(tmp_path / "eastmoney" / "600001.json", changed)
    mismatch = build_price_artifact(target, tmp_path, archive, "2026-08-25")
    assert mismatch["provider_complete"] is True
    assert mismatch["independently_verified"] is False
    assert mismatch["mismatched_stock_count"] == 1
    assert mismatch["manual_data_gate_status"] == "complete_with_exclusions"
    assert mismatch["data_quality_eligible_codes"] == []
    assert mismatch["filtered_unverifiable_codes"] == ["600001"]


def test_build_price_artifact_uses_any_exact_independent_source(tmp_path):
    target = _master()["records"]
    archive = tmp_path / "prices.json.gz"
    write_json_atomic(
        tmp_path / "tonghuashun" / "600001.json",
        _price_checkpoint("tonghuashun"),
    )
    write_json_atomic(
        tmp_path / "eastmoney" / "600001.json",
        _price_checkpoint("eastmoney", close=10.1),
    )
    write_json_atomic(
        tmp_path / "sina" / "600001.json", _price_checkpoint("sina")
    )
    manifest = build_price_artifact(target, tmp_path, archive, "2026-08-25")
    assert manifest["data_quality_eligible_codes"] == ["600001"]
    assert manifest["stocks"][0]["verification"]["provider"] == "sina"


def test_build_price_artifact_applies_three_source_majority(tmp_path):
    target = _master()["records"]
    archive = tmp_path / "prices.json.gz"
    write_json_atomic(
        tmp_path / "tonghuashun" / "600001.json",
        _price_checkpoint("tonghuashun", close=10.0),
    )
    write_json_atomic(
        tmp_path / "sina" / "600001.json",
        _price_checkpoint("sina", close=10.1),
    )
    values = {"2015-01-05": 10.1}
    write_json_atomic(tmp_path / "tencent" / "600001.json", {
        "schema_version": 1,
        "provider": "tencent_raw_daily",
        "source": "tencent/appstock/app/kline/kline day（无复权参数）",
        "code": "600001",
        "requested_dates": ["2015-01-05"],
        "provider_complete": True,
        "error": "",
        "values": values,
        "values_sha256": canonical_sha256(values),
        "response_hashes": {},
    })
    manifest = build_price_artifact(target, tmp_path, archive, "2026-08-25")
    assert manifest["data_quality_eligible_codes"] == ["600001"]
    assert manifest["changed_date_count"] == 1
    stock = read_gzip_json(archive)["stocks"][0]
    assert stock["rows"] == [{"date": "2015-01-05", "close": 10.1}]
    assert stock["arbitration"]["decisions"][0]["majority_sources"] == [
        "sina", "tencent"
    ]


def test_price_collection_stops_after_consecutive_provider_failures(tmp_path):
    targets = [
        {"code": f"60000{index}", "name": "测试", "list_date": "2000-01-01"}
        for index in range(1, 7)
    ]

    def fail(_code, _start, _end):
        raise RuntimeError("HTTP 456")

    try:
        collect_price_provider(
            targets, tmp_path, "sina", "2026-08-25", fail,
            retry=0, max_consecutive_failures=3,
        )
    except RuntimeError as exc:
        assert "连续 3 只" in str(exc)
    else:
        raise AssertionError("连续供应商失败时必须停止继续请求")
    assert len(list((tmp_path / "sina").glob("*.json"))) == 3
