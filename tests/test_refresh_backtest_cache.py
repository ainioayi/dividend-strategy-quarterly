import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from refresh_backtest_cache import (
    _dividend_summary,
    _fetch_dividends_eastmoney,
    _fetch_kline_sina,
    _normalize_dividend_rows,
    refresh_dividend_cache,
)


def test_dividend_normalization_keeps_only_implemented_point_in_time_rows():
    rows = [
        {"ASSIGNMENT_PROGRESS": "实施", "REPORT_DATE": "2025-12-31",
         "EX_DIVIDEND_DATE": "2026-06-01", "PRETAX_BONUS_RMB": 10},
        {"ASSIGNMENT_PROGRESS": "预案", "REPORT_DATE": "2025-12-31",
         "EX_DIVIDEND_DATE": "2026-07-01", "PRETAX_BONUS_RMB": 20},
        {"ASSIGNMENT_PROGRESS": "完成", "REPORT_DATE": "2026-06-30",
         "EX_DIVIDEND_DATE": "2026-09-01", "PRETAX_BONUS_RMB": 5},
    ]
    normalized = _normalize_dividend_rows(rows, "2026-08-31")
    assert normalized == [{"year": 2025, "ex_date": "2026-06-01", "dps": 1.0,
                           "bonus_ratio": 0.0, "transfer_ratio": 0.0}]
    assert _dividend_summary(normalized)[0]["dps"] == 1.0


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_sina_kline_is_unadjusted_and_cut_off_by_explicit_date(monkeypatch):
    captured = {}

    def fake_get(*args, **kwargs):
        captured.update(kwargs["params"])
        return _Response([
            {"day": "2026-08-25", "close": "10.50"},
            {"day": "2026-08-26", "close": "11.00"},
        ])

    monkeypatch.setattr("refresh_backtest_cache.requests.get", fake_get)
    result = _fetch_kline_sina("920982", "2026-08-25")

    assert captured["symbol"] == "bj920982"
    assert captured["ma"] == "no"
    assert result == {"2026-08-25": 10.5}


def test_dividend_fetch_reads_all_pages(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        page = kwargs["params"]["pageNumber"]
        calls.append(page)
        row = {"ASSIGNMENT_PROGRESS": "实施", "REPORT_DATE": f"202{page}-12-31",
               "EX_DIVIDEND_DATE": f"202{page + 1}-06-01", "PRETAX_BONUS_RMB": 10}
        return _Response({"result": {"data": [row], "pages": 2}})

    monkeypatch.setattr("refresh_backtest_cache.requests.get", fake_get)
    assert len(_fetch_dividends_eastmoney("600000", "2026-08-31")) == 2
    assert calls == [1, 2]


def test_dividend_fetch_fails_closed_on_bad_response(monkeypatch):
    monkeypatch.setattr(
        "refresh_backtest_cache.requests.get", lambda *args, **kwargs: _Response({"result": None})
    )
    with pytest.raises(RuntimeError, match="响应结构异常"):
        _fetch_dividends_eastmoney("600000", "2026-08-31")


def test_dividend_batch_failure_does_not_mix_old_and_new_cache(tmp_path, monkeypatch):
    old = '[{"year": 2024}]'
    for code in ("600000", "600001"):
        (tmp_path / f"dvd_{code}.json").write_text(old, encoding="utf-8")
        (tmp_path / f"dv_{code}.json").write_text(old, encoding="utf-8")

    def fake_fetch(code, as_of):
        if code == "600001":
            raise RuntimeError("接口失败")
        return [{"year": 2025, "ex_date": "2026-06-01", "dps": 1.0,
                 "bonus_ratio": 0.0, "transfer_ratio": 0.0}]

    monkeypatch.setattr("refresh_backtest_cache._fetch_dividends_eastmoney", fake_fetch)
    failed = refresh_dividend_cache(["600000", "600001"], tmp_path, "2026-08-31", interval=0)
    assert failed == ["600001"]
    for code in ("600000", "600001"):
        assert (tmp_path / f"dvd_{code}.json").read_text(encoding="utf-8") == old
        assert (tmp_path / f"dv_{code}.json").read_text(encoding="utf-8") == old


def test_empty_dividend_response_is_failure_and_keeps_old_cache(tmp_path, monkeypatch):
    old = '[{"year": 2024}]'
    (tmp_path / "dvd_600000.json").write_text(old, encoding="utf-8")
    (tmp_path / "dv_600000.json").write_text(old, encoding="utf-8")
    monkeypatch.setattr("refresh_backtest_cache._fetch_dividends_eastmoney", lambda *args: [])

    failed = refresh_dividend_cache(["600000"], tmp_path, "2026-08-31", interval=0)

    assert failed == ["600000"]
    assert (tmp_path / "dvd_600000.json").read_text(encoding="utf-8") == old
    assert (tmp_path / "dv_600000.json").read_text(encoding="utf-8") == old
