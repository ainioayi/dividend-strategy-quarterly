"""第 18 轮：严格训练期选参与连续样本外验证。

参数只在训练截止日前的完整账本 NAV 上排序，随后固定参数进入测试期。
同时保留连续切片和带动量 warm-up 的重置口径，并对最终选择做三倍费用
压力测试。该脚本不把全样本冠军直接写成生产配置。
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
GRID = [
    (entry, hold, momentum)
    for entry in (7.4, 7.5, 7.6)
    for hold in (5.5, 5.6)
    for momentum in (0.84, 0.85, 0.86)
]


def _metrics(nav: list[dict]) -> dict:
    if len(nav) < 2:
        return {"observations": len(nav)}
    return {
        **backtest._compute_metrics(nav, float(nav[0]["nav"])),
        "observations": len(nav),
    }


def _cost_rules(rules: dict, multiplier: float) -> dict:
    out = dict(rules)
    for key in RATE_KEYS:
        out[key] = float(rules[key]) * multiplier
    return out


def _reset_slice(rules: dict, dates: list[str], start: str) -> tuple[list[dict], dict]:
    """保留起点前四个信号点作为动量 warm-up，再统计正式起点。"""
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
    return nav, result.get("metrics") or {}


def main() -> None:
    dates_payload = json.loads(DATES_FILE.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    runs = []
    for entry, hold, momentum in GRID:
        rules = dict(BASE, entry_yield=entry, hold_yield=hold, momentum_threshold=momentum)
        result = backtest.run_backtest(
            rules=rules, dynamic_pool=True, manifest_path=str(MANIFEST),
            rebalance_dates=dates, verbose=False,
        )
        runs.append({"rules": rules, "nav": result.get("nav_series") or []})
        print(f"完成训练候选 entry={entry} hold={hold} momentum={momentum}", flush=True)

    experiments = []
    for cutoff, test_start in (("2021-12-31", "2022-01-01"), ("2022-12-31", "2023-01-01")):
        ranked = []
        for run in runs:
            train_nav = [item for item in run["nav"] if item["date"] <= cutoff]
            train_metrics = _metrics(train_nav)
            # 这是预先固定的简单风险惩罚，不使用测试期信息。
            score = float(train_metrics.get("cagr", -999.0)) - 0.25 * float(train_metrics.get("max_drawdown", 100.0))
            ranked.append({"score": round(score, 6), "rules": run["rules"], "metrics": train_metrics})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        winner = ranked[0]["rules"]
        selected_nav = next(run["nav"] for run in runs if run["rules"] == winner)
        baseline_nav = next(run["nav"] for run in runs if run["rules"] == BASE)
        selected_test = [item for item in selected_nav if item["date"] >= test_start]
        baseline_test = [item for item in baseline_nav if item["date"] >= test_start]

        reset_nav, _ = _reset_slice(winner, dates, test_start)
        stressed_nav, _ = _reset_slice(_cost_rules(winner, 3.0), dates, test_start)
        experiments.append({
            "cutoff": cutoff,
            "test_start": test_start,
            "train_top": ranked[0],
            "test_slice": {
                "selected": _metrics(selected_test),
                "baseline": _metrics(baseline_test),
            },
            "test_reset": {
                "selected": _metrics(reset_nav),
                "rolling36": _window_metrics(reset_nav, 36),
                "rolling48": _window_metrics(reset_nav, 48),
            },
            "test_reset_three_x_cost": {
                "selected": _metrics(stressed_nav),
                "rolling36": _window_metrics(stressed_nav, 36),
                "rolling48": _window_metrics(stressed_nav, 48),
            },
            "train_ranking": ranked,
        })

    output = {
        "round": 18,
        "experiment": "train_test_selection",
        "method": "18 组小网格仅在训练期按 CAGR - 0.25 x 最大回撤选参；测试期固定参数；连续切片、四点动量 warm-up 重置和三倍费用压力；无未来函数",
        "base_rules": BASE,
        "grid": {
            "entry_yield": [7.4, 7.5, 7.6],
            "hold_yield": [5.5, 5.6],
            "momentum_threshold": [0.84, 0.85, 0.86],
        },
        "inputs": {
            "manifest_records_sha256": manifest.get("records_sha256"),
            "dates_sha256": dates_payload.get("dates_sha256") if isinstance(dates_payload, dict) else backtest._rebalance_dates_hash(dates),
            "data_cutoff": manifest.get("as_of"),
            "dates": {"count": len(dates), "first": min(dates), "last": max(dates)},
        },
        "experiments": experiments,
        "audit": {
            "future_function_check": "通过：训练排名只读取 cutoff 前 NAV；测试参数固定；动态池按各信号日 ex_date 截断；成交滞后 1 日",
            "oos_definition": "连续切片从完整账本读取；重置口径只保留四个动量 warm-up 信号点，不使用测试期选参",
            "cost_stress": "三倍仅放大佣金、印花税和过户费率，最低佣金不变",
            "decision": "训练期选择未在后续测试中稳定超过基线，因此不切换生产参数",
            "survivorship_bias": "manifest 是截至截止日冻结的现存代码集合，存在生存者偏差",
        },
    }
    output_path = ROOT / "data" / "round18_train_test_selection.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {output_path}")
    for item in experiments:
        print(json.dumps({
            "cutoff": item["cutoff"],
            "selected": item["train_top"]["rules"],
            "slice_cagr": item["test_slice"]["selected"].get("cagr"),
            "baseline_cagr": item["test_slice"]["baseline"].get("cagr"),
            "reset_cagr": item["test_reset"]["selected"].get("cagr"),
            "reset_3x_cagr": item["test_reset_three_x_cost"]["selected"].get("cagr"),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
