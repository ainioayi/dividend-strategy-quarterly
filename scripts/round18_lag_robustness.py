"""第 18 轮：信息延迟下的窄参数稳健性实验。

只比较预先指定的少量邻域参数，并同时记录正常费用和三倍费用，避免把
单一全样本峰值误当成稳健改进。每次回测仍按信号日截断分红明细，执行
滞后固定为 1 个交易日。
"""
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

# 只测试已经在第 17 轮附近出现过的简单邻域，不扩张成大网格。
VARIANTS = (
    ("baseline", {}),
    ("hold56", {"hold_yield": 5.6}),
    ("momentum084", {"momentum_threshold": 0.84}),
    ("entry74", {"entry_yield": 7.4}),
)


def _cost_rules(rules: dict, multiplier: float) -> dict:
    out = dict(rules)
    for key in RATE_KEYS:
        out[key] = float(rules[key]) * multiplier
    return out


def _case_metrics(result: dict) -> dict:
    nav = result.get("nav_series") or []
    return {
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {
            year: _continuous_metrics(nav, f"{year}-01-01")
            for year in ("2021", "2023", "2025")
        },
    }


def main() -> None:
    dates_payload = json.loads(DATES_FILE.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = []
    for lag in (0, 30, 60):
        for name, change in VARIANTS:
            rules = dict(BASE, **change, dividend_information_lag_days=lag)
            normal = backtest.run_backtest(
                rules=rules, dynamic_pool=True, manifest_path=str(MANIFEST),
                rebalance_dates=dates, verbose=False,
            )
            stressed = backtest.run_backtest(
                rules=_cost_rules(rules, 3.0), dynamic_pool=True,
                manifest_path=str(MANIFEST), rebalance_dates=dates, verbose=False,
            )
            rows.append({
                "lag_days": lag,
                "variant": name,
                "rules": rules,
                "normal_cost": _case_metrics(normal),
                "three_x_cost": _case_metrics(stressed),
            })
            print(f"完成 lag={lag} variant={name}", flush=True)

    out = {
        "round": 18,
        "experiment": "lag_robustness",
        "method": "预注册 3 个信息延迟档位 x 4 个简单邻域；完整账本、rolling36/48、连续 OOS 和三倍费用压力；无未来函数",
        "inputs": {
            "manifest_records_sha256": manifest.get("records_sha256"),
            "dates_sha256": dates_payload.get("dates_sha256") if isinstance(dates_payload, dict) else backtest._rebalance_dates_hash(dates),
            "data_cutoff": manifest.get("as_of"),
            "dates": {"count": len(dates), "first": min(dates), "last": max(dates)},
        },
        "base_rules": BASE,
        "variants": [{"name": name, "change": change} for name, change in VARIANTS],
        "experiments": rows,
        "audit": {
            "future_function_check": "通过：动态池和信号明细均按 ex_date <= signal_date - lag_days 过滤，成交沿用 execution_lag_days=1",
            "cost_stress": "三倍只放大佣金、印花税和过户费率，最低佣金不变",
            "information_limit": "缓存没有公告日/登记日，lag_days 是对 ex_date 的保守代理，不是实际发布延迟",
            "oos_definition": "OOS 从完整账本 NAV 连续切片，不重新初始化账户",
            "survivorship_bias": "manifest 是截至截止日的冻结现存代码集合，存在生存者偏差",
        },
    }
    output = ROOT / "data" / "round18_lag_robustness.json"
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {output}（{len(rows)} 组）")


if __name__ == "__main__":
    main()
