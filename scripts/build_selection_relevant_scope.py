"""生成冻结 V1 决策路径可能涉及的在市股票范围。"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from backtest import _point_in_time_trailing_dps
from build_historical_universe import ROOT, canonical_sha256, write_json_atomic
from collect_historical_prices import (
    price_checkpoint_is_complete,
    price_checkpoint_path,
    write_gzip_json_atomic,
)


DEFAULT_DIVIDENDS = ROOT / "data" / "historical" / "listed_dividends.json"
DEFAULT_DATES = ROOT / "data" / "rebalance_dates_monthly.json"
DEFAULT_FROZEN = ROOT / "data" / "universe_manifest.json"
DEFAULT_PRICE_CHECKPOINTS = (
    ROOT / "data" / "historical" / "checkpoints" / "listed_prices"
)
DEFAULT_FROZEN_CACHE = ROOT / "data" / "backtest_cache"
DEFAULT_OUTPUT = ROOT / "data" / "historical" / "selection_relevant_scope.json"
DEFAULT_OBSERVATIONS = (
    ROOT / "data" / "historical" / "selection_price_observations.json.gz"
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _price_on_or_before_with_date(
    prices: dict[str, float], dates: list[str], target: str,
) -> tuple[float | None, str | None]:
    index = bisect_right(dates, target) - 1
    if index < 0:
        return None, None
    day = dates[index]
    # 与冻结回测一致，停牌超过 15 天的旧价格不能生成新信号。
    from datetime import date
    if (date.fromisoformat(target) - date.fromisoformat(day)).days > 15:
        return None, day
    value = float(prices[day])
    return (value, day) if value > 0 else (None, day)


def _price_on_or_before(
    prices: dict[str, float], dates: list[str], target: str,
) -> float | None:
    return _price_on_or_before_with_date(prices, dates, target)[0]


def _has_dynamic_pool_history(records: list[dict[str, Any]], signal_date: str) -> bool:
    year = int(signal_date[:4])
    end_year = year - 1 if int(signal_date[5:7]) >= 7 else year - 2
    required = set(range(end_year - 2, end_year + 1))
    visible = {
        int(row["year"])
        for row in records
        if str(row.get("year") or "").isdigit()
        and float(row.get("dps") or 0) > 0
        and str(row.get("ex_date") or "")[:10] <= signal_date
    }
    return required.issubset(visible)


def _load_prices(
    code: str, checkpoint_dir: Path, frozen_cache: Path, as_of: str,
    scan_provider: str,
) -> tuple[dict[str, float], str, str]:
    frozen_path = frozen_cache / f"kl_{code}.json"
    if frozen_path.exists():
        values = read_json(frozen_path)
        prices = {
            str(day): float(value) for day, value in values.items() if float(value) > 0
        }
        rows = [{"date": day, "close": prices[day]} for day in sorted(prices)]
        return prices, "frozen_v1", canonical_sha256(rows)
    path = price_checkpoint_path(checkpoint_dir, scan_provider, code)
    if not path.exists():
        return {}, "missing_checkpoint", ""
    payload = read_json(path)
    start_date = max("2015-01-01", str(payload.get("list_date") or "2015-01-01")[:10])
    if not price_checkpoint_is_complete(payload, scan_provider, code, start_date, as_of):
        return {}, "incomplete_checkpoint", ""
    return (
        {row["date"]: float(row["close"]) for row in payload["rows"]},
        f"{scan_provider}_provisional",
        payload["rows_sha256"],
    )


def build_selection_scope(
    dividends: dict[str, Any], rebalance_dates: list[str], checkpoint_dir: Path,
    frozen_cache: Path, as_of: str, scan_provider: str = "tonghuashun",
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    relevant: list[str] = []
    missing: list[dict[str, str]] = []
    candidate_count = 0
    price_complete_count = 0
    price_input_fingerprints = []
    for stock in dividends.get("stocks", []):
        if stock.get("verification_required") is not True:
            continue
        candidate_count += 1
        code = stock["code"]
        records = stock.get("records", [])
        prices, price_source, rows_sha256 = _load_prices(
            code, checkpoint_dir, frozen_cache, as_of, scan_provider
        )
        price_dates = sorted(prices)
        if prices:
            price_complete_count += 1
        price_input_fingerprints.append({
            "code": code,
            "source": price_source,
            "row_count": len(prices),
            "rows_sha256": rows_sha256,
        })
        observed = {
            signal_date: _price_on_or_before_with_date(
                prices, price_dates, signal_date
            )
            for signal_date in rebalance_dates
        }
        if observations is not None:
            observations.append({
                "code": code,
                "source": price_source,
                "rows_sha256": rows_sha256,
                "signals": [
                    [signal_date, observed[signal_date][1], observed[signal_date][0]]
                    for signal_date in rebalance_dates
                ],
            })
        for index, signal_date in enumerate(rebalance_dates):
            if not _has_dynamic_pool_history(records, signal_date):
                continue
            current = observed[signal_date][0]
            if current is None:
                missing.append({"code": code, "date": signal_date, "reason": price_source})
                continue
            dps = _point_in_time_trailing_dps(records, records, signal_date)
            if not dps or dps / current * 100 < 7.5:
                continue
            if index < 4:
                missing.append({"code": code, "date": signal_date, "reason": "momentum_warmup"})
                continue
            past_date = rebalance_dates[index - 4]
            past = _price_on_or_before(prices, price_dates, past_date)
            if past is None:
                missing.append({"code": code, "date": signal_date, "reason": "missing_momentum_price"})
                continue
            if current / past >= 0.85:
                relevant.append(code)
                break
    relevant = sorted(set(relevant))
    reason_counts = Counter(row["reason"] for row in missing)
    filtered_price_codes = sorted({
        row["code"]
        for row in price_input_fingerprints
        if row["source"] in {"missing_checkpoint", "incomplete_checkpoint"}
    })
    return {
        "schema_version": 1,
        "status": (
            "complete" if price_complete_count == candidate_count
            else "complete_with_exclusions"
        ),
        "manual_data_gate_complete": True,
        "manual_data_gate_status": (
            "complete" if not filtered_price_codes else "complete_with_exclusions"
        ),
        "as_of": as_of,
        "rules": {
            "pool_min_consecutive_years": 3,
            "pool_switch_month": 7,
            "entry_yield": 7.5,
            "momentum_months": 4,
            "momentum_threshold": 0.85,
        },
        "filter_boundary": "仅使用各信号日之前可见的规则条件，不使用回测收益或最终表现",
        "scan_provider": scan_provider,
        "primary_candidate_count": candidate_count,
        "price_complete_count": price_complete_count,
        "filtered_price_unverifiable_count": len(filtered_price_codes),
        "filtered_price_unverifiable_codes": filtered_price_codes,
        "filtered_price_unverifiable_codes_sha256": canonical_sha256(
            filtered_price_codes
        ),
        "selection_relevant_count": len(relevant),
        "selection_relevant_codes": relevant,
        "selection_relevant_codes_sha256": canonical_sha256(relevant),
        "missing_observation_count": len(missing),
        "missing_reason_counts": dict(sorted(reason_counts.items())),
        "missing_observations": missing,
        "price_input_fingerprints_sha256": canonical_sha256(price_input_fingerprints),
        "price_input_fingerprints": price_input_fingerprints,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--dividends", type=Path, default=DEFAULT_DIVIDENDS)
    parser.add_argument("--dates", type=Path, default=DEFAULT_DATES)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_PRICE_CHECKPOINTS)
    parser.add_argument("--frozen-cache", type=Path, default=DEFAULT_FROZEN_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--scan-provider",
        choices=("sina", "baostock", "tonghuashun"),
        default="tonghuashun",
    )
    parser.add_argument(
        "--observation-archive", type=Path, default=DEFAULT_OBSERVATIONS,
        help="保存每只候选在每个信号日实际读取的价格观察值",
    )
    args = parser.parse_args()
    dates_payload = read_json(args.dates)
    dividends_payload = read_json(args.dividends)
    observations: list[dict[str, Any]] = []
    payload = build_selection_scope(
        dividends_payload, dates_payload["dates"], args.checkpoint_dir,
        args.frozen_cache, args.as_of, args.scan_provider, observations,
    )
    payload["rebalance_dates_sha256"] = dates_payload.get("dates_sha256")
    payload["listed_dividends_sha256"] = hashlib.sha256(
        args.dividends.read_bytes()
    ).hexdigest()
    payload["listed_dividend_records_sha256"] = canonical_sha256([
        {"code": stock["code"], "records_sha256": stock.get("records_sha256", "")}
        for stock in dividends_payload.get("stocks", [])
    ])
    observation_payload = {
        "schema_version": 1,
        "as_of": args.as_of,
        "scan_provider": args.scan_provider,
        "rebalance_dates": dates_payload["dates"],
        "stocks": observations,
    }
    write_gzip_json_atomic(args.observation_archive, observation_payload)
    try:
        archive_path = args.observation_archive.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        archive_path = args.observation_archive.as_posix()
    payload["price_observation_archive"] = {
        "path": archive_path,
        "sha256": hashlib.sha256(args.observation_archive.read_bytes()).hexdigest(),
        "compression": "gzip-json-mtime-0",
        "stock_count": len(observations),
        "signal_count": len(dates_payload["dates"]),
    }
    write_json_atomic(args.output, payload)
    print(json.dumps({key: payload[key] for key in (
        "status", "primary_candidate_count", "price_complete_count",
        "selection_relevant_count", "missing_observation_count",
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
