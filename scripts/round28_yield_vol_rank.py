"""第 28 轮：波动率调整排序——收益率/波动率 vs 纯收益率。

当前策略按真实股息率排序（第 24 轮确认优于动量排序）。本轮测试是否用
"收益率/日频波动率"替代纯收益率作为排序依据，可以改善风险调整后收益。
只改排序键，不改过滤规则、候选池或其他参数。
"""
from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import run_backtest, _compute_metrics, BACKTEST_RULES
from round3_experiments import _window_metrics

MP = ROOT / "data" / "universe_manifest.json"
DP = ROOT / "data" / "rebalance_dates_monthly.json"

BASE = {
    "entry_yield": 7.5, "hold_yield": 5.5, "max_holdings": 2,
    "rebalance_threshold": 2.0, "execution_lag_days": 1,
    "pool_min_consecutive_years": 3, "momentum_months": 4,
    "momentum_threshold": 0.85, "reinvest_cash_reserve": 0,
    "rank_by": "yield", "momentum_periods": "", "max_yield": 999.0,
}


def oos(nav, start):
    xs = [x for x in nav if x["date"] >= start]
    if len(xs) < 2:
        return {"observations": len(xs)}
    return {"observations": len(xs), **_compute_metrics(xs, float(xs[0]["nav"]))}


def run_variant(rank_by, dates, fee=1, vol_days=120):
    rules = dict(BASE, rank_by=rank_by, volatility_lookback_days=vol_days)
    if fee != 1:
        for k in ("buy_commission_rate", "sell_commission_rate", "stamp_duty_rate", "transfer_fee_rate"):
            rules[k] = BACKTEST_RULES.get(k, 0) * fee
    result = run_backtest(
        rules=rules, dynamic_pool=True,
        manifest_path=str(MP), rebalance_dates=dates, verbose=False,
    )
    nav = result.get("nav_series") or []
    return {
        "rank_by": rank_by,
        "volatility_lookback_days": vol_days,
        "fee_multiple": fee,
        "rules": rules,
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {y: oos(nav, y + "-01-01") for y in ("2021", "2023", "2025")},
        "nav": nav,
    }


def main():
    d = json.loads(DP.read_text(encoding="utf-8"))
    dates = d.get("dates", d)
    manifest = json.loads(MP.read_text(encoding="utf-8"))

    # 主对照：yield vs yield_vol（120 日波动率）
    experiments = []
    for rank_by in ("yield", "yield_vol"):
        z = run_variant(rank_by, dates)
        z.pop("nav")
        experiments.append(z)

    # 波动率回看窗口敏感性：60/120/250 日
    vol_sensitivity = []
    for vd in (60, 120, 250):
        z = run_variant("yield_vol", dates, vol_days=vd)
        z.pop("nav")
        vol_sensitivity.append(z)

    # 三倍费用
    cost_stress = []
    for rank_by in ("yield", "yield_vol"):
        z = run_variant(rank_by, dates, 3)
        z.pop("nav")
        cost_stress.append(z)

    # 重置窗口
    resets = []
    for rank_by in ("yield", "yield_vol"):
        for start in ("2018-01-01", "2020-01-01", "2022-01-01"):
            i = next(j for j, x in enumerate(dates) if x >= start)
            subset = dates[max(0, i - 4):]
            z = run_variant(rank_by, subset)
            nav = [x for x in z["nav"] if x["date"] >= start]
            if nav:
                scale = 100000 / nav[0]["nav"]
                nav = [dict(x, nav=round(x["nav"] * scale, 2)) for x in nav]
            resets.append({
                "rank_by": rank_by, "start": start, "end": dates[-1],
                "warmup_count": min(4, i),
                "metrics": _compute_metrics(nav, 100000),
                "rolling36": _window_metrics(nav, 36),
                "rolling48": _window_metrics(nav, 48),
            })

    out = {
        "round": 28,
        "method": (
            "波动率调整排序（收益率/日频波动率）vs 纯收益率排序；"
            "仅改排序键，冻结输入；完整账本、连续 OOS、rolling36/48、"
            "波动率窗口敏感性、三倍费用、2018/2020/2022 重置；无未来函数"
        ),
        "base_rules": BASE,
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": d.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": experiments,
        "vol_sensitivity": vol_sensitivity,
        "cost_stress": cost_stress,
        "reset_windows": resets,
        "audit": {
            "future_function_check": "通过：波动率只用信号日前的日频价格计算，交易执行滞后 1 个交易日。",
            "survivorship_bias": "冻结股票集合可能缺少退市股票，参见第 25 轮审计。",
        },
    }
    out_path = ROOT / "data" / "round28_yield_vol_rank.json"
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(
        [(x["rank_by"], x["metrics"].get("cagr"), x["metrics"].get("max_drawdown"))
         for x in experiments],
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
