"""在临时目录预演 V1/V2/V3 首期信号、执行和公开业绩链路。

演练只复制正式前向缓存，并用最后一个已知收盘价补出指定的未来交易日；不会
修改正式缓存、正式账本或公开页面。模拟价格只用于验证程序控制流，不是预测。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_rebalance_dates import build_dates
from build_universe_manifest import records_from_cache
from forward_daily import baostock_trading_days, decide_action
from forward_performance import build_strategy_suite
from monthly_forward import (
    FORWARD_CACHE_DIR,
    V1_START_DATE,
    _load_journal,
    record_execution,
    record_signal,
    strategy_profile,
    verify_forward_contract,
)
from quarterly_strategy import transaction_fees
from universe_manifest import build_manifest, write_manifest


DEFAULT_SIGNAL_DATE = "2026-08-31"
DEFAULT_EXECUTION_DATE = "2026-09-01"
EXPECTED_CODE_COUNT = 210


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.exists():
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _inject_prices(cache_dir: Path, signal_date: str, execution_date: str) -> int:
    """缺少演练日期时沿用最近已知价；已有真实价时保持原值。"""
    count = 0
    for path in sorted(cache_dir.glob("kl_*.json")):
        prices = _read_json(path)
        if not isinstance(prices, dict):
            raise ValueError(f"K 线缓存格式无效: {path.name}")
        for target in (signal_date, execution_date):
            if target in prices:
                continue
            known = [
                (str(day)[:10], float(price))
                for day, price in prices.items()
                if str(day)[:10] < target and float(price or 0) > 0
            ]
            if not known:
                raise ValueError(f"{path.name} 在 {target} 前没有可用价格")
            prices[target] = max(known, key=lambda item: item[0])[1]
        _write_json(path, prices)
        count += 1
    return count


def _build_inputs(cache_dir: Path, input_dir: Path, as_of: str) -> tuple[Path, Path, dict[str, Any]]:
    marker = _read_json(cache_dir / "price_format.json")
    if marker.get("format") != "unadjusted_close":
        raise ValueError("演练缓存不是不复权收盘价")
    records = records_from_cache(cache_dir, as_of)
    manifest = build_manifest(
        records,
        as_of=as_of,
        top=0,
        min_years=0,
        source={
            "name": "isolated_forward_rehearsal",
            "path": str(cache_dir).replace("\\", "/"),
            "point_in_time_cutoff": as_of,
            "price_format": marker["format"],
            "price_source": "synthetic_carry_forward_for_control_flow_only",
        },
    )
    manifest["rules"]["pool_mode"] = "dynamic"
    manifest["rules"]["pool_min_consecutive_years"] = 3
    manifest_path = input_dir / "universe_manifest.json"
    dates_path = input_dir / "rebalance_dates_monthly.json"
    write_manifest(manifest_path, manifest)
    dates = build_dates(manifest_path, cache_dir, as_of=as_of)
    _write_json(dates_path, dates)
    return manifest_path, dates_path, manifest


def _trading_days(*days: str):
    values = sorted(date.fromisoformat(day) for day in days)

    def provider(start: date, end: date) -> list[date]:
        return [day for day in values if start <= day <= end]

    return provider


def _performance_market(
    cache_dir: Path,
    executions: dict[str, dict[str, Any]],
    signal_date: str,
    execution_date: str,
) -> dict[str, Any]:
    benchmark_days = sorted({V1_START_DATE, signal_date, execution_date})
    securities = {}
    holdings = [
        row for execution in executions.values()
        for row in execution.get("holdings") or []
    ]
    for row in holdings:
        code = str(row.get("code") or "").zfill(6)
        prices = _read_json(cache_dir / f"kl_{code}.json")
        securities[code] = {
            "name": code,
            "prices": [
                {"date": day, "close": float(price)}
                for day, price in sorted(prices.items())
                if V1_START_DATE <= str(day)[:10] <= execution_date
            ],
            "dividends": _read_json(cache_dir / f"dvd_{code}.json"),
        }
    return {
        "schema_version": 1,
        "as_of": execution_date,
        "price_format": "unadjusted_close",
        "benchmark": {
            "code": "510300",
            "name": "沪深300ETF华泰柏瑞",
            "prices": [
                {"date": day, "close": 4.0, "volume_shares": 100000}
                for day in benchmark_days
            ],
            "dividends": [],
            "sources": {"prices": {"provider": "rehearsal"}, "dividends": {"provider": "rehearsal"}},
        },
        "securities": securities,
        "hashes": {"content_sha256": "isolated-rehearsal"},
    }


def run_rehearsal(
    *,
    source_cache: Path = FORWARD_CACHE_DIR,
    signal_date: str = DEFAULT_SIGNAL_DATE,
    execution_date: str = DEFAULT_EXECUTION_DATE,
    expected_code_count: int = EXPECTED_CODE_COUNT,
    verify_live_calendar: bool = False,
) -> dict[str, Any]:
    """返回演练报告；任何门禁、信号、执行或公开业绩异常都会抛错。"""
    signal_day = date.fromisoformat(signal_date)
    execution_day = date.fromisoformat(execution_date)
    if execution_day <= signal_day:
        raise ValueError("执行日必须晚于信号日")
    profiles = {key: strategy_profile(key) for key in ("v1", "v2", "v3")}
    for key in profiles:
        verify_forward_contract(strategy_id=key)
    production_journal_before = {
        key: _file_sha256(profile["journal_path"])
        for key, profile in profiles.items()
    }
    calendar_check = {
        "source": "fixed_rehearsal_dates",
        "trading_days": [signal_date, execution_date],
        "status": "未联网复核",
    }
    if verify_live_calendar:
        official_days = baostock_trading_days(signal_day, execution_day)
        expected_days = {signal_day, execution_day}
        if not expected_days.issubset(set(official_days)):
            raise RuntimeError("BaoStock 没有把演练信号日和执行日同时标记为交易日")
        calendar_check = {
            "source": "baostock_query_trade_dates",
            "trading_days": [day.isoformat() for day in official_days],
            "status": "通过",
        }

    with tempfile.TemporaryDirectory(prefix="v123-forward-rehearsal-") as raw_temp:
        root = Path(raw_temp)
        cache_dir = root / "cache"
        input_dir = root / "inputs"
        journal_paths = {
            key: root / f"monthly_{key}.jsonl" for key in profiles
        }
        shutil.copytree(source_cache, cache_dir)
        code_count = _inject_prices(cache_dir, signal_date, execution_date)
        if code_count != expected_code_count:
            raise ValueError(f"演练缓存应有 {expected_code_count} 只股票，实际 {code_count} 只")

        manifest_path, dates_path, signal_manifest = _build_inputs(cache_dir, input_dir, signal_date)
        signal_plan = decide_action(signal_day, [], _trading_days(signal_date, execution_date))
        if signal_plan.get("action") != "signal":
            raise RuntimeError(f"信号日门禁没有触发 signal: {signal_plan}")
        signals = {}
        for key, journal_path in journal_paths.items():
            signal, signal_appended = record_signal(
                signal_date,
                manifest_path=manifest_path,
                dates_path=dates_path,
                cache_dir=cache_dir,
                journal_path=journal_path,
                strategy_id=key,
                allow_isolated_journal=True,
            )
            if not signal_appended:
                raise RuntimeError(f"{profiles[key]['version']} 隔离演练未能追加首期信号")
            signals[key] = signal

        manifest_path, dates_path, execution_manifest = _build_inputs(cache_dir, input_dir, execution_date)
        executions = {}
        execution_plans = {}
        journals = {}
        strategy_reports = {}
        metadatas = {key: _read_json(profile["metadata_path"]) for key, profile in profiles.items()}
        for key, journal_path in journal_paths.items():
            execution_plan = decide_action(
                execution_day,
                _load_journal(journal_path),
                _trading_days(signal_date, execution_date),
            )
            if execution_plan.get("action") != "execute":
                raise RuntimeError(f"{profiles[key]['version']} 执行日门禁没有触发 execute: {execution_plan}")
            execution, execution_appended = record_execution(
                signal_date[:7],
                manifest_path=manifest_path,
                dates_path=dates_path,
                cache_dir=cache_dir,
                journal_path=journal_path,
                strategy_id=key,
                allow_isolated_journal=True,
            )
            if not execution_appended:
                raise RuntimeError(f"{profiles[key]['version']} 隔离演练未能追加首期执行")
            if not execution.get("operations") or not execution.get("holdings"):
                raise RuntimeError(f"{profiles[key]['version']} 演练执行没有产生模拟买入或持仓")

            metadata = metadatas[key]
            lot = int((metadata.get("rules") or {}).get("lot_size") or 100)
            next_lot_costs = []
            for holding in execution["holdings"]:
                code = str(holding.get("code") or "").zfill(6)
                price = float(_read_json(cache_dir / f"kl_{code}.json")[execution_date])
                gross = price * lot
                fee = transaction_fees(gross, "buy", code, metadata.get("rules") or {})["total"]
                next_lot_costs.append(gross + fee)
            minimum_next_lot_cost = round(min(next_lot_costs), 2)
            if float(execution["cash"]) + 1e-8 >= minimum_next_lot_cost:
                raise RuntimeError(
                    f"{profiles[key]['version']} 演练执行后仍可买一手，不符合全量投入合同"
                )
            journal = _load_journal(journal_path)
            executions[key] = execution
            execution_plans[key] = execution_plan
            journals[key] = journal
            strategy_reports[key] = {
                "version": profiles[key]["version"],
                "shadow": profiles[key]["shadow"],
                "max_holdings": profiles[key]["max_holdings"],
                "signal": {
                    "candidate_pool_count": signals[key]["candidate_pool"]["count"],
                    "eligible_entry_count": len(signals[key]["decision_snapshot"]["eligible_entry_codes"]),
                },
                "execution": {
                    "operation_count": len(execution["operations"]),
                    "holding_count": len(execution["holdings"]),
                    "cash": execution["cash"],
                    "nav": execution["nav"],
                    "invested_pct": round((1 - float(execution["cash"]) / float(execution["nav"])) * 100, 4),
                    "minimum_next_lot_cost": minimum_next_lot_cost,
                    "residual_cash_below_one_lot": True,
                },
                "isolated_journal": {
                    "path": journal_path.name,
                    "sha256": _file_sha256(journal_path),
                    "signal_count": sum(row.get("event_type") == "signal" for row in journal),
                    "execution_count": sum(row.get("event_type") == "execution" for row in journal),
                },
            }

        performance = build_strategy_suite(
            metadatas,
            journals,
            _performance_market(cache_dir, executions, signal_date, execution_date),
        )
        pre_execution = [row for row in performance["series"] if row["date"] < execution_date]
        if any(
            row["v1_return_pct"] != 0 or row["v2_return_pct"] != 0
            or row["v3_return_pct"] != 0 or row["benchmark_return_pct"] != 0
            for row in pre_execution
        ):
            raise RuntimeError("首笔执行前 V1/V2/V3 或 510300 提前产生收益")
        if performance["benchmark"].get("inception_date") != execution_date:
            raise RuntimeError("510300 没有在 V1 首笔执行日同步建仓")

        report = {
            "schema_version": 2,
            "kind": "v123_first_cycle_isolated_rehearsal",
            "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "synthetic_data": True,
            "synthetic_price_rule": "未来日期缺价时沿用该股票最近已知收盘价；仅验证控制流，不是预测或选股信号",
            "source_cache": _portable_path(source_cache),
            "code_count": code_count,
            "calendar": calendar_check,
            "signal": {
                "date": signal_date,
                "plan": signal_plan["action"],
                "candidate_pool_count": strategy_reports["v1"]["signal"]["candidate_pool_count"],
                "eligible_entry_count": strategy_reports["v1"]["signal"]["eligible_entry_count"],
                "manifest_records_sha256": signal_manifest["records_sha256"],
            },
            "execution": {
                "date": execution_date,
                "plan": "execute",
                **strategy_reports["v1"]["execution"],
                "manifest_records_sha256": execution_manifest["records_sha256"],
            },
            "strategies": strategy_reports,
            "public_performance": {
                "strategy_total_assets": performance["strategy"]["total_assets"],
                "strategy_total_assets_by_version": {
                    key: value["summary"]["total_assets"]
                    for key, value in performance["strategies"].items()
                },
                "benchmark_total_assets": performance["benchmark"]["total_assets"],
                "benchmark_inception_date": performance["benchmark"]["inception_date"],
                "transaction_count": len(performance["transactions"]),
                "transaction_count_by_version": {
                    key: len(value["transactions"])
                    for key, value in performance["strategies"].items()
                },
            },
            "isolated_journal": {
                "signal_count": strategy_reports["v1"]["isolated_journal"]["signal_count"],
                "execution_count": strategy_reports["v1"]["isolated_journal"]["execution_count"],
            },
            "production_journal_sha256_before": production_journal_before,
            "status": "通过",
        }

    production_journal_after = {
        key: _file_sha256(profile["journal_path"])
        for key, profile in profiles.items()
    }
    if production_journal_after != production_journal_before:
        raise RuntimeError("演练意外修改了正式 V1/V2/V3 账本")
    report["production_journal_sha256_after"] = production_journal_after
    report["production_journals_unchanged"] = True
    report["production_journal_unchanged"] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离预演 V1/V2/V3 首期信号、执行和公开业绩链路")
    parser.add_argument("--source-cache", type=Path, default=FORWARD_CACHE_DIR)
    parser.add_argument("--signal-date", default=DEFAULT_SIGNAL_DATE)
    parser.add_argument("--execution-date", default=DEFAULT_EXECUTION_DATE)
    parser.add_argument("--expected-code-count", type=int, default=EXPECTED_CODE_COUNT)
    parser.add_argument("--verify-live-calendar", action="store_true", help="联网复核两个日期均为交易日")
    parser.add_argument("--output", type=Path, help="可选的演练报告 JSON 输出路径")
    args = parser.parse_args()
    report = run_rehearsal(
        source_cache=args.source_cache,
        signal_date=args.signal_date,
        execution_date=args.execution_date,
        expected_code_count=args.expected_code_count,
        verify_live_calendar=args.verify_live_calendar,
    )
    if args.output:
        _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
