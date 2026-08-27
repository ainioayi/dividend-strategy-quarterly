"""从冻结缓存生成可审计的候选池 manifest。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from universe_manifest import build_manifest, write_manifest


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _canonical_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _positive_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def records_from_cache(cache_dir: Path, as_of: str) -> list[dict]:
    """从冻结缓存生成点时记录，逐笔分红只保留截止日前已除权事件。"""
    if not DATE_RE.fullmatch(as_of):
        raise ValueError("as_of 必须为 YYYY-MM-DD")
    result: list[dict] = []
    for kl in sorted(cache_dir.glob("kl_*.json")):
        code = kl.stem[3:]
        if not code.isdigit() or len(code) != 6:
            continue
        try:
            prices = json.loads(kl.read_text(encoding="utf-8"))
            point_in_time_prices = {
                str(day)[:10]: value
                for day, value in prices.items()
                if DATE_RE.fullmatch(str(day)[:10]) and str(day)[:10] <= as_of
            }
            if not point_in_time_prices:
                continue
            detail_path = cache_dir / f"dvd_{code}.json"
            details = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else []
            known_details = [
                row for row in details
                if DATE_RE.fullmatch(str(row.get("ex_date") or "")[:10])
                and str(row.get("ex_date"))[:10] <= as_of
            ]
            years = {
                int(row["year"])
                for row in known_details
                if str(row.get("year") or "").isdigit() and _positive_float(row.get("dps")) > 0
            }
            result.append({
                "code": code,
                "years": len(years),
                "total_dps": round(sum(_positive_float(row.get("dps")) for row in known_details), 6),
                "data_max_date": max(point_in_time_prices),
                "latest_event_date": max(
                    (str(row.get("ex_date"))[:10] for row in known_details),
                    default=None,
                ),
                "kline_sha256": _canonical_hash(point_in_time_prices),
                "dividend_detail_sha256": _canonical_hash(known_details),
            })
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="生成可版本化候选池 manifest")
    parser.add_argument("--input", default="data/universe.json")
    parser.add_argument("--output", default="data/universe_manifest.json")
    parser.add_argument("--as-of", required=True, help="数据截止日 YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=200, help="累计 DPS 排名前 N；0 表示保留全部缓存代码")
    parser.add_argument("--min-years", type=int, default=3)
    parser.add_argument("--pool-min-years", type=int, default=3,
                        help="动态池连续分红年限；与 manifest 覆盖过滤分开")
    parser.add_argument("--from-cache", help="从 backtest_cache 生成输入记录")
    args = parser.parse_args()
    source_path = Path(args.from_cache) if args.from_cache else Path(args.input)
    cache_marker = source_path / "price_format.json" if args.from_cache else None
    marker = {}
    if cache_marker and cache_marker.exists():
        marker = json.loads(cache_marker.read_text(encoding="utf-8"))
        if marker.get("format") != "unadjusted_close":
            raise ValueError("缓存 price_format 不是不复权收盘价，不能生成严格回测 manifest")
    records = records_from_cache(source_path, args.as_of) if args.from_cache else json.loads(source_path.read_text(encoding="utf-8"))
    qualified = [r for r in records if int(r.get("years", 0)) >= args.min_years]
    qualified.sort(key=lambda r: (-float(r.get("total_dps", 0)), -int(r.get("years", 0)), str(r.get("code", ""))))
    selected = qualified[: args.top] if args.top > 0 else qualified
    source = {
        "name": "local_backtest_cache" if args.from_cache else "universe_summary",
        "path": str(source_path).replace("\\", "/"),
        "point_in_time_cutoff": args.as_of,
    }
    if marker:
        source["price_format"] = marker.get("format")
        source["price_source"] = marker.get("source")
        source["price_source_fallback"] = "eastmoney_online"
    manifest = build_manifest(
        selected,
        as_of=args.as_of,
        top=args.top,
        min_years=args.min_years,
        source=source,
    )
    manifest["rules"]["pool_mode"] = "dynamic"
    manifest["rules"]["pool_min_consecutive_years"] = args.pool_min_years
    write_manifest(args.output, manifest)
    print(f"manifest 已写入 {args.output}，候选 {len(manifest['codes'])} 只，sha256={manifest['records_sha256']}")


if __name__ == "__main__":
    main()
