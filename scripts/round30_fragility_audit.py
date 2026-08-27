"""第 30 轮：V1 收益集中度脆弱性审计。

本轮冻结 V1 参数和输入，不搜索参数。审计依次剔除第 29 轮归因中的最大
盈利股票、前三大盈利股票，以及实际交易股票的当前主营行业代理簇。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backtest import BACKTEST_RULES, _compute_metrics, run_backtest
from round3_experiments import _window_metrics
from tradeable_benchmark import _canonical_sha256, simulate_total_return

MANIFEST_PATH = ROOT / "data" / "universe_manifest.json"
DATES_PATH = ROOT / "data" / "rebalance_dates_monthly.json"
ATTRIBUTION_PATH = ROOT / "data" / "round29_attribution.json"
OUTPUT_PATH = ROOT / "data" / "round30_fragility_audit.json"
BENCHMARK_PATH = ROOT / "data" / "benchmarks" / "510880_total_return.json"

EXPECTED_MANIFEST_HASH = "24de009d9bb60c857fc89e8f7510b93583b17f9abde50350ea63a6a5830a7409"
EXPECTED_DATES_HASH = "f62fc22c2f2f972e3b29dea42e2a41202bfa620e702acc3c750e26f8c959ec3e"
EXPECTED_CUTOFF = "2026-08-25"

V1_RULES = {
    "entry_yield": 7.5,
    "hold_yield": 5.5,
    "momentum_months": 4,
    "momentum_threshold": 0.85,
    "pool_min_consecutive_years": 3,
    "pool_switch_month": 7,
    "max_holdings": 2,
    "rebalance_threshold": 2.0,
    "execution_lag_days": 1,
    "dividend_information_lag_days": 0,
    "reinvest_cash_reserve": 0,
    "rank_by": "yield",
    "momentum_periods": "",
    "max_yield": 999.0,
    "through_date": EXPECTED_CUTOFF,
}

# 当前冻结缓存没有历史点时行业字段。以下代理只覆盖第 29 轮实际交易的股票，
# 按截至本轮研究时可识别的主营业务宽口径分组，并固化在脚本中供复核。
CURRENT_INDUSTRY_PROXY = {
    "煤炭能源": ("601088", "600188"),
    "材料化工": ("600295", "603688", "002756", "600873", "600389"),
    "汽车产业链": ("000550", "600066", "002048"),
    "家电": ("002032", "000651"),
    "食品消费": ("000895",),
    "医药": ("600566",),
    "零售": ("600729",),
    "交通科技": ("002869",),
    "石油石化": ("600028",),
    "航运": ("601919",),
}


def _load_inputs() -> tuple[dict, dict, dict, list[str]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    dates_payload = json.loads(DATES_PATH.read_text(encoding="utf-8"))
    attribution = json.loads(ATTRIBUTION_PATH.read_text(encoding="utf-8"))
    dates = list(dates_payload.get("dates") or [])
    if manifest.get("records_sha256") != EXPECTED_MANIFEST_HASH:
        raise ValueError("manifest 哈希与冻结 V1 不一致")
    if dates_payload.get("dates_sha256") != EXPECTED_DATES_HASH:
        raise ValueError("日期哈希与冻结 V1 不一致")
    if manifest.get("as_of") != EXPECTED_CUTOFF or dates_payload.get("as_of") != EXPECTED_CUTOFF:
        raise ValueError("输入截止日与冻结 V1 不一致")
    if attribution.get("manifest_records_sha256") != EXPECTED_MANIFEST_HASH:
        raise ValueError("第 29 轮归因没有绑定冻结 V1 manifest")
    if not dates:
        raise ValueError("调仓日期为空")
    return manifest, dates_payload, attribution, dates


def _continuous_oos(nav: list[dict], start: str) -> dict:
    sample = [item for item in nav if str(item.get("date", "")) >= start]
    if len(sample) < 2:
        return {"observations": len(sample)}
    return {
        "observations": len(sample),
        "window_start": sample[0]["date"],
        "window_end": sample[-1]["date"],
        **_compute_metrics(sample, float(sample[0]["nav"])),
    }


def _load_benchmark() -> dict:
    payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    if payload.get("as_of") != EXPECTED_CUTOFF:
        raise ValueError("可交易基准截止日与冻结 V1 不一致")
    hashes = payload.get("hashes") or {}
    for key in ("prices", "dividends"):
        actual = _canonical_sha256(payload.get(key) or [])
        if hashes.get(f"{key}_sha256") != actual:
            raise ValueError(f"可交易基准 {key} 哈希校验失败")
    return payload


def _build_benchmark(dates: list[str], baseline_normal: dict) -> dict:
    payload = _load_benchmark()
    result = simulate_total_return(
        payload, dates, initial_capital=100000, signal_start=dates[0]
    )
    nav = result.pop("nav_series")
    events = result.pop("events")
    return {
        "symbol": result["symbol"],
        "name": result["name"],
        "data_file": "data/benchmarks/510880_total_return.json",
        "as_of": payload["as_of"],
        "retrieved_at": payload.get("retrieved_at"),
        "sources": payload.get("sources"),
        "input_hashes": result["input_hashes"],
        "method": result["method"],
        "metrics": result["metrics"],
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "continuous_oos": {
            year: _continuous_oos(nav, f"{year}-01-01")
            for year in ("2021", "2023", "2025")
        },
        "total_dividend_cash": result["total_dividend_cash"],
        "dividend_event_count": sum(event.get("side") == "分红" for event in events),
        "ending_shares": result["shares"],
        "ending_cash": result["cash"],
        "difference_vs_v1_baseline": {
            "metrics": _metric_differences(result["metrics"], baseline_normal["metrics"]),
            "rolling36_min_cagr": round(
                float(_window_metrics(nav, 36).get("min_cagr", 0))
                - float(baseline_normal["rolling36"].get("min_cagr", 0)), 3
            ),
            "rolling48_min_cagr": round(
                float(_window_metrics(nav, 48).get("min_cagr", 0))
                - float(baseline_normal["rolling48"].get("min_cagr", 0)), 3
            ),
            "continuous_oos_cagr": {
                year: round(
                    float(_continuous_oos(nav, f"{year}-01-01").get("cagr", 0))
                    - float(baseline_normal["continuous_oos"][year].get("cagr", 0)), 3
                ) for year in ("2021", "2023", "2025")
            },
        },
        "limitations": payload.get("limitations") or [],
    }


def _run_once(codes: list[str], fee_multiple: int) -> dict:
    rules = dict(V1_RULES)
    if fee_multiple != 1:
        for key in (
            "buy_commission_rate", "sell_commission_rate",
            "stamp_duty_rate", "transfer_fee_rate",
        ):
            rules[key] = float(BACKTEST_RULES.get(key, 0)) * fee_multiple
    result = run_backtest(
        rules=rules,
        codes=codes,
        dynamic_pool=True,
        rebalance_dates_path=str(DATES_PATH),
        verbose=False,
    )
    nav = result.get("nav_series") or []
    return {
        "fee_multiple": fee_multiple,
        "metrics": result.get("metrics") or {},
        "rolling36": _window_metrics(nav, 36),
        "rolling48": _window_metrics(nav, 48),
        "continuous_oos": {
            year: _continuous_oos(nav, f"{year}-01-01")
            for year in ("2021", "2023", "2025")
        },
    }


def _metric_differences(candidate: dict, baseline: dict) -> dict:
    keys = ("cagr", "max_drawdown", "sharpe", "ending_nav", "trade_count")
    return {
        key: round(float(candidate.get(key, 0)) - float(baseline.get(key, 0)), 3)
        for key in keys
    }


def _build_variant(name: str, label: str, excluded: list[str], universe: list[str],
                   baseline_normal: dict | None = None,
                   baseline_stress: dict | None = None) -> dict:
    excluded = sorted(set(excluded))
    codes = [code for code in universe if code not in set(excluded)]
    normal = _run_once(codes, 1)
    stress = _run_once(codes, 3)
    row = {
        "name": name,
        "label": label,
        "excluded_codes": excluded,
        "remaining_universe_count": len(codes),
        "normal_cost": normal,
        "triple_cost": stress,
    }
    if baseline_normal is not None and baseline_stress is not None:
        row["difference_vs_baseline"] = {
            "normal_cost_metrics": _metric_differences(
                normal["metrics"], baseline_normal["metrics"]
            ),
            "triple_cost_metrics": _metric_differences(
                stress["metrics"], baseline_stress["metrics"]
            ),
            "rolling36_min_cagr": round(
                float(normal["rolling36"].get("min_cagr", 0))
                - float(baseline_normal["rolling36"].get("min_cagr", 0)), 3
            ),
            "rolling48_min_cagr": round(
                float(normal["rolling48"].get("min_cagr", 0))
                - float(baseline_normal["rolling48"].get("min_cagr", 0)), 3
            ),
            "continuous_oos_cagr": {
                year: round(
                    float(normal["continuous_oos"][year].get("cagr", 0))
                    - float(baseline_normal["continuous_oos"][year].get("cagr", 0)), 3
                ) for year in ("2021", "2023", "2025")
            },
        }
    return row


def main() -> None:
    manifest, dates_payload, attribution, dates = _load_inputs()
    universe = [str(code).zfill(6) for code in manifest.get("codes") or []]
    ranked = attribution.get("per_stock_pl") or []
    if len(ranked) < 3:
        raise ValueError("第 29 轮归因不足以确定前三大盈利股票")
    top_codes = [str(item["code"]).zfill(6) for item in ranked[:3]]

    baseline = _build_variant("baseline", "冻结 V1 基线", [], universe)
    baseline_normal = baseline["normal_cost"]
    baseline_stress = baseline["triple_cost"]
    baseline["difference_vs_baseline"] = {
        "normal_cost_metrics": _metric_differences(
            baseline_normal["metrics"], baseline_normal["metrics"]
        ),
        "triple_cost_metrics": _metric_differences(
            baseline_stress["metrics"], baseline_stress["metrics"]
        ),
        "rolling36_min_cagr": 0.0,
        "rolling48_min_cagr": 0.0,
        "continuous_oos_cagr": {year: 0.0 for year in ("2021", "2023", "2025")},
    }
    variants = [baseline]
    variants.append(_build_variant(
        "exclude_top1", "剔除最大盈利股票", top_codes[:1], universe,
        baseline_normal, baseline_stress,
    ))
    variants.append(_build_variant(
        "exclude_top3", "同时剔除前三大盈利股票", top_codes, universe,
        baseline_normal, baseline_stress,
    ))
    for industry, codes in CURRENT_INDUSTRY_PROXY.items():
        variants.append(_build_variant(
            f"exclude_industry_{industry}", f"剔除行业代理簇：{industry}",
            list(codes), universe, baseline_normal, baseline_stress,
        ))

    output = {
        "round": 30,
        "description": "冻结 V1 的收益集中度脆弱性审计，不调参",
        "preregistered_question": (
            "剔除历史最大盈利股票、前三大贡献股票或实际持仓的行业代理簇后，"
            "V1 的完整样本、滚动窗口、连续样本外和三倍费用表现是否迅速恶化。"
        ),
        "v1_rules": V1_RULES,
        "inputs": {
            "manifest_records_sha256": manifest["records_sha256"],
            "dates_sha256": dates_payload["dates_sha256"],
            "data_cutoff": manifest["as_of"],
            "dates": {"count": len(dates), "first": dates[0], "last": dates[-1]},
            "attribution_file": "data/round29_attribution.json",
            "top_profit_codes_from_round29": top_codes,
        },
        "industry_proxy": {
            "method": (
                "冻结缓存和 manifest 没有历史点时行业字段；仅将第 29 轮实际交易的 18 只股票"
                "按截至本轮研究时的主营业务宽口径人工分组，映射固化于本脚本。"
            ),
            "groups": {key: list(value) for key, value in CURRENT_INDUSTRY_PROXY.items()},
            "coverage": {
                "traded_stock_count": len(ranked),
                "mapped_traded_stock_count": len({code for values in CURRENT_INDUSTRY_PROXY.values() for code in values}),
                "manifest_stock_count": len(universe),
            },
            "limitation": (
                "该代理不是历史点时行业库，也没有覆盖未曾成交的全部候选；行业剔除结果只衡量"
                "已实现收益对已交易主营行业簇的依赖，不能证明全市场行业暴露或行业中性。"
            ),
        },
        "tradeable_total_return_benchmark": _build_benchmark(dates, baseline_normal),
        "variants": variants,
        "audit": {
            "parameter_search": "无；所有变体使用同一套冻结 V1 参数。",
            "future_function_check": "信号只读信号日及之前数据，下一可用交易日执行。",
            "oos_definition": "从完整账本 NAV 连续切片，不重置现金和持仓。",
            "survivorship_bias": "冻结 manifest 仍缺少退市股票，不能排除幸存者偏差。",
            "investment_warning": "历史回测不代表未来收益，不构成投资建议。",
        },
    }
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入：{OUTPUT_PATH}")
    for row in variants:
        metrics = row["normal_cost"]["metrics"]
        print(f"{row['label']}：CAGR {metrics.get('cagr')}%，最大回撤 {metrics.get('max_drawdown')}%，交易 {metrics.get('trade_count')} 次")


if __name__ == "__main__":
    main()
