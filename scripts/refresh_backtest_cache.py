"""按显式截止日刷新不复权 K 线和完整已实施分红缓存。"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import requests

from backtest import CACHE_DIR, _fetch_kline_eastmoney


FORWARD_CACHE_DIR = CACHE_DIR.parent / "forward" / "cache"


def _cached_codes(cache_dir: Path) -> list[str]:
    return sorted({path.stem[4:] for path in cache_dir.glob("dvd_*.json")})


def _fetch_kline_sina(code: str, as_of: str) -> dict[str, float]:
    """直连新浪接口读取完整未复权日线，避免额外行情封装层。"""
    if code.startswith("6"):
        market = "sh"
    elif code.startswith(("8", "9")):
        market = "bj"
    else:
        market = "sz"
    response = requests.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        params={"symbol": market + code, "scale": "240", "ma": "no", "datalen": "5000"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"新浪行情响应结构异常: {code}")
    result = {}
    for row in payload:
        day = str(row.get("day") or row.get("date") or "")[:10]
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if len(day) == 10 and day <= as_of and close > 0:
            result[day] = close
    return result


def _positive_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _canonical_row(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_dividend_rows(rows: list[dict], as_of: str) -> list[dict]:
    payments = []
    for row in rows:
        progress = str(row.get("ASSIGNMENT_PROGRESS") or row.get("ASSIGN_PROGRESS") or "")
        if "实施" not in progress and "完成" not in progress:
            continue
        report_date = str(row.get("REPORT_DATE") or "")[:10]
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if len(report_date) != 10 or len(ex_date) != 10 or ex_date > as_of:
            continue
        payments.append({
            "year": int(report_date[:4]), "ex_date": ex_date,
            "dps": round(_positive_float(row.get("PRETAX_BONUS_RMB")) / 10.0, 4),
            "bonus_ratio": _positive_float(row.get("BONUS_RATIO")),
            "transfer_ratio": _positive_float(row.get("IT_RATIO") or row.get("TRANSFER_RATIO")),
        })
    unique = {_canonical_row(row): row for row in payments}
    return sorted(unique.values(), key=lambda row: (row["ex_date"], row["year"], row["dps"]))


def _fetch_dividends_eastmoney(code: str, as_of: str, timeout: float = 20.0) -> list[dict]:
    """串行拉取全部分页；失败时抛错，绝不沿用旧缓存。"""
    endpoint = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    raw_rows = []
    page = 1
    while True:
        response = requests.get(endpoint, params={
            "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")', "pageNumber": page,
            "pageSize": 50, "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        }, timeout=timeout, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise RuntimeError(f"东财分红响应结构异常: {code}")
        raw_rows.extend(result["data"])
        pages = int(result.get("pages") or 1)
        if page >= pages:
            break
        page += 1
    return _normalize_dividend_rows(raw_rows, as_of)


def _dividend_summary(details: list[dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
    for row in details:
        item = grouped.setdefault(int(row["year"]), {"dps": 0.0, "bonus_ratio": 0.0, "transfer_ratio": 0.0})
        item["dps"] += float(row.get("dps") or 0)
        item["bonus_ratio"] = max(item["bonus_ratio"], float(row.get("bonus_ratio") or 0))
        item["transfer_ratio"] = max(item["transfer_ratio"], float(row.get("transfer_ratio") or 0))
    return [{"year": year, "dps": round(value["dps"], 4),
             "bonus_ratio": value["bonus_ratio"], "transfer_ratio": value["transfer_ratio"]}
            for year, value in sorted(grouped.items())]


def refresh_dividend_cache(
    codes: list[str], cache_dir: Path, as_of: str, *, interval: float = 1.05,
) -> list[str]:
    """先完整采集后统一写入；任一失败时不改动任何旧分红缓存。"""
    staged: dict[str, list[dict]] = {}
    failed = []
    for index, code in enumerate(codes, 1):
        try:
            staged[code] = _fetch_dividends_eastmoney(code, as_of)
            if not staged[code]:
                raise RuntimeError("已纳入 V1 的高股息股票返回空分红历史")
        except Exception as exc:
            print(f"分红刷新失败 {code}: {exc}")
            failed.append(code)
        if index < len(codes):
            delay = max(interval, 0.0)
            time.sleep(delay + random.uniform(0.0, 0.25) if delay else 0.0)
    if failed:
        return failed
    for code in codes:
        details = staged[code]
        (cache_dir / f"dvd_{code}.json").write_text(
            json.dumps(details, ensure_ascii=False), encoding="utf-8"
        )
        (cache_dir / f"dv_{code}.json").write_text(
            json.dumps(_dividend_summary(details), ensure_ascii=False), encoding="utf-8"
        )
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新回测 K 线与分红缓存")
    parser.add_argument("--as-of", required=True, help="行情截止日 YYYY-MM-DD")
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument("--retry", type=int, default=2, help="空结果重试次数")
    parser.add_argument("--interval", type=float, default=1.05, help="东财请求间隔秒数")
    parser.add_argument("--source", choices=("sina", "eastmoney"), default="sina",
                        help="不复权行情来源；默认新浪，东财仅作备选")
    parser.add_argument("--dividend-interval", type=float, default=1.05,
                        help="东财分红请求的串行间隔秒数")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    if cache_dir.resolve() not in {CACHE_DIR.resolve(), FORWARD_CACHE_DIR.resolve()}:
        raise ValueError("当前刷新器只允许写入项目 V1 缓存或隔离的前向缓存")
    cache_dir.mkdir(parents=True, exist_ok=True)
    codes = _cached_codes(cache_dir)
    failed: list[str] = []
    staged_prices: dict[str, dict[str, float]] = {}
    for index, code in enumerate(codes, 1):
        rows = {}
        for attempt in range(max(args.retry, 0) + 1):
            if args.source == "sina":
                try:
                    rows = _fetch_kline_sina(code, args.as_of)
                except Exception:
                    rows = {}
                if not rows:
                    # 新浪对部分北交所新股尚未提供历史序列，再尝试同语义
                    # 的东财不复权接口；失败时仍视为失败，不沿用旧缓存。
                    try:
                        rows = _fetch_kline_eastmoney(code, args.as_of)
                    except Exception:
                        rows = {}
            else:
                try:
                    rows = _fetch_kline_eastmoney(code, args.as_of)
                except Exception:
                    rows = {}
            if rows:
                break
            if attempt < args.retry:
                time.sleep(0.5)
        if not rows:
            failed.append(code)
        else:
            staged_prices[code] = rows
        if index % 10 == 0 or index == len(codes):
            print(f"进度 {index}/{len(codes)}，有效 {index - len(failed)}，失败 {len(failed)}")
        if index < len(codes):
            time.sleep(max(args.interval, 0.0))

    summary = {
        "as_of": args.as_of,
        "requested": len(codes),
        "succeeded": len(codes) - len(failed),
        "failed": failed,
        "dividend_failed": [],
    }
    if not failed:
        summary["dividend_failed"] = refresh_dividend_cache(
            codes, cache_dir, args.as_of, interval=args.dividend_interval
        )
        if not summary["dividend_failed"]:
            for code, rows in staged_prices.items():
                (cache_dir / f"kl_{code}.json").write_text(
                    json.dumps(rows, ensure_ascii=False), encoding="utf-8"
                )
            (cache_dir / "price_format.json").write_text(
                json.dumps({
                    "format": "unadjusted_close",
                    "source": "sina_cn_marketdata" if args.source == "sina" else "eastmoney_push2his",
                    "as_of": args.as_of,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed or summary["dividend_failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
