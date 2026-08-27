"""第13轮：极端高息上限的窄规则族实验。

上限仅作用于当期信号的股息率，不读取未来价格或未来分红；所有变体
共享冻结 manifest、日期和本地回测缓存，便于复现。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"
BASE = {
    "entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
    "rebalance_threshold": 2.0, "execution_lag_days": 1,
    "pool_min_consecutive_years": 3, "momentum_months": 4,
    "momentum_threshold": 0.85, "reinvest_cash_reserve": 0,
    "rank_by": "yield", "momentum_periods": "",
}


def _oos(nav: list[dict], start: str) -> dict:
    rows = [x for x in nav if str(x.get("date", "")) >= start]
    if len(rows) < 2:
        return {"observations": len(rows)}
    return {"observations": len(rows), **_compute_metrics(rows, float(rows[0]["nav"]))}


def _run(name: str, max_yield: float, dates: list[str]) -> dict:
    rules = dict(BASE, max_yield=max_yield)
    result = run_backtest(rules=rules, dynamic_pool=True, manifest_path=str(MP),
                          rebalance_dates=dates, verbose=False)
    nav = result.get("nav_series") or []
    return {
        "name": name, "rules": rules, "full": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {y: _oos(nav, f"{y}-01-01") for y in ("2021", "2023", "2025")},
    }


def main() -> None:
    dates_doc = json.loads(DP.read_text(encoding="utf-8"))
    dates = dates_doc.get("dates", dates_doc)
    manifest = json.loads(MP.read_text(encoding="utf-8"))
    # 预注册：从严格异常过滤到逐步放宽，再到无上限基线。
    limits = (12.0, 15.0, 20.0, 30.0, 50.0, 75.0, 100.0, 999.0)
    rows = []
    for i, limit in enumerate(limits, 1):
        name = "max_yield_unlimited" if limit >= 999 else f"max_yield_{limit:g}"
        print(f"[{i}/{len(limits)}] {name}", flush=True)
        rows.append(_run(name, limit, dates))
    payload = {
        "round": 13,
        "method": "预注册单变量极端高息上限；冻结缓存完整账本、rolling36/48及连续OOS；无未来函数",
        "base_rules": BASE,
        "tested_max_yield": list(limits),
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_doc.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": rows,
    }
    out = ROOT / "data" / "round13_simple_rule.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{"name": r["name"], "cagr": r["full"].get("cagr"),
                       "dd": r["full"].get("max_drawdown"), "sharpe": r["full"].get("sharpe"),
                       "r36": r["rolling36"].get("min_cagr"), "r48": r["rolling48"].get("min_cagr"),
                       "oos21": r["oos"]["2021"].get("cagr"), "oos23": r["oos"]["2023"].get("cagr"),
                       "oos25": r["oos"]["2025"].get("cagr")} for r in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
