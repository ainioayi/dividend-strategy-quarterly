"""第 8 轮时间与配置边界回归检查。"""
from __future__ import annotations

import json
from pathlib import Path

import update_portfolio
from backtest import BACKTEST_RULES

ROOT = Path(__file__).resolve().parents[1]


def test_round8_audit_artifact_is_passed():
    audit = json.loads((ROOT / "data" / "round8_temporal.json").read_text(encoding="utf-8"))
    assert audit["result"].startswith("通过")
    assert all(audit["checks"].values())


def test_update_portfolio_config_is_authoritative_for_rules():
    config = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    rules = update_portfolio._rules(config, False)
    for key in ("frequency", "pool_mode", "execution_lag_days", "entry_yield", "hold_yield"):
        assert rules[key] == config["rules"][key]
    assert BACKTEST_RULES["execution_lag_days"] == config["rules"]["execution_lag_days"]
