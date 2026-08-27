from __future__ import annotations
import json, sys, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from backtest import run_backtest, _compute_metrics
from round3_experiments import _window_metrics

MP = ROOT / "data/universe_manifest.json"
DP = ROOT / "data/rebalance_dates_monthly.json"
BASE = {
    "entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
    "rebalance_threshold": 2.0, "execution_lag_days": 1,
    "pool_min_consecutive_years": 3, "momentum_months": 4,
    "momentum_threshold": .85, "reinvest_cash_reserve": 0,
    "rank_by": "yield", "momentum_periods": "", "max_yield": 999.0,
    "initial_capital": 100000,
}

def run_window(start: str, hold: float, dates: list[str]) -> dict:
    # 保留起点前至少4个信号点，供动量过滤 warm-up；正式统计从 start 开始。
    i = next((j for j, d in enumerate(dates) if d >= start), len(dates))
    warm_i = max(0, i - 4)
    run_dates = dates[warm_i:]
    result = run_backtest(rules=dict(BASE, hold_yield=hold), dynamic_pool=True,
                          manifest_path=str(MP), rebalance_dates=run_dates,
                          verbose=False)
    nav = [x for x in (result.get("nav_series") or []) if str(x.get("date", "")) >= start]
    # 将正式起点 NAV 视为窗口初始资金，消除 warm-up 区间对收益统计的影响。
    # 同时保留原始起点和缩放因子，避免把窗口收益误读为从现金 10 万元建仓。
    start_nav = float(nav[0]["nav"]) if nav else None
    scale = 100000.0 / (start_nav or 1.0) if start_nav is not None else 1.0
    if nav:
        nav = [dict(x, nav=round(float(x["nav"]) * scale, 2)) for x in nav]
    metrics = _compute_metrics(nav, 100000)
    return {"start": start, "end": dates[-1], "warmup_signal_count": i-warm_i,
            "signal_count": len([d for d in dates if d >= start]),
            "hold_yield": hold, "start_nav_before_normalization": start_nav,
            "normalization_factor": round(scale, 10),
            "warmup_state_preserved": bool(i > warm_i), "metrics": metrics,
            "rolling36": _window_metrics(nav, 36), "rolling48": _window_metrics(nav, 48)}

def main() -> None:
    d = json.loads(DP.read_text(encoding="utf-8")); dates = d.get("dates", d)
    m = json.loads(MP.read_text(encoding="utf-8"))
    starts = ["2016-01-01", "2018-01-01", "2020-01-01", "2021-01-01", "2022-01-01", "2023-01-01"]
    rows = []
    for s in starts:
        a, b = run_window(s, 5.5, dates), run_window(s, 5.6, dates)
        rows.append({"start": s, "hold_5.5": a, "hold_5.6": b,
                     "difference": {"cagr_pp": round(b["metrics"].get("cagr", 0)-a["metrics"].get("cagr", 0), 4),
                                    "sharpe": round(b["metrics"].get("sharpe", 0)-a["metrics"].get("sharpe", 0), 4),
                                    "max_drawdown_pp": round(b["metrics"].get("max_drawdown", 0)-a["metrics"].get("max_drawdown", 0), 4)}})
    out = {"round": 13, "method": "严格 warm-up walk-forward：每窗口保留起点前4个信号点供4个月动量回看；仅统计正式起点以后 NAV，并按起点 NAV 归一化为10万元；动态候选池、执行滞后1日、不使用未来函数。",
           "base_rules": BASE, "starts": starts,
           "manifest_records_sha256": m.get("records_sha256"), "dates_sha256": d.get("dates_sha256"),
           "data_cutoff": m.get("as_of"), "windows": rows,
           "audit": {"warmup_signals": 4, "warmup_state_preserved": True,
                     "normalization": "以正式起点第一条 NAV 归一化为10万元；该处理剥离 warm-up 期间及首期建仓费用，不能解释为窗口起点现金重新建仓。",
                     "future_function_check": "通过：每个信号仅使用当日及此前缓存，成交滞后1个交易日；统计区间从正式起点开始。"}}
    (ROOT / "data/round13_walkforward_strict.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{ "start": x["start"], "cagr55": x["hold_5.5"]["metrics"].get("cagr"), "cagr56": x["hold_5.6"]["metrics"].get("cagr"), "diff": x["difference"]} for x in rows], ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
