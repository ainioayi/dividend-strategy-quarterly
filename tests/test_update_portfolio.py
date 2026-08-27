"""分红入账和季度账本落盘测试。"""
from __future__ import annotations

import json

import pytest

import update_portfolio


def _state():
    return {
        "as_of": "2026-01-10",
        "initial_capital": 100000.0,
        "cash": 1000.0,
        "holdings": {
            "600000": {"code": "600000", "name": "测试", "shares": 100,
                       "entry_date": "2025-12-20", "entry_price": 10,
                       "soft_exit_streak": 0, "sector": "银行", "bank": True},
        },
        "events": [],
    }


def _verified():
    return {"rows": [{
        "code": "600000",
        "quote": {"price": 11.0},
        "dividend": {"implemented_records": [{
            "report_date": "2025-12-31",
            "ex_dividend_date": "2026-02-01",
            "cash_div_per_share": 1.0,
            "bonus_ratio": 1.0,
            "trans_ratio": 0.0,
        }]},
    }]}


def test_apply_dividends_updates_cash_shares_event_and_is_idempotent():
    first = update_portfolio._apply_dividends(_state(), _verified(), "2026-04-01")
    assert first["cash"] == pytest.approx(1090.0)
    assert first["holdings"]["600000"]["shares"] == 110
    assert len(first["events"]) == 1
    assert first["events"][0]["gross"] == pytest.approx(100.0)
    assert first["events"][0]["tax"] == pytest.approx(10.0)
    assert first["events"][0]["net_cash"] == pytest.approx(90.0)
    assert len(first["processed_dividends"]) == 1

    first["as_of"] = "2026-01-10"
    second = update_portfolio._apply_dividends(first, _verified(), "2026-04-01")
    assert second["cash"] == first["cash"]
    assert second["holdings"]["600000"]["shares"] == 110
    assert len(second["events"]) == 1


def test_update_one_skips_an_already_processed_period(tmp_path, monkeypatch):
    ledger = tmp_path / "relaxed.json"
    state = _state() | {"last_processed_period": "2026Q1"}
    ledger.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(update_portfolio, "HISTORY_DIR", tmp_path / "history")

    result = update_portfolio.update_one(
        ledger, _verified(), [], "2026Q1", "2026-04-01", {}, False,
    )
    assert result == state
    assert json.loads(ledger.read_text(encoding="utf-8")) == state
    assert not (tmp_path / "history").exists()


def test_update_one_writes_ledger_history_and_summary(tmp_path, monkeypatch):
    ledger = tmp_path / "relaxed.json"
    ledger.write_text(json.dumps(_state(), ensure_ascii=False), encoding="utf-8")
    history_dir = tmp_path / "history"
    monkeypatch.setattr(update_portfolio, "HISTORY_DIR", history_dir)
    row = {"code": "600000", "name": "测试", "price": 11.0, "yield": 6.5,
           "pr": 0.8, "sustainability": "可持续", "industry": "银行",
           "bank": True, "dps": 1.0}

    result = update_portfolio.update_one(
        ledger, _verified(), [row], "2026Q1", "2026-04-01", {}, False,
    )
    persisted = json.loads(ledger.read_text(encoding="utf-8"))
    archived = json.loads((history_dir / "2026Q1-relaxed.json").read_text(encoding="utf-8"))
    assert result["last_processed_period"] == "2026Q1"
    assert persisted == archived
    assert persisted["cash"] == pytest.approx(1090.0)
    assert persisted["holdings"]["600000"]["shares"] == 110
    assert persisted["signal_history"][-1]["period"] == "2026Q1"
    assert persisted["model_notice"].startswith("研究用模拟账本")
