"""验证第 31 轮持仓上限扫描产物。"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "data" / "round31_holdings_sweep.json"
MANIFEST_SHA256 = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"
DATES_SHA256 = "f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e"


def _load() -> dict:
    return json.loads(RESULT_PATH.read_text(encoding="utf-8"))


def test_round31使用冻结输入且仅改变持仓上限() -> None:
    payload = _load()
    assert payload["round"] == 31
    assert payload["manifest_records_sha256"] == MANIFEST_SHA256
    assert payload["dates_sha256"] == DATES_SHA256
    assert payload["data_cutoff"] == "2026-08-25"
    assert payload["audit"]["only_changed_rule"] == "max_holdings"

    experiments = payload["experiments"]
    assert [item["max_holdings"] for item in experiments] == list(range(2, 11))
    base_rules = payload["base_rules"]
    for item in experiments:
        changed = {
            key
            for key, value in item["rules"].items()
            if value != base_rules.get(key)
        }
        expected = set() if item["max_holdings"] == 2 else {"max_holdings"}
        assert changed == expected


def test_round31基线与稳健性检查完整() -> None:
    payload = _load()
    by_limit = {
        item["max_holdings"]: item
        for item in payload["experiments"]
    }
    baseline = by_limit[2]["metrics"]
    assert baseline["cagr"] == 41.38
    assert baseline["max_drawdown"] == 28.06
    assert baseline["sharpe"] == 1.217
    assert baseline["trade_count"] == 75

    assert len(payload["cost_stress"]) == 9
    assert len(payload["reset_windows"]) == 27
    assert all(item["warmup_count"] == 4 for item in payload["reset_windows"])
    assert {
        item["start"] for item in payload["reset_windows"]
    } == {"2018-01-01", "2020-01-01", "2022-01-01"}


def test_round31汇总表与明细一致() -> None:
    payload = _load()
    experiments = {
        item["max_holdings"]: item
        for item in payload["experiments"]
    }
    stress = {
        item["max_holdings"]: item
        for item in payload["cost_stress"]
    }
    assert len(payload["summary"]) == 9
    for row in payload["summary"]:
        limit = row["max_holdings"]
        detail = experiments[limit]
        assert row["cagr"] == detail["metrics"]["cagr"]
        assert row["max_drawdown"] == detail["metrics"]["max_drawdown"]
        assert row["rolling36_worst_cagr"] == detail["rolling36"]["min_cagr"]
        assert row["rolling48_worst_cagr"] == detail["rolling48"]["min_cagr"]
        assert row["oos_2023_cagr"] == detail["oos"]["2023"]["cagr"]
        assert row["triple_cost_cagr"] == stress[limit]["metrics"]["cagr"]

    assert max(payload["summary"], key=lambda row: row["cagr"])["max_holdings"] == 2
    assert max(payload["summary"], key=lambda row: row["triple_cost_cagr"])["max_holdings"] == 3
