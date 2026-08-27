"""构建独立历史缓存并用冻结 V1 规则做临时幸存者偏差回放。

该回放只补入 BaoStock 识别的 2015 年后退市且曾连续三年正分红股票。
它没有补齐 5,212 只在市股票的全历史分红，也没有独立核验数据源，结果只能
作为 provisional（临时审计），不能替代当前正式基线。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import backtest
from build_historical_universe import ROOT, canonical_sha256, price_record_is_valid, write_json_atomic


def convert_price_rows(stock: dict[str, Any], through_date: str) -> dict[str, float]:
    if not price_record_is_valid(stock):
        raise ValueError(f"{stock.get('code')} 价格行校验失败")
    delist_date = stock["delist_date"]
    prices = {}
    for row in stock["rows"]:
        day = row["date"][:10]
        if day > delist_date or day > through_date:
            continue
        try:
            close = float(row["close"])
        except (TypeError, ValueError):
            continue
        if close > 0:
            prices[day] = close
    return prices


def convert_dividends(stock: dict[str, Any], through_date: str) -> tuple[list[dict], list[dict]]:
    """把 BaoStock 每股送转口径转换为回测使用的每 10 股口径。"""
    details = []
    by_year: dict[int, dict[str, float]] = {}
    for row in stock.get("records", []):
        year = int(row["report_year"])
        ex_date = str(row.get("ex_date") or "")[:10]
        dps = float(row.get("cash_per_share_before_tax") or 0)
        bonus = float(row.get("stock_dividend_per_share") or 0) * 10
        transfer = float(row.get("reserve_to_stock_per_share") or 0) * 10
        if len(ex_date) == 10 and ex_date <= through_date:
            details.append({
                "year": year, "ex_date": ex_date, "dps": round(dps, 6),
                "bonus_ratio": round(bonus, 6), "transfer_ratio": round(transfer, 6),
            })
        annual = by_year.setdefault(
            year, {"dps": 0.0, "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        )
        annual["dps"] += dps
        annual["bonus_ratio"] = max(annual["bonus_ratio"], bonus)
        annual["transfer_ratio"] = max(annual["transfer_ratio"], transfer)
    summaries = [
        {"year": year, "dps": round(value["dps"], 6),
         "bonus_ratio": round(value["bonus_ratio"], 6),
         "transfer_ratio": round(value["transfer_ratio"], 6)}
        for year, value in sorted(by_year.items()) if year <= int(through_date[:4])
    ]
    details.sort(key=lambda row: (row["ex_date"], row["year"]))
    return summaries, details


def copy_frozen_cache(source: Path, destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in source.glob("*.json"):
        shutil.copy2(path, destination / path.name)
        count += 1
    return count


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_replay(
    security_master_path: Path,
    dividends_path: Path,
    prices_path: Path,
    cache_dir: Path,
    output: Path,
    through_date: str = "2026-08-25",
) -> dict[str, Any]:
    security_master = json.loads(security_master_path.read_text(encoding="utf-8"))
    dividends = json.loads(dividends_path.read_text(encoding="utf-8"))
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    if not all(item.get("provider_complete") for item in
               (security_master, dividends, prices)):
        raise RuntimeError("历史数据源流水线尚未完成，拒绝回放")

    baseline = json.loads((ROOT / "data" / "current_best.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "data" / "universe_manifest.json").read_text(encoding="utf-8"))
    copied = copy_frozen_cache(ROOT / "data" / "backtest_cache", cache_dir)
    dividends_by_code = {item["code"]: item for item in dividends["stocks"]}
    master_by_code = {item["code"]: item for item in security_master["records"]}
    eligible_codes = []
    for stock in prices["stocks"]:
        code = stock["code"]
        price_map = convert_price_rows(stock, through_date)
        summaries, details = convert_dividends(dividends_by_code[code], through_date)
        write_json_atomic(cache_dir / f"kl_{code}.json", price_map)
        write_json_atomic(cache_dir / f"dv_{code}.json", summaries)
        write_json_atomic(cache_dir / f"dvd_{code}.json", details)
        eligible_codes.append(code)

    codes = sorted(set(manifest["codes"]) | set(eligible_codes))
    listing_windows = {
        code: {
            "list_date": master_by_code[code]["list_date"],
            "delist_date": master_by_code[code]["delist_date"],
        }
        for code in codes if code in master_by_code
    }
    expected_control = {
        "cagr": baseline["metrics"]["cagr"],
        "max_drawdown": baseline["metrics"]["max_drawdown"],
        "trade_count": baseline["metrics"]["trade_count"],
    }
    original_cache = backtest.CACHE_DIR
    try:
        backtest.CACHE_DIR = cache_dir
        control_result = backtest.run_backtest(
            rules={**baseline["rules"], "through_date": through_date},
            codes=manifest["codes"],
            rebalance_dates_path=ROOT / "data" / "rebalance_dates_monthly.json",
            dynamic_pool=True,
            listing_windows={code: listing_windows[code] for code in manifest["codes"]
                             if code in listing_windows},
            delisting_recovery_rate=0.0,
            verbose=False,
        )
        actual_control = {key: control_result["metrics"][key] for key in expected_control}
        control_matches = actual_control == expected_control
        if not control_matches:
            raise RuntimeError(
                f"同缓存 V1 控制组不匹配: expected={expected_control}, actual={actual_control}"
            )
        scenarios = {}
        for name, recovery_rate in (("zero_recovery", 0.0), ("last_close_recovery", 1.0)):
            result = backtest.run_backtest(
                rules={**baseline["rules"], "through_date": through_date},
                codes=codes,
                rebalance_dates_path=ROOT / "data" / "rebalance_dates_monthly.json",
                dynamic_pool=True,
                listing_windows=listing_windows,
                delisting_recovery_rate=recovery_rate,
                verbose=False,
                return_events=True,
            )
            events = result.pop("_events", [])
            delisting_events = [event for event in events if event.get("side") == "delisting"]
            traded_delisted = sorted({
                str(event.get("code") or "").zfill(6) for event in events
                if str(event.get("code") or "").zfill(6) in set(eligible_codes)
            })
            scenarios[name] = {
                "recovery_rate": recovery_rate,
                "metrics": result["metrics"],
                "traded_delisted_codes": traded_delisted,
                "delisting_events": delisting_events,
                "unresolved_delisted_final_holdings": [
                    holding for holding in result["final_holdings"]
                    if holding["code"] in set(eligible_codes)
                ],
                "result": result,
            }
    finally:
        backtest.CACHE_DIR = original_cache
    payload = {
        "schema_version": 1,
        "status": "provisional",
        "authoritative_baseline_replaced": False,
        "through_date": through_date,
        "frozen_v1_rules_sha256": canonical_sha256(baseline["rules"]),
        "baseline_manifest_records_sha256": manifest["records_sha256"],
        "inputs": {
            "security_master_sha256": file_sha256(security_master_path),
            "delisted_dividends_sha256": file_sha256(dividends_path),
            "eligible_delisted_prices_sha256": file_sha256(prices_path),
        },
        "cache": {
            "path": cache_dir.resolve().relative_to(ROOT.resolve()).as_posix(),
            "copied_frozen_files": copied,
            "baseline_codes": len(manifest["codes"]),
            "eligible_delisted_codes": len(eligible_codes),
            "combined_codes": len(codes),
        },
        "provider_complete": True,
        "independently_verified": False,
        "same_cache_control": {
            "matches_frozen_v1": control_matches,
            "expected_core_metrics": expected_control,
            "actual_core_metrics": actual_control,
            "metrics": control_result["metrics"],
            "codes": len(manifest["codes"]),
            "cache_path": cache_dir.resolve().relative_to(ROOT.resolve()).as_posix(),
            "rebalance_dates_path": "data/rebalance_dates_monthly.json",
        },
        "baseline_metrics": baseline["metrics"],
        "scenarios": scenarios,
        "limitations": [
            "只补入 255 只 2015 年后退市股中的 139 只连续三年正分红股票。",
            "尚未为 5,212 只在市股票重建全历史分红，可能遗漏历史曾高息但不在冻结 210 只缓存中的股票。",
            "BaoStock 查询完成不等于独立核验，不能声称已消除幸存者偏差。",
            "缺少真实退市清算、换股或终止上市处置数据；归零与末收盘价全额回收仅给出压力测试边界。",
        ],
    }
    write_json_atomic(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--security-master", type=Path,
                        default=ROOT / "data" / "historical" / "security_master.json")
    parser.add_argument("--dividends", type=Path,
                        default=ROOT / "data" / "historical" / "delisted_dividends.json")
    parser.add_argument("--prices", type=Path,
                        default=ROOT / "data" / "historical" / "eligible_delisted_prices.json")
    parser.add_argument("--cache-dir", type=Path,
                        default=ROOT / "data" / "historical_v1_cache")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data" / "historical_v1_provisional.json")
    parser.add_argument("--through-date", default="2026-08-25")
    args = parser.parse_args()
    result = run_replay(
        args.security_master, args.dividends, args.prices, args.cache_dir,
        args.output, args.through_date,
    )
    print(json.dumps({"status": result["status"],
                      "scenario_metrics": {name: value["metrics"]
                                           for name, value in result["scenarios"].items()},
                      "delisting_events": {name: value["delisting_events"]
                                           for name, value in result["scenarios"].items()}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
