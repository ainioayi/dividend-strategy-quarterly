"""第 6 轮：当前稳健候选的窄范围压力测试。

只测试已经有理论依据的邻域，并把完整账本切成连续时间块，避免以
末期 NAV 重新开账户造成样本外口径漂移。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import _compute_metrics, run_backtest
from round3_experiments import _window_metrics

DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"

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
    sample = [x for x in nav if x.get("date", "") >= start and (end is None or x.get("date", "") <= end)]
    if len(sample) < 2:
        return {"observations": len(sample)}
    return {
        "observations": len(sample),
        "start": sample[0]["date"],
        "end": sample[-1]["date"],
        **_compute_metrics(sample, float(sample[0]["nav"])),
    }


def _variants() -> list[tuple[str, dict]]:
    result: list[tuple[str, dict]] = [("base", {})]
    for years in (2, 3, 4, 5, 6, 8):
        result.append((f"pool{years}", {"pool_min_consecutive_years": years}))
    for entry in (7.2, 7.4, 7.5, 7.6, 7.8):
        result.append((f"entry{entry:g}", {"entry_yield": entry}))
    for hold in (5.0, 5.5, 6.0):
        result.append((f"hold{hold:g}", {"hold_yield": hold}))
    for months in (2, 3, 5, 6):
        result.append((f"mom{months}", {"momentum_months": months}))
    for lag in (2, 3):
        result.append((f"lag{lag}", {"execution_lag_days": lag}))
    for holdings in (1, 3, 4):
        result.append((f"holdings{holdings}", {"max_holdings": holdings}))
    return result


def main() -> None:
    dates_payload = json.loads(DATES_PATH.read_text(encoding="utf-8"))
    dates = dates_payload["dates"]
    blocks = [
        ("2016_2018", "2016-01-01", "2018-12-31"),
        ("2019_2021", "2019-01-01", "2021-12-31"),
        ("2022_2024", "2022-01-01", "2024-12-31"),
        ("2025_2026", "2025-01-01", None),
    ]
    rows = []
    variants = _variants()
    for index, (name, override) in enumerate(variants, 1):
        rules = dict(BASE)
        rules.update(override)
        print(f"[{index}/{len(variants)}] {name}", flush=True)
        result = run_backtest(
            rules=rules,
            dynamic_pool=True,
            manifest_path=str(MANIFEST_PATH),
            rebalance_dates=dates,
            verbose=False,
        )
        nav = result.get("nav_series") or []
        rows.append({
            "name": name,
            "rules": rules,
            "full": result.get("metrics") or {},
            "rolling36": _window_metrics(nav, 36),
            "rolling48": _window_metrics(nav, 48),
            "blocks": {label: _window(nav, start, end) for label, start, end in blocks},
            "oos_continuous": {
                "2021": _window(nav, "2021-01-01"),
                "2023": _window(nav, "2023-01-01"),
                "2025": _window(nav, "2025-01-01"),
            },
        })
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    output = ROOT / "data" / "round6_robustness.json"
    output.write_text(json.dumps({
        "method": "完整账本连续切片；四个非重叠时间块；不重新初始化 OOS 账户",
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_payload.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ranked = sorted(rows, key=lambda row: (
        float(row["full"].get("cagr", -999)),
        float(row["rolling36"].get("min_cagr", -999)),
        float(row["full"].get("sharpe", -999)),
    ), reverse=True)
    print(json.dumps([{
        "name": row["name"],
        "cagr": row["full"].get("cagr"),
        "dd": row["full"].get("max_drawdown"),
        "r36": row["rolling36"].get("min_cagr"),
        "blocks": {k: v.get("cagr") for k, v in row["blocks"].items()},
    } for row in ranked[:12]], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
