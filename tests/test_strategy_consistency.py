"""主策略配置与回测默认值的一致性测试。"""
from __future__ import annotations

import json
from pathlib import Path

from backtest import BACKTEST_RULES, _dynamic_pool_enabled
from optimized_strategy import DEFAULT_RULES as LIVE_RULES
from quarterly_strategy import DEFAULT_QUARTERLY_RULES


ROOT = Path(__file__).resolve().parents[1]


def test_backtest_defaults_match_authoritative_config():
    config = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    rules = config["rules"]
    keys = (
        "entry_yield",
        "hold_yield",
        "max_holdings",
        "rebalance_threshold",
        "execution_lag_days",
        "dividend_information_lag_days",
        "pool_mode",
        "pool_min_consecutive_years",
        "momentum_months",
        "momentum_threshold",
        "reinvest_cash_reserve",
        "stop_loss_pct",
        "frequency",
    )
    for key in keys:
        assert BACKTEST_RULES[key] == rules[key], key


def test_dynamic_pool_default_follows_rules_but_explicit_override_wins():
    """未指定模式时跟随配置，显式 True/False 仍可用于对照实验。"""
    assert _dynamic_pool_enabled({"pool_mode": "dynamic"}, None) is True
    assert _dynamic_pool_enabled({"pool_mode": "curated"}, None) is False
    assert _dynamic_pool_enabled({"pool_mode": "dynamic"}, False) is False
    assert _dynamic_pool_enabled({"pool_mode": "curated"}, True) is True


def test_pr_rules_explicitly_keep_backtest_and_live_layers_separate():
    """历史纯股息率回测与实时季度筛选的 PR 口径必须保持可辨识。"""
    assert BACKTEST_RULES["entry_pr"] == 999.0
    assert BACKTEST_RULES["hold_pr"] == 999.0
    assert BACKTEST_RULES["exit_pr"] == 999.0
    assert LIVE_RULES["pr_ceiling"] == 1.2
    assert DEFAULT_QUARTERLY_RULES["hold_pr"] == 1.2
    assert DEFAULT_QUARTERLY_RULES["exit_pr"] == 1.5
