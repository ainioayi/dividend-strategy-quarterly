"""生成 V1 前向模拟盘的每日公开业绩。

数据层只读取只追加账本，并用未复权收盘价做每日盯市；它不生成信号、不改写
交易事件，也不连接券商。510300 基准在 V1 首笔模拟成交日同步建仓，之后按场内
整手买入、现金分红复投和相同佣金口径模拟，避免用沪深 300 指数冒充可交易 ETF。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from bisect import bisect_right
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from refresh_backtest_cache import _fetch_dividends_eastmoney
from tradeable_benchmark import parse_dividends


ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = ROOT / "data" / "forward" / "v1_metadata.json"
JOURNAL_PATH = ROOT / "data" / "forward" / "monthly_v1.jsonl"
MARKET_PATH = ROOT / "data" / "forward" / "performance_market.json"
PERFORMANCE_PATH = ROOT / "data" / "forward" / "performance.json"
SITE_PATH = ROOT / "site" / "performance.json"
SECURITY_MASTER_PATH = ROOT / "data" / "historical" / "security_master.json"

BENCHMARK_CODE = "510300"
BENCHMARK_NAME = "沪深300ETF华泰柏瑞"
PRICE_URL_SINA = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
PRICE_URL_TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
DIVIDEND_URL_TEMPLATE = "https://fundf10.eastmoney.com/fhsp_{code}.html"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"前向账本第 {number} 行不是有效 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"前向账本第 {number} 行不是对象")
        rows.append(row)
    return rows


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


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


def _market_symbol(code: str) -> str:
    normalized = str(code).strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if normalized.startswith(prefix):
            normalized = normalized[2:]
            break
    if not re.fullmatch(r"\d{6}", normalized):
        raise ValueError(f"证券代码格式错误: {code}")
    if normalized.startswith(("92", "8")):
        prefix = "bj"
    elif normalized.startswith(("5", "6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return prefix + normalized


def _parse_sina_prices(payload: Any, as_of: str) -> list[dict[str, Any]]:
    rows = []
    for raw in payload if isinstance(payload, list) else []:
        day = str(raw.get("day") or raw.get("date") or "")[:10]
        if not DATE_RE.fullmatch(day) or day > as_of:
            continue
        try:
            close = float(raw.get("close") or 0)
            volume = int(float(raw.get("volume") or 0))
        except (TypeError, ValueError):
            continue
        if close > 0:
            rows.append({"date": day, "close": close, "volume_shares": max(volume, 0)})
    return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])


def _parse_tencent_prices(payload: Any, market_symbol: str, as_of: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    security = data.get(market_symbol) if isinstance(data, dict) else None
    raw_rows = security.get("day") if isinstance(security, dict) else None
    rows = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        day = str(raw[0])[:10]
        if not DATE_RE.fullmatch(day) or day > as_of:
            continue
        try:
            close = float(raw[2])
            volume_shares = int(float(raw[5]) * 100)
        except (TypeError, ValueError):
            continue
        if close > 0:
            rows.append({"date": day, "close": close, "volume_shares": max(volume_shares, 0)})
    return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])


def fetch_unadjusted_prices(
    code: str,
    start_date: str,
    as_of: str,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    """用新浪主源和腾讯同语义备源获取未复权日线，并交叉核对重叠收盘价。"""
    market_symbol = _market_symbol(code)
    session = _session()
    providers: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    try:
        response = session.get(
            PRICE_URL_SINA,
            params={"symbol": market_symbol, "scale": "240", "ma": "no", "datalen": "5000"},
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=timeout,
        )
        response.raise_for_status()
        providers["sina_cn_marketdata"] = _parse_sina_prices(response.json(), as_of)
    except Exception as exc:  # pragma: no cover - 网络异常由集成运行覆盖
        errors["sina_cn_marketdata"] = str(exc)

    try:
        response = session.get(
            PRICE_URL_TENCENT,
            params={"param": f"{market_symbol},day,{start_date},{as_of},640,"},
            headers={"Referer": "https://gu.qq.com/"},
            timeout=timeout,
        )
        response.raise_for_status()
        providers["tencent_fqkline_raw_day"] = _parse_tencent_prices(
            response.json(), market_symbol, as_of
        )
    except Exception as exc:  # pragma: no cover - 网络异常由集成运行覆盖
        errors["tencent_fqkline_raw_day"] = str(exc)

    providers = {
        name: [row for row in rows if row["date"] >= start_date]
        for name, rows in providers.items()
        if rows
    }
    if not providers:
        raise RuntimeError(f"{code} 两个未复权行情源均失败: {errors}")

    if len(providers) > 1:
        left = {row["date"]: row["close"] for row in providers["sina_cn_marketdata"]}
        right = {row["date"]: row["close"] for row in providers["tencent_fqkline_raw_day"]}
        overlap = sorted(set(left) & set(right))
        mismatches = [day for day in overlap if not math.isclose(left[day], right[day], abs_tol=1e-6)]
        if mismatches:
            raise RuntimeError(f"{code} 新浪与腾讯未复权收盘价不一致: {mismatches[-5:]}")

    selected = "sina_cn_marketdata" if "sina_cn_marketdata" in providers else next(iter(providers))
    rows = providers[selected]
    if not rows:
        raise RuntimeError(f"{code} 没有覆盖 {start_date} 至 {as_of} 的行情")
    return {
        "code": market_symbol[2:],
        "price_format": "unadjusted_close",
        "selected_provider": selected,
        "validated_providers": sorted(providers),
        "errors": errors,
        "prices": rows,
        "prices_sha256": _sha256(rows),
    }


def fetch_benchmark_dividends(as_of: str, *, timeout: int = 30) -> dict[str, Any]:
    url = DIVIDEND_URL_TEMPLATE.format(code=BENCHMARK_CODE)
    response = _session().get(
        url,
        headers={"Referer": "https://fundf10.eastmoney.com/"},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = parse_dividends(response.text, as_of)
    return {
        "provider": "天天基金基金档案（东方财富 Choice 数据）",
        "url": url,
        "dividends": rows,
        "dividends_sha256": _sha256(rows),
    }


def _security_names(path: Path = SECURITY_MASTER_PATH) -> dict[str, str]:
    payload = _read_json(path, {}) or {}
    records = payload.get("records") if isinstance(payload, dict) else []
    return {
        str(row.get("code") or "").zfill(6): str(row.get("name") or row.get("code") or "")
        for row in records or []
        if row.get("code")
    }


def _execution_rows(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [row for row in journal if row.get("event_type") == "execution"]
    return sorted(rows, key=lambda row: (str(row.get("execution_date") or ""), str(row.get("period") or "")))


def _journal_codes(journal: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    executions = _execution_rows(journal)
    all_codes = {
        str(item.get("code") or "").zfill(6)
        for row in executions
        for item in list(row.get("holdings") or []) + list(row.get("operations") or [])
        if item.get("code")
    }
    active = {
        str(item.get("code") or "").zfill(6)
        for item in (executions[-1].get("holdings") or [])
        if item.get("code")
    } if executions else set()
    return all_codes, active


def update_market_snapshot(
    metadata: dict[str, Any],
    journal: list[dict[str, Any]],
    as_of: str,
    existing: dict[str, Any] | None = None,
    *,
    price_fetcher: Callable[[str, str, str], dict[str, Any]] = fetch_unadjusted_prices,
    benchmark_dividend_fetcher: Callable[[str], dict[str, Any]] = fetch_benchmark_dividends,
    stock_dividend_fetcher: Callable[[str, str], list[dict[str, Any]]] = _fetch_dividends_eastmoney,
    sleep_seconds: float = 1.1,
    names: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not DATE_RE.fullmatch(as_of):
        raise ValueError("as_of 必须为 YYYY-MM-DD")
    start_date = str(metadata.get("forward_start_date") or "")[:10]
    if not DATE_RE.fullmatch(start_date) or as_of < start_date:
        raise ValueError("公开业绩截止日不能早于前向观察起点")

    benchmark_prices = price_fetcher(BENCHMARK_CODE, start_date, as_of)
    prices = benchmark_prices["prices"]
    if not prices or prices[-1]["date"] != as_of:
        raise RuntimeError(f"510300 尚无 {as_of} 收盘价，拒绝发布未收盘或缺失数据")
    if sleep_seconds:
        time.sleep(sleep_seconds)
    benchmark_dividends = benchmark_dividend_fetcher(as_of)
    benchmark_dividend_rows = [
        row for row in benchmark_dividends["dividends"]
        if str(row.get("record_date") or "") >= start_date
    ]

    previous = existing if isinstance(existing, dict) else {}
    securities = dict(previous.get("securities") or {})
    all_codes, active_codes = _journal_codes(journal)
    missing_codes = all_codes - set(securities)
    to_fetch = sorted(active_codes | missing_codes)
    name_map = names if names is not None else _security_names()

    for index, code in enumerate(to_fetch):
        price_payload = price_fetcher(code, start_date, as_of)
        if not price_payload.get("prices"):
            raise RuntimeError(f"当前或历史持仓 {code} 没有可用盯市价格")
        if sleep_seconds:
            time.sleep(sleep_seconds)
        dividends = stock_dividend_fetcher(code, as_of)
        securities[code] = {
            "code": code,
            "name": name_map.get(code, code),
            "price_format": "unadjusted_close",
            "selected_provider": price_payload["selected_provider"],
            "validated_providers": price_payload.get("validated_providers") or [],
            "prices": price_payload["prices"],
            "dividends": dividends,
            "hashes": {
                "prices_sha256": price_payload["prices_sha256"],
                "dividends_sha256": _sha256(dividends),
            },
        }
        if sleep_seconds and index + 1 < len(to_fetch):
            time.sleep(sleep_seconds)

    snapshot = {
        "schema_version": 1,
        "as_of": as_of,
        "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "price_format": "unadjusted_close",
        "benchmark": {
            "code": BENCHMARK_CODE,
            "name": BENCHMARK_NAME,
            "prices": prices,
            "dividends": benchmark_dividend_rows,
            "sources": {
                "prices": {
                    "selected_provider": benchmark_prices["selected_provider"],
                    "validated_providers": benchmark_prices.get("validated_providers") or [],
                    "sina_url": PRICE_URL_SINA,
                    "tencent_url": PRICE_URL_TENCENT,
                },
                "dividends": {
                    "provider": benchmark_dividends["provider"],
                    "url": benchmark_dividends["url"],
                },
            },
            "hashes": {
                "prices_sha256": benchmark_prices["prices_sha256"],
                "dividends_sha256": _sha256(benchmark_dividend_rows),
            },
        },
        "securities": securities,
        "active_codes": sorted(active_codes),
        "hashes": {},
    }
    snapshot["hashes"]["content_sha256"] = _sha256({k: v for k, v in snapshot.items() if k != "retrieved_at"})
    return snapshot


def _normalize_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    return {
        "buy": "买入", "买入": "买入",
        "sell": "卖出", "卖出": "卖出",
        "dividend": "分红", "分红": "分红",
        "split": "送转", "送转": "送转",
        "delisting": "退市处置",
    }.get(side, str(value or "其他"))


def _fee_total(operation: dict[str, Any]) -> float:
    fees = operation.get("fees")
    if isinstance(fees, dict):
        return float(fees.get("total") or 0)
    return float(fees or 0)


def _operations(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for row in _execution_rows(journal) for item in (row.get("operations") or [])]


def build_transactions(
    journal: list[dict[str, Any]], names: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    positions: dict[str, dict[str, Any]] = {}
    rows = []
    for operation in _operations(journal):
        side = _normalize_side(operation.get("side") or operation.get("type"))
        code = str(operation.get("code") or "").zfill(6) if operation.get("code") else ""
        shares = int(float(operation.get("shares") or 0))
        price = float(operation.get("price") or 0) if operation.get("price") is not None else None
        fees = _fee_total(operation)
        gross = float(operation.get("gross") or 0)
        realized_pnl = None
        position = positions.get(code)
        if side == "买入" and code:
            if position is None or int(position.get("shares") or 0) <= 0:
                position = {
                    "shares": 0,
                    "entry_price": float(price or 0),
                    "entry_date": str(operation.get("date") or "")[:10],
                }
                positions[code] = position
            position["shares"] = int(position.get("shares") or 0) + shares
        elif side == "卖出" and code:
            entry_price = float((position or {}).get("entry_price") or 0)
            realized_pnl = (float(price or 0) - entry_price) * shares - fees
            if position:
                position["shares"] = max(int(position.get("shares") or 0) - shares, 0)
                if position["shares"] == 0:
                    positions.pop(code, None)
        elif side == "分红":
            realized_pnl = float(operation.get("net_cash") or gross or 0)
        elif side == "送转" and position:
            position["shares"] = shares

        net_cash = operation.get("net_cash")
        if side == "买入":
            cash_flow = -float(net_cash if net_cash is not None else gross + fees)
        elif side in ("卖出", "分红"):
            cash_flow = float(net_cash if net_cash is not None else gross - fees)
        else:
            cash_flow = 0.0
        event_date = operation.get("ex_date") if side == "分红" else operation.get("date")
        rows.append({
            "date": str(event_date or operation.get("date") or "")[:10],
            "recorded_date": str(operation.get("date") or "")[:10],
            "code": code,
            "name": str(operation.get("name") or names.get(code) or code or "—"),
            "side": side,
            "shares": shares,
            "price": round(price, 4) if price is not None else None,
            "gross": round(gross, 2),
            "realized_pnl": round(realized_pnl, 2) if realized_pnl is not None else None,
            "fees": round(fees, 2),
            "cash_flow": round(cash_flow, 2),
            "reason": str(operation.get("reason") or "模型事件"),
        })
    rows.sort(key=lambda row: (row["date"], row["recorded_date"]), reverse=True)
    return rows, positions


def _entry_dates(events: list[dict[str, Any]]) -> dict[str, str]:
    positions: dict[str, dict[str, Any]] = {}
    for event in events:
        side = _normalize_side(event.get("side"))
        code = str(event.get("code") or "").zfill(6) if event.get("code") else ""
        shares = int(float(event.get("shares") or 0))
        if side == "买入" and code:
            if code not in positions or positions[code]["shares"] <= 0:
                positions[code] = {
                    "shares": 0,
                    "entry_date": str(event.get("date") or "")[:10],
                }
            positions[code]["shares"] += shares
        elif side == "卖出" and code in positions:
            positions[code]["shares"] = max(positions[code]["shares"] - shares, 0)
            if positions[code]["shares"] == 0:
                positions.pop(code, None)
        elif side == "送转" and code in positions:
            positions[code]["shares"] = shares
    return {code: value["entry_date"] for code, value in positions.items()}


def _price_on_or_before(rows: list[dict[str, Any]], target: str) -> tuple[float, str]:
    eligible = [row for row in rows if str(row.get("date") or "") <= target]
    if not eligible:
        raise ValueError(f"没有 {target} 当日或之前的盯市价格")
    row = max(eligible, key=lambda item: item["date"])
    return float(row["close"]), str(row["date"])


def _tax_rate(entry_date: str, ex_date: str) -> float:
    try:
        days = max((date.fromisoformat(ex_date) - date.fromisoformat(entry_date)).days, 0)
    except (TypeError, ValueError):
        days = 400
    return 0.0 if days > 365 else (0.10 if days > 30 else 0.20)


def _strategy_state(
    target: str,
    execution: dict[str, Any] | None,
    market: dict[str, Any],
    initial_capital: float,
) -> dict[str, Any]:
    if execution is None:
        return {"cash": initial_capital, "holdings": {}, "nav": initial_capital, "accrued_dividends": []}
    execution_date = str(execution["execution_date"])
    holdings = {
        str(row.get("code") or "").zfill(6): {
            "code": str(row.get("code") or "").zfill(6),
            "shares": int(float(row.get("shares") or 0)),
            "entry_price": float(row.get("entry_price") or 0),
        }
        for row in execution.get("holdings") or []
    }
    entries = _entry_dates(execution.get("cumulative_events") or [])
    cash = float(execution.get("cash") or 0)
    accrued = []
    for code, holding in holdings.items():
        security = (market.get("securities") or {}).get(code)
        if not security:
            raise ValueError(f"市场快照缺少持仓 {code}")
        entry_date = entries.get(code, execution_date)
        for item in security.get("dividends") or []:
            ex_date = str(item.get("ex_date") or "")[:10]
            if not (execution_date < ex_date <= target) or ex_date <= entry_date:
                continue
            shares_before = holding["shares"]
            dps = float(item.get("dps") or 0)
            gross = round(dps * shares_before, 2)
            tax = round(gross * _tax_rate(entry_date, ex_date), 2)
            net = round(gross - tax, 2)
            cash += net
            bonus = float(item.get("bonus_ratio") or 0)
            transfer = float(item.get("transfer_ratio") or 0)
            if bonus > 0 or transfer > 0:
                holding["shares"] = int(round(shares_before * (1 + (bonus + transfer) / 10.0)))
            accrued.append({"date": ex_date, "code": code, "gross": gross, "tax": tax, "net_cash": net})

    market_value = 0.0
    marks = {}
    for code, holding in holdings.items():
        security = market["securities"][code]
        price, price_date = _price_on_or_before(security.get("prices") or [], target)
        value = price * holding["shares"]
        market_value += value
        marks[code] = {"price": price, "price_date": price_date, "market_value": value}
    return {
        "cash": round(cash, 2),
        "holdings": holdings,
        "marks": marks,
        "nav": round(cash + market_value, 2),
        "accrued_dividends": accrued,
    }


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return round(worst * 100, 4)


def _buy_lots(cash: float, price: float, lot: int, rate: float, minimum: float) -> tuple[int, float]:
    shares = int(cash // (price * lot)) * lot
    while shares >= lot:
        gross = shares * price
        fee = max(gross * rate, minimum)
        if gross + fee <= cash + 1e-9:
            return shares, fee
        shares -= lot
    return 0, 0.0


def _benchmark_series(
    market: dict[str, Any],
    calendar: list[str],
    initial_capital: float,
    rules: dict[str, Any],
    inception_date: str | None,
) -> dict[str, Any]:
    benchmark = market["benchmark"]
    price_map = {row["date"]: float(row["close"]) for row in benchmark.get("prices") or []}
    lot = int(rules.get("lot_size", 100))
    rate = float(rules.get("buy_commission_rate", 0.0003))
    minimum = float(rules.get("min_commission", 5.0))
    if inception_date is not None and inception_date not in calendar:
        raise ValueError("510300 行情缺少 V1 首笔模拟成交日，不能同步建立基准")

    shares = 0
    cash = initial_capital
    distributions = [
        dict(row, eligible_shares=None, credited=False)
        for row in benchmark.get("dividends") or []
        if inception_date is not None and str(row.get("record_date") or "")[:10] >= inception_date
    ]
    nav_by_date = {}
    events = []
    total_dividends = 0.0
    total_fees = 0.0
    for day in calendar:
        if inception_date is None or day < inception_date:
            nav_by_date[day] = round(initial_capital, 2)
            continue
        if day == inception_date:
            shares, initial_fee = _buy_lots(initial_capital, price_map[day], lot, rate, minimum)
            if not shares:
                raise ValueError("初始资金不足以整手买入 510300")
            cash -= shares * price_map[day] + initial_fee
            total_fees += initial_fee
            events.append({
                "date": day, "side": "买入", "shares": shares,
                "price": price_map[day], "fee": round(initial_fee, 2),
            })
        for item in distributions:
            if item["eligible_shares"] is None and day >= item["record_date"]:
                item["eligible_shares"] = shares
            if not item["credited"] and item["eligible_shares"] is not None and day >= item["pay_date"]:
                amount = int(item["eligible_shares"] or 0) * float(item["cash_per_unit"])
                cash += amount
                total_dividends += amount
                item["credited"] = True
                events.append({"date": day, "side": "分红", "net_cash": round(amount, 2)})
        if day != inception_date and any(item["credited"] and not item.get("reinvested") for item in distributions):
            quantity, fee = _buy_lots(cash, price_map[day], lot, rate, minimum)
            if quantity:
                cash -= quantity * price_map[day] + fee
                shares += quantity
                total_fees += fee
                events.append({
                    "date": day, "side": "分红复投", "shares": quantity,
                    "price": price_map[day], "fee": round(fee, 2),
                })
            for item in distributions:
                if item["credited"]:
                    item["reinvested"] = True
        nav_by_date[day] = round(cash + shares * price_map[day], 2)
    return {
        "nav_by_date": nav_by_date,
        "shares": shares,
        "cash": round(cash, 2),
        "total_dividends": round(total_dividends, 2),
        "total_fees": round(total_fees, 2),
        "events": events,
        "inception_date": inception_date,
    }


def build_performance(
    metadata: dict[str, Any],
    journal: list[dict[str, Any]],
    market: dict[str, Any],
) -> dict[str, Any]:
    initial = float((metadata.get("rules") or {}).get("initial_capital") or 100000)
    capital_policy = metadata.get("capital_policy") or {
        "target_allocation_pct": 100,
        "cash_reserve": float((metadata.get("rules") or {}).get("reinvest_cash_reserve") or 0),
        "residual_cash_rule": "仅保留整数手和交易费用约束下无法继续买入的现金",
    }
    observation_policy = metadata.get("observation_policy") or {}
    start_date = str(metadata.get("forward_start_date") or "")[:10]
    as_of = str(market.get("as_of") or "")[:10]
    benchmark_prices = [
        row for row in (market.get("benchmark") or {}).get("prices") or []
        if start_date <= row.get("date", "") <= as_of
    ]
    calendar = sorted({row["date"] for row in benchmark_prices})
    if not calendar or calendar[0] != start_date or calendar[-1] != as_of:
        raise ValueError("510300 行情没有完整覆盖前向起点和公开截止日")

    executions = _execution_rows(journal)
    execution_dates = [str(row["execution_date"]) for row in executions]
    benchmark_inception_date = execution_dates[0] if execution_dates else None
    benchmark_result = _benchmark_series(
        market,
        calendar,
        initial,
        metadata.get("rules") or {},
        benchmark_inception_date,
    )
    series = []
    latest_state = None
    for day in calendar:
        index = bisect_right(execution_dates, day) - 1
        execution = executions[index] if index >= 0 else None
        state = _strategy_state(day, execution, market, initial)
        strategy_nav = float(execution["nav"]) if execution and day == execution["execution_date"] else state["nav"]
        benchmark_nav = benchmark_result["nav_by_date"][day]
        series.append({
            "date": day,
            "strategy_nav": round(strategy_nav, 2),
            "strategy_return_pct": round((strategy_nav / initial - 1) * 100, 6),
            "benchmark_nav": round(benchmark_nav, 2),
            "benchmark_return_pct": round((benchmark_nav / initial - 1) * 100, 6),
        })
        latest_state = state

    security_names = {
        code: str(row.get("name") or code)
        for code, row in (market.get("securities") or {}).items()
    }
    transactions, _ = build_transactions(journal, security_names)
    latest_state = latest_state or _strategy_state(as_of, None, market, initial)
    holdings = []
    for code, holding in latest_state["holdings"].items():
        mark = latest_state["marks"][code]
        shares = int(holding["shares"])
        cost_value = float(holding["entry_price"]) * shares
        pnl = float(mark["market_value"]) - cost_value
        holdings.append({
            "code": code,
            "name": security_names.get(code, code),
            "shares": shares,
            "entry_price": round(float(holding["entry_price"]), 4),
            "last_price": round(float(mark["price"]), 4),
            "price_date": mark["price_date"],
            "market_value": round(float(mark["market_value"]), 2),
            "cost_value": round(cost_value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_return_pct": round(pnl / cost_value * 100, 4) if cost_value else None,
        })
    holdings.sort(key=lambda row: row["market_value"], reverse=True)

    latest = series[-1]
    transaction_dividends = sum(
        float(row.get("cash_flow") or 0) for row in transactions if row.get("side") == "分红"
    )
    accrued_dividends = sum(
        float(row.get("net_cash") or 0) for row in latest_state.get("accrued_dividends") or []
    )
    fees = sum(float(row.get("fees") or 0) for row in transactions)
    strategy_values = [float(row["strategy_nav"]) for row in series]
    benchmark_values = [float(row["benchmark_nav"]) for row in series]
    strategy = {
        "name": "月度高息动量 V1",
        "status": "V1 前向模拟" if executions else "等待首期信号",
        "initial_capital": initial,
        "total_assets": latest["strategy_nav"],
        "cash": latest_state["cash"],
        "total_pnl": round(latest["strategy_nav"] - initial, 2),
        "cumulative_return_pct": latest["strategy_return_pct"],
        "max_drawdown_pct": _max_drawdown(strategy_values),
        "fees": round(fees, 2),
        "dividends": round(transaction_dividends + accrued_dividends, 2),
        "trade_count": sum(row["side"] in ("买入", "卖出") for row in transactions),
        "event_count": len(transactions),
        "holdings_count": len(holdings),
        "max_holdings": int((metadata.get("rules") or {}).get("max_holdings") or 2),
        "target_allocation_pct": capital_policy.get("target_allocation_pct", 100),
        "cash_reserve": capital_policy.get("cash_reserve", 0),
    }
    benchmark = {
        "code": BENCHMARK_CODE,
        "name": BENCHMARK_NAME,
        "status": "与 V1 同日建仓" if benchmark_inception_date else "等待 V1 首笔模拟成交",
        "inception_date": benchmark_inception_date,
        "method": "与 V1 首笔模拟成交同日用 10 万元收盘整手买入，现金分红到账后整手复投，计最低佣金",
        "total_assets": latest["benchmark_nav"],
        "cumulative_return_pct": latest["benchmark_return_pct"],
        "max_drawdown_pct": _max_drawdown(benchmark_values),
        "fees": benchmark_result["total_fees"],
        "dividends": benchmark_result["total_dividends"],
    }
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "as_of": as_of,
        "forward_start_date": start_date,
        "strategy": strategy,
        "benchmark": benchmark,
        "excess_return_pct": round(
            strategy["cumulative_return_pct"] - benchmark["cumulative_return_pct"], 6
        ),
        "series": series,
        "holdings": holdings,
        "transactions": transactions,
        "audit": {
            "v1_commit": metadata.get("v1_commit"),
            "rules_sha256": metadata.get("rules_sha256"),
            "journal_sha256": _sha256(journal),
            "market_content_sha256": (market.get("hashes") or {}).get("content_sha256"),
            "price_format": market.get("price_format"),
            "benchmark_sources": (market.get("benchmark") or {}).get("sources"),
            "comparison_inception_rule": "510300 与 V1 首笔模拟成交同日建仓；此前双方均按现金 0% 计",
            "monthly_journal": "data/forward/monthly_v1.jsonl",
            "capital_policy": capital_policy,
            "observation_policy": observation_policy,
        },
        "limitations": [
            "这是模型模拟盘，不连接券商、不读取真实账户，也不会自动下单。",
            "V1 对策略账户采用 100% 目标投入且不设置额外现金保留；整数手和交易费用导致的不可用尾差仍留在现金。",
            "V1 仅在月末形成信号并在下一真实交易日收盘模拟执行；其他交易日只做收盘盯市。",
            "持仓分红在两次月度执行之间按真实除权日计入日频估值，下一次执行时以只追加账本检查点重置。",
            "510300 在 V1 首笔模拟成交日同步建仓；此前双方均为现金 0%，之后使用场内未复权收盘价和现金分红复投。",
            "历史回测和前向模拟都不代表未来收益，也不是买卖建议。",
        ],
    }
    payload["audit"]["performance_sha256"] = _sha256({k: v for k, v in payload.items() if k != "generated_at"})
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新 V1 每日公开业绩")
    parser.add_argument("--as-of", required=True, help="公开数据截止日 YYYY-MM-DD")
    parser.add_argument("--metadata", type=Path, default=METADATA_PATH)
    parser.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    parser.add_argument("--market-output", type=Path, default=MARKET_PATH)
    parser.add_argument("--performance-output", type=Path, default=PERFORMANCE_PATH)
    parser.add_argument("--site-output", type=Path, default=SITE_PATH)
    args = parser.parse_args()

    metadata = _read_json(args.metadata)
    if not isinstance(metadata, dict):
        raise ValueError("V1 前向元数据不存在或格式错误")
    journal = _read_journal(args.journal)
    existing = _read_json(args.market_output, {})
    market = update_market_snapshot(metadata, journal, args.as_of, existing)
    performance = build_performance(metadata, journal, market)

    _atomic_write_json(args.market_output, market)
    _atomic_write_json(args.performance_output, performance)
    _atomic_write_json(args.site_output, performance)
    print(json.dumps({
        "as_of": performance["as_of"],
        "strategy_return_pct": performance["strategy"]["cumulative_return_pct"],
        "benchmark_return_pct": performance["benchmark"]["cumulative_return_pct"],
        "holdings": performance["strategy"]["holdings_count"],
        "transactions": performance["strategy"]["event_count"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
