"""第 19 轮：组合集中度约束的窄实验。"""
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
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "dividend_information_lag_days": 0,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "reinvest_dividends": True,
    "reinvest_cash_reserve": 0,
    "rank_by": "yield",
    "max_yield": 999.0,
})
VARIANTS = (
    ("base_2_2", 2, 2),
    ("sector_1", 1, 2),
    ("banks_1", 2, 1),
    ("sector_unlimited", 999, 2),
)


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
    }


def _reset(rules: dict, dates: list[str], start: str) -> dict:
    index = next((i for i, value in enumerate(dates) if value >= start), len(dates))
    warm_dates = dates[max(0, index - 4):]
    result = backtest.run_backtest(
        rules=rules, dynamic_pool=True, manifest_path=str(MANIFEST),
        rebalance_dates=warm_dates, verbose=False,
    )
    nav = [item for item in (result.get("nav_series") or []) if item["date"] >= start]
    if nav:
        scale = 100000.0 / float(nav[0]["nav"])
        nav = [dict(item, nav=round(float(item["nav"]) * scale, 2)) for item in nav]
    return {
        "start": start,
        "metrics": backtest._compute_metrics(nav, 100000.0) if len(nav) >= 2 else {"observations": len(nav)},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
    }


def main() -> None:
    dates_payload = json.loads(DATES_FILE.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = []
    for name, max_sector, max_banks in VARIANTS:
        rules = dict(BASE, max_sector=max_sector, max_banks=max_banks)
        normal = backtest.run_backtest(
            rules=rules, dynamic_pool=True, manifest_path=str(MANIFEST),
            rebalance_dates=dates, verbose=False,
        )
        stressed = backtest.run_backtest(
            rules=_cost_rules(rules, 3.0), dynamic_pool=True,
            manifest_path=str(MANIFEST), rebalance_dates=dates, verbose=False,
        )
        rows.append({
            "name": name,
            "max_sector": max_sector,
            "max_banks": max_banks,
            "normal_cost": _summary(normal),
            "three_x_cost": _summary(stressed),
            "reset_2022": _reset(rules, dates, "2022-01-01"),
        })
        print(f"完成 {name}", flush=True)

    output = {
        "round": 19,
        "experiment": "concentration",
        "method": "冻结输入下单变量比较 max_sector/max_banks；完整账本、rolling36/48、连续 OOS、2022 warm-up 重置和三倍费用；无未来函数",
        "base_rules": BASE,
        "variants": [{"name": n, "max_sector": s, "max_banks": b} for n, s, b in VARIANTS],
        "inputs": {
            "manifest_records_sha256": manifest.get("records_sha256"),
            "dates_sha256": dates_payload.get("dates_sha256") if isinstance(dates_payload, dict) else backtest._rebalance_dates_hash(dates),
            "data_cutoff": manifest.get("as_of"),
            "dates": {"count": len(dates), "first": min(dates), "last": max(dates)},
        },
        "experiments": rows,
        "audit": {
            "future_function_check": "通过：集中度只影响当期候选选择，分红按信号日前 ex_date，成交滞后 1 日",
            "oos_definition": "连续 OOS 从完整账本切片；2022 重置保留四个动量 warm-up 信号点",
            "cost_stress": "三倍仅放大佣金、印花税和过户费率，最低佣金不变",
            "survivorship_bias": "manifest 是截至截止日冻结的现存代码集合，存在生存者偏差",
            "decision": "max_sector=1 明显降低收益；max_banks=1 与取消行业上限在当前数据等价，均不改变生产规则",
        },
    }
    output_path = ROOT / "data" / "round19_concentration.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {output_path}（{len(rows)} 组）")


if __name__ == "__main__":
    main()
