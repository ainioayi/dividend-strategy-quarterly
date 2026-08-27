from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import round30_fragility_audit as audit


def test_industry_proxy_covers_each_round29_traded_stock_once():
    _, _, attribution, _ = audit._load_inputs()
    traded = {str(item["code"]).zfill(6) for item in attribution["per_stock_pl"]}
    mapped = [code for values in audit.CURRENT_INDUSTRY_PROXY.values() for code in values]
    assert len(mapped) == len(set(mapped))
    assert set(mapped) == traded


def test_v1_inputs_and_parameters_are_frozen():
    manifest, dates_payload, attribution, dates = audit._load_inputs()
    assert manifest["records_sha256"] == audit.EXPECTED_MANIFEST_HASH
    assert dates_payload["dates_sha256"] == audit.EXPECTED_DATES_HASH
    assert attribution["manifest_records_sha256"] == audit.EXPECTED_MANIFEST_HASH
    assert dates[-1] == audit.EXPECTED_CUTOFF
    assert audit.V1_RULES["entry_yield"] == 7.5
    assert audit.V1_RULES["hold_yield"] == 5.5
    assert audit.V1_RULES["momentum_months"] == 4
    assert audit.V1_RULES["momentum_threshold"] == 0.85
    assert audit.V1_RULES["max_holdings"] == 2


def test_tradeable_benchmark_input_is_frozen_and_hash_verified():
    payload = audit._load_benchmark()
    assert payload["symbol"] == "510880"
    assert payload["as_of"] == audit.EXPECTED_CUTOFF
    assert payload["hashes"]["prices_sha256"] == audit._canonical_sha256(payload["prices"])
    assert payload["hashes"]["dividends_sha256"] == audit._canonical_sha256(payload["dividends"])


def test_metric_differences_use_candidate_minus_baseline():
    baseline = {
        "cagr": 40, "max_drawdown": 20, "sharpe": 1.2,
        "ending_nav": 200000, "trade_count": 10,
    }
    candidate = {
        "cagr": 35, "max_drawdown": 25, "sharpe": 1.0,
        "ending_nav": 180000, "trade_count": 12,
    }
    diff = audit._metric_differences(candidate, baseline)
    assert diff == {
        "cagr": -5.0,
        "max_drawdown": 5.0,
        "sharpe": -0.2,
        "ending_nav": -20000.0,
        "trade_count": 2.0,
    }


def test_generated_result_contains_every_required_audit_dimension():
    output = __import__("json").loads(audit.OUTPUT_PATH.read_text(encoding="utf-8"))
    assert output["inputs"]["manifest_records_sha256"] == audit.EXPECTED_MANIFEST_HASH
    assert output["inputs"]["dates_sha256"] == audit.EXPECTED_DATES_HASH
    assert output["inputs"]["top_profit_codes_from_round29"] == [
        "601088", "600295", "603688",
    ]
    expected_names = {"baseline", "exclude_top1", "exclude_top3"} | {
        f"exclude_industry_{name}" for name in audit.CURRENT_INDUSTRY_PROXY
    }
    assert {row["name"] for row in output["variants"]} == expected_names
    benchmark = output["tradeable_total_return_benchmark"]
    assert benchmark["symbol"] == "510880"
    assert benchmark["as_of"] == audit.EXPECTED_CUTOFF
    assert benchmark["total_dividend_cash"] > 0
    assert benchmark["dividend_event_count"] > 0
    assert benchmark["rolling36"]["count"] > 0
    assert benchmark["rolling48"]["count"] > 0
    assert set(benchmark["continuous_oos"]) == {"2021", "2023", "2025"}
    assert "difference_vs_v1_baseline" in benchmark
    assert "dividend_reinvestment" in benchmark["method"]
    for row in output["variants"]:
        for cost_key in ("normal_cost", "triple_cost"):
            result = row[cost_key]
            assert "trade_count" in result["metrics"]
            assert result["rolling36"]["count"] > 0
            assert result["rolling48"]["count"] > 0
            assert set(result["continuous_oos"]) == {"2021", "2023", "2025"}
        assert "difference_vs_baseline" in row
