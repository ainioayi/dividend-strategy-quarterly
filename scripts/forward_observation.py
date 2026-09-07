"""从公开绩效快照生成固定口径的前向观察评估。"""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date
from typing import Any


STRATEGY_IDS = ("v1", "v2", "v3", "v5", "ma_v22")
PLAN = {
    "version": "1.0.0",
    "created_at": "2026-09-07",
    "registration": "观察已开始后制定，不能宣称前瞻预注册",
    "strategy_ids": list(STRATEGY_IDS),
    "checkpoint_months": [6, 12],
    "metrics": [
        "区间收益率",
        "日频最大回撤",
        "买卖双边换手率",
        "交易费用",
        "相对共同510300基准收益",
    ],
    "turnover_definition": "观察期内买入和卖出成交额之和 / 含期初基数的每日资产均值；双边口径，不年化",
    "review_policy": "6个月和12个月检查点仅供人工复核，不自动排名或切换策略",
}


def _sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _return_pct(first: float, last: float) -> float:
    return round((last / first - 1) * 100, 6) if first else 0.0


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            worst = max(worst, (peak - value) / peak)
    return round(worst * 100, 4)


def _month_keys(start: date, end: date) -> list[tuple[int, int]]:
    keys = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        keys.append((cursor.year, cursor.month))
        cursor = _add_months(cursor, 1)
    return keys


def _base_nav(rows: list[dict[str, Any]], start: str, initial: float) -> float:
    before = [row for row in rows if row["date"] < start]
    return float(before[-1]["nav"]) if before else initial


def _period_metrics(
    rows: list[dict[str, Any]], transactions: list[dict[str, Any]], base_nav: float,
) -> dict[str, Any]:
    navs = [float(row["nav"]) for row in rows]
    values = [base_nav, *navs]
    trades = [row for row in transactions if row.get("side") in ("买入", "卖出")]
    average_nav = sum(values) / len(values)
    gross = sum(abs(float(row.get("gross") or 0)) for row in trades)
    return {
        "return_pct": _return_pct(base_nav, navs[-1]),
        "max_drawdown_pct": _max_drawdown(values),
        "two_way_turnover_pct": round(gross / average_nav * 100, 4) if average_nav else None,
        "fees": round(sum(float(row.get("fees") or 0) for row in trades), 2),
    }


def _awaiting_execution(performance: dict[str, Any], quality_status: str) -> dict[str, Any]:
    as_of = str(performance.get("as_of") or "")[:10] or None
    return {
        "schema_version": 1,
        "plan": json.loads(json.dumps(PLAN, ensure_ascii=False)),
        "plan_sha256": _sha256(PLAN),
        "period": {"start_date": None, "as_of": as_of, "calendar_days": 0, "observation_months": 0},
        "data_quality": {"status": quality_status, "allows_conclusion": False,
                         "warnings": list((performance.get("data_quality") or {}).get("warnings") or [])},
        "checkpoints": [
            {"months": months, "due_date": None, "review_date": None, "status": "pending"}
            for months in PLAN["checkpoint_months"]
        ],
        "strategies": {
            strategy_id: {
                "name": ((performance.get("strategies") or {}).get(strategy_id, {}).get("summary") or {}).get("name", strategy_id),
                "return_pct": None, "max_drawdown_pct": None, "two_way_turnover_pct": None,
                "fees": 0.0, "benchmark_return_pct": None, "excess_return_pct": None,
                "daily_observations": 0,
            }
            for strategy_id in STRATEGY_IDS
        },
        "monthly_records": [],
        "conclusion": {"status": "awaiting_execution", "ranking_allowed": False,
                       "automatic_switch": False, "message": "尚无首笔执行，观察期尚未开始。"},
        "audit": {"history_corrections": list((performance.get("audit") or {}).get("history_corrections") or [])},
        "limitations": [
            "观察计划制定于 2026-09-07；观察尚未开始，不能产生策略比较结论。",
            "历史回测和前向模拟不代表未来收益，也不是买卖建议。",
        ],
    }


def build_observation(performance: dict[str, Any]) -> dict[str, Any]:
    """接受 ``build_strategy_suite`` 的 schema 4 结果并返回观察对象。"""
    if performance.get("schema_version") != 4:
        raise ValueError("前向观察只接受 build_strategy_suite schema 4")
    strategies = performance.get("strategies") or {}
    if any(strategy_id not in strategies for strategy_id in STRATEGY_IDS):
        raise ValueError("前向观察缺少五套策略")

    quality = performance.get("data_quality") or {}
    quality_status = str(quality.get("status") or "missing")
    start_text = str((performance.get("benchmark") or {}).get("inception_date") or "")[:10]
    as_of_text = str(performance.get("as_of") or "")[:10]
    if not start_text:
        return _awaiting_execution(performance, quality_status)
    start, as_of = date.fromisoformat(start_text), date.fromisoformat(as_of_text)
    if as_of < start:
        raise ValueError("绩效截止日早于首笔执行日")

    combined = sorted(performance.get("series") or [], key=lambda row: row["date"])
    all_benchmark_rows = [
        {"date": row["date"], "nav": row["benchmark_nav"]}
        for row in combined
        if row["date"] <= as_of_text
    ]
    benchmark_rows = [row for row in all_benchmark_rows if row["date"] >= start_text]
    if not benchmark_rows:
        raise ValueError("观察期缺少共同 510300 基准序列")
    benchmark_initial = float((strategies["v1"].get("summary") or {}).get("initial_capital") or 100000)
    benchmark_base = _base_nav(all_benchmark_rows, start_text, benchmark_initial)
    benchmark_return = _return_pct(benchmark_base, float(benchmark_rows[-1]["nav"]))

    results: dict[str, Any] = {}
    filtered_series: dict[str, list[dict[str, Any]]] = {}
    for strategy_id in STRATEGY_IDS:
        item = strategies[strategy_id]
        all_rows = sorted(
            (row for row in item.get("series") or [] if row["date"] <= as_of_text),
            key=lambda row: row["date"],
        )
        rows = [row for row in all_rows if row["date"] >= start_text]
        if not rows:
            raise ValueError(f"观察期缺少策略 {strategy_id} 序列")
        filtered_series[strategy_id] = rows
        transactions = [
            row for row in item.get("transactions") or []
            if start_text <= str(row.get("date") or "")[:10] <= as_of_text
            and row.get("side") in ("买入", "卖出")
        ]
        initial = float((item.get("summary") or {}).get("initial_capital") or 100000)
        metrics = _period_metrics(rows, transactions, _base_nav(all_rows, start_text, initial))
        strategy_return = metrics["return_pct"]
        results[strategy_id] = {
            "name": (item.get("summary") or {}).get("name", strategy_id),
            **metrics,
            "benchmark_return_pct": benchmark_return,
            "excess_return_pct": round(strategy_return - benchmark_return, 6),
            "daily_observations": len(rows),
        }

    monthly_records = []
    for year, month in _month_keys(start, as_of):
        month_start = max(start, date(year, month, 1))
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        period_end = min(as_of, month_end)
        benchmark_month_rows = [
            row for row in all_benchmark_rows if month_start.isoformat() <= row["date"] <= period_end.isoformat()
        ]
        benchmark_month_base = _base_nav(all_benchmark_rows, month_start.isoformat(), benchmark_initial)
        benchmark_month_return = (
            _return_pct(benchmark_month_base, float(benchmark_month_rows[-1]["nav"]))
            if benchmark_month_rows else None
        )
        month_strategies = {}
        for strategy_id, all_rows in filtered_series.items():
            item = strategies[strategy_id]
            month_rows = [
                row for row in all_rows if month_start.isoformat() <= row["date"] <= period_end.isoformat()
            ]
            if not month_rows:
                month_strategies[strategy_id] = {
                    "return_pct": None, "max_drawdown_pct": None,
                    "two_way_turnover_pct": None, "fees": 0.0, "excess_return_pct": None,
                }
                continue
            initial = float((item.get("summary") or {}).get("initial_capital") or 100000)
            base = _base_nav(all_rows, month_start.isoformat(), initial)
            month_transactions = [
                row for row in item.get("transactions") or []
                if month_start.isoformat() <= str(row.get("date") or "")[:10] <= period_end.isoformat()
            ]
            metrics = _period_metrics(month_rows, month_transactions, base)
            month_strategies[strategy_id] = {
                **metrics,
                "excess_return_pct": round(metrics["return_pct"] - benchmark_month_return, 6)
                if benchmark_month_return is not None else None,
            }
        monthly_records.append({
            "month": f"{year:04d}-{month:02d}",
            "start_date": max(start, date(year, month, 1)).isoformat(),
            "end_date": min(as_of, month_end).isoformat(),
            "is_complete": as_of >= month_end,
            "benchmark_return_pct": benchmark_month_return,
            "strategies": month_strategies,
        })

    checkpoints = []
    for months in PLAN["checkpoint_months"]:
        due_date = _add_months(start, months)
        review_date = next(
            (row["date"] for row in benchmark_rows if date.fromisoformat(row["date"]) >= due_date),
            None,
        )
        checkpoints.append({
            "months": months,
            "due_date": due_date.isoformat(),
            "review_date": review_date,
            "status": "ready_for_manual_review" if review_date else "pending",
        })
    if quality_status != "passed":
        conclusion_status = "data_quality_blocked"
        message = "数据质量未通过，当前指标仅供排查，不作排名或通过结论。"
    elif checkpoints[0]["status"] == "pending":
        conclusion_status = "insufficient_observation"
        message = "观察不足6个月，不作策略排名或切换结论。"
    else:
        conclusion_status = "manual_review_required"
        message = "已到检查点，只能人工复核；系统不会自动排名或切换策略。"

    return {
        "schema_version": 1,
        "plan": json.loads(json.dumps(PLAN, ensure_ascii=False)),
        "plan_sha256": _sha256(PLAN),
        "period": {
            "start_date": start_text,
            "as_of": as_of_text,
            "calendar_days": (as_of - start).days,
            "observation_months": sum(as_of >= _add_months(start, months) for months in range(1, 13)),
        },
        "data_quality": {
            "status": quality_status,
            "allows_conclusion": quality_status == "passed",
            "warnings": list(quality.get("warnings") or []),
        },
        "checkpoints": checkpoints,
        "strategies": results,
        "monthly_records": monthly_records,
        "conclusion": {
            "status": conclusion_status,
            "ranking_allowed": False,
            "automatic_switch": False,
            "message": message,
        },
        "audit": {
            "history_corrections": list((performance.get("audit") or {}).get("history_corrections") or []),
        },
        "limitations": [
            "观察计划制定于 2026-09-07，晚于首笔执行，不能称为前瞻预注册。",
            "首批已发布日线曾受持仓行情更新缺陷影响；修复后按原始账本和可得行情重算，不把旧展示视为正常数据。",
            "冻结历史集合可能缺少退市股票，仍存在幸存者偏差。",
            "历史回测和前向模拟不代表未来收益，也不是买卖建议。",
        ],
    }
