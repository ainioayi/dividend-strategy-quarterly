"""固定 manifest 下的参数网格、样本外和滚动窗口审计。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import (
    _compute_metrics,
    _get_monthly_dates,
    run_backtest,
)


def _metrics(result: dict) -> dict:
    return dict(result.get("metrics") or {})


def _window_metrics(nav_series: list[dict], length: int) -> dict:
    windows = []
    for start in range(0, max(len(nav_series) - length + 1, 0)):
        sample = nav_series[start:start + length]
        if len(sample) < length:
            continue
        metric = _compute_metrics(sample, float(sample[0]["nav"]))
        windows.append({
            "start": sample[0]["date"],
            "end": sample[-1]["date"],
            **metric,
        })
    if not windows:
        return {"count": 0}
    return {
        "count": len(windows),
        "min_cagr": min(item["cagr"] for item in windows),
        "median_cagr": sorted(item["cagr"] for item in windows)[len(windows) // 2],
        "max_drawdown_worst": max(item["max_drawdown"] for item in windows),
        "worst_window": min(windows, key=lambda item: item["cagr"]),
    }


def _continuous_metrics(nav_series: list[dict], start: str) -> dict:
    """从完整账本 NAV 切片，样本外窗口不重新初始化持仓。"""
    sample = [item for item in nav_series if str(item.get("date", "")) >= start]
    if len(sample) < 2:
        return {"observations": len(sample)}
    return {
        **_compute_metrics(sample, float(sample[0]["nav"])),
        "observations": len(sample),
        "window_start": sample[0]["date"],
        "window_end": sample[-1]["date"],
    }


def _run_one(name: str, overrides: dict, dates: list[str], manifest: str) -> dict:
    full = run_backtest(
        rules=overrides,
        dynamic_pool=True,
        manifest_path=manifest,
        rebalance_dates=dates,
        verbose=False,
    )
    full_metrics = _metrics(full)
    result = {
        "name": name,
        "rules": overrides,
        "full": full_metrics,
        "rolling36": _window_metrics(full.get("nav_series") or [], 36),
        "rolling48": _window_metrics(full.get("nav_series") or [], 48),
    }
    for label, start in (("oos2021", "2021-01-01"), ("oos2023", "2023-01-01"), ("oos2025", "2025-01-01")):
        result[label] = _continuous_metrics(full.get("nav_series") or [], start)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="运行第 3 轮统一策略实验")
    parser.add_argument("--manifest", default="data/universe_manifest.json")
    parser.add_argument("--dates", default="data/rebalance_dates_monthly.json")
    parser.add_argument("--output", default="data/round3_experiments.json")
    args = parser.parse_args()

    dates_data = json.loads((ROOT / args.dates).read_text(encoding="utf-8"))
    dates = dates_data.get("dates", dates_data) if isinstance(dates_data, dict) else dates_data
    dates_hash = dates_data.get("dates_sha256") if isinstance(dates_data, dict) else None
    base = {
        "entry_yield": 7.5,
        "hold_yield": 5.5,
        "momentum_months": 4,
        "momentum_threshold": 0.95,
        "max_holdings": 2,
        "rebalance_threshold": 2.0,
        "execution_lag_days": 1,
    }
    experiments = [("base", {})]
    for ey in (7.0, 7.2, 7.4, 7.6, 8.0):
        experiments.append((f"ey_{ey:g}", {"entry_yield": ey}))
    for hy in (5.0, 5.5, 6.0):
        experiments.append((f"hy_{hy:g}", {"hold_yield": hy}))
    for mm, mt in ((0, 1.0), (3, 0.93), (3, 0.95), (4, 0.93), (4, 0.98), (5, 0.95), (6, 0.95)):
        experiments.append((f"mom_{mm}_{mt:g}", {"momentum_months": mm, "momentum_threshold": mt}))
    for mh in (1, 3, 4):
        experiments.append((f"mh_{mh}", {"max_holdings": mh}))
    for rt in (1.4, 1.8, 2.5, 3.0):
        experiments.append((f"rt_{rt:g}", {"rebalance_threshold": rt}))

    rows = []
    for index, (name, change) in enumerate(experiments, 1):
        rules = dict(base)
        rules.update(change)
        print(f"[{index}/{len(experiments)}] {name}", flush=True)
        rows.append(_run_one(name, rules, dates, args.manifest))

    payload = {
        "manifest": json.loads((ROOT / args.manifest).read_text(encoding="utf-8")),
        "dates": {
            "count": len(dates),
            "first": min(dates),
            "last": max(dates),
            "sha256": dates_hash,
            "path": args.dates,
        },
        "experiments": rows,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ranked = sorted(rows, key=lambda row: (
        float(row["oos2021"].get("cagr", -999)),
        float(row["rolling36"].get("min_cagr", -999)),
        -float(row["full"].get("max_drawdown", 999)),
    ), reverse=True)
    print(json.dumps({
        "output": str(output),
        "top_by_oos2021_then_worst36": [
            {"name": row["name"], "full": row["full"], "oos2021": row["oos2021"], "rolling36": row["rolling36"]}
            for row in ranked[:10]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
