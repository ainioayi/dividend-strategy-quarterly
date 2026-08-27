"""从固定行情缓存生成可版本化的月末调仓日期。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from universe_manifest import load_manifest

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def dates_hash(dates: list[str]) -> str:
    """按紧凑 JSON 计算日期序列哈希。"""
    payload = json.dumps(dates, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_date(value: str, name: str) -> date:
    if not DATE_RE.fullmatch(str(value)):
        raise ValueError(f"{name} 必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} 不是有效日期: {value}") from exc


def _increment_month(current: date) -> date:
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)


def build_dates(
    manifest_path: str | Path,
    cache_dir: str | Path,
    *,
    as_of: str,
    start_date: str = "2016-01-01",
) -> dict:
    """只用 manifest 指定代码的缓存交易日生成每月最后交易日。"""
    cutoff = _parse_date(as_of, "as_of")
    start = _parse_date(start_date, "start_date")
    if start > cutoff:
        raise ValueError("start_date 不能晚于 as_of")
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute():
        manifest_file = ROOT / manifest_file
    manifest = load_manifest(manifest_file)
    if str(manifest.get("as_of") or "") != as_of:
        raise ValueError("as_of 必须与 manifest.as_of 一致")
    cache = Path(cache_dir)
    if not cache.is_absolute():
        cache = ROOT / cache

    all_dates: set[str] = set()
    missing: list[str] = []
    for code in manifest["codes"]:
        path = cache / f"kl_{code}.json"
        if not path.exists():
            missing.append(code)
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"K 线缓存无法读取: {code}") from exc
        if not isinstance(rows, dict):
            raise ValueError(f"K 线缓存格式无效: {code}")
        for raw_day, raw_price in rows.items():
            day_text = str(raw_day)[:10]
            if not DATE_RE.fullmatch(day_text):
                continue
            try:
                day = date.fromisoformat(day_text)
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if start <= day <= cutoff and price > 0:
                all_dates.add(day_text)
    if missing:
        raise ValueError("manifest 对应 K 线缓存缺失: " + ",".join(missing))

    dates: list[str] = []
    month = date(start.year, start.month, 1)
    while month <= cutoff:
        next_month = _increment_month(month)
        month_end = min(next_month - timedelta(days=1), cutoff)
        prefix = month.strftime("%Y-%m")
        candidates = [day for day in all_dates if day.startswith(prefix) and day <= month_end.isoformat()]
        if candidates:
            dates.append(max(candidates))
        month = next_month
    if not dates:
        raise ValueError("指定范围内没有可用交易日")

    try:
        normalized_manifest = str(manifest_file.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        normalized_manifest = str(manifest_file).replace("\\", "/")
    try:
        normalized_cache = str(cache.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        normalized_cache = str(cache).replace("\\", "/")
    return {
        "schema_version": 1,
        "kind": "monthly_rebalance_dates",
        "frequency": "monthly",
        "start_date": start.isoformat(),
        "as_of": as_of,
        "source": {
            "manifest": normalized_manifest,
            "manifest_records_sha256": manifest["records_sha256"],
            "cache_dir": normalized_cache,
            "code_count": len(manifest["codes"]),
        },
        "dates": dates,
        "dates_sha256": dates_hash(dates),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成可审计的月末调仓日期")
    parser.add_argument("--manifest", default="data/universe_manifest.json")
    parser.add_argument("--cache-dir", default="data/backtest_cache")
    parser.add_argument("--as-of", required=True, help="数据截止日 YYYY-MM-DD")
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--output", default="data/rebalance_dates_monthly.json")
    args = parser.parse_args()
    payload = build_dates(
        args.manifest,
        args.cache_dir,
        as_of=args.as_of,
        start_date=args.start_date,
    )
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "count": len(payload["dates"]),
        "first": payload["dates"][0],
        "last": payload["dates"][-1],
        "dates_sha256": payload["dates_sha256"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
