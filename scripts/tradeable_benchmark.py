"""获取并模拟红利 ETF 的含分红可交易总回报基准。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from bisect import bisect_right
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "benchmarks" / "510880_total_return.json"
PRICE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
DIVIDEND_URL_TEMPLATE = "https://fundf10.eastmoney.com/fhsp_{symbol}.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": UA, "Connection": "close"})
    return session


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _DividendTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.cells: list[str] = []
        self.rows: list[list[str]] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "table" and "cfxq" in str(values.get("class") or "").split():
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.cells = []
        elif self.in_row and tag in ("td", "th"):
            self.in_cell = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_cell and tag in ("td", "th"):
            self.cells.append("".join(self._parts).strip())
            self.in_cell = False
        elif self.in_row and tag == "tr":
            if self.cells:
                self.rows.append(self.cells)
            self.in_row = False
        elif self.in_table and tag == "table":
            self.in_table = False


def parse_dividends(html: str, as_of: str) -> list[dict[str, Any]]:
    parser = _DividendTableParser()
    parser.feed(html)
    records: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) != 5 or not DATE_RE.fullmatch(cells[1]):
            continue
        match = re.search(r"每10份派现金([0-9.]+)元", cells[3])
        if not match:
            continue
        record_date, ex_date, pay_date = cells[1], cells[2], cells[4]
        if not all(DATE_RE.fullmatch(day) for day in (record_date, ex_date, pay_date)):
            continue
        if ex_date > as_of:
            continue
        records.append({
            "record_date": record_date,
            "ex_date": ex_date,
            "pay_date": pay_date,
            "cash_per_unit": round(float(match.group(1)) / 10.0, 8),
        })
    records.sort(key=lambda item: (item["ex_date"], item["pay_date"]))
    if not records:
        raise ValueError("未从基金分红页面解析到截止日内的分红记录")
    return records


def parse_prices(payload: Any, as_of: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    prices: list[dict[str, Any]] = []
    for raw in rows:
        day = str(raw.get("day") or "")
        if not DATE_RE.fullmatch(day) or day > as_of:
            continue
        close = float(raw.get("close") or 0)
        volume = int(float(raw.get("volume") or 0))
        if close > 0:
            prices.append({"date": day, "close": close, "volume_shares": volume})
    prices.sort(key=lambda item: item["date"])
    if not prices:
        raise ValueError("红利 ETF 未返回截止日内的未复权行情")
    return prices


def _market_symbol(symbol: str) -> str:
    code = str(symbol).strip().lower().removeprefix("sh").removeprefix("sz")
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("ETF 代码必须为 6 位数字")
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return prefix + code


def fetch_benchmark(
    as_of: str,
    timeout: int = 30,
    *,
    symbol: str = "510880",
    name: str = "红利ETF华泰柏瑞",
) -> dict[str, Any]:
    if not DATE_RE.fullmatch(as_of):
        raise ValueError("as_of 必须为 YYYY-MM-DD")
    market_symbol = _market_symbol(symbol)
    code = market_symbol[2:]
    dividend_url = DIVIDEND_URL_TEMPLATE.format(symbol=code)
    session = _session()
    response = session.get(
        PRICE_URL,
        params={
            "symbol": market_symbol,
            "scale": "240",
            "ma": "no",
            "datalen": "5000",
        },
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=timeout,
    )
    response.raise_for_status()
    prices = parse_prices(response.json(), as_of)

    time.sleep(1.2)
    response = session.get(
        dividend_url,
        headers={"Referer": "https://fundf10.eastmoney.com/"},
        timeout=timeout,
    )
    response.raise_for_status()
    dividends = parse_dividends(response.text, as_of)
    payload = {
        "schema_version": 1,
        "kind": "tradeable_total_return_benchmark",
        "symbol": code,
        "name": name,
        "as_of": as_of,
        "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "sources": {
            "prices": {
                "provider": "新浪财经行情接口",
                "url": PRICE_URL,
                "price_format": "unadjusted_close",
                "volume_unit": "shares",
            },
            "dividends": {
                "provider": "天天基金基金档案（数据来源标注为东方财富 Choice）",
                "url": dividend_url,
                "basis": "权益登记日、除息日、每份现金、发放日",
            },
        },
        "coverage": {
            "price_start": prices[0]["date"],
            "price_end": prices[-1]["date"],
            "price_count": len(prices),
            "dividend_count": len(dividends),
        },
        "prices": prices,
        "dividends": dividends,
        "hashes": {
            "prices_sha256": _canonical_sha256(prices),
            "dividends_sha256": _canonical_sha256(dividends),
        },
        "limitations": [
            "基准使用 ETF 场内未复权收盘价，不使用基金净值替代成交价",
            "分红记录来自基金档案页面，后续刷新必须重新保存来源时间和哈希",
            "模拟按整手成交并计最低佣金，不含冲击成本和申赎费用",
            "该 ETF 仍可能存在跟踪误差，不能等同于无摩擦指数总回报",
        ],
    }
    return payload


def _trade(cash: float, price: float, lot_size: int, rate: float, minimum: float) -> tuple[int, float]:
    shares = int(cash // (price * lot_size)) * lot_size
    while shares > 0:
        gross = shares * price
        fee = max(gross * rate, minimum)
        if gross + fee <= cash + 1e-9:
            return shares, fee
        shares -= lot_size
    return 0, 0.0


def simulate_total_return(
    payload: dict[str, Any],
    observation_dates: list[str],
    *,
    initial_capital: float = 100000.0,
    signal_start: str = "2016-01-29",
    lot_size: int = 100,
    commission_rate: float = 0.0003,
    min_commission: float = 5.0,
) -> dict[str, Any]:
    prices = {row["date"]: float(row["close"]) for row in payload.get("prices") or []}
    volumes = {row["date"]: int(row.get("volume_shares") or 0) for row in payload.get("prices") or []}
    calendar = sorted(day for day, price in prices.items() if price > 0 and volumes.get(day, 0) > 0)
    if not calendar or calendar[-1] < observation_dates[-1]:
        raise ValueError("基准行情没有覆盖完整观察期")
    first_index = bisect_right(calendar, signal_start)
    if first_index >= len(calendar):
        raise ValueError("基准没有信号日后的可成交日期")
    first_trade_date = calendar[first_index]

    distributions = [
        dict(item, eligible_shares=None, credited=False, reinvested=False)
        for item in payload.get("dividends") or []
        if item["record_date"] >= signal_start
    ]
    cash = float(initial_capital)
    shares = 0
    total_dividends = 0.0
    events: list[dict[str, Any]] = []
    daily_nav: dict[str, float] = {}

    for day in calendar:
        if day < signal_start:
            continue
        for item in distributions:
            if not item["credited"] and day >= item["pay_date"]:
                amount = int(item["eligible_shares"] or 0) * float(item["cash_per_unit"])
                cash += amount
                total_dividends += amount
                item["credited"] = True
                events.append({
                    "date": item["pay_date"],
                    "side": "分红",
                    "shares": int(item["eligible_shares"] or 0),
                    "cash_per_unit": item["cash_per_unit"],
                    "net_cash": round(amount, 2),
                })

        should_buy = day == first_trade_date
        reinvest_items = [
            item for item in distributions
            if item["credited"] and not item["reinvested"] and day > item["pay_date"]
        ]
        if reinvest_items:
            should_buy = True
        if should_buy:
            quantity, fee = _trade(cash, prices[day], lot_size, commission_rate, min_commission)
            if quantity:
                gross = quantity * prices[day]
                cash -= gross + fee
                shares += quantity
                events.append({
                    "date": day,
                    "side": "买入" if day == first_trade_date else "分红复投",
                    "shares": quantity,
                    "price": prices[day],
                    "gross": round(gross, 2),
                    "fee": round(fee, 2),
                })
            for item in reinvest_items:
                item["reinvested"] = True
        # 权益登记日在收盘后确认持仓，因此当日买入的份额也具有分红资格。
        for item in distributions:
            if item["eligible_shares"] is None and day >= item["record_date"]:
                item["eligible_shares"] = shares
        daily_nav[day] = cash + shares * prices[day]

    nav_series = []
    known_days = sorted(daily_nav)
    for observation in observation_dates:
        index = bisect_right(known_days, observation) - 1
        if index < 0:
            nav = initial_capital
        else:
            nav = daily_nav[known_days[index]]
        nav_series.append({"date": observation, "nav": round(nav, 2)})

    from backtest import _compute_metrics

    metrics = _compute_metrics(nav_series, initial_capital)
    metrics["trade_count"] = sum(item["side"] in ("买入", "分红复投") for item in events)
    metrics["dividend_event_count"] = sum(item["side"] == "分红" for item in events)
    metrics["ending_cash"] = round(cash, 2)
    return {
        "symbol": payload["symbol"],
        "name": payload["name"],
        "method": {
            "signal_start": signal_start,
            "first_trade_date": first_trade_date,
            "execution_price": "未复权收盘价",
            "lot_size": lot_size,
            "commission_rate": commission_rate,
            "min_commission": min_commission,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "dividend_reinvestment": "现金发放日后的下一可交易日，整手复投",
        },
        "metrics": metrics,
        "shares": shares,
        "cash": round(cash, 2),
        "total_dividend_cash": round(total_dividends, 2),
        "events": events,
        "nav_series": nav_series,
        "input_hashes": payload["hashes"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True, help="数据截止日 YYYY-MM-DD")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--symbol", default="510880", help="ETF 六位代码")
    parser.add_argument("--name", default="红利ETF华泰柏瑞", help="ETF 展示名称")
    args = parser.parse_args()
    payload = fetch_benchmark(args.as_of, symbol=args.symbol, name=args.name)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {output}：{payload['coverage']}")


if __name__ == "__main__":
    main()
