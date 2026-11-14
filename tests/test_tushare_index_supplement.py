"""以固定供应商报文验证同代码补尾，不读取真实凭据或消耗API额度。"""
from copy import deepcopy
import gzip
import json
from types import SimpleNamespace

import pytest

import tushare_index_supplement as supplement


@pytest.fixture(autouse=True)
def no_live_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)


def payloads():
    prices = {"code": 0, "msg": None, "data": {"fields": ["ts_code", "trade_date", "close"], "items": [
        ["H00300.CSI", "20261113", 6048.3191], ["H00300.CSI", "20261112", 6089.937], ["H00300.CSI", "20261111", 6137.8019]]}}
    calendar = {"code": 0, "msg": None, "data": {"fields": ["exchange", "cal_date", "is_open"], "items": [
        ["SSE", "20261113", 1], ["SSE", "20261112", 1], ["SSE", "20261111", 1]]}}
    return prices, calendar


def official():
    stock = {"2004-12-31": 1000, "2026-11-11": 6137.8}
    return stock, dict(stock)


def install_transport(monkeypatch, prices, calendar, calls=None):
    def respond(url, *, json, timeout):
        if calls is not None:
            calls.append(deepcopy(json))
        value = prices if json["api_name"] == "index_daily" else calendar
        return SimpleNamespace(content=__import__("json").dumps(value).encode(), raise_for_status=lambda: None)
    monkeypatch.setattr(supplement.requests, "post", respond)


def test_same_code_tail_preserves_csi_precision_and_sse_calendar():
    stock, calendar = official()
    prices, dates = payloads()
    merged, market = supplement.merge(stock, calendar, prices, dates, "2026-11-11", "2026-11-13")
    assert stock == official()[0]
    assert merged["2004-12-31"] == 1000
    assert merged["2026-11-11"] == 6137.8
    assert merged["2026-11-12"] == 6089.937
    assert merged["2026-11-13"] == 6048.3191
    assert set(market) == set(merged)


@pytest.mark.parametrize("change", ["wrong_code", "nan", "boolean", "overlap", "duplicate", "unordered", "future", "calendar_missing", "exchange", "closed", "empty"])
def test_invalid_tail_or_calendar_is_rejected(change):
    prices, calendar = payloads()
    rows = prices["data"]["items"]
    if change == "wrong_code": rows[0][0] = "000300.SH"
    elif change == "nan": rows[0][2] = float("nan")
    elif change == "boolean": rows[0][2] = True
    elif change == "overlap": rows[-1][2] += .02
    elif change == "duplicate": rows.append(rows[-1])
    elif change == "unordered": rows[0], rows[1] = rows[1], rows[0]
    elif change == "future": rows[0][1] = "20261114"
    elif change == "calendar_missing": calendar["data"]["items"].pop(1)
    elif change == "exchange": calendar["data"]["items"][0][0] = "SZSE"
    elif change == "closed": calendar["data"]["items"][0][2] = 0
    elif change == "empty": prices["data"]["items"] = []
    with pytest.raises(supplement.Unavailable):
        supplement.merge(*official(), prices, calendar, "2026-11-11", "2026-11-13")


def test_one_request_per_api_and_no_token_in_raw_or_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret-never-persist")
    prices, calendar = payloads()
    calls, sources = [], []
    install_transport(monkeypatch, prices, calendar, calls)
    stock, _, status = supplement.fetch(*official(), tmp_path, sources, "2026-11-13")
    assert max(stock) == "2026-11-13" and status["status"] == "applied"
    assert [call["api_name"] for call in calls] == ["index_daily", "trade_cal"]
    assert all(call["token"] == "fixture-secret-never-persist" for call in calls)
    assert all("token" not in row["body"] for row in sources)
    assert "fixture-secret-never-persist" not in json.dumps(sources)
    for path in tmp_path.iterdir():
        assert b"fixture-secret-never-persist" not in gzip.decompress(path.read_bytes())
    original = {row["file"]: json.loads(gzip.decompress((tmp_path / row["file"]).read_bytes())) for row in sources}
    assert supplement.replay(*official(), original, sources, "2026-11-13")[0] == stock


def test_no_token_does_not_call_or_fabricate_data(tmp_path, monkeypatch):
    monkeypatch.setattr(supplement.requests, "post", lambda *a, **k: pytest.fail("无token不得调用"))
    sources = []
    stock, calendar, status = supplement.fetch(*official(), tmp_path, sources, "2026-11-13")
    assert (stock, calendar) == official()
    assert status["status"] == "not_configured" and not sources and not list(tmp_path.iterdir())


def test_hourly_limit_is_optional_safe_failure_and_never_calls_calendar(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    calls, sources = [], []
    install_transport(monkeypatch, {"code": 40203, "msg": "一小时一次", "data": None}, payloads()[1], calls)
    stock, _, status = supplement.fetch(*official(), tmp_path, sources, "2026-11-13")
    assert max(stock) == "2026-11-11" and status["status"] == "unavailable"
    assert "40203" in status["message"]
    assert len(calls) == 1 and not sources and not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload", [[], None, "非对象响应"])
def test_nonobject_json_is_optional_failure_without_raw_files(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    calls, sources = [], []
    install_transport(monkeypatch, payload, payloads()[1], calls)
    stock, calendar, status = supplement.fetch(*official(), tmp_path, sources, "2026-11-13")
    assert (stock, calendar) == official()
    assert status["status"] == "unavailable" and "JSON对象" in status["message"]
    assert calls == [{"api_name": "index_daily", "params": {"ts_code": "H00300.CSI", "start_date": "20261111", "end_date": "20261113"}, "fields": supplement.PRICE_FIELDS, "token": "fixture-secret"}]
    assert not sources and not list(tmp_path.iterdir())


def test_echoed_secret_is_not_saved_or_reported(tmp_path, monkeypatch):
    secret = "fixture-private-token"
    monkeypatch.setenv("TUSHARE_TOKEN", secret)
    prices, calendar = payloads()
    prices["msg"] = "reflected " + secret
    install_transport(monkeypatch, prices, calendar)
    stock, _, status = supplement.fetch(*official(), tmp_path, [], "2026-11-13")
    assert max(stock) == "2026-11-11"
    assert secret not in json.dumps(status)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("has_token", [True, False])
def test_optional_failure_or_missing_token_reuses_verified_previous_tail(tmp_path, monkeypatch, has_token):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    previous, current = tmp_path / "previous", tmp_path / "current"
    previous.mkdir(); current.mkdir()
    sources = []
    install_transport(monkeypatch, *payloads())
    expected, _, status = supplement.fetch(*official(), previous, sources, "2026-11-13")
    (previous / "manifest.json").write_text(json.dumps({"sources": sources, "stock_supplement": status}), encoding="utf-8")
    if has_token:
        install_transport(monkeypatch, {"code": 40203, "data": None}, payloads()[1])
    else:
        monkeypatch.delenv("TUSHARE_TOKEN")
        monkeypatch.setattr(supplement.requests, "post", lambda *a, **k: pytest.fail("无token只读缓存"))
    copied = []
    actual, market, reused = supplement.fetch(*official(), current, copied, "2026-11-13", previous=previous)
    assert actual == expected and max(market) == "2026-11-13"
    assert reused["status"] == "cached" and reused["last_success_at"] == status["last_success_at"]
    assert len(copied) == 2
    assert all((current / name).read_bytes() == (previous / name).read_bytes() for name in supplement.FILES)


def test_offline_replay_rejects_tampered_request_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    install_transport(monkeypatch, *payloads())
    sources = []
    supplement.fetch(*official(), tmp_path, sources, "2026-11-13")
    original = {row["file"]: json.loads(gzip.decompress((tmp_path / row["file"]).read_bytes())) for row in sources}
    sources[0]["body"]["params"]["ts_code"] = "000300.SH"
    with pytest.raises(supplement.Unavailable, match="身份"):
        supplement.replay(*official(), original, sources, "2026-11-13")


@pytest.mark.parametrize("has_token", [True, False])
def test_revised_official_history_can_replace_fully_covered_incompatible_cache(tmp_path, monkeypatch, has_token):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    previous, current = tmp_path / "previous", tmp_path / "current"
    previous.mkdir(); current.mkdir()
    sources = []
    install_transport(monkeypatch, *payloads())
    _, _, status = supplement.fetch(*official(), previous, sources, "2026-11-13")
    (previous / "manifest.json").write_text(json.dumps({"sources": sources, "stock_supplement": status}), encoding="utf-8")
    if has_token:
        install_transport(monkeypatch, {"code": 40203, "data": None}, payloads()[1])
    else:
        monkeypatch.delenv("TUSHARE_TOKEN")
        monkeypatch.setattr(supplement.requests, "post", lambda *a, **k: pytest.fail("无token只核验旧缓存"))
    stock, calendar = official()
    stock.update({"2026-11-12": 6089.94, "2026-11-13": 6048.35})
    calendar.update({"2026-11-12": 1, "2026-11-13": 1})
    copied = []
    actual, actual_calendar, result = supplement.fetch(stock, calendar, current, copied, "2026-11-13", previous=previous)
    assert actual == stock and actual_calendar == calendar
    assert actual["2026-11-13"] == 6048.35
    assert result["status"] == "unavailable" and "官方完整覆盖" in result["message"]
    assert not copied and not list(current.iterdir())


def test_incompatible_cache_cannot_be_dropped_when_official_tail_is_not_covered(tmp_path, monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-secret")
    previous, current = tmp_path / "previous", tmp_path / "current"
    previous.mkdir(); current.mkdir()
    sources = []
    install_transport(monkeypatch, *payloads())
    _, _, status = supplement.fetch(*official(), previous, sources, "2026-11-13")
    (previous / "manifest.json").write_text(json.dumps({"sources": sources, "stock_supplement": status}), encoding="utf-8")
    monkeypatch.delenv("TUSHARE_TOKEN")
    stock, calendar = official()
    stock["2026-11-11"] = 6137.9
    with pytest.raises(supplement.Unavailable, match="尚未完整覆盖"):
        supplement.fetch(stock, calendar, current, [], "2026-11-13", previous=previous)
    assert not list(current.iterdir())
