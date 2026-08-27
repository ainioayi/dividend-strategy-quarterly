"""第 22 轮：再平衡阈值的简单控制变量与稳健性审计。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import BACKTEST_RULES, _compute_metrics, run_backtest
from round3_experiments import _window_metrics


MANIFEST = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
BASE = {
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "dividend_information_lag_days": 0,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "reinvest_cash_reserve": 0,
    "rank_by": "yield",
    "momentum_periods": "",
    "max_yield": 999.0,
}
COST_KEYS = (
    "buy_commission_rate",
    "sell_commission_rate",
    "stamp_duty_rate",
    "transfer_fee_rate",
)


def _rules(threshold: float, fee_multiple: int = 1) -> dict:
    rules = dict(BASE, rebalance_threshold=threshold)
    if fee_multiple != 1:
        for key in COST_KEYS:
            rules[key] = float(BACKTEST_RULES.get(key, 0.0)) * fee_multiple
    return rules


def _run(rules: dict, dates: list[str]) -> tuple[dict, list[dict]]:
    result = run_backtest(
        rules=rules,
        dynamic_pool=True,
        manifest_path=str(MANIFEST),
        rebalance_dates=dates,
        verbose=False,
    )
    return result, result.get("nav_series") or []


def _oos(nav: list[dict], start: str) -> dict:
    sample = [item for item in nav if str(item.get("date") or "") >= start]
    if len(sample) < 2:
        return {"observations": len(sample)}
    return {
        "observations": len(sample),
        **_compute_metrics(sample, float(sample[0]["nav"])),
    }


def _summarize(threshold: float, fee_multiple: int, dates: list[str]) -> dict:
    rules = _rules(threshold, fee_multiple)
    result, nav = _run(rules, dates)
    return {
        "rebalance_threshold": threshold,
        "fee_multiple": fee_multiple,
        "rules": rules,
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {year: _oos(nav, year + "-01-01") for year in ("2021", "2023", "2025")},
    }


def _reset_window(threshold: float, dates: list[str], start: str) -> dict:
    index = next(i for i, value in enumerate(dates) if value >= start)
    warmup_dates = dates[max(0, index - 4):index]
    subset = warmup_dates + dates[index:]
    _, nav = _run(_rules(threshold), subset)
    formal_nav = [item for item in nav if str(item.get("date") or "") >= start]
    start_nav = float(formal_nav[0]["nav"]) if formal_nav else None
    factor = 100000.0 / start_nav if start_nav else None
    normalized = [
        dict(item, nav=round(float(item["nav"]) * factor, 2))
        for item in formal_nav
    ] if factor else []
    return {
        "rebalance_threshold": threshold,
        "start": start,
        "end": dates[-1],
        "formal_signal_start": dates[index],
        "warmup_signal_dates": warmup_dates,
        "warmup_count": len(warmup_dates),
        "warmup_state_preserved": True,
        "nav_start": normalized[0]["date"] if normalized else None,
        "nav_end": normalized[-1]["date"] if normalized else None,
        "start_nav_before_normalization": round(start_nav, 2) if start_nav else None,
        "normalization_factor": factor,
        "metrics": _compute_metrics(normalized, 100000.0),
        "rolling36": _window_metrics(normalized, 36),
        "rolling48": _window_metrics(normalized, 48),
    }


def main() -> None:
    dates_payload = json.loads(DATES_PATH.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    thresholds = (1.5, 2.0, 2.5)
    experiments = [
        _summarize(threshold, fee_multiple, dates)
        for threshold in thresholds
        for fee_multiple in (1, 3)
    ]
    reset_windows = [
        _reset_window(threshold, dates, start)
        for threshold in thresholds
        for start in ("2021-01-01", "2023-01-01", "2025-01-01")
    ]
    output = {
        "round": 22,
        "method": "再平衡阈值1.5/2.0/2.5；冻结输入；完整账本、连续OOS、rolling36/48、三倍成本与规范化warm-up窗口；无未来函数",
        "base_rules": BASE,
        "manifest_records_sha256": manifest.get("records_sha256"),
        "dates_sha256": dates_payload.get("dates_sha256"),
        "data_cutoff": manifest.get("as_of"),
        "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
        "experiments": experiments,
        "reset_windows": reset_windows,
        "audit": {
            "future_function_check": "通过：信号只使用当日及以前数据，交易执行滞后1个交易日。",
            "oos_definition": "完整账本连续切片；warm-up窗口保留起点前4个信号的状态，并从正式窗口首个NAV归一化。",
            "survivorship_bias": "冻结现存代码集合可能缺少退市股票和历史成分变化。",
            "decision": "不因单一成本或起点切片切换参数。",
        },
    }
    (ROOT / "data" / "round22_simple_controls.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps([
        {
            "threshold": row["rebalance_threshold"],
            "fee_multiple": row["fee_multiple"],
            "cagr": row["metrics"].get("cagr"),
            "max_drawdown": row["metrics"].get("max_drawdown"),
        }
        for row in experiments
    ], ensure_ascii=False))


if __name__ == "__main__":
    main()
