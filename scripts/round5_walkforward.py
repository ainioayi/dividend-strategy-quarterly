"""第 5 轮：在完整账本上计算连续样本外和滚动窗口指标。

样本外窗口不重新初始化现金或持仓，而是从完整回测的 NAV 序列切片。
这样窗口只评价当时已经运行的策略，避免把 warm-up 和初始建仓差异混入比较。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import _compute_metrics, run_backtest
from round3_experiments import _window_metrics


BASE_RULES = {
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "pool_min_consecutive_years": 3,
    "momentum_months": 4,
}


def _continuous_window(nav_series: list[dict], start: str) -> dict:
    sample = [item for item in nav_series if str(item.get("date", "")) >= start]
    if len(sample) < 2:
        return {"count": len(sample), "start": sample[0]["date"] if sample else None}
    return {
        "count": len(sample),
        "start": sample[0]["date"],
        "end": sample[-1]["date"],
        "initial_nav": sample[0]["nav"],
        **_compute_metrics(sample, float(sample[0]["nav"])),
    }


def run_one(name: str, overrides: dict, dates: list[str], manifest: str) -> dict:
    rules = dict(BASE_RULES)
    rules.update(overrides)
    result = run_backtest(
        rules=rules,
        dynamic_pool=True,
        manifest_path=manifest,
        rebalance_dates=dates,
        verbose=False,
    )
    nav = result.get("nav_series") or []
    return {
        "name": name,
        "rules": rules,
        "full": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos_continuous": {
            "2021": _continuous_window(nav, "2021-01-01"),
            "2023": _continuous_window(nav, "2023-01-01"),
            "2025": _continuous_window(nav, "2025-01-01"),
        },
    }


def main() -> None:
    manifest_path = ROOT / "data" / "universe_manifest.json"
    dates_path = ROOT / "data" / "rebalance_dates_monthly.json"
    dates_payload = json.loads(dates_path.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload)
    variants: list[tuple[str, dict]] = []
    for reserve in (0, 3000, 4000, 5000, 6000):
        for threshold in (0.84, 0.85, 0.86, 0.88, 0.90, 0.92, 0.95):
            variants.append((
                f"r{reserve}_m{threshold:g}",
                {"reinvest_cash_reserve": reserve, "momentum_threshold": threshold},
            ))
    # 2 个月动量是上一轮低回撤候选，加入同一口径对照。
    variants.extend([
        ("r0_m2_0.95", {"momentum_months": 2, "momentum_threshold": 0.95}),
        ("r5000_m2_0.95", {"momentum_months": 2, "momentum_threshold": 0.95,
                            "reinvest_cash_reserve": 5000}),
    ])
    rows = []
    for index, (name, overrides) in enumerate(variants, 1):
        print(f"[{index}/{len(variants)}] {name}", flush=True)
        rows.append(run_one(name, overrides, dates, str(manifest_path)))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = {
        "method": "完整账本运行后按 NAV 序列切片，窗口不重新初始化",
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_payload.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": rows,
    }
    output = ROOT / "data" / "round5_walkforward.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ranked = sorted(rows, key=lambda row: (
        float(row["full"].get("cagr", -999)),
        float(row["rolling36"].get("min_cagr", -999)),
        float(row["full"].get("sharpe", -999)),
    ), reverse=True)
    print(json.dumps([
        {
            "name": row["name"],
            "cagr": row["full"].get("cagr"),
            "max_drawdown": row["full"].get("max_drawdown"),
            "rolling36_min": row["rolling36"].get("min_cagr"),
            "oos2021": row["oos_continuous"]["2021"].get("cagr"),
            "oos2023": row["oos_continuous"]["2023"].get("cagr"),
            "oos2025": row["oos_continuous"]["2025"].get("cagr"),
        }
        for row in ranked[:12]
    ], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
