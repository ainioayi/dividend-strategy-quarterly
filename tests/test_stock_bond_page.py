"""股债独立页面的数据边界、参数重算和失败保护。"""
from copy import deepcopy
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import stock_bond_page as page


@pytest.fixture(autouse=True)
def no_live_tushare_credentials(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)


def make_inputs(folder, *, requested="2005-01-12", stock=None, bond=None):
    folder.mkdir(parents=True)
    stock = stock or {page.BASE: 1000, "2005-01-04": 1100, "2005-01-05": 900, "2005-01-06": 1200, "2005-01-07": 800, "2005-01-10": 900, "2005-01-11": 800}
    bond = bond or {page.BASE: 100, "2005-01-04": 100, "2005-01-05": 100, "2005-01-06": 100, "2005-01-10": 100}
    page.write_csv(folder / "stock.csv", stock)
    page.write_csv(folder / "bond.csv", bond)
    sources = []
    for name in ["h00300", "000300", "885007-basic", "885007"]:
        if name in {"h00300", "000300"}:
            code = "H00300" if name == "h00300" else "000300"
            payload = {"code": 200, "data": [{"indexCode": code, "indexNameCnAll": "沪深300全收益指数" if name == "h00300" else "沪深300指数", "tradeDate": day.replace("-", ""), "close": value} for day, value in sorted(stock.items())]}
        elif name == "885007-basic":
            payload = {"code": 0, "data": {"files": [{"title": "代码", "name": "885007.WI"}, {"title": "全称", "name": "万得混合债券型二级基金指数"}]}}
        else:
            payload = {"code": 0, "data": {"index_create_time": "2003-12-31", "tb_data": {"x_data": sorted(bond), "series": [{"name": "本指数", "data": [bond[day] - 100 for day in sorted(bond)]}]}}}
        raw = json.dumps(payload).encode()
        path = folder / (name + "-raw.json.gz")
        path.write_bytes(gzip.compress(raw, mtime=0))
        sources.append({"file": path.name, "file_sha256": page.fingerprint(path), "raw_sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {"requested_as_of": requested, "stock": {"code": "H00300.CSI", "rows": len(stock), "first": min(stock), "last": max(stock)},
                "bond": {"code": "885007.WI", "rows": len(bond), "first": min(bond), "last": max(bond)},
                "sources": sources, "normalized_sha256": {name: page.fingerprint(folder / name) for name in ["stock.csv", "bond.csv"]},
                "excluded_stock_nontrading_dates": []}
    (folder / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8")
    return folder


@pytest.fixture
def files(tmp_path):
    folder = make_inputs(tmp_path / "cache")
    parameters = tmp_path / "parameters.json"
    parameters.write_text(page.json_text({"base_date": page.BASE, "cooldown_sessions": 126}), encoding="utf-8")
    return folder, parameters, tmp_path / "site" / "stock-bond.json"


def test_crossings_use_only_past_and_do_not_jump_gaps():
    days = [page.BASE, "2005-01-04", "2005-01-05", "2005-01-06", "2005-01-07", "2005-01-10"]
    stock = dict(zip(days, [1000, 1100, 900, 1200, 800, 1100]))
    bond = {day: 100 for day in days if day != "2005-01-07"}
    early = page.find_signals(days[:4], stock, bond, page.BASE)
    assert early == [{"index": 2, "date": days[2], "direction": "down"}, {"index": 3, "date": days[3], "direction": "up"}]
    stock[days[-1]] = 5000
    assert page.find_signals(days, stock, bond, page.BASE) == early


def test_cooldown_counts_from_last_retained_downcrossing():
    signals = [{"index": i, "date": str(i), "direction": direction} for i, direction in [(2, "down"), (3, "up"), (90, "down"), (127, "down"), (128, "down")]]
    assert [row["index"] for row in page.merge_signals(signals, 126)] == [2, 128]


def test_build_uses_latest_common_day_and_preserves_null(files):
    folder, parameter_path, output = files
    result = page.update(folder, parameter_path, output)
    assert result["as_of"] == "2005-01-10"
    assert result["requested_as_of"] == "2005-01-12"
    assert result["chart"]["dates"][-1] == result["as_of"]
    assert result["chart"]["stock"][0] == result["chart"]["bond"][0] == 1000
    assert result["chart"]["bond"][-2] is None
    assert page.verify(folder, parameter_path, output) == result


def test_source_dates_and_candidate_status_are_not_a_buy_instruction(files):
    folder, parameters, output = files
    result = page.update(folder, parameters, output)
    assert result["source_dates"]["stock_as_of"] == "2005-01-11"
    assert result["source_dates"]["bond_as_of"] == "2005-01-10"
    assert result["quality"]["historical_availability_verified"] is False
    assert result["quality"]["can_issue_buy_signal"] is False
    assert all(row["verification_status"] == "pending_date_validation" for row in result["crossings"])


def test_rounding_sensitive_crossing_is_identified_without_moving_date(tmp_path):
    stock = {page.BASE: 1000, "2005-01-04": 1000.001, "2005-01-05": 999.999}
    bond = dict.fromkeys(stock, 100)
    folder = make_inputs(tmp_path / "cache", stock=stock, bond=bond)
    parameters = {"base_date": page.BASE, "cooldown_sessions": 126}
    result = page.build(folder, parameters, "test")
    assert result["crossings"][0]["date"] == "2005-01-05"
    assert result["crossings"][0]["precision_sensitive"] is True


def test_changed_parameters_require_rebuild_and_change_normalization(files):
    folder, parameter_path, output = files
    original = page.update(folder, parameter_path, output)
    parameter_path.write_text(page.json_text({"base_date": "2005-01-04", "cooldown_sessions": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="页面数据"):
        page.verify(folder, parameter_path, output)
    rebuilt = page.update(folder, parameter_path, output)
    assert rebuilt["chart"]["dates"][0] == "2005-01-04"
    assert rebuilt["chart"]["stock"][0] == rebuilt["chart"]["bond"][0] == 1000
    assert rebuilt["audit"]["parameters_sha256"] != original["audit"]["parameters_sha256"]
    assert page.verify(folder, parameter_path, output) == rebuilt


@pytest.mark.parametrize("parameters", [
    {"base_date": "20041231", "cooldown_sessions": 126},
    {"base_date": "2004-12-30", "cooldown_sessions": 126},
    {"base_date": page.BASE, "cooldown_sessions": True},
    {"base_date": page.BASE, "cooldown_sessions": 0},
    {"base_date": page.BASE, "cooldown_sessions": 1.5},
    {"base_date": page.BASE, "cooldown_sessions": 126, "stock_code": "000300"},
])
def test_rejects_invalid_parameters(tmp_path, parameters):
    path = tmp_path / "params.json"
    path.write_text(json.dumps(parameters), encoding="utf-8")
    with pytest.raises(ValueError):
        page.load_parameters(path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True])
def test_rejects_invalid_points(value):
    with pytest.raises(ValueError):
        page.positive(value)


def test_provider_identity_and_cumulative_return_conversion():
    basic = {"code": 0, "data": {"files": [{"title": "代码", "name": "885007.WI"}, {"title": "全称", "name": "万得混合债券型二级基金指数"}]}}
    payload = {"code": 0, "data": {"index_create_time": "2003-12-31", "tb_data": {"x_data": ["1970-01-01", page.BASE, "2005-01-04", "2005-01-05"], "series": [{"name": "本指数", "data": [0, 2.64, 4, 6]}]}}}
    values, _ = page.parse_bond(basic, payload, "2005-01-04")
    assert values == {page.BASE: 102.64, "2005-01-04": 104}
    wrong = deepcopy(basic)
    wrong["data"]["files"][0]["name"] = "885008.WI"
    with pytest.raises(ValueError, match="代码或名称"):
        page.parse_bond(wrong, payload, "2005-01-04")
    payload["data"]["tb_data"]["series"][0]["data"][1] = float("nan")
    with pytest.raises(ValueError):
        page.parse_bond(basic, payload, "2005-01-04")


def test_downloaded_manifest_and_csv_keep_identical_lf_bytes_on_windows(tmp_path, monkeypatch):
    values = {(date(2004, 12, 31) + timedelta(days=i)).isoformat(): 1000 for i in range(5000)}
    response = SimpleNamespace(content=b"{}", url="https://example.invalid/", json=lambda: {}, raise_for_status=lambda: None)
    session = SimpleNamespace(headers={}, get=lambda *a, **k: response, post=lambda *a, **k: response)
    monkeypatch.setattr(page.requests, "Session", lambda: nullcontext(session))
    monkeypatch.setattr(page, "parse_csi", lambda *a: values)
    monkeypatch.setattr(page, "parse_bond", lambda *a: (values, "万得混合债券型二级基金指数"))
    folder = tmp_path / "cache"
    manifest = page.acquire(folder, "2026-11-13")
    assert (folder / "manifest.json").read_bytes() == page.json_text(manifest).encode("utf-8")
    for name in ["stock.csv", "bond.csv"]:
        raw = (folder / name).read_bytes()
        assert b"\r" not in raw
        assert hashlib.sha256(raw).hexdigest() == manifest["normalized_sha256"][name]


def test_csi_rejects_price_index_and_clips_to_requested_date():
    def row(code, day, close=1000):
        return {"indexCode": code, "indexNameCnAll": "沪深300全收益指数", "tradeDate": day, "close": close}
    payload = {"code": 200, "data": [row("H00300", "20041231"), row("H00300", "20050104", 1100)]}
    assert page.parse_csi(payload, "H00300", page.BASE) == {page.BASE: 1000}
    payload["data"][0]["indexCode"] = "000300"
    with pytest.raises(ValueError, match="其他指数"):
        page.parse_csi(payload, "H00300", page.BASE)


def test_tampered_inputs_and_output_are_rejected(files):
    folder, parameters, output = files
    result = page.update(folder, parameters, output)
    result["as_of"] = "2005-01-12"
    output.write_text(page.json_text(result), encoding="utf-8")
    with pytest.raises(ValueError, match="页面数据"):
        page.verify(folder, parameters, output)
    (folder / "stock.csv").write_text("date,value\n2004-12-31,999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="输入指纹"):
        page.build(folder, page.load_parameters(parameters), page.fingerprint(parameters))


def test_csv_and_updated_checksum_must_still_match_provider_response(files):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before = output.read_bytes()
    _, stock, _ = page.load_inputs(folder)
    stock["2005-01-04"] = 1110
    page.write_csv(folder / "stock.csv", stock)
    manifest = page.read_json(folder / "manifest.json")
    manifest["normalized_sha256"]["stock.csv"] = page.fingerprint(folder / "stock.csv")
    (folder / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="原始响应"):
        page.update(folder, parameters, output)
    assert output.read_bytes() == before


def test_future_generation_timestamp_is_rejected(files):
    folder, parameters, output = files
    result = page.update(folder, parameters, output)
    result["generated_at"] = "2099-01-01T00:00:00+00:00"
    output.write_text(page.json_text(result), encoding="utf-8")
    with pytest.raises(ValueError, match="不能晚于"):
        page.verify(folder, parameters, output)


@pytest.mark.parametrize("stamp", ["2099-01-01T00:00:00+00:00", "2026-01-01T00:00:00"])
def test_invalid_check_timestamp_cannot_replace_existing_page(files, stamp):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before = output.read_bytes()
    manifest = page.read_json(folder / "manifest.json")
    manifest["downloaded_at_utc"] = stamp
    (folder / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="成功检查时间"):
        page.update(folder, parameters, output)
    assert output.read_bytes() == before


def test_failed_download_does_not_touch_existing_output_or_cache(files, monkeypatch):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before_output, before_input = output.read_bytes(), (folder / "manifest.json").read_bytes()
    def broken(stage, _):
        stage.mkdir()
        (stage / "stock.csv").write_text("partial", encoding="utf-8")
        raise RuntimeError("公开接口暂不可用")
    monkeypatch.setattr(page, "acquire", broken)
    with pytest.raises(RuntimeError, match="接口暂不可用"):
        page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert output.read_bytes() == before_output
    assert (folder / "manifest.json").read_bytes() == before_input


def test_next_day_without_new_prices_records_successful_check_at_same_asof(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    source = make_inputs(tmp_path / "stale", requested="2005-01-13")
    manifest = page.read_json(source / "manifest.json")
    checked = datetime.now(timezone.utc).isoformat()
    manifest["downloaded_at_utc"] = checked
    (source / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8", newline="\n")
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    result = page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert result["as_of"] == "2005-01-10"
    assert result["source_dates"]["checked_at"] == checked
    assert result["generated_at"] == json.loads(before)["generated_at"]
    assert result["chart"] == json.loads(before)["chart"]
    assert page.read_json(folder / "manifest.json")["requested_as_of"] == "2005-01-13"
    assert (folder / "manifest.json").read_bytes() != before_manifest
    assert page.verify(folder, parameters, output) == result


def test_one_sided_update_keeps_shared_chart_and_reports_source_latest_date(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    _, stock, bond = page.load_inputs(folder)
    stock["2005-01-12"] = 1500
    source = make_inputs(tmp_path / "one-sided", requested="2005-01-13", stock=stock, bond=bond)
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    result = page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert result["as_of"] == json.loads(before)["as_of"]
    assert result["chart"] == json.loads(before)["chart"]
    assert result["source_dates"]["stock_as_of"] == "2005-01-12"
    assert result["source_dates"]["bond_as_of"] == "2005-01-10"
    assert (folder / "manifest.json").read_bytes() != before_manifest


def test_regressing_common_cutoff_is_rejected(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before = output.read_bytes()
    _, stock, bond = page.load_inputs(folder)
    del bond["2005-01-10"]
    source = make_inputs(tmp_path / "regressed", requested="2005-01-13", stock=stock, bond=bond)
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    with pytest.raises(ValueError, match="日期倒退"):
        page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert output.read_bytes() == before


def test_output_failure_rolls_back_installed_cache(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before_output, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    _, stock, bond = page.load_inputs(folder)
    stock["2005-01-12"], bond["2005-01-12"] = 1200, 100
    source = make_inputs(tmp_path / "fresh", requested="2005-01-13", stock=stock, bond=bond)
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    replace = page.os.replace
    def fail_output(src, dest):
        if Path(dest) == output:
            raise OSError("模拟页面替换失败")
        return replace(src, dest)
    monkeypatch.setattr(page.os, "replace", fail_output)
    with pytest.raises(OSError, match="页面替换失败"):
        page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert output.read_bytes() == before_output
    assert (folder / "manifest.json").read_bytes() == before_manifest


def test_partial_history_is_not_published_even_if_last_date_advances(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before_output, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    _, stock, bond = page.load_inputs(folder)
    stock["2005-01-12"], bond["2005-01-12"] = 1200, 100
    del bond["2005-01-04"]
    source = make_inputs(tmp_path / "partial", requested="2005-01-13", stock=stock, bond=bond)
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    with pytest.raises(ValueError, match="丢失已有历史"):
        page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert output.read_bytes() == before_output
    assert (folder / "manifest.json").read_bytes() == before_manifest


def test_refresh_requires_explicit_cutoff(files):
    folder, parameters, output = files
    with pytest.raises(ValueError, match="显式"):
        page.update(folder, parameters, output, refresh=True)


def test_offline_cli_and_verify_never_request_network(files, monkeypatch):
    folder, parameters, output = files
    monkeypatch.setattr(page.requests, "Session", lambda: pytest.fail("离线操作不应联网"))
    args = ["--data-dir", str(folder), "--parameters", str(parameters), "--output", str(output)]
    page.main(args)
    before = output.read_bytes()
    page.main(args + ["--verify-only"])
    assert output.read_bytes() == before


def test_unchanged_refresh_and_offline_rebuild_keep_generated_at(files, monkeypatch):
    folder, parameters, output = files
    original = page.update(folder, parameters, output)
    before_output, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    assert page.update(folder, parameters, output) == original
    def same_inputs(stage, _):
        shutil.copytree(folder, stage)
        manifest = page.read_json(stage / "manifest.json")
        manifest["downloaded_at_utc"] = datetime.now(timezone.utc).isoformat()
        (stage / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8")
    monkeypatch.setattr(page, "acquire", same_inputs)
    refreshed = page.update(folder, parameters, output, refresh=True, as_of="2005-01-12")
    assert refreshed["generated_at"] == original["generated_at"]
    assert refreshed["chart"] == original["chart"]
    assert refreshed["source_dates"]["checked_at"] != original["source_dates"]["checked_at"]
    assert (folder / "manifest.json").read_bytes() != before_manifest
    after_refresh = output.read_bytes()
    page.update(folder, parameters, output)
    assert output.read_bytes() == after_refresh


def install_complete_provider_fixture(monkeypatch, *, calendar_ahead=False, internal_gap=False):
    values = {(date(2004, 12, 31) + timedelta(days=i)).isoformat(): 1000 for i in range(5000)}
    values["2026-11-11"] = 6137.8
    bond = {stamp: 100 for stamp in values}
    bond["2026-11-12"] = 100
    def response(payload, url):
        return SimpleNamespace(content=json.dumps(payload).encode(), url=url, json=lambda: payload, raise_for_status=lambda: None)
    def csi_get(url, *, params, timeout):
        code = params["indexCode"]
        current = dict(values)
        if code == "000300" and calendar_ahead:
            current.update({"2026-11-12": 4000, "2026-11-13": 4001})
        if code == "H00300" and internal_gap:
            current.pop("2005-01-04")
        payload = {"code": 200, "data": [{"indexCode": code, "indexNameCnAll": "沪深300全收益指数" if code == "H00300" else "沪深300指数", "tradeDate": stamp.replace("-", ""), "close": value} for stamp, value in sorted(current.items())]}
        return response(payload, url)
    def jcs_response(url, *, json, timeout):
        if url == page.JCS_BASIC:
            payload = {"code": 0, "data": {"files": [{"title": "代码", "name": "885007.WI"}, {"title": "全称", "name": "万得混合债券型二级基金指数"}]}}
        else:
            payload = {"code": 0, "data": {"index_create_time": "2003-12-31", "tb_data": {"x_data": sorted(bond), "series": [{"name": "本指数", "data": [bond[stamp] - 100 for stamp in sorted(bond)]}]}}}
        return response(payload, url)
    session = SimpleNamespace(headers={}, get=csi_get, post=jcs_response)
    monkeypatch.setattr(page.requests, "Session", lambda: nullcontext(session))


def install_supplement_fixture(monkeypatch, *, limited=False):
    calls = []
    def response(url, *, json: object, timeout):
        calls.append(json["api_name"])
        if limited:
            payload = {"code": 40203, "msg": "每小时一次", "data": None}
        elif json["api_name"] == "index_daily":
            payload = {"code": 0, "data": {"fields": ["ts_code", "trade_date", "close"], "items": [["H00300.CSI", "20261113", 6048.3191], ["H00300.CSI", "20261112", 6089.937], ["H00300.CSI", "20261111", 6137.8019]]}}
        else:
            payload = {"code": 0, "data": {"fields": ["exchange", "cal_date", "is_open"], "items": [["SSE", "20261113", 1], ["SSE", "20261112", 1], ["SSE", "20261111", 1]]}}
        return SimpleNamespace(content=__import__("json").dumps(payload).encode(), raise_for_status=lambda: None)
    monkeypatch.setattr(page.tushare_supplement.requests, "post", response)
    return calls


def test_real_shape_supplement_acquisition_rebuilds_and_only_advances_shared_day(tmp_path, monkeypatch):
    install_complete_provider_fixture(monkeypatch)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
    calls = install_supplement_fixture(monkeypatch)
    folder = tmp_path / "cache"
    manifest = page.acquire(folder, "2026-11-13")
    assert calls == ["index_daily", "trade_cal"]
    assert manifest["stock"]["last"] == "2026-11-13"
    assert manifest["stock"]["official_as_of"] == "2026-11-11"
    _, stock, _ = page.load_inputs(folder)
    assert stock["2026-11-11"] == 6137.8
    assert stock["2026-11-12"] == 6089.937
    result = page.build(folder, {"base_date": page.BASE, "cooldown_sessions": 126}, "test")
    assert result["as_of"] == "2026-11-12"
    assert result["source_dates"]["stock_as_of"] == "2026-11-13"
    assert result["source_dates"]["bond_as_of"] == "2026-11-12"
    assert result["quality"]["stock_source"] == "中证官方历史＋Tushare同代码补充"
    assert result["quality"]["stock_supplement"]["status"] == "applied"
    assert result["quality"]["stock_supplement"]["last_success_at"] <= result["source_dates"]["checked_at"]
    # 即使CSV与自身校验同时被改，仍须与脱敏原始响应重建一致。
    stock["2026-11-13"] = 6000
    page.write_csv(folder / "stock.csv", stock)
    manifest["normalized_sha256"]["stock.csv"] = page.fingerprint(folder / "stock.csv")
    (folder / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="原始响应"):
        page.load_inputs(folder)


def test_first_hourly_limit_keeps_original_csi_jcs_usable(tmp_path, monkeypatch):
    install_complete_provider_fixture(monkeypatch)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
    calls = install_supplement_fixture(monkeypatch, limited=True)
    folder = tmp_path / "cache"
    page.acquire(folder, "2026-11-13")
    result = page.build(folder, {"base_date": page.BASE, "cooldown_sessions": 126}, "test")
    assert calls == ["index_daily"]
    assert result["as_of"] == "2026-11-11"
    assert result["source_dates"]["stock_as_of"] == "2026-11-11"
    assert any("40203" in warning for warning in result["quality"]["warnings"])
    assert not (folder / "tushare-h00300-raw.json.gz").exists()


def test_refresh_limit_reuses_old_supplement_and_offline_rebuild_never_calls_tushare(tmp_path, monkeypatch):
    install_complete_provider_fixture(monkeypatch)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
    install_supplement_fixture(monkeypatch)
    previous, current = tmp_path / "previous", tmp_path / "current"
    page.acquire(previous, "2026-11-13")
    calls = install_supplement_fixture(monkeypatch, limited=True)
    manifest = page.acquire(current, "2026-11-13", previous=previous)
    assert calls == ["index_daily"]
    assert manifest["stock_supplement"]["status"] == "cached"
    assert manifest["stock"]["last"] == "2026-11-13"
    assert manifest["stock_supplement"]["last_success_at"] == page.read_json(previous / "manifest.json")["stock_supplement"]["last_success_at"]
    assert [row for row in manifest["sources"] if row["file"] in page.tushare_supplement.FILES] == [row for row in page.read_json(previous / "manifest.json")["sources"] if row["file"] in page.tushare_supplement.FILES]
    monkeypatch.setattr(page.tushare_supplement.requests, "post", lambda *a, **k: pytest.fail("离线重建不得联网"))
    _, values, _ = page.load_inputs(current)
    assert values["2026-11-13"] == 6048.3191


def test_official_calendar_can_lead_all_return_index_and_tushare_fills_only_tail(tmp_path, monkeypatch):
    install_complete_provider_fixture(monkeypatch, calendar_ahead=True)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
    calls = install_supplement_fixture(monkeypatch)
    folder = tmp_path / "calendar-ahead"
    manifest = page.acquire(folder, "2026-11-13")
    assert calls == ["index_daily", "trade_cal"]
    assert manifest["stock"]["official_as_of"] == "2026-11-11"
    assert manifest["stock"]["last"] == "2026-11-13"
    result = page.build(folder, {"base_date": page.BASE, "cooldown_sessions": 126}, "test")
    assert result["as_of"] == "2026-11-12"
    assert result["source_dates"]["stock_as_of"] == "2026-11-13"


@pytest.mark.parametrize("limited", [True, False])
def test_official_calendar_tail_without_supplement_keeps_available_day(tmp_path, monkeypatch, limited):
    install_complete_provider_fixture(monkeypatch, calendar_ahead=True)
    if limited:
        monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
        calls = install_supplement_fixture(monkeypatch, limited=True)
    else:
        calls = []
        monkeypatch.setattr(page.tushare_supplement.requests, "post", lambda *a, **k: pytest.fail("无token不得调用"))
    folder = tmp_path / "calendar-tail"
    page.acquire(folder, "2026-11-13")
    result = page.build(folder, {"base_date": page.BASE, "cooldown_sessions": 126}, "test")
    assert result["as_of"] == "2026-11-11"
    assert calls == (["index_daily"] if limited else [])


def test_internal_official_gap_still_fails_before_optional_provider(tmp_path, monkeypatch):
    install_complete_provider_fixture(monkeypatch, calendar_ahead=True, internal_gap=True)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token-not-a-credential")
    monkeypatch.setattr(page.tushare_supplement.requests, "post", lambda *a, **k: pytest.fail("不能用补尾掩盖历史内部缺口"))
    with pytest.raises(ValueError, match="历史内部"):
        page.acquire(tmp_path / "internal-gap", "2026-11-13")
