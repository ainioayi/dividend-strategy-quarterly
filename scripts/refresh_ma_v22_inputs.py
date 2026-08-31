"""刷新并冻结多资产风险预算 V2.2 的 ETF 行情与分红输入。"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from ma_v22_strategy import (
    MA_V22_ASSETS,
    MA_V22_ATTACHMENT_SHA256,
    canonical_sha256,
)
from tradeable_benchmark import parse_dividends


TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DIVIDEND_URL = "https://fundf10.eastmoney.com/fhsp_{code}.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _fetch_tencent(
    symbol: str,
    fq: str,
    start: str,
    as_of: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
    interval: float = 0.4,
) -> list[dict[str, Any]]:
    """分页读取腾讯日线；hfq 用于信号，day 用于真实开盘成交。"""
    if fq not in {"hfq", "day"}:
        raise ValueError("腾讯复权参数只能是 hfq 或 day")
    rows: dict[str, dict[str, Any]] = {}
    end = as_of
    while True:
        response = get(
            TENCENT_URL,
            params={"param": f"{symbol},day,{start},{end},640,{fq}"},
            headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
            timeout=30,
        )
        response.raise_for_status()
        inner = ((response.json().get("data") or {}).get(symbol) or {})
        batch = inner.get(f"{fq}day") or inner.get("day") or []
        if not batch:
            break
        for raw in batch:
            day = str(raw[0])[:10]
            if start <= day <= as_of:
                rows[day] = {
                    "date": day,
                    "open": float(raw[1]),
                    "close": float(raw[2]),
                }
        earliest = str(batch[0][0])[:10]
        if earliest <= start or len(batch) < 640:
            break
        next_end = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
        if next_end >= end:
            raise RuntimeError(f"腾讯 {symbol}/{fq} 分页日期没有前移")
        end = next_end
        if interval:
            time.sleep(interval)
    result = [rows[day] for day in sorted(rows)]
    if not result:
        raise RuntimeError(f"腾讯 {symbol}/{fq} 没有返回行情")
    return result


def _fetch_eastmoney(
    code: str,
    fqt: int,
    start: str,
    as_of: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> list[dict[str, Any]]:
    response = get(
        EASTMONEY_URL,
        params={
            "secid": f"1.{code}", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101", "fqt": str(fqt), "beg": start.replace("-", ""),
            "end": as_of.replace("-", ""), "lmt": "1000000",
        },
        headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
        timeout=30,
    )
    response.raise_for_status()
    klines = ((response.json().get("data") or {}).get("klines") or [])
    rows = []
    for line in klines:
        values = line.split(",")
        day = values[0]
        if start <= day <= as_of:
            rows.append({"date": day, "open": float(values[1]), "close": float(values[2])})
    if not rows:
        raise RuntimeError(f"东方财富 {code}/fqt={fqt} 没有返回行情")
    return rows


def fetch_price_pair(
    asset: dict[str, str],
    start: str,
    as_of: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
    interval: float = 0.4,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    selected = {}
    series = {}
    for field, fq, fqt in (("hfq", "hfq", 2), ("raw", "day", 0)):
        try:
            values = _fetch_tencent(asset["symbol"], fq, start, as_of, get=get, interval=interval)
            selected[field] = "tencent"
        except Exception:
            values = _fetch_eastmoney(asset["code"], fqt, start, as_of, get=get)
            selected[field] = "eastmoney_fallback"
        series[field] = {row["date"]: row for row in values}
    common = sorted(set(series["hfq"]) & set(series["raw"]))
    rows = [{
        "date": day,
        "hfq_open": series["hfq"][day]["open"],
        "hfq_close": series["hfq"][day]["close"],
        "raw_open": series["raw"][day]["open"],
        "raw_close": series["raw"][day]["close"],
    } for day in common]
    if len(rows) < 127:
        raise RuntimeError(f"{asset['name']} 公共行情不足 127 个交易日")
    return rows, selected


def fetch_fund_dividends(
    code: str,
    as_of: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> tuple[list[dict[str, Any]], str]:
    url = DIVIDEND_URL.format(code=code)
    response = get(
        url,
        headers={"User-Agent": UA, "Referer": "https://fundf10.eastmoney.com/"},
        timeout=30,
    )
    response.raise_for_status()
    try:
        rows = parse_dividends(response.text, as_of)
    except ValueError:
        rows = []
    return [{"code": code, **row} for row in rows], url


def build_inputs(
    asset_rows: dict[str, list[dict[str, Any]]],
    dividends: list[dict[str, Any]],
    as_of: str,
    sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    common_dates = sorted(set.intersection(*({row["date"] for row in rows} for rows in asset_rows.values())))
    if not common_dates or common_dates[-1] != as_of:
        raise ValueError("四只 ETF 公共行情必须覆盖输入截止日")
    by_asset = {asset: {row["date"]: row for row in rows} for asset, rows in asset_rows.items()}
    prices = [{
        "date": day,
        "assets": {
            asset: {key: value for key, value in by_asset[asset][day].items() if key != "date"}
            for asset in MA_V22_ASSETS
        },
    } for day in common_dates]
    dividends = sorted(dividends, key=lambda row: (row.get("pay_date", ""), row.get("code", "")))
    payload = {
        "schema_version": 1,
        "strategy": "ma_v22",
        "as_of": as_of,
        "price_format": "tencent_hfq_signal_raw_execution",
        "assets": MA_V22_ASSETS,
        "attachments": [
            {"name": name, "sha256": sha256}
            for name, sha256 in MA_V22_ATTACHMENT_SHA256.items()
        ],
        "sources": sources or {},
        "inputs": {"prices": prices, "dividends": dividends},
        "hashes": {
            "prices": canonical_sha256(prices),
            "dividends": canonical_sha256(dividends),
        },
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def refresh_inputs(as_of: str, start: str = "2005-01-01", interval: float = 0.4) -> dict[str, Any]:
    date.fromisoformat(as_of)
    asset_rows, dividends, sources = {}, [], {"prices": {}, "dividends": {}}
    for index, (asset, spec) in enumerate(MA_V22_ASSETS.items()):
        rows, providers = fetch_price_pair(spec, start, as_of, interval=interval)
        asset_rows[asset] = rows
        sources["prices"][asset] = providers
        if interval:
            time.sleep(interval)
        fund_rows, url = fetch_fund_dividends(spec["code"], as_of)
        dividends.extend(fund_rows)
        sources["dividends"][asset] = {"provider": "天天基金基金档案", "url": url}
        if interval and index + 1 < len(MA_V22_ASSETS):
            time.sleep(interval)
    return build_inputs(asset_rows, dividends, as_of, sources)


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新多资产风险预算 V2.2 冻结输入")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--interval", type=float, default=0.4)
    parser.add_argument("--output", type=Path, default=Path("data/ma_v22_inputs.json"))
    args = parser.parse_args()
    payload = refresh_inputs(args.as_of, args.start, args.interval)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "as_of": payload["as_of"],
        "trading_days": len(payload["inputs"]["prices"]),
        "dividends": len(payload["inputs"]["dividends"]),
        "content_sha256": payload["content_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
