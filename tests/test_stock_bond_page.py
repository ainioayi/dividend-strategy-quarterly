"""股债独立页面的数据边界、参数重算和失败保护。"""
from copy import deepcopy
from contextlib import nullcontext
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import stock_bond_page as page


def make_inputs(folder, *, requested="2005-01-12", stock=None, bond=None):
    folder.mkdir(parents=True)
    stock = stock or {page.BASE: 1000, "2005-01-04": 1100, "2005-01-05": 900, "2005-01-06": 1200, "2005-01-07": 800, "2005-01-10": 900, "2005-01-11": 800}
    bond = bond or {page.BASE: 100, "2005-01-04": 100, "2005-01-05": 100, "2005-01-06": 100, "2005-01-10": 100}
    page.write_csv(folder / "stock.csv", stock)
    page.write_csv(folder / "bond.csv", bond)
    sources = []
    for name in ["h00300", "000300", "885007-basic", "885007"]:
        raw = json.dumps({"source": name}).encode()
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


def test_future_generation_timestamp_is_rejected(files):
    folder, parameters, output = files
    result = page.update(folder, parameters, output)
    result["generated_at"] = "2099-01-01T00:00:00+00:00"
    output.write_text(page.json_text(result), encoding="utf-8")
    with pytest.raises(ValueError, match="不能晚于"):
        page.verify(folder, parameters, output)


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


def test_next_day_without_new_common_data_succeeds_without_changes(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    source = make_inputs(tmp_path / "stale", requested="2005-01-13")
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    result = page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert result["as_of"] == "2005-01-10"
    assert output.read_bytes() == before
    assert (folder / "manifest.json").read_bytes() == before_manifest
    assert page.read_json(folder / "manifest.json")["requested_as_of"] == "2005-01-12"


def test_one_sided_update_without_new_common_day_is_not_published(files, monkeypatch, tmp_path):
    folder, parameters, output = files
    page.update(folder, parameters, output)
    before, before_manifest = output.read_bytes(), (folder / "manifest.json").read_bytes()
    _, stock, bond = page.load_inputs(folder)
    stock["2005-01-12"] = 1500
    source = make_inputs(tmp_path / "one-sided", requested="2005-01-13", stock=stock, bond=bond)
    monkeypatch.setattr(page, "acquire", lambda stage, _: shutil.copytree(source, stage))
    page.update(folder, parameters, output, refresh=True, as_of="2005-01-13")
    assert output.read_bytes() == before
    assert (folder / "manifest.json").read_bytes() == before_manifest


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
        manifest["downloaded_at_utc"] = "2099-01-01T00:00:00+00:00"
        (stage / "manifest.json").write_text(page.json_text(manifest), encoding="utf-8")
    monkeypatch.setattr(page, "acquire", same_inputs)
    assert page.update(folder, parameters, output, refresh=True, as_of="2005-01-12") == original
    assert output.read_bytes() == before_output
    assert (folder / "manifest.json").read_bytes() == before_manifest
