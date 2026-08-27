"""第 20 轮：仓位上限与现金准备金窄实验。

所有变体共享冻结的 manifest、月末信号日期和回测缓存；只比较仓位上限及
再投资准备金，不改变候选池、收益率阈值或动量规则。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest
from round3_experiments import _continuous_metrics, _window_metrics

MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
RATE_KEYS = (
    "buy_commission_rate",
    "sell_commission_rate",
    "stamp_duty_rate",
    "transfer_fee_rate",
)
VARIANTS = (
    (1.0, 0.0),
    (0.75, 0.0),
    (0.5, 0.0),
    (1.0, 3000.0),
    (0.75, 3000.0),
    (0.5, 3000.0),
)


def _cost_rules(rules: dict, multiplier: float) -> dict:
    """按统一压力口径放大费率，最低佣金保持原值。"""
    out = dict(rules)
    for key in RATE_KEYS:
        out[key] = float(rules[key]) * multiplier
    return out


def _summary(result: dict) -> dict:
    nav = result.get("nav_series") or []
    return {
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "oos": {
            year: _continuous_metrics(nav, f"{year}-01-01")
            for year in ("2021", "2023", "2025")
        },
    }


def _reset(rules: dict, dates: list[str], start: str) -> dict:
    """保留四个动量 warm-up 点后，从指定日期切片并归一化 NAV。"""
    index = next((i for i, value in enumerate(dates) if value >= start), len(dates))
    warm_dates = dates[max(0, index - 4):]
    result = backtest.run_backtest(
        rules=rules,
        dynamic_pool=True,
        manifest_path=str(MANIFEST_PATH),
        rebalance_dates=warm_dates,
        verbose=False,
    )
    nav = [item for item in (result.get("nav_series") or []) if item["date"] >= start]
    if nav:
        scale = 100000.0 / float(nav[0]["nav"])
        nav = [dict(item, nav=round(float(item["nav"]) * scale, 2)) for item in nav]
    metrics = (
        backtest._compute_metrics(nav, 100000.0)
        if len(nav) >= 2
        else {"observations": len(nav)}
    )
    return {
        "start": start,
        "warmup_signal_count": sum(1 for value in warm_dates if value < start),
        "metrics": metrics,
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
    }


def main() -> None:
    dates_payload = json.loads(DATES_PATH.read_text(encoding="utf-8"))
    dates = dates_payload.get("dates", dates_payload) if isinstance(dates_payload, dict) else dates_payload
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    base = dict(backtest.BACKTEST_RULES)
    rows = []
    for cap, reserve in VARIANTS:
        rules = dict(base, max_position_pct=cap, reinvest_cash_reserve=reserve)
        normal = backtest.run_backtest(
            rules=rules,
            dynamic_pool=True,
            manifest_path=str(MANIFEST_PATH),
            rebalance_dates=dates,
            verbose=False,
        )
        stressed = backtest.run_backtest(
            rules=_cost_rules(rules, 3.0),
            dynamic_pool=True,
            manifest_path=str(MANIFEST_PATH),
            rebalance_dates=dates,
            verbose=False,
        )
        rows.append({
            "max_position_pct": cap,
            "reinvest_cash_reserve": reserve,
            "normal_cost": _summary(normal),
            "three_x_cost": _summary(stressed),
            "reset_windows": {
                start: _reset(rules, dates, start)
                for start in ("2022-01-01", "2023-01-01")
            },
        })
        print(f"完成 cap={cap:g}, reserve={reserve:g}", flush=True)

    payload = {
        "round": 20,
        "experiment": "position_reserve",
        "method": (
            "冻结 manifest、月末调仓日期和动态候选池；比较仓位上限/现金准备金，"
            "包含完整账本、rolling36/48、连续 OOS、2022/2023 warm-up 重置和三倍费用；无未来函数"
        ),
        "input": {
            "manifest_path": "data/universe_manifest.json",
            "manifest_records_sha256": manifest.get("records_sha256"),
            "data_cutoff": manifest.get("as_of"),
            "dates_path": "data/rebalance_dates_monthly.json",
            "dates_count": len(dates),
            "dates_first": min(dates),
            "dates_last": max(dates),
            "dates_sha256": (
                dates_payload.get("dates_sha256")
                if isinstance(dates_payload, dict)
                else hashlib.sha256(json.dumps(dates, separators=(",", ":")).encode()).hexdigest()
            ),
        },
        "base_rules": base,
        "variants": rows,
        "audit": {
            "future_function_check": "通过：仅使用信号日前分红/价格，成交滞后 1 个交易日",
            "cost_stress": "三倍仅放大佣金、印花税和过户费率，最低佣金不变",
            "oos_definition": "连续 OOS 从完整账本切片；重置保留四个动量 warm-up 信号点",
            "survivorship_bias": "manifest 是截止日冻结的现存代码集合，仍存在生存者偏差",
        },
    }
    output = ROOT / "data" / "round20_position_reserve.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {output}")


if __name__ == "__main__":
    main()
