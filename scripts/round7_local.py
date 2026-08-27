"""第 7 轮：当前策略邻域的局部参数实验。

所有变体从同一份显式基线出发；完整 NAV 序列生成后再切连续 OOS，
不以窗口末端净值重新开户，也不依赖其它实验脚本的隐藏默认值。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import _compute_metrics, run_backtest
from round3_experiments import _window_metrics


MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"

BASE = {
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "pool_min_consecutive_years": 3,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "reinvest_cash_reserve": 0,
}


def _window(nav: list[dict], start: str, end: str | None = None) -> dict:
    sample = [
        item for item in nav
        if str(item.get("date", "")) >= start
        and (end is None or str(item.get("date", "")) <= end)
    ]
    if len(sample) < 2:
        return {"observations": len(sample)}
    return {
        "observations": len(sample),
        "start": sample[0]["date"],
        "end": sample[-1]["date"],
        **_compute_metrics(sample, float(sample[0]["nav"])),
    }


def _run_one(name: str, overrides: dict, dates: list[str]) -> dict:
    rules = dict(BASE)
    rules.update(overrides)
    result = run_backtest(
        rules=rules,
        dynamic_pool=True,
        manifest_path=str(MANIFEST_PATH),
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
        "blocks": {
            "2016_2018": _window(nav, "2016-01-01", "2018-12-31"),
            "2019_2021": _window(nav, "2019-01-01", "2021-12-31"),
            "2022_2024": _window(nav, "2022-01-01", "2024-12-31"),
            "2025_2026": _window(nav, "2025-01-01"),
        },
        "oos_continuous": {
            "2021": _window(nav, "2021-01-01"),
            "2023": _window(nav, "2023-01-01"),
            "2025": _window(nav, "2025-01-01"),
        },
    }


def _variants() -> list[tuple[str, dict]]:
    variants: list[tuple[str, dict]] = []
    for threshold in (0.82, 0.83, 0.84, 0.85, 0.86, 0.87, 0.88):
        variants.append((f"momentum_{threshold:g}", {"momentum_threshold": threshold}))
    for entry in (7.3, 7.4, 7.5, 7.6, 7.7):
        variants.append((f"entry_{entry:g}", {"entry_yield": entry}))
    for threshold in (0.84, 0.86, 0.88):
        for entry in (7.4, 7.5, 7.6):
            variants.append((
                f"cross_m{threshold:g}_e{entry:g}",
                {"momentum_threshold": threshold, "entry_yield": entry},
            ))
    return variants


def main() -> None:
    dates_payload = json.loads(DATES_PATH.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    variants = _variants()
    rows = []
    for index, (name, overrides) in enumerate(variants, 1):
        print(f"[{index}/{len(variants)}] {name}", flush=True)
        rows.append(_run_one(name, overrides, dates))

    payload = {
        "round": 7,
        "method": "显式当前基线；完整账本连续切片 OOS；不重新初始化账户",
        "base_rules": BASE,
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_payload.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": rows,
    }
    output = ROOT / "data" / "round7_local.json"
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
            "sharpe": row["full"].get("sharpe"),
            "rolling36_min": row["rolling36"].get("min_cagr"),
            "oos2021": row["oos_continuous"]["2021"].get("cagr"),
            "oos2023": row["oos_continuous"]["2023"].get("cagr"),
            "oos2025": row["oos_continuous"]["2025"].get("cagr"),
        }
        for row in ranked[:12]
    ], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
