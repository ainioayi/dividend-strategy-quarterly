"""构建经过人工数据质量门禁的历史股票池，并原样回放冻结 V1。

本入口允许排除双源或官方证据无法闭合的特殊数据股票，但过滤只能依据事前
数据质量和冻结 V1 的信号条件，不能依据回测盈亏。结果属于
complete_with_exclusions，不代表已经得到无偏的全市场历史回测。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import backtest
from build_historical_universe import ROOT, canonical_sha256, write_json_atomic
from build_universe_manifest import records_from_cache
from build_historical_v1_replay import (
    convert_dividends,
    convert_price_rows,
    copy_frozen_cache,
    file_sha256,
)
from universe_manifest import build_manifest, write_manifest


DEFAULT_SECURITY = ROOT / "data" / "historical" / "security_master_verification.json"
DEFAULT_DELISTED_DIVIDENDS = (
    ROOT / "data" / "historical" / "delisted_dividends_verified.json"
)
DEFAULT_DELISTED_PRICES = (
    ROOT / "data" / "historical" / "eligible_delisted_prices_verified.json"
)
DEFAULT_LISTED_DIVIDENDS = ROOT / "data" / "historical" / "listed_dividends.json"
DEFAULT_LISTED_PRICE_MANIFEST = (
    ROOT / "data" / "historical" / "eligible_listed_prices_manifest.json"
)
DEFAULT_CACHE = ROOT / "data" / "historical_filtered_cache"
DEFAULT_OUTPUT = ROOT / "data" / "historical_v1_filtered.json"
DEFAULT_STATUS = ROOT / "data" / "historical_universe_status.json"
DEFAULT_MANIFEST = ROOT / "data" / "historical_filtered_manifest.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_gzip_json(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def convert_listed_dividends(
    stock: dict[str, Any], through_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把在市分红逐笔记录转换为冻结回测缓存格式。"""
    details = []
    annual: dict[int, dict[str, float]] = {}
    for raw in stock.get("records", []):
        ex_date = str(raw.get("ex_date") or "")[:10]
        if len(ex_date) != 10 or ex_date > through_date:
            continue
        year = int(raw["year"])
        row = {
            "year": year,
            "ex_date": ex_date,
            "dps": round(float(raw.get("dps") or 0), 6),
            "bonus_ratio": round(float(raw.get("bonus_ratio") or 0), 6),
            "transfer_ratio": round(float(raw.get("transfer_ratio") or 0), 6),
        }
        details.append(row)
        current = annual.setdefault(
            year, {"dps": 0.0, "bonus_ratio": 0.0, "transfer_ratio": 0.0}
        )
        current["dps"] += row["dps"]
        current["bonus_ratio"] = max(current["bonus_ratio"], row["bonus_ratio"])
        current["transfer_ratio"] = max(
            current["transfer_ratio"], row["transfer_ratio"]
        )
    summaries = [
        {
            "year": year,
            "dps": round(value["dps"], 6),
            "bonus_ratio": round(value["bonus_ratio"], 6),
            "transfer_ratio": round(value["transfer_ratio"], 6),
        }
        for year, value in sorted(annual.items())
    ]
    details.sort(key=lambda row: (row["ex_date"], row["year"]))
    return summaries, details


def convert_listed_price_rows(
    stock: dict[str, Any], through_date: str,
) -> dict[str, float]:
    rows = stock.get("rows")
    if not isinstance(rows, list) or stock.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError(f"{stock.get('code')} 在市价格行或哈希无效")
    prices: dict[str, float] = {}
    for row in rows:
        day = str(row.get("date") or "")[:10]
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if len(day) == 10 and day <= through_date and close > 0:
            prices[day] = close
    if not prices:
        raise ValueError(f"{stock.get('code')} 在市价格没有有效交易日")
    return prices


def _resolve_archive(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    archive = manifest.get("archive") or {}
    path = Path(str(archive.get("path") or ""))
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise RuntimeError(f"在市价格归档不存在: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != archive.get("sha256"):
        raise RuntimeError("在市价格归档哈希与 manifest 不一致")
    return path


def _require_inputs(
    security: dict[str, Any], delisted_dividends: dict[str, Any],
    delisted_prices: dict[str, Any], listed_dividends: dict[str, Any],
    listed_price_manifest: dict[str, Any],
) -> None:
    if security.get("independently_verified") is not True:
        raise RuntimeError("证券主表尚未独立核验")
    if delisted_dividends.get("manual_data_gate_complete") is not True:
        raise RuntimeError("退市分红人工数据门禁尚未完成")
    if delisted_prices.get("independently_verified") is not True:
        raise RuntimeError("退市价格尚未独立核验")
    if listed_dividends.get("manual_data_gate_complete") is not True:
        raise RuntimeError("在市分红人工数据门禁尚未完成")
    if listed_price_manifest.get("manual_data_gate_complete") is not True:
        raise RuntimeError("在市候选价格人工数据门禁尚未完成")
    if listed_price_manifest.get("eligible_scope_independently_verified") is not True:
        raise RuntimeError("在市候选价格门禁没有放行任何独立核验股票")


def _run_backtests(
    cache_dir: Path, codes: list[str], listing_windows: dict[str, dict[str, str]],
    through_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = read_json(ROOT / "data" / "current_best.json")
    manifest = read_json(ROOT / "data" / "universe_manifest.json")
    expected = {
        "cagr": baseline["metrics"]["cagr"],
        "max_drawdown": baseline["metrics"]["max_drawdown"],
        "trade_count": baseline["metrics"]["trade_count"],
    }
    original_cache = backtest.CACHE_DIR
    try:
        backtest.CACHE_DIR = ROOT / "data" / "backtest_cache"
        control = backtest.run_backtest(
            rules={**baseline["rules"], "through_date": through_date},
            codes=manifest["codes"],
            rebalance_dates_path=ROOT / "data" / "rebalance_dates_monthly.json",
            dynamic_pool=True,
            listing_windows={
                code: listing_windows[code]
                for code in manifest["codes"] if code in listing_windows
            },
            delisting_recovery_rate=0.0,
            verbose=False,
        )
        actual = {key: control["metrics"][key] for key in expected}
        if actual != expected:
            raise RuntimeError(
                f"冻结 V1 控制组不匹配: expected={expected}, actual={actual}"
            )
        backtest.CACHE_DIR = cache_dir
        scenarios = {}
        for name, recovery_rate in (
            ("zero_recovery", 0.0), ("last_close_recovery", 1.0)
        ):
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
            scenarios[name] = {
                "recovery_rate": recovery_rate,
                "metrics": result["metrics"],
                "delisting_events": [
                    event for event in events if event.get("side") == "delisting"
                ],
                "result": result,
            }
    finally:
        backtest.CACHE_DIR = original_cache
    control_summary = {
        "matches_frozen_v1": True,
        "expected_core_metrics": expected,
        "actual_core_metrics": actual,
        "metrics": control["metrics"],
        "codes": len(manifest["codes"]),
    }
    return control_summary, scenarios


def run_replay(
    security_path: Path = DEFAULT_SECURITY,
    delisted_dividends_path: Path = DEFAULT_DELISTED_DIVIDENDS,
    delisted_prices_path: Path = DEFAULT_DELISTED_PRICES,
    listed_dividends_path: Path = DEFAULT_LISTED_DIVIDENDS,
    listed_price_manifest_path: Path = DEFAULT_LISTED_PRICE_MANIFEST,
    cache_dir: Path = DEFAULT_CACHE,
    output: Path = DEFAULT_OUTPUT,
    through_date: str = "2026-08-25",
    status_output: Path = DEFAULT_STATUS,
    manifest_output: Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    security = read_json(security_path)
    delisted_dividends = read_json(delisted_dividends_path)
    delisted_prices = read_json(delisted_prices_path)
    listed_dividends = read_json(listed_dividends_path)
    listed_price_manifest = read_json(listed_price_manifest_path)
    _require_inputs(
        security, delisted_dividends, delisted_prices,
        listed_dividends, listed_price_manifest,
    )
    archive_path = _resolve_archive(listed_price_manifest_path, listed_price_manifest)
    listed_price_archive = read_gzip_json(archive_path)

    copy_frozen_cache(ROOT / "data" / "backtest_cache", cache_dir)
    master_by_code = {row["code"]: row for row in security.get("records", [])}
    delisted_dividend_by_code = {
        row["code"]: row for row in delisted_dividends.get("stocks", [])
    }
    delisted_price_by_code = {
        row["code"]: row for row in delisted_prices.get("stocks", [])
    }
    listed_dividend_by_code = {
        row["code"]: row for row in listed_dividends.get("stocks", [])
    }
    listed_price_by_code = {
        row["code"]: row for row in listed_price_archive.get("stocks", [])
    }

    delisted_codes = sorted(set(delisted_dividends["data_quality_eligible_codes"]))
    listed_dividend_codes = set(listed_dividends["data_quality_eligible_codes"])
    listed_price_codes = set(listed_price_manifest["data_quality_eligible_codes"])
    listed_codes = sorted(listed_dividend_codes & listed_price_codes)
    if not listed_codes:
        raise RuntimeError("在市分红与价格门禁没有共同放行任何股票")
    for code in delisted_codes:
        if code not in delisted_price_by_code or code not in master_by_code:
            raise RuntimeError(f"退市门禁放行代码缺少价格或主表: {code}")
        price_map = convert_price_rows(delisted_price_by_code[code], through_date)
        verified = dict(delisted_dividend_by_code[code])
        verified["records"] = verified.get("verified_records", [])
        summaries, details = convert_dividends(verified, through_date)
        write_json_atomic(cache_dir / f"kl_{code}.json", price_map)
        write_json_atomic(cache_dir / f"dv_{code}.json", summaries)
        write_json_atomic(cache_dir / f"dvd_{code}.json", details)
    for code in listed_codes:
        if (
            code not in listed_price_by_code
            or code not in listed_dividend_by_code
            or code not in master_by_code
        ):
            raise RuntimeError(f"在市门禁放行代码缺少价格、分红或主表: {code}")
        price_map = convert_listed_price_rows(listed_price_by_code[code], through_date)
        summaries, details = convert_listed_dividends(
            listed_dividend_by_code[code], through_date
        )
        write_json_atomic(cache_dir / f"kl_{code}.json", price_map)
        write_json_atomic(cache_dir / f"dv_{code}.json", summaries)
        write_json_atomic(cache_dir / f"dvd_{code}.json", details)

    codes = sorted(set(delisted_codes) | set(listed_codes))
    if not codes:
        raise RuntimeError("人工数据门禁没有放行任何历史候选")
    manifest_records = [
        row for row in records_from_cache(cache_dir, through_date)
        if row["code"] in set(codes)
    ]
    if len(manifest_records) != len(codes):
        present = {row["code"] for row in manifest_records}
        raise RuntimeError(
            "过滤 manifest 缺少缓存代码: "
            + ", ".join(sorted(set(codes) - present)[:20])
        )
    filtered_manifest = build_manifest(
        manifest_records,
        as_of=through_date,
        top=0,
        min_years=0,
        source={
            "name": "manual_data_quality_filtered_cache",
            "path": cache_dir.resolve().relative_to(ROOT.resolve()).as_posix(),
            "point_in_time_cutoff": through_date,
            "price_format": "unadjusted_close",
            "filter_status": "complete_with_exclusions",
        },
    )
    filtered_manifest["rules"]["pool_mode"] = "dynamic"
    filtered_manifest["rules"]["pool_min_consecutive_years"] = 3
    filtered_manifest["limitations"] = [
        "仅包含通过人工数据质量门禁且曾进入冻结 V1 信号前候选路径的股票。",
        "不使用回测盈亏过滤，但仍存在数据可得性偏差，不能称为全市场无偏 manifest。",
    ]
    write_manifest(manifest_output, filtered_manifest)
    listing_windows = {
        code: {
            "list_date": str(master_by_code[code].get("list_date") or "")[:10],
            "delist_date": str(master_by_code[code].get("delist_date") or "")[:10],
        }
        for code in set(codes) | set(read_json(ROOT / "data" / "universe_manifest.json")["codes"])
        if code in master_by_code
    }
    control, scenarios = _run_backtests(
        cache_dir, codes, listing_windows, through_date
    )
    payload = {
        "schema_version": 1,
        "status": "complete_with_exclusions",
        "authoritative_baseline_replaced": False,
        "through_date": through_date,
        "filter_boundary": (
            "仅使用冻结 V1 信号前规则和数据质量证据；不使用回测盈亏或最终表现过滤"
        ),
        "inputs": {
            "security_master_sha256": file_sha256(security_path),
            "delisted_dividends_sha256": file_sha256(delisted_dividends_path),
            "delisted_prices_sha256": file_sha256(delisted_prices_path),
            "listed_dividends_sha256": file_sha256(listed_dividends_path),
            "listed_price_manifest_sha256": file_sha256(listed_price_manifest_path),
            "listed_price_archive_sha256": file_sha256(archive_path),
            "historical_filtered_manifest_sha256": file_sha256(manifest_output),
        },
        "scope": {
            "listed_eligible_count": len(listed_codes),
            "listed_dividend_eligible_count": len(listed_dividend_codes),
            "listed_price_eligible_count": len(listed_price_codes),
            "listed_filtered_by_price_gate_count": len(
                listed_dividend_codes - listed_price_codes
            ),
            "delisted_eligible_count": len(delisted_codes),
            "combined_code_count": len(codes),
            "combined_codes_sha256": canonical_sha256(codes),
            "listed_filtered_unverifiable_count": listed_dividends[
                "filtered_unverifiable_count"
            ],
            "listed_price_filtered_unverifiable_count": listed_price_manifest[
                "filtered_unverifiable_count"
            ],
            "delisted_filtered_unverifiable_count": delisted_dividends[
                "filtered_unverifiable_count"
            ],
            "delisted_filtered_non_candidate_count": delisted_dividends[
                "filtered_non_candidate_count"
            ],
        },
        "same_cache_control": control,
        "scenarios": scenarios,
        "limitations": [
            "双源或官方证据无法闭合的股票按人工数据门禁排除，因此不是全市场无偏回测。",
            "在市股票只有同时通过分红和价格门禁才进入回放。",
            "过滤规则不读取回测盈亏，但当前可取得的数据覆盖仍可能造成数据可得性偏差。",
            "退市处置缺少真实清算或换股结果，继续同时报告归零和末收盘价回收边界。",
            "冻结 V1 参数、manifest、日期文件和原始缓存未修改。",
        ],
    }
    write_json_atomic(output, payload)
    status_payload = {
        "schema_version": 3,
        "source_snapshot_date": through_date,
        "status": "complete_with_exclusions",
        "manifest_generation_allowed": False,
        "full_market_manifest_generation_allowed": False,
        "manual_filtered_replay_allowed": True,
        "manual_filtered_manifest_generation_allowed": True,
        "independently_verified": False,
        "manual_data_gate_complete": True,
        "manual_data_gate_status": "complete_with_exclusions",
        "filtered_replay_path": output.resolve().relative_to(ROOT.resolve()).as_posix(),
        "filtered_replay_sha256": file_sha256(output),
        "scope": payload["scope"],
        "artifacts": {
            key: {"path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
                  "sha256": file_sha256(path)}
            for key, path in {
                "security_master": security_path,
                "delisted_dividends": delisted_dividends_path,
                "delisted_prices": delisted_prices_path,
                "listed_dividends": listed_dividends_path,
                "listed_price_manifest": listed_price_manifest_path,
                "listed_price_archive": archive_path,
                "historical_filtered_manifest": manifest_output,
            }.items()
        },
        "limitation": (
            "人工数据门禁已处理完整，但排除了证据无法闭合的股票；"
            "因此允许生成过滤回放，不允许声称得到全市场无偏 manifest。"
        ),
    }
    write_json_atomic(status_output, status_payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--security-master", type=Path, default=DEFAULT_SECURITY)
    parser.add_argument("--delisted-dividends", type=Path, default=DEFAULT_DELISTED_DIVIDENDS)
    parser.add_argument("--delisted-prices", type=Path, default=DEFAULT_DELISTED_PRICES)
    parser.add_argument("--listed-dividends", type=Path, default=DEFAULT_LISTED_DIVIDENDS)
    parser.add_argument("--listed-price-manifest", type=Path, default=DEFAULT_LISTED_PRICE_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status-output", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--manifest-output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--through-date", default="2026-08-25")
    args = parser.parse_args()
    result = run_replay(
        args.security_master, args.delisted_dividends, args.delisted_prices,
        args.listed_dividends, args.listed_price_manifest, args.cache_dir,
        args.output, args.through_date, args.status_output, args.manifest_output,
    )
    print(json.dumps({
        "status": result["status"],
        "scope": result["scope"],
        "scenario_metrics": {
            name: value["metrics"] for name, value in result["scenarios"].items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
