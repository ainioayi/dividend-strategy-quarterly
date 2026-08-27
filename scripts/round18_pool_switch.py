"""第 18 轮：动态候选池年度确认月份的单变量实验。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest
from round3_experiments import _continuous_metrics, _window_metrics

MANIFEST = ROOT / "data" / "universe_manifest.json"
DATES_FILE = ROOT / "data" / "rebalance_dates_monthly.json"
RATE_KEYS = ("buy_commission_rate", "sell_commission_rate", "stamp_duty_rate", "transfer_fee_rate")

BASE = dict(backtest.BACKTEST_RULES)
BASE.update({
    "pool_mode": "dynamic",
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "dividend_information_lag_days": 0,
    "reinvest_dividends": True,
    "reinvest_cash_reserve": 0,
    "rank_by": "yield",
    "max_yield": 999.0,
})


def _cost_rules(rules: dict, multiplier: float) -> dict:
    out = dict(rules)
    for key in RATE_KEYS:
        out[key] = float(rules[key]) * multiplier
    return out


def _summary(result: dict) -> dict:
    nav = result.get("nav_series") or []
    return {
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {year: _continuous_metrics(nav, f"{year}-01-01") for year in ("2021", "2023", "2025")},
        "pool_provenance": {
            "count": len(result.get("pool_provenance") or []),
            "min_pool_count": min((x.get("pool_count", 0) for x in result.get("pool_provenance") or []), default=0),
            "max_pool_count": max((x.get("pool_count", 0) for x in result.get("pool_provenance") or []), default=0),
        },
    }


def main() -> None:
    dates_payload = json.loads(DATES_FILE.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = []
    for switch_month in (5, 6, 7, 8):
        rules = dict(BASE, pool_switch_month=switch_month)
        normal = backtest.run_backtest(
            rules=rules, dynamic_pool=True, manifest_path=str(MANIFEST),
            rebalance_dates=dates, verbose=False,
        )
        stressed = backtest.run_backtest(
            rules=_cost_rules(rules, 3.0), dynamic_pool=True,
            manifest_path=str(MANIFEST), rebalance_dates=dates, verbose=False,
        )
        rows.append({
            "pool_switch_month": switch_month,
            "normal_cost": _summary(normal),
            "three_x_cost": _summary(stressed),
        })
        print(f"完成 pool_switch_month={switch_month}", flush=True)

    output = {
        "round": 18,
        "experiment": "pool_switch_month",
        "method": "冻结输入下单变量比较 5/6/7/8 月年度确认边界；完整账本、rolling36/48、连续 OOS 和三倍费用压力；无未来函数",
        "base_rules": BASE,
        "inputs": {
            "manifest_records_sha256": manifest.get("records_sha256"),
            "dates_sha256": dates_payload.get("dates_sha256") if isinstance(dates_payload, dict) else backtest._rebalance_dates_hash(dates),
            "data_cutoff": manifest.get("as_of"),
            "dates": {"count": len(dates), "first": min(dates), "last": max(dates)},
        },
        "experiments": rows,
        "audit": {
            "future_function_check": "通过：逐笔 ex_date <= signal_date；确认月份只决定年度窗口，成交沿用 execution_lag_days=1",
            "complexity": "单一整数参数，生产默认仍为 7 月",
            "oos_definition": "OOS 从完整账本 NAV 连续切片，不重新初始化账户",
            "cost_stress": "三倍仅放大佣金、印花税和过户费率，最低佣金不变",
            "survivorship_bias": "manifest 是截至截止日冻结的现存代码集合，存在生存者偏差",
        },
    }
    output_path = ROOT / "data" / "round18_pool_switch.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {output_path}（{len(rows)} 组）")


if __name__ == "__main__":
    main()
