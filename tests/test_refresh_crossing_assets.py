"""真实交易标的采集的身份、分页、公司事件及失败保护。"""
from datetime import date, timedelta
import gzip
import json

import pytest

import refresh_crossing_assets as assets


def tencent_payload(spec, rows):
    return {"code": 0, "data": {spec["symbol"]: {"day": [[row["date"], row["open"], row["close"]] for row in rows],
            "qt": {spec["symbol"]: ["1", "沪深300ETF", spec["symbol"][2:]]}}}}


def test_tencent_full_history_paginates_before_earliest_not_last_640(monkeypatch):
    spec = assets.SPECS["160706"]
    start = date.fromisoformat(spec["listing_date"])
    rows = [{"date": (start + timedelta(days=i)).isoformat(), "open": 1, "close": 1} for i in range(650)]
    calls = []
    def request(name, url, *, params):
        calls.append(params["param"])
        return tencent_payload(spec, rows[10:] if len(calls) == 1 else rows[:10])
    monkeypatch.setattr(assets.time, "sleep", lambda _: None)
    actual, identity = assets.fetch_tencent(request, "160706", spec, rows[-1]["date"])
    assert actual == rows
    assert len(identity["pages"]) == 2
    assert calls[1].split(",")[3] == rows[9]["date"]


def test_tencent_shortened_or_wrong_market_series_is_rejected():
    spec = assets.SPECS["160706"]
    rows = [{"date": "2006-01-01", "open": 1, "close": 1}]
    with pytest.raises(ValueError, match="上市首日"):
        assets.fetch_tencent(lambda *a, **k: tencent_payload(spec, rows), "160706", spec, "2026-11-13")
    payload = tencent_payload(spec, rows)
    payload["data"][spec["symbol"]]["qt"][spec["symbol"]][2] = "510300"
    with pytest.raises(ValueError, match="其他证券"):
        assets.parse_tencent(payload, spec, "2026-11-13")


def test_tencent_stuck_page_is_not_an_endless_loop(monkeypatch):
    spec = assets.SPECS["160706"]
    start = date.fromisoformat(spec["listing_date"]) + timedelta(days=10)
    rows = [{"date": (start + timedelta(days=i)).isoformat(), "open": 1, "close": 1} for i in range(640)]
    monkeypatch.setattr(assets.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="前移"):
        assets.fetch_tencent(lambda *a, **k: tencent_payload(spec, rows), "160706", spec, rows[-1]["date"])


@pytest.mark.parametrize("value", ["NaN", "Infinity", -1, 0, True])
def test_nonfinite_or_nonpositive_prices_are_rejected(value):
    payload = [{"day": "2012-05-28", "close": value, "open": 2, "volume": 100}]
    with pytest.raises(ValueError):
        assets.parse_sina(payload, "2012-05-28", "2026-11-13")


def test_sina_must_cover_listing_and_never_include_future():
    rows = [{"day": "2012-05-28", "open": 2.551, "close": 2.604, "volume": 100},
            {"day": "2012-05-29", "open": 2.602, "close": 2.644, "volume": 100}]
    assert len(assets.parse_sina(rows, "2012-05-28", "2012-05-28")) == 1
    with pytest.raises(ValueError, match="上市首日"):
        assets.parse_sina(rows[1:], "2012-05-28", "2026-11-13")


def test_price_reconciliation_records_even_one_tick_conflict_and_missing_dates():
    sina = [{"date": "2024-01-01", "open": 1, "close": 1}, {"date": "2024-01-02", "open": 1, "close": 1}]
    tencent = [{"date": "2024-01-01", "open": 1, "close": 1.001}]
    result = assets.reconcile(sina, tencent)
    assert result["compared_rows"] == 1
    assert result["conflicts"][0]["fields"] == ["close"]
    assert result["only_sina_dates"] == ["2024-01-02"]


def test_split_parser_keeps_share_ratio_and_recognizes_future_cutoff():
    html = '<table class="comm fhxq"><tr><td>2012年</td><td>2012-05-11</td><td>份额折算</td><td>1:0.3709</td></tr></table>'
    assert assets.parse_splits(html, "2026-11-13") == [{"date": "2012-05-11", "ratio": .3709, "description": "份额折算"}]
    assert assets.parse_splits(html, "2012-05-10") == []
    assert assets.parse_splits('<table class="fhxq"><tr><td>暂无拆分信息!</td></tr></table>', "2026-11-13") == []
    with pytest.raises(ValueError, match="无法解析"):
        assets.parse_splits(html.replace("1:0.3709", "不明比例"), "2026-11-13")
    with pytest.raises(ValueError, match="未识别"):
        assets.parse_splits("服务器错误", "2026-11-13")


def test_dividend_table_cash_is_per_unit_and_keeps_three_dates():
    html = '<table class="cfxq"><tr><td>2024年</td><td>2024-01-17</td><td>2024-01-18</td><td>每10份派现金0.6900元</td><td>2024-01-23</td></tr></table>'
    records = assets.parse_dividends(html, "2026-11-13")
    assert records == [{"record_date": "2024-01-17", "ex_date": "2024-01-18", "pay_date": "2024-01-23", "cash_per_unit": .069}]
    verified, unresolved = assets.prepare_dividends("510300", records, lambda *a, **k: pytest.fail("ETF无需读取LOF公告"), "2026-11-13")
    assert verified[0]["verified"] and not unresolved


def test_lof_dates_only_corrected_with_exact_reviewed_notice(monkeypatch):
    record = {"record_date": "2026-01-16", "ex_date": "2026-01-16", "pay_date": "2026-01-20", "cash_per_unit": .0355}
    known = list(assets.LOF_NOTICES[record["record_date"]])
    known[-1] = assets.digest(b"%PDF-tested")
    monkeypatch.setitem(assets.LOF_NOTICES, record["record_date"], tuple(known))
    values, unresolved = assets.prepare_dividends("160706", [record], lambda *a, **k: b"%PDF-tested", "2026-11-13")
    assert values[0]["ex_date"] == "2026-01-19"
    assert values[0]["pay_date"] == "2026-01-21"
    assert values[0]["original_fund_table"] == record and not unresolved
    with pytest.raises(ValueError, match="重新审核"):
        assets.prepare_dividends("160706", [record], lambda *a, **k: b"different PDF", "2026-11-13")


def test_unknown_lof_dividend_is_not_automatically_shifted():
    record = {"record_date": "2007-08-22", "ex_date": "2007-08-22", "pay_date": "2007-08-24", "cash_per_unit": .429}
    values, unresolved = assets.prepare_dividends("160706", [record], lambda *a, **k: pytest.fail("未知公告不得猜测"), "2026-11-13")
    assert not values[0]["verified"] and values[0]["ex_date"] == "2007-08-22"
    assert unresolved[0]["start"] == "2007-08-22"
    assert unresolved[0]["end"] >= record["pay_date"]


def test_failed_refresh_retains_previous_snapshot(tmp_path, monkeypatch):
    folder = tmp_path / "frozen"
    folder.mkdir()
    (folder / "inputs.json").write_bytes(b"old inputs")
    (folder / "manifest.json").write_bytes(b"old manifest")
    monkeypatch.setattr(assets, "verify", lambda _: {})
    def fail(stage, as_of):
        stage.mkdir()
        (stage / "inputs.json").write_bytes(b"partial inputs")
        raise RuntimeError("测试接口失败")
    monkeypatch.setattr(assets, "acquire", fail)
    with pytest.raises(RuntimeError, match="接口失败"):
        assets.refresh(folder, "2026-11-13")
    assert (folder / "inputs.json").read_bytes() == b"old inputs"
    assert (folder / "manifest.json").read_bytes() == b"old manifest"
    assert not list(tmp_path.glob(".crossing-assets-*"))


def test_json_serialization_uses_lf_and_rejects_nonfinite(tmp_path):
    path = tmp_path / "input.json"
    assets.write_json(path, {"中文": "值"})
    assert b"\r" not in path.read_bytes()
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    with pytest.raises(ValueError):
        assets.write_json(path, {"close": float("nan")})


def test_real_frozen_input_replays_entire_raw_without_network(monkeypatch):
    folder = assets.ROOT / "data" / "crossing-assets"
    if not folder.exists():
        pytest.skip("仅在已提交冻结研究输入存在时执行")
    monkeypatch.setattr(assets.requests, "Session", lambda: pytest.fail("离线核验不得联网"))
    monkeypatch.setattr(assets.requests, "get", lambda *a, **k: pytest.fail("离线公告核验不得联网"))
    result = assets.verify(folder)
    assert set(result["assets"]) == {"510300", "160706"}
    for code, asset in result["assets"].items():
        assert asset["sources"]["symbol"] == assets.SPECS[code]["symbol"]
        assert asset["prices"][0]["date"] == asset["listing_date"]
    lof = result["assets"]["160706"]
    expected = sum(row["record_date"] in assets.LOF_NOTICES for row in lof["dividends"])
    assert sum(row["verified"] for row in lof["dividends"]) == expected
    assert len(lof["quality"]["unresolved_events"]) == len(lof["dividends"]) - expected


def test_self_consistent_tampered_inputs_fail_raw_rebuild(tmp_path, monkeypatch):
    folder = assets.ROOT / "data" / "crossing-assets"
    if not folder.exists():
        pytest.skip("仅在已提交冻结研究输入存在时执行")
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(folder, copy)
    result = json.loads((copy / "inputs.json").read_text(encoding="utf-8"))
    result["assets"]["510300"]["prices"][10]["close"] += .001
    result["hashes"]["510300"] = assets.canonical(result["assets"]["510300"])
    assets.write_json(copy / "inputs.json", result)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    manifest["inputs_sha256"] = assets.digest((copy / "inputs.json").read_bytes())
    assets.write_json(copy / "manifest.json", manifest)
    monkeypatch.setattr(assets.requests, "Session", lambda: pytest.fail("不能联网修正输入"))
    with pytest.raises(ValueError, match="完整重建"):
        assets.verify(copy)


def test_refresh_requires_explicit_cutoff():
    with pytest.raises(SystemExit):
        assets.main(["--refresh"])


def calendar_payload():
    start = date(2005, 1, 4)
    return {"code": 200, "data": [{"indexCode": "000300", "tradeDate": (start + timedelta(days=i)).strftime("%Y%m%d")} for i in range(5001)]}


def test_calendar_requires_full_history_and_preserves_legitimate_update_delay():
    payload = calendar_payload()
    last = (date(2005, 1, 4) + timedelta(days=5000)).isoformat()
    delayed = (date.fromisoformat(last) + timedelta(days=2)).isoformat()
    dates = assets.parse_calendar(payload, delayed)
    assert dates[0] == "2005-01-04" and dates[-1] == last and len(dates) == 5001
    payload["data"].insert(0, {"indexCode": "000300", "tradeDate": "20050101"})
    assert assets.parse_calendar(payload, delayed) == dates
    for bad in [[], None, {}, payload["data"][:100]]:
        with pytest.raises(ValueError):
            assets.parse_calendar({"code": 200, "data": bad}, delayed)


@pytest.mark.parametrize("change", ["reverse", "duplicate", "wrong_code", "wrong_start", "missing_tail"])
def test_calendar_rejects_order_identity_and_truncation(change):
    payload = calendar_payload()
    as_of = (date(2005, 1, 4) + timedelta(days=5000)).isoformat()
    if change == "reverse": payload["data"].reverse()
    elif change == "duplicate": payload["data"].append(payload["data"][-1])
    elif change == "wrong_code": payload["data"][100]["indexCode"] = "000905"
    elif change == "wrong_start": payload["data"][0]["tradeDate"] = "20050103"
    elif change == "missing_tail": payload["data"] = payload["data"][:-20]
    with pytest.raises(ValueError):
        assets.parse_calendar(payload, as_of)


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_truncated_calendar_refresh_keeps_all_previous_files(tmp_path, monkeypatch, empty):
    import shutil
    source = assets.ROOT / "data" / "crossing-assets"
    if not source.exists():
        pytest.skip("需要已提交的全历史冻结输入")
    folder, altered = tmp_path / "frozen", tmp_path / "altered"
    shutil.copytree(source, folder)
    shutil.copytree(source, altered)
    before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    raw_path = altered / "raw" / "000300-calendar.response.gz"
    payload = json.loads(gzip.decompress(raw_path.read_bytes()))
    payload["data"] = [] if empty else payload["data"][:-1]
    raw_path.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))
    acquire = assets.acquire
    def faulty_download(stage, as_of, *, replay_from=None):
        if replay_from is not None:
            return acquire(stage, as_of, replay_from=replay_from)
        if empty:
            # 仍为HTTP成功且code=200，但空日历必须在原响应重建时失败。
            return acquire(stage, as_of, replay_from=altered)
        # 即使下载层误放行，安装前仍必须检查旧已知日历日期没有丢失。
        shutil.copytree(altered, stage)
        return json.loads((stage / "inputs.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(assets, "acquire", faulty_download)
    with pytest.raises(ValueError, match="日历"):
        assets.refresh(folder, "2026-11-13")
    after = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    assert before == after


def test_calendar_cannot_omit_a_day_with_real_fund_quotes(tmp_path):
    import shutil
    source = assets.ROOT / "data" / "crossing-assets"
    if not source.exists():
        pytest.skip("需要已提交的全历史冻结输入")
    altered = tmp_path / "altered"
    shutil.copytree(source, altered)
    raw_path = altered / "raw" / "000300-calendar.response.gz"
    payload = json.loads(gzip.decompress(raw_path.read_bytes()))
    payload["data"] = [row for row in payload["data"] if row["tradeDate"] != "20260105"]
    raw_path.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))
    with pytest.raises(ValueError, match="实际有报价"):
        assets.acquire(tmp_path / "rebuild", "2026-11-13", replay_from=altered)
