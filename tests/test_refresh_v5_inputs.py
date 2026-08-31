import hashlib
import json
from urllib.error import URLError

import pytest

import refresh_v5_inputs as refresh
from refresh_v5_inputs import build_v5_inputs, load_strategy_nav
from v5_strategy import build_forward_execution, build_forward_signal, run_frozen_backtest


def _source():
    return {
        "adjustment_factors": [{"code": "600001", "date": "1900-01-01", "factor": 2}],
        "fundamentals": [{"code": "600001", "year": 2025, "eps": 1,
                           "published_date": "2026-04-01"}],
        "industries": [{"code": "600001", "industry": "制造业",
                         "published_date": "2025-12-31"}],
        "h00922": [{"date": "2026-08-25", "close": 1000}],
        "strategy_nav": [{"date": f"2026-06-{index + 1:02d}", "nav": 100000 + index}
                         for index in range(30)] +
                        [{"date": f"2026-07-{index + 1:02d}", "nav": 100030 + index}
                         for index in range(21)],
    }


def test_build_v5_inputs_records_hashes_and_attachments(tmp_path):
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"report")
    result = build_v5_inputs(_source(), "2026-08-25", [attachment])
    assert result["price_format"] == "sina_qfq_factors_with_unadjusted_cache"
    assert result["attachments"] == [{"name": "report.pdf",
                                      "sha256": hashlib.sha256(b"report").hexdigest()}]
    assert len(result["content_sha256"]) == 64
    json.dumps(result, ensure_ascii=False)


def test_strategy_nav合并回测与前向并统一十万元量级(tmp_path):
    backtest = tmp_path / "round32.json"
    backtest.write_text(json.dumps({"initial_capital": 1_000_000, "nav_series": [
        {"date": "2026-08-24", "nav": 1_000_000},
        {"date": "2026-08-25", "nav": 1_100_000},
    ]}),
                        encoding="utf-8")
    performance = tmp_path / "performance.json"
    performance.write_text(json.dumps({"series": [
        {"date": "2026-08-25", "v5_nav": 109_000},
        {"date": "2026-08-26", "v5_nav": 111_000},
    ]}), encoding="utf-8")
    assert load_strategy_nav([backtest, performance], "2026-08-26") == [
        {"date": "2026-08-24", "nav": pytest.approx(99_090.9090909)},
        {"date": "2026-08-25", "nav": 109_000.0},
        {"date": "2026-08-26", "nav": 111_000.0},
    ]


def test新浪年度EPS按报告级发布日期解析(monkeypatch):
    monkeypatch.setattr(refresh, "_get_json", lambda *_args, **_kwargs: {
        "result": {"data": {"report_list": {
            "20251231": {"publish_date": "20260430", "data": [
                {"item_title": "基本每股收益", "item_value": "1.25"},
            ]},
            "20260331": {"publish_date": "20260430", "data": [
                {"item_title": "基本每股收益", "item_value": "0.31"},
            ]},
        }}}
    })
    assert refresh.fetch_sina_fundamentals("600001", "2026-08-30") == [{
        "code": "600001", "year": 2025, "eps": 1.25,
        "published_date": "2026-04-30", "source_url": refresh.SINA_FINANCE_URL,
    }]


def test中证基础日期统一为ISO格式(monkeypatch):
    monkeypatch.setattr(refresh, "_get_json", lambda *_args, **_kwargs: {
        "data": [{"tradeDate": "20260825", "close": 1234.5}],
    })
    assert refresh.fetch_h00922("2026-08-01", "2026-08-25")[0]["date"] == "2026-08-25"


def test网络瞬时失败会重试(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok": true}'

    calls = []

    def urlopen(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise URLError("temporary timeout")
        return Response()

    monkeypatch.setattr(refresh.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)

    assert refresh._get_json("https://example.test", {}) == {"ok": True}
    assert len(calls) == 2


def test联网采集不遗漏科创板和北交所(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"codes": ["688016", "920982"]}), encoding="utf-8")
    industries = tmp_path / "industries.json"
    industries.write_text(json.dumps({"records": [
        {"code": "688016", "industry": "制造业", "published_date": "2025-12-31"},
        {"code": "920982", "industry": "制造业", "published_date": "2025-12-31"},
    ]}), encoding="utf-8")
    for code in ("688016", "920982"):
        (tmp_path / f"dv_{code}.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(refresh, "fetch_sina_adjust_factors", lambda _code: [
        {"date": "1900-01-01", "factor": 1, "source_url": "sina"},
    ])
    monkeypatch.setattr(refresh, "fetch_sina_fundamentals", lambda *_args: [])
    monkeypatch.setattr(refresh, "fetch_h00922", lambda *_args: [
        {"date": "2026-08-31", "close": 1000, "source_url": "csindex"},
    ])

    result = refresh.collect_from_manifest(
        manifest, "2026-08-31", industries, cache_dir=tmp_path,
    )

    assert {row["code"] for row in result["adjustment_factors"]} == {"688016", "920982"}
    assert refresh._sina_symbol("688016") == "sh688016"
    assert refresh._sina_symbol("920982") == "bj920982"


def test_build_v5_inputs_rejects_future_information():
    source = _source()
    source["fundamentals"][0]["published_date"] = "2026-08-26"
    with pytest.raises(ValueError, match="截止日之后"):
        build_v5_inputs(source, "2026-08-25")


def test_execution_without_exact_close_does_not_trade(tmp_path):
    from build_historical_universe import write_json_atomic
    artifact = build_v5_inputs(_source(), "2026-08-25")
    input_path = tmp_path / "v5.json"
    write_json_atomic(input_path, artifact)
    journal = [{"event_type": "signal", "strategy_id": "v5", "period": "2026-08",
                "signal_date": "2026-08-25", "target_codes": ["600001"],
                "risk_multiplier": 1.0, "rebalance_band": 0.2,
                "new_buy_budget_multiplier": 1.0}]
    result = build_forward_execution("2026-08", "2026-08-26", tmp_path, journal, input_path)
    assert result["holdings"] == []
    assert result["cash"] == 100000
    assert not any(row.get("side") == "买入" for row in result["operations"])


def test_signal_and_execution_form_complete_forward_event(tmp_path):
    from datetime import date, timedelta
    from build_historical_universe import write_json_atomic
    days = [(date(2025, 8, 1) + timedelta(days=index)).isoformat() for index in range(390)]
    source = {
        "adjustment_factors": [{"code": "600001", "date": "1900-01-01", "factor": 2}],
        "fundamentals": [{"code": "600001", "year": year, "eps": 2, "dps": 1,
                           "published_date": f"{year + 1}-04-01"}
                         for year in (2022, 2023, 2024, 2025)],
        "industries": [{"code": "600001", "industry": "制造业",
                         "published_date": "2025-12-31"}],
        "h00922": [{"date": day, "close": 1000 + index}
                    for index, day in enumerate(days) if day <= "2026-08-25"],
        "strategy_nav": [{"date": day, "nav": 100000 + index}
                         for index, day in enumerate(days[-60:])],
    }
    input_path = tmp_path / "v5.json"
    write_json_atomic(input_path, build_v5_inputs(source, "2026-08-25"))
    manifest = tmp_path / "manifest.json"
    dates_path = tmp_path / "dates.json"
    write_json_atomic(manifest, {"as_of": "2026-08-25", "codes": ["600001"]})
    write_json_atomic(dates_path, {"dates": ["2026-08-25"]})
    write_json_atomic(tmp_path / "kl_600001.json", {
        day: 10 + index / 1000 for index, day in enumerate(days) if day <= "2026-08-25"
    })
    signal = build_forward_signal("2026-08-25", manifest, dates_path, tmp_path, [], input_path)
    assert signal["candidate_pool"]["count"] == 1
    assert signal["decision_snapshot"]["eligible_entry_codes"] == ["600001"]
    assert signal["candidates"][0]["yield"] == pytest.approx(1 / (10 + 389 / 1000) * 100)
    write_json_atomic(tmp_path / "kl_600001.json", {"2026-08-26": 10.4})
    execution = build_forward_execution("2026-08", "2026-08-26", tmp_path, [signal], input_path)
    assert execution["holdings"][0]["shares"] % 100 == 0
    assert execution["cash"] + execution["holdings"][0]["shares"] * 10.4 == pytest.approx(execution["nav"])
    buy = next(row for row in execution["operations"] if row["side"] == "买入")
    assert buy["fees"]["total"] > 0


def test_frozen_backtest_outputs_metrics_and_cost_stress(tmp_path):
    from datetime import date, timedelta
    from build_historical_universe import write_json_atomic
    days = [(date(2025, 1, 1) + timedelta(days=index)).isoformat() for index in range(500)]
    prices = {day: 10 + index / 1000 for index, day in enumerate(days)}
    source = {
        "adjustment_factors": [{"code": "600001", "date": "1900-01-01", "factor": 1}],
        "fundamentals": [{"code": "600001", "year": year, "eps": 2, "dps": 1,
                           "published_date": f"{year + 1}-01-01"}
                         for year in (2022, 2023, 2024, 2025)],
        "industries": [{"code": "600001", "industry": "制造业",
                         "published_date": "2024-01-01"}],
        "h00922": [{"date": day, "close": 1000 + index}
                    for index, day in enumerate(days)],
        "strategy_nav": [],
    }
    input_path = tmp_path / "input.json"
    write_json_atomic(input_path, build_v5_inputs(source, days[-1]))
    write_json_atomic(tmp_path / "kl_600001.json", prices)
    write_json_atomic(tmp_path / "dvd_600001.json", [
        {"year": 2025, "ex_date": "2026-02-15", "dps": 0.1,
         "bonus_ratio": 1.0, "transfer_ratio": 0.0}
    ])
    result = run_frozen_backtest(input_path,
                                  ["2026-01-31", "2026-02-28", "2026-03-31"],
                                  tmp_path)
    assert result["initial_capital"] == 1_000_000
    assert result["metrics"]["trade_count"] >= 1
    assert result["metrics"]["cagr"] is not None
    assert result["high_cost_metrics"]["cagr"] is not None
    assert len(result["nav_series"]) > len(result["events"])
    operations = [operation for event in result["events"] for operation in event.get("operations", [])]
    assert sum(operation.get("side") == "分红" for operation in operations) == 1
    assert sum(operation.get("side") == "送转" for operation in operations) == 1
