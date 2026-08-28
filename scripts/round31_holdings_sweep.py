"""第 31 轮：固定 V1 其余规则，比较最多持有 2–10 只股票。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import _compute_metrics, run_backtest
from round3_experiments import _window_metrics
from verify_v1_freeze import verify as verify_v1_freeze


MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
FREEZE_PATH = ROOT / "data" / "v1_freeze.json"
OUTPUT_PATH = ROOT / "data" / "round31_holdings_sweep.json"
HOLDING_LIMITS = tuple(range(2, 11))
OOS_STARTS = ("2021", "2023", "2025")
RESET_STARTS = ("2018-01-01", "2020-01-01", "2022-01-01")
FEE_FIELDS = (
    "buy_commission_rate",
    "sell_commission_rate",
    "stamp_duty_rate",
    "transfer_fee_rate",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _oos_metrics(nav: list[dict[str, Any]], start: str) -> dict[str, Any]:
    sliced = [point for point in nav if point["date"] >= start]
    if len(sliced) < 2:
        return {"observations": len(sliced)}
    initial_nav = float(sliced[0]["nav"])
    return {
        "observations": len(sliced),
        **_compute_metrics(sliced, initial_nav),
    }


def _run_variant(
    max_holdings: int,
    dates: list[str],
    base_rules: dict[str, Any],
    *,
    fee_multiple: int = 1,
) -> dict[str, Any]:
    rules = {**base_rules, "max_holdings": max_holdings}
    if fee_multiple != 1:
        for field in FEE_FIELDS:
            rules[field] = float(base_rules[field]) * fee_multiple

    result = run_backtest(
        rules=rules,
        dynamic_pool=True,
        manifest_path=str(MANIFEST_PATH),
        rebalance_dates=dates,
        verbose=False,
    )
    nav = result.get("nav_series") or []
    return {
        "max_holdings": max_holdings,
        "fee_multiple": fee_multiple,
        "rules": rules,
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {
            year: _oos_metrics(nav, f"{year}-01-01")
            for year in OOS_STARTS
        },
        "nav": nav,
    }


def _without_nav(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "nav"}


def _normalized_reset_nav(
    nav: list[dict[str, Any]],
    start: str,
    *,
    initial_capital: float,
) -> list[dict[str, Any]]:
    sliced = [point for point in nav if point["date"] >= start]
    if not sliced:
        return []
    scale = initial_capital / float(sliced[0]["nav"])
    return [
        {**point, "nav": round(float(point["nav"]) * scale, 2)}
        for point in sliced
    ]


def _summary_rows(
    experiments: list[dict[str, Any]],
    cost_stress: list[dict[str, Any]],
    reset_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stress_by_limit = {item["max_holdings"]: item for item in cost_stress}
    resets_by_limit = {
        limit: {
            item["start"][:4]: item["metrics"].get("cagr")
            for item in reset_windows
            if item["max_holdings"] == limit
        }
        for limit in HOLDING_LIMITS
    }
    return [
        {
            "max_holdings": item["max_holdings"],
            "cagr": item["metrics"].get("cagr"),
            "max_drawdown": item["metrics"].get("max_drawdown"),
            "sharpe": item["metrics"].get("sharpe"),
            "rolling36_worst_cagr": item["rolling36"].get("min_cagr"),
            "rolling48_worst_cagr": item["rolling48"].get("min_cagr"),
            "oos_2023_cagr": item["oos"]["2023"].get("cagr"),
            "trade_count": item["metrics"].get("trade_count"),
            "triple_cost_cagr": stress_by_limit[item["max_holdings"]]["metrics"].get("cagr"),
            "reset_cagr": resets_by_limit[item["max_holdings"]],
        }
        for item in experiments
    ]


def main() -> None:
    verify_v1_freeze(check_git=False)
    frozen = _load_json(FREEZE_PATH)
    manifest = _load_json(MANIFEST_PATH)
    dates_payload = _load_json(DATES_PATH)
    dates = dates_payload["dates"]
    base_rules = frozen["rules"]
    initial_capital = float(base_rules["initial_capital"])

    experiments = []
    for max_holdings in HOLDING_LIMITS:
        print(f"正常成本：最多持有 {max_holdings} 只")
        experiments.append(_without_nav(
            _run_variant(max_holdings, dates, base_rules)
        ))

    cost_stress = []
    for max_holdings in HOLDING_LIMITS:
        print(f"三倍成本：最多持有 {max_holdings} 只")
        cost_stress.append(_without_nav(
            _run_variant(max_holdings, dates, base_rules, fee_multiple=3)
        ))

    reset_windows = []
    warmup_months = int(base_rules["momentum_months"])
    for max_holdings in HOLDING_LIMITS:
        for start in RESET_STARTS:
            print(f"重置窗口 {start[:4]}：最多持有 {max_holdings} 只")
            first_index = next(
                index for index, date in enumerate(dates) if date >= start
            )
            warmup_dates = dates[max(0, first_index - warmup_months):]
            result = _run_variant(max_holdings, warmup_dates, base_rules)
            nav = _normalized_reset_nav(
                result["nav"],
                start,
                initial_capital=initial_capital,
            )
            reset_windows.append({
                "max_holdings": max_holdings,
                "start": start,
                "end": dates[-1],
                "warmup_count": min(warmup_months, first_index),
                "metrics": _compute_metrics(nav, initial_capital),
                "rolling36": _window_metrics(nav, 36),
                "rolling48": _window_metrics(nav, 48),
            })

    summary = _summary_rows(experiments, cost_stress, reset_windows)
    output = {
        "round": 31,
        "question": (
            "固定 V1 其他规则时，将 max_holdings 从 2 扩展到 10，能否在不明显牺牲长期收益的情况下，"
            "改善滚动最差表现、不同起点和高交易成本稳定性？"
        ),
        "method": (
            "只改变 max_holdings=2..10；使用冻结 V1 manifest、日期、价格、分红和费用口径；"
            "比较完整账本、连续 OOS、滚动 36/48 月、三倍费用及 2018/2020/2022 重置窗口。"
        ),
        "baseline_max_holdings": 2,
        "base_rules": base_rules,
        "manifest_records_sha256": manifest["records_sha256"],
        "dates_sha256": dates_payload["dates_sha256"],
        "data_cutoff": manifest["as_of"],
        "dates": {
            "count": len(dates),
            "first": dates[0],
            "last": dates[-1],
        },
        "experiments": experiments,
        "cost_stress": cost_stress,
        "reset_windows": reset_windows,
        "summary": summary,
        "audit": {
            "only_changed_rule": "max_holdings",
            "future_function_check": (
                "信号仅使用信号日及以前的数据，成交使用信号日后的下一可用交易日。"
            ),
            "oos_definition": "完整账本连续切片，不重新初始化账户。",
            "reset_definition": (
                "每个重置起点保留 4 个信号点作动量预热，并在正式起点将 NAV 归一化为 10 万元。"
            ),
            "survivorship_bias": (
                "冻结 210 只股票集合可能缺少退市股票和历史成分变化，结果不是全市场无偏回测。"
            ),
            "forward_policy": (
                "本轮是用户明确要求的一次性研究比较，不修改冻结 V1 或前向模拟账本。"
            ),
        },
    }
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
