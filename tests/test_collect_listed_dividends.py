import json

from collect_listed_dividends import (
    BaoStockDividendClient,
    fetch_eastmoney_dividends,
    build_verified_artifact,
    checkpoint_is_complete,
    collect_provider,
    eligible_primary_scope,
    parse_tonghuashun_dividends,
    parse_sina_dividends,
    normalize_baostock_dividend,
)
from build_historical_universe import canonical_sha256, write_json_atomic


def _stock(code="600001"):
    return {
        "code": code,
        "name": "测试股份",
        "list_date": "2000-01-01",
        "delist_date": "",
        "status": "listed",
    }


def _checkpoint(provider, code="600001", records=None, as_of="2026-08-25"):
    records = [] if records is None else records
    return {
        "schema_version": 1,
        "provider": provider,
        "code": code,
        "name": "测试股份",
        "list_date": "2000-01-01",
        "as_of": as_of,
        "provider_complete": True,
        "empty_response": not records,
        "error": "",
        "records": records,
        "records_sha256": canonical_sha256(records),
        "source_metadata": {},
    }


def _candidate_records():
    return [
        {"year": year, "ex_date": f"{year + 1}-06-01", "dps": 0.35,
         "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        for year in (2022, 2023, 2024)
    ]


def test_parse_sina_only_keeps_implemented_events():
    html = """
    <table id="sharebonus_1"><thead><tr><th>分红方案(每10股)</th>
    <th>除权除息日</th></tr></thead><tbody>
    <tr><td>2025-04-01</td><td>1</td><td>2</td><td>3.5</td><td>实施</td>
    <td>2025-06-01</td><td>2025-05-30</td><td>--</td><td>查看</td></tr>
    <tr><td>2026-04-01</td><td>0</td><td>0</td><td>4</td><td>预案</td>
    <td>--</td><td>--</td><td>--</td><td>查看</td></tr>
    </tbody></table>
    """
    rows = parse_sina_dividends(html, "2026-08-25")
    assert rows == [{
        "plan_announce_date": "2025-04-01",
        "registration_date": "2025-05-30",
        "ex_date": "2025-06-01",
        "dps": 0.35,
        "bonus_ratio": 1.0,
        "transfer_ratio": 2.0,
    }]


def test_parse_sina_requires_real_table():
    try:
        parse_sina_dividends("分红方案(每10股) 除权除息日", "2026-08-25")
    except RuntimeError as exc:
        assert "sharebonus_1" in str(exc)
    else:
        raise AssertionError("缺少真实表格时必须失败")


def test_parse_tonghuashun_only_keeps_implemented_events_before_cutoff():
    html = """
    <div>A股除权除息日 分红方案说明</div>
    <table id="bonus_table"><tbody>
    <tr><td>2025年度</td><td>董事会预案</td><td>股东大会通过</td>
    <td>2026-04-01</td><td>10送1股转增2股派3.5元(含税)</td>
    <td>2026-05-30</td><td>2026-06-01</td><td>查看</td><td>实施方案</td></tr>
    <tr><td>2026年度</td><td>董事会预案</td><td>股东大会通过</td>
    <td>2026-09-01</td><td>10派4元</td>
    <td>2026-09-29</td><td>2026-09-30</td><td>查看</td><td>实施方案</td></tr>
    <tr><td>2026年度</td><td>董事会预案</td><td>--</td>
    <td>2026-04-01</td><td>10派4元</td>
    <td>--</td><td>--</td><td>查看</td><td>预案</td></tr>
    </tbody></table>
    """
    rows = parse_tonghuashun_dividends(html, "2026-08-25")
    assert rows == [{
        "plan_announce_date": "2026-04-01",
        "registration_date": "2026-05-30",
        "ex_date": "2026-06-01",
        "dps": 0.35,
        "bonus_ratio": 1.0,
        "transfer_ratio": 2.0,
    }]


def test_parse_tonghuashun_requires_real_table():
    try:
        parse_tonghuashun_dividends("A股除权除息日 分红方案说明", "2026-08-25")
    except RuntimeError as exc:
        assert "bonus_table" in str(exc)
    else:
        raise AssertionError("缺少真实表格时必须失败")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _Response(self.payload)


def test_eastmoney_explicit_no_data_is_not_network_failure():
    result = fetch_eastmoney_dividends(
        "001239", "2026-08-25",
        _Client({"success": False, "code": 9201, "message": "返回数据为空"}),
    )
    assert result["provider_complete"] is True
    assert result["empty_response"] is True
    assert result["records"] == []


def test_checkpoint_hash_and_scope_are_verified():
    payload = _checkpoint("eastmoney")
    assert checkpoint_is_complete(payload, "eastmoney", "600001", "2026-08-25")
    payload["records_sha256"] = "0" * 64
    assert not checkpoint_is_complete(payload, "eastmoney", "600001", "2026-08-25")
    assert not checkpoint_is_complete(
        _checkpoint("eastmoney"), "eastmoney", "600001", "2026-08-24"
    )


def test_collect_provider_resumes_and_retries_failed_checkpoint(tmp_path):
    calls = []

    def fetcher(code, as_of):
        calls.append((code, as_of))
        if code == "600002":
            raise RuntimeError("临时失败")
        return {"provider_complete": True, "empty_response": True, "records": []}

    existing_path = tmp_path / "eastmoney" / "600001.json"
    write_json_atomic(existing_path, _checkpoint("eastmoney"))
    result = collect_provider(
        [_stock("600001"), _stock("600002"), _stock("600003")],
        tmp_path,
        "eastmoney",
        "2026-08-25",
        fetcher,
        retry=1,
    )
    assert result == {"attempted": 2, "skipped": 1, "succeeded": 1, "failed": 1}
    assert calls.count(("600002", "2026-08-25")) == 2
    failed = json.loads((tmp_path / "eastmoney" / "600002.json").read_text(encoding="utf-8"))
    assert failed["provider_complete"] is False
    assert failed["empty_response"] is False


def test_collect_provider_stops_after_consecutive_failures(tmp_path):
    calls = []

    def fetcher(code, as_of):
        calls.append((code, as_of))
        raise RuntimeError("上游服务不可用")

    try:
        collect_provider(
            [_stock("600001"), _stock("600002"), _stock("600003")],
            tmp_path,
            "tonghuashun",
            "2026-08-25",
            fetcher,
            retry=0,
            max_consecutive_failures=2,
        )
    except RuntimeError as exc:
        assert "连续 2 只采集失败" in str(exc)
    else:
        raise AssertionError("连续失败达到阈值时必须停止")
    assert calls == [
        ("600001", "2026-08-25"),
        ("600002", "2026-08-25"),
    ]
    assert not (tmp_path / "tonghuashun" / "600003.json").exists()


def test_build_artifact_requires_two_sources_to_match(tmp_path):
    primary = _candidate_records()
    verifier = [
        {"ex_date": row["ex_date"], "dps": 0.350004,
         "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        for row in primary
    ]
    east = _checkpoint("eastmoney", records=primary)
    sina = _checkpoint("baostock", records=verifier)
    sina["source_metadata"] = {"raw_response_sha256": "a" * 64}
    write_json_atomic(tmp_path / "eastmoney" / "600001.json", east)
    write_json_atomic(tmp_path / "baostock" / "600001.json", sina)
    payload = build_verified_artifact([_stock()], tmp_path, "2026-08-25")
    assert payload["provider_complete"] is True
    assert payload["independently_verified"] is True
    assert payload["verified_stock_count"] == 1
    assert payload["stocks"][0]["records"] == primary

    sina["records"][0]["dps"] = 0.34
    sina["records_sha256"] = canonical_sha256(sina["records"])
    write_json_atomic(tmp_path / "baostock" / "600001.json", sina)
    mismatch = build_verified_artifact([_stock()], tmp_path, "2026-08-25")
    assert mismatch["provider_complete"] is True
    assert mismatch["independently_verified"] is False
    assert mismatch["mismatched_stock_count"] == 1
    assert mismatch["stocks"][0]["verification"]["missing_from_verifier"]


def test_build_artifact_uses_sina_when_baostock_is_unavailable(tmp_path):
    primary = _candidate_records()
    verifier = [{
        "ex_date": row["ex_date"], "dps": row["dps"],
        "bonus_ratio": row["bonus_ratio"],
        "transfer_ratio": row["transfer_ratio"],
    } for row in primary]
    write_json_atomic(
        tmp_path / "eastmoney" / "600001.json",
        _checkpoint("eastmoney", records=primary),
    )
    write_json_atomic(
        tmp_path / "sina" / "600001.json",
        _checkpoint("sina", records=verifier),
    )
    payload = build_verified_artifact(
        [_stock()], tmp_path, "2026-08-25", {"600001"}
    )
    assert payload["data_quality_eligible_codes"] == ["600001"]
    assert payload["stocks"][0]["verification"]["provider_key"] == "sina"


def test_manual_data_gate_filters_resolved_mismatch_without_blocking_scope(tmp_path):
    primary = _candidate_records()
    verifier = [
        {"ex_date": row["ex_date"], "dps": row["dps"],
         "bonus_ratio": row["bonus_ratio"], "transfer_ratio": row["transfer_ratio"]}
        for row in primary
    ]
    verifier[0]["dps"] = 0.01
    write_json_atomic(
        tmp_path / "eastmoney" / "600001.json",
        _checkpoint("eastmoney", records=primary),
    )
    write_json_atomic(
        tmp_path / "baostock" / "600001.json",
        _checkpoint("baostock", records=verifier),
    )
    payload = build_verified_artifact(
        [_stock()], tmp_path, "2026-08-25", {"600001"}
    )
    assert payload["manual_data_gate_complete"] is True
    assert payload["manual_data_gate_status"] == "complete_with_exclusions"
    assert payload["data_quality_eligible_codes"] == []
    assert payload["filtered_unverifiable_count"] == 1
    assert payload["eligible_scope_independently_verified"] is False


def test_build_artifact_rejects_duplicate_event_multiplicity(tmp_path):
    records = _candidate_records()
    east = _checkpoint("eastmoney", records=records + [dict(records[-1])])
    sina = _checkpoint("baostock", records=[{
        "ex_date": row["ex_date"], "dps": row["dps"],
        "bonus_ratio": row["bonus_ratio"], "transfer_ratio": row["transfer_ratio"],
    } for row in records])
    write_json_atomic(tmp_path / "eastmoney" / "600001.json", east)
    write_json_atomic(tmp_path / "baostock" / "600001.json", sina)
    payload = build_verified_artifact([_stock()], tmp_path, "2026-08-25")
    assert payload["independently_verified"] is False
    verification = payload["stocks"][0]["verification"]
    assert verification["primary_duplicate_event_count"] == 1
    assert verification["missing_from_verifier"] == [{
        "ex_date": "2025-06-01", "dps": 0.35,
        "bonus_ratio": 0.0, "transfer_ratio": 0.0,
    }]


def test_eligible_scope_requires_complete_primary_and_only_uses_candidate_event_years(tmp_path):
    candidate = _stock("600001")
    filtered = _stock("600002")
    write_json_atomic(
        tmp_path / "eastmoney" / "600001.json",
        _checkpoint("eastmoney", "600001", _candidate_records()),
    )
    write_json_atomic(
        tmp_path / "eastmoney" / "600002.json",
        _checkpoint("eastmoney", "600002", [
            {"year": year, "ex_date": f"{year + 1}-07-01", "dps": 1.0,
             "bonus_ratio": 0.0, "transfer_ratio": 0.0}
            for year in (2020, 2022, 2023)
        ]),
    )
    eligible, query_years = eligible_primary_scope(
        [candidate, filtered], tmp_path, "2026-08-25"
    )
    assert [row["code"] for row in eligible] == ["600001"]
    assert query_years == {"600001": [2022, 2023, 2024, 2025, 2026]}

    (tmp_path / "eastmoney" / "600002.json").unlink()
    try:
        eligible_primary_scope([candidate, filtered], tmp_path, "2026-08-25")
    except RuntimeError as exc:
        assert "尚未全量完成" in str(exc)
    else:
        raise AssertionError("东财主源不完整时必须失败关闭")


def test_build_artifact_marks_non_candidate_without_verifier(tmp_path):
    candidate = _stock("600001")
    filtered = _stock("600002")
    primary = _candidate_records()
    verifier = [{key: row[key] for key in ("ex_date", "dps", "bonus_ratio", "transfer_ratio")}
                for row in primary]
    write_json_atomic(tmp_path / "eastmoney" / "600001.json", _checkpoint(
        "eastmoney", "600001", primary
    ))
    write_json_atomic(tmp_path / "baostock" / "600001.json", _checkpoint(
        "baostock", "600001", verifier
    ))
    write_json_atomic(tmp_path / "eastmoney" / "600002.json", _checkpoint(
        "eastmoney", "600002", primary[:2]
    ))
    payload = build_verified_artifact([candidate, filtered], tmp_path, "2026-08-25")
    assert payload["primary_provider_complete"] is True
    assert payload["eligible_scope_count"] == 1
    assert payload["eligible_scope_independently_verified"] is True
    assert payload["filtered_non_candidate_count"] == 1
    non_candidate = payload["stocks"][1]
    assert non_candidate["verification_not_required"] is True
    assert non_candidate["verification_required"] is False


def test_normalize_baostock_ignores_report_year_and_converts_per_share_ratios():
    row = {
        "dividOperateDate": "2025-06-12", "dividPlanAnnounceDate": "2025-03-15",
        "dividRegistDate": "2025-06-11", "dividPayDate": "2025-06-12",
        "dividCashPsBeforeTax": "0.362", "dividStocksPs": "0.6",
        "dividReserveToStockPs": "0.2", "report_year": "错误值",
    }
    assert normalize_baostock_dividend(row, "2026-08-25") == {
        "plan_announce_date": "2025-03-15", "registration_date": "2025-06-11",
        "ex_date": "2025-06-12", "pay_date": "2025-06-12", "dps": 0.362,
        "bonus_ratio": 6.0, "transfer_ratio": 2.0,
    }


class _BaoResult:
    error_code = "0"
    error_msg = "success"
    fields = ["dividOperateDate", "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]

    def __init__(self, rows):
        self.rows = list(rows)
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


def test_baostock_fetch_queries_every_year_and_distinguishes_empty(monkeypatch):
    calls = []
    client = object.__new__(BaoStockDividendClient)
    client.interval = 0
    client.sleeper = lambda value: None
    client.bs = type("BS", (), {
        "query_dividend_data": staticmethod(
            lambda code, year, yearType: calls.append((code, year, yearType)) or _BaoResult([])
        )
    })()
    result = client.fetch("000001", "2014-08-25", "2013-01-01")
    assert calls == [("sz.000001", "2013", "report"), ("sz.000001", "2014", "report")]
    assert result["provider_complete"] is True
    assert result["empty_response"] is True
    assert result["records"] == []

    explicit_empty = client.fetch("000001", "2014-08-25", "2013-01-01", [])
    assert calls == [("sz.000001", "2013", "report"), ("sz.000001", "2014", "report")]
    assert explicit_empty["queried_years"] == []
